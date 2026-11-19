"""Stress testing a Tip Robustness Score.

Scenare pro kazdou sazku (EV na jednotku vkladu):
  optimisticky   – model A (vlastni), kdyz je; jinak C
  zakladni       – kombinovany model C
  konzervativni  – model D (nejhorsi z C/A s nejistotou a trhu)
  trzni          – model B (kurzy bez marze)
Citlivost: pravdepodobnost vyhry -3 p.b., kurz -5 %, nepotvrzena sestava (favorit -7 % golu),
horsi data (dvojnasobna nejistota parametru).

Robustness Score 0–100 je transparentni soucet slozek (vahy v config.RobustnessWeights). NENI to
pravdepodobnost vyhry a nema vyznam mimo porovnani tipu mezi sebou.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..models import football
from . import mathcore
from .config import Profil4Config
from .models import GRID, ModelSet, uncertain_matrix


@dataclass
class StressResult:
    scenarios: list[tuple[str, float]]  # (nazev, EV)
    pass_rate: float
    worst_ev: float
    base_ev: float

    def table(self) -> list[str]:
        return [f"{n}: EV {ev * 100:+.1f} %" for n, ev in self.scenarios]


def scenario_matrices(ms: ModelSet, cfg: Profil4Config, lineups_confirmed: bool) -> list[tuple[str, np.ndarray]]:
    """Maticove scenare nezavisle na sazce (pocitaji se jednou na zapas)."""
    out: list[tuple[str, np.ndarray]] = []
    if not lineups_confirmed and ms.c.lam_home is not None:
        lh, la = ms.c.lam_home, ms.c.lam_away
        shock = 1.0 - cfg.model.lineup_shock
        lh2, la2 = (lh * shock, la) if lh >= la else (lh, la * shock)
        out.append(("nepotvrzená sestava (favorit −7 % gólů)", football.score_matrix(lh2, la2, ms.c.rho, GRID)))
    if ms.c.lam_home is not None:
        out.append(("horší data (2× nejistota parametrů)",
                    uncertain_matrix(ms.c.lam_home, ms.c.lam_away, ms.c.rho, max(ms.k / 2.0, 2.0))))
    return out


def stress_test(ms: ModelSet, market: str, selection: str, odds: float, cfg: Profil4Config,
                lineups_confirmed: bool, mats: list[tuple[str, np.ndarray]] | None = None) -> StressResult:
    sc: list[tuple[str, float]] = []
    base = mathcore.evaluate(market, selection, ms.c.matrix, odds)
    opt = ms.a if ms.a.available else ms.c
    sc.append(("optimistický (model A)" if ms.a.available else "optimistický (= C)",
               mathcore.evaluate(market, selection, opt.matrix, odds).ev))
    sc.append(("základní (model C)", base.ev))
    d = ms.evaluate_d(market, selection, odds)
    if d:
        sc.append((f"konzervativní (D: {d[1]})", d[0].ev))
    if ms.b.available:
        sc.append(("tržní (model B)", mathcore.evaluate(market, selection, ms.b.matrix, odds).ev))
    sc.append(("šance na výhru −3 p.b.", mathcore.ev_from_ab(max(0.0, base.a - 0.03), base.b, odds)))
    sc.append(("kurz −5 %", mathcore.ev_from_ab(base.a, base.b, odds * 0.95)))
    for name, m in (mats if mats is not None else scenario_matrices(ms, cfg, lineups_confirmed)):
        sc.append((name, mathcore.evaluate(market, selection, m, odds).ev))
    evs = [ev for _, ev in sc]
    return StressResult(sc, float(np.mean([ev > 0 for ev in evs])), float(min(evs)), float(base.ev))


@dataclass
class Robustness:
    score: float
    parts: dict[str, float] = field(default_factory=dict)  # body za slozku

    def lines(self) -> list[str]:
        names = {"model_agreement": "shoda modelu a trhu", "ev_stability": "stabilita EV ve scénářích",
                 "data_quality": "kvalita dat", "odds_freshness": "aktuálnost kurzu",
                 "lineup_certainty": "jistota sestav", "parameter_sensitivity": "citlivost na parametry",
                 "stress_worst": "nejhorší scénář"}
        return [f"{names.get(k, k)}: {v:.1f} b." for k, v in self.parts.items()]


def robustness(ms: ModelSet, market: str, selection: str, stress: StressResult, data_quality: float,
               odds_fresh: float, lineup_state: str, cfg: Profil4Config) -> Robustness:
    """`odds_fresh` 0–1 (1 = cerstvy kurz), `lineup_state`: confirmed | pending | missing_near_kickoff."""
    w = cfg.robustness
    if ms.a.available and ms.b.available:
        pa = mathcore.evaluate(market, selection, ms.a.matrix).p_effective
        pb = mathcore.evaluate(market, selection, ms.b.matrix).p_effective
        agree = max(0.0, 1.0 - abs(pa - pb) / 0.10)
    else:
        agree = 0.0  # shodu nelze overit
    ev_stab = stress.pass_rate
    lineup = {"confirmed": 1.0, "pending": 0.6, "missing_near_kickoff": 0.2}.get(lineup_state, 0.5)
    sens = next((ev for n, ev in stress.scenarios if n.startswith("horší data")), stress.base_ev)
    param = 1.0 if sens >= 0 else max(0.0, 1.0 + sens / 0.05)
    worst = 1.0 if stress.worst_ev >= 0 else max(0.0, 1.0 + stress.worst_ev / 0.10)
    parts = {
        "model_agreement": w.model_agreement * agree,
        "ev_stability": w.ev_stability * ev_stab,
        "data_quality": w.data_quality * max(0.0, min(1.0, data_quality)),
        "odds_freshness": w.odds_freshness * max(0.0, min(1.0, odds_fresh)),
        "lineup_certainty": w.lineup_certainty * lineup,
        "parameter_sensitivity": w.parameter_sensitivity * param,
        "stress_worst": w.stress_worst * worst,
    }
    return Robustness(round(sum(parts.values()), 1), {k: round(v, 1) for k, v in parts.items()})


def freshness_factor(age_min: float, limit_min: float) -> float:
    """1 do poloviny limitu, pak linearne k 0 na dvojnasobku limitu."""
    if age_min <= limit_min / 2:
        return 1.0
    return max(0.0, 1.0 - (age_min - limit_min / 2) / (1.5 * limit_min))
