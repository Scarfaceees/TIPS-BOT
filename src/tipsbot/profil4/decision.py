"""Decision Engine Profilu 4: APPROVED BET / WATCH / NO BET, BEST AVAILABLE a kategorie A/B/C.

Prahy jsou v config.DecisionConfig. Pozadavky se NIKDY nesnizuji jen proto, aby se nasla sazka:
kdyz nic nesplnuje APPROVED BET, ukaze se BEST AVAILABLE – nejlepsi nalezena moznost s jasnym duvodem,
proc neni schvalena.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import DecisionConfig
from .odds import CURRENT, OddsQuote

APPROVED, WATCH, NO_BET, BEST = "APPROVED BET", "WATCH", "NO BET", "BEST AVAILABLE"
CAT_A, CAT_B, CAT_C = "A – NEJROZUMNĚJŠÍ TIP", "B – VALUE TIP", "C – MAXIMUM VALUE"
ORDER = {APPROVED: 0, WATCH: 1, NO_BET: 2}


@dataclass
class Candidate:
    fixture_id: int
    match: str
    market: str
    selection: str
    label: str
    quote: OddsQuote
    quote_status: str
    p: dict  # {"A":..., "B":..., "C":..., "D":...} efektivni pravdepodobnost vyhry (None = model chybi)
    p_win_c: float
    p_push_c: float
    fair_odds_c: float
    fair_odds_d: float | None
    ev_c: float
    ev_d: float | None
    min_odds: float  # nejnizsi kurz pro prah EV (model C)
    robustness: float
    data_quality: float
    models_complete: bool  # A i B k dispozici
    lineups: str  # confirmed | pending | missing_near_kickoff
    stress: list = field(default_factory=list)
    robustness_parts: dict = field(default_factory=dict)
    decision: str = NO_BET
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    category: str = ""
    # kontrolni vrstva (integrity.py); validation None = nezavisle overeni neprobehlo => nelze schvalit
    validation: tuple[list[str], list[str]] | None = None  # (tvrde chyby, mekke vyhrady)
    p_market: float | None = None  # ferova pravdepodobnost trhu pro presne tento vyber
    ev_market: float | None = None  # EV kurzu proti ferove cene trhu
    market_src: str = ""
    market_covered: bool = False  # ma trh B pro tento typ sazky aktualni kurzy
    confirmed_by: list[str] = field(default_factory=list)  # jine kancelare s podobnym kurzem

    @property
    def odds(self) -> float:
        return self.quote.odds

    @property
    def key(self) -> str:
        return f"{self.market}:{self.selection}"

    @property
    def gap(self) -> float | None:
        if self.p.get("A") is None or self.p.get("B") is None:
            return None
        return abs(self.p["A"] - self.p["B"])


def decide(c: Candidate, cfg: DecisionConfig) -> Candidate:
    """Doplni rozhodnuti a duvody. Tvrde podminky => NO BET, mekke => WATCH."""
    fails_hard, fails_soft, ok = [], [], []
    if c.quote_status != CURRENT:
        fails_soft.append(f"kurz není aktuální ({c.quote_status.lower()})")
    if not (cfg.min_odds <= c.odds <= cfg.max_odds):
        fails_hard.append(f"kurz {c.odds:.2f} mimo pásmo {cfg.min_odds:.2f}–{cfg.max_odds:.2f}")
    if c.ev_c < cfg.ev_min:
        (fails_soft if c.ev_c > cfg.watch_ev else fails_hard).append(
            f"EV {c.ev_c:+.1%} pod prahem {cfg.ev_min:.0%}" + (f" (potřebný kurz {c.min_odds:.2f})" if c.ev_c > cfg.watch_ev else ""))
    else:
        ok.append(f"EV {c.ev_c:+.1%} nad prahem {cfg.ev_min:.0%}")
    if c.ev_c > cfg.suspicious_ev:
        fails_soft.append(f"EV {c.ev_c:+.0%} je podezřele vysoké – ověř kurz, trh a sestavy")
    if c.ev_d is None:
        fails_soft.append("konzervativní model nelze spočítat")
    elif c.ev_d < cfg.ev_min_conservative:
        (fails_soft if c.ev_d > -0.05 else fails_hard).append(f"konzervativní EV {c.ev_d:+.1%} je záporné")
    else:
        ok.append(f"i konzervativní EV {c.ev_d:+.1%} je kladné")
    if not c.models_complete:
        fails_soft.append("chybí vlastní model nebo trh – shodu nelze ověřit")
    elif c.gap is not None and c.gap > cfg.max_model_gap:
        (fails_soft if c.gap <= 2 * cfg.max_model_gap else fails_hard).append(
            f"model a trh se rozcházejí o {c.gap * 100:.0f} p.b.")
    if c.robustness < cfg.robustness_min:
        (fails_soft if c.robustness >= cfg.robustness_min - 25 else fails_hard).append(
            f"robustnost {c.robustness:.0f} pod {cfg.robustness_min:.0f}")
    else:
        ok.append(f"robustnost {c.robustness:.0f}")
    if c.data_quality < cfg.data_quality_min:
        fails_soft.append(f"kvalita dat {c.data_quality:.0%} pod {cfg.data_quality_min:.0%}")
    if c.lineups != "confirmed":
        c.risks.append("sestavy nejsou potvrzené")
    _integrity(c, cfg, fails_hard, fails_soft)

    if not fails_hard and not fails_soft:
        c.decision, c.reasons = APPROVED, ok
    elif not fails_hard and c.ev_c > cfg.watch_ev:
        c.decision, c.reasons = WATCH, fails_soft
    else:
        c.decision, c.reasons = NO_BET, fails_hard + fails_soft
    return c


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _integrity(c: Candidate, cfg: DecisionConfig, fails_hard: list[str], fails_soft: list[str]) -> None:
    """Kontrolni vrstva: bez prokazatelne konzistence kurzu, trhu, pravdepodobnosti a EV neni APPROVED BET."""
    if c.validation is None:
        fails_soft.append("výpočet nebyl nezávisle ověřen")
    else:
        fails_hard.extend(c.validation[0])
        fails_soft.extend(c.validation[1])
    if not c.market_covered:
        fails_soft.append(f"cenu trhu pro tuto sázku nelze ověřit ({c.market_src or 'trh chybí'})")
    pa, pb = c.p.get("A"), c.p.get("B")
    if pa is not None and pb is not None and c.market_covered:
        lg = abs(_logit(pa) - _logit(pb))
        if lg > cfg.max_logit_gap:
            fails_soft.append(f"model A ({pa:.1%}) a trh ({pb:.1%}) se výrazně rozcházejí (poměr šancí {math.exp(lg):.2f}×)")
    edge = c.ev_market if c.ev_market is not None else c.ev_c
    if edge > cfg.max_market_edge:
        missing = []
        if not c.market_covered:
            missing.append("cena trhu neznámá")
        if not c.confirmed_by:
            missing.append(f"žádná jiná kancelář nenabízí podobný kurz (do −{cfg.confirm_tolerance:.0%})")
        if missing:
            where = f"o {edge:+.0%} nad férovou cenou trhu" if c.ev_market is not None else f"EV {edge:+.0%}"
            fails_soft.append(f"kurz {c.quote.bookmaker} {c.odds:.2f} je {where} – extrémní hodnota nebyla nezávisle "
                              f"potvrzena: {'; '.join(missing)}")


def rank_key(c: Candidate) -> tuple:
    """Poradi: rozhodnuti, pak konzervativni EV, robustnost a EV."""
    return (ORDER[c.decision], -(c.ev_d if c.ev_d is not None else -1.0), -c.robustness, -c.ev_c)


@dataclass
class Selection:
    approved: list[Candidate]
    watch: list[Candidate]
    best_available: Candidate | None
    categories: dict[str, Candidate | None]
    verdict: str
    explanation: str


def select(cands: list[Candidate], cfg: DecisionConfig) -> Selection:
    approved = sorted([c for c in cands if c.decision == APPROVED], key=rank_key)
    watch = sorted([c for c in cands if c.decision == WATCH], key=rank_key)
    # maximum value jen z kandidatu, jejichz vypocet a sparovani kurzu prosly (jinak by vyhral chybny kurz)
    pos = [c for c in cands if c.ev_c > 0 and c.ev_c <= cfg.suspicious_ev and c.validation is not None
           and not c.validation[0]]

    def sensible_score(c: Candidate) -> float:
        return c.p_win_c * (c.robustness / 100.0) * c.data_quality

    pool_a = [c for c in (approved or watch) if c.odds >= cfg.sensible_min_odds]
    cat_a = max(pool_a, key=sensible_score) if pool_a else None
    pool_b = [c for c in approved if c.odds >= cfg.value_min_odds]
    cat_b = max(pool_b, key=lambda c: c.ev_c) if pool_b else None
    cat_c = max(pos, key=lambda c: c.ev_c) if pos else None
    for cat, c in ((CAT_C, cat_c), (CAT_B, cat_b), (CAT_A, cat_a)):
        if c is not None:
            c.category = cat if not c.category else c.category + " · " + cat

    best = None
    if not approved and cands:
        best = sorted(cands, key=rank_key)[0]
    if approved:
        verdict, why = APPROVED, f"{len(approved)} sázka/y splňují všechna kritéria."
    elif watch:
        verdict, why = WATCH, "Žádná sázka nesplňuje všechna kritéria; sledovat: " + "; ".join(watch[0].reasons[:3]) + "."
    else:
        verdict, why = NO_BET, "Není prokázaná hodnota." if cands else "Žádné použitelné kurzy."
    if best is not None:
        why += f" Nejlepší dostupná možnost ({best.label} @ {best.odds:.2f}) NENÍ schválená: " + \
               "; ".join(best.reasons[:4]) + "."
    return Selection(approved, watch, best, {CAT_A: cat_a, CAT_B: cat_b, CAT_C: cat_c}, verdict, why)
