"""Profil 3 – model trhu se simulaci (z rozboru Wales - Portugalsko, 11/2026).

Postup:
1. Z kurzu ceskych kancelari se odstrani marze a dopocita se, kolik golu cekaji (ocekavane goly domacich
   a hosti, Dixon-Coles). Kurzy vic kancelari se fitujou spolecne, takze hodnota vznikne jen tam, kde
   nektera kancelar nabizi vic nez spolecny odhad (napr. remiza u Fortuny 5,20 proti 5,00 jinde).
2. Simulace 1000 zapasu ukaze nejcastejsi vysledky (kontrola, ze cisla davaji smysl).
3. Kazdy zadany kurz se ohodnoti ve ctyrech scenarich: trh (vaha 50 %), model bota (25 %), vic golu
   (+12 %, vaha 12,5 %) a min golu (-12 %, vaha 12,5 %). Pocita se i vraceni vkladu (cele linie 2.0, 4.0)
   a ctvrtinove linie.
4. Vybere se sazka s nizsim kurzem (1.15-1.75), ktera prohraje nanejvys ve 25 % zapasu, ani v nejhorsim
   scenari neztrati vic nez 15 % a ma nejlepsi vazeny cekany zisk – obstoji i pri mene priznivem prubehu.

Kdyz zadna sazka nema kladny cekany zisk, vybere se ta nejmene ztratova (do -3 %) a oznaci se jako
sazka bez overene vyhody – jde o sledovani bez penez, ktere ma ukazat, jestli tenhle zpusob vydelava.
Na Wales - Portugalsko (kurzy Tipsportu 17. 11. 2026) vychazi min nez 4 goly @ 1.33.
"""

from __future__ import annotations

import zlib
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy.optimize import minimize

from .devig import DevigError, devig_power
from .models.football import score_matrix
from .settlement import grade

RHO = -0.08  # korekce nizkych skore (Dixon-Coles), stejna jako v rozboru
MAX_GOALS = 10
_PROFIT = {"won": 1.0, "half_won": 0.5, "void": 0.0, "half_lost": -0.5, "lost": -1.0}


@dataclass(frozen=True)
class SimConfig:
    min_odds: float = 1.15
    max_odds: float = 1.75
    max_loss: float = 0.25  # sazka smi prohrat nanejvys v tolika zapasech (pul-prohra se pocita napul)
    min_ev: float = -0.03  # nejnizsi pripustny vazeny cekany zisk
    worst_ev: float = -0.15  # ani v nejhorsim scenari nesmi sazka ztracet vic
    weights: tuple[tuple[str, float], ...] = (("trh", 0.5), ("model bota", 0.25), ("víc gólů", 0.125), ("méně gólů", 0.125))
    goal_shift: float = 0.12  # stresove scenare: o tolik vic / min golu
    sims: int = 1000
    max_fit_error: float = 0.04  # nad touto chybou si kurzy kancelari odporuji
    prior_weight: float = 0.002  # slabe pritazeni k modelu bota, kdyz kurzy nestaci (napr. jen goly)


DEFAULT = SimConfig()
_FALLBACK_PRIOR = (1.45, 1.15)


def selections(market: str) -> tuple[str, ...] | None:
    if market.startswith(("OU_", "TTH_", "TTA_")):
        return ("over", "under")
    return {"1X2": ("1", "X", "2"), "BTTS": ("yes", "no"), "DC": ("1X", "12", "X2"), "DNB": ("home", "away")}.get(market)


@lru_cache(maxsize=512)
def _masks(market: str, sel: str, size: int) -> dict[str, np.ndarray] | None:
    """Pro kazdy vysledek sazky (won/lost/void/half_*) maska skore, kde nastane."""
    out: dict[str, np.ndarray] = {}
    for i in range(size):
        for j in range(size):
            r = grade(market, sel, i, j)
            if r is None:
                return None
            out.setdefault(r, np.zeros((size, size), dtype=bool))[i, j] = True
    return out


def _outcomes(market: str, sel: str, m: np.ndarray) -> dict[str, float] | None:
    """Pravdepodobnost kazdeho vysledku sazky ze skorove matice."""
    masks = _masks(market, sel, m.shape[0])
    return None if masks is None else {r: float(m[k].sum()) for r, k in masks.items()}


def evaluate(market: str, sel: str, odds: float, m: np.ndarray) -> dict | None:
    """Cekany zisk na 1 jednotku vkladu a sance (vyhra / vraceni / prohra) vcetne pul-vysledku."""
    oc = _outcomes(market, sel, m)
    if oc is None:
        return None
    ev = sum(p * (_PROFIT[r] * (odds - 1.0) if _PROFIT[r] > 0 else _PROFIT[r]) for r, p in oc.items())
    win = oc.get("won", 0.0) + oc.get("half_won", 0.0)
    loss = oc.get("lost", 0.0) + 0.5 * oc.get("half_lost", 0.0)
    return {"ev": ev, "win": win, "push": oc.get("void", 0.0), "loss": loss}


def _fit_rows(entries: list) -> list[tuple[str, tuple[str, ...], list[float]]]:
    """Trhy bez vraceni vkladu s odstranenou marzi (pro fit ocekavanych golu)."""
    rows = []
    probe = score_matrix(1.4, 1.1, RHO, MAX_GOALS)
    for e in entries:
        sels = selections(e.market)
        if sels is None or len(sels) != len(e.odds) or e.market in ("DC", "DNB"):
            continue
        oc = _outcomes(e.market, sels[0], probe)
        if oc is None or set(oc) - {"won", "lost"}:
            continue  # cela / ctvrtinova linie - pri fitu se nepouziva
        try:
            fair = devig_power(list(e.odds))
        except DevigError:
            continue
        rows.append((e.market, sels, fair))
    return rows


def fit_lambdas(entries: list, prior: tuple[float, float] | None = None, cfg: SimConfig = DEFAULT):
    """Ocekavane goly (domaci, hoste) z kurzu bez marze. Vraci (lam, mu, chyba, pocet trhu) nebo None."""
    rows = _fit_rows(entries)
    if not rows:
        return None
    prior = prior or _FALLBACK_PRIOR
    lp = np.log(prior)

    def loss(x: np.ndarray) -> float:
        m = score_matrix(float(np.exp(x[0])), float(np.exp(x[1])), RHO, MAX_GOALS)
        err = 0.0
        for market, sels, fair in rows:
            for s, f in zip(sels, fair):
                won = _masks(market, s, m.shape[0]).get("won")
                err += ((float(m[won].sum()) if won is not None else 0.0) - f) ** 2
        return err + cfg.prior_weight * float(np.sum((x - lp) ** 2))

    res = minimize(loss, lp, method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-9, "maxiter": 400})
    n = sum(len(r[1]) for r in rows)
    rmse = float(np.sqrt(max(res.fun, 0.0) / n))
    return float(np.exp(res.x[0])), float(np.exp(res.x[1])), rmse, len(rows)


def simulate(lam: float, mu: float, n: int, seed: int) -> list[tuple[str, float]]:
    """Nejcastejsi vysledky z `n` simulovanych zapasu: [(skore, podil)]."""
    m = score_matrix(lam, mu, RHO, MAX_GOALS)
    idx = np.random.default_rng(seed).choice(m.size, size=n, p=(m / m.sum()).ravel())
    cnt = Counter(f"{i // m.shape[1]}:{i % m.shape[1]}" for i in idx)
    return [(s, c / n) for s, c in cnt.most_common(5)]


def scenarios(lam: float, mu: float, model: dict | None, cfg: SimConfig = DEFAULT) -> dict[str, tuple[float, float]]:
    out = {"trh": (lam, mu)}
    if model and model.get("lambda_home") and model.get("lambda_away"):
        out["model bota"] = (float(model["lambda_home"]), float(model["lambda_away"]))
    k = cfg.goal_shift
    out["víc gólů"] = (lam * (1 + k), mu * (1 + k))
    out["méně gólů"] = (lam * (1 - k), mu * (1 - k))
    return out


@dataclass
class SimPick:
    market: str
    selection: str
    odds: float
    book: str
    ev: float  # scenar trhu
    score: float  # vazeny cekany zisk pres scenare
    worst_ev: float
    win: float
    push: float
    loss: float
    detail: str
    flags: list[str]
    scen: dict | None = None  # Faze 3.2.1: EV a sance kazdeho scenare, vahy, λ, simulace (uklada se k vyberu)

    @property
    def p_equiv(self) -> float:
        """Pravdepodobnost, ktera pri kurzu da stejny cekany zisk (pro trhy s vracenim vkladu)."""
        return (self.ev + 1.0) / self.odds


def analyse(match_id: str, model: dict | None, entries: list, cfg: SimConfig = DEFAULT) -> tuple[SimPick | None, dict]:
    """Rozbor zapasu: (vybrana sazka nebo None, rozbor). Rozbor se uklada i bez sazky – co trh ceka, nejcastejsi
    vysledky a proc zadna sazka neprosla pravidly (nejblizsi kandidat a duvod)."""
    prior = None
    if model and model.get("lambda_home") and model.get("lambda_away"):
        prior = (float(model["lambda_home"]), float(model["lambda_away"]))
    info: dict = {"markets": sorted({e.market for e in entries}), "checked": 0}
    fit = fit_lambdas(entries, prior, cfg)
    if fit is None:
        info["verdict"] = "kurzy nestačí na odhad gólů (chybí 1X2 nebo góly)"
        return None, info
    lam, mu, err, n_mk = fit
    info.update(lam=round(lam, 2), mu=round(mu, 2), fit_err=round(err, 4), fit_markets=n_mk,
                top=[(sc_, round(p, 3)) for sc_, p in simulate(lam, mu, cfg.sims, zlib.crc32(match_id.encode()))])
    sc = {k: score_matrix(a, b, RHO, MAX_GOALS) for k, (a, b) in scenarios(lam, mu, model, cfg).items()}
    best: tuple[tuple[float, float], SimPick] | None = None
    near: tuple[float, dict] | None = None  # nejblizsi zamitnuty kandidat
    for e in entries:
        sels = selections(e.market)
        if sels is None or len(sels) != len(e.odds):
            continue
        for sel, odds in zip(sels, e.odds):
            if not (cfg.min_odds <= odds <= cfg.max_odds):
                continue
            res = {k: evaluate(e.market, sel, odds, m) for k, m in sc.items()}
            if any(r is None for r in res.values()):
                continue
            info["checked"] += 1
            base = res["trh"]
            worst = min(r["ev"] for r in res.values())
            w = {k: v for k, v in cfg.weights if k in res}
            score = sum(res[k]["ev"] * v for k, v in w.items()) / sum(w.values())
            why = [t for ok, t in ((base["loss"] <= cfg.max_loss, f"prohra {base['loss'] * 100:.0f} % (max {cfg.max_loss * 100:.0f} %)"),
                                   (worst >= cfg.worst_ev, f"v nejhorším scénáři {worst * 100:+.1f} % (min {cfg.worst_ev * 100:.0f} %)"),
                                   (score >= cfg.min_ev, f"vážený čekaný zisk {score * 100:+.1f} % (min {cfg.min_ev * 100:.0f} %)"))
                   if not ok]
            if why:
                gap = max(0.0, base["loss"] - cfg.max_loss) + max(0.0, cfg.worst_ev - worst) + max(0.0, cfg.min_ev - score)
                if near is None or gap < near[0]:
                    near = (gap, {"market": e.market, "selection": sel, "odds": odds, "why": why})
                continue
            key = (score, -base["loss"])
            if best is None or key > best[0]:
                best = (key, SimPick(e.market, sel, odds, e.book, base["ev"], score, worst, base["win"], base["push"],
                                     base["loss"], "", []))
    if near is not None:
        info["near"] = near[1]
    if best is None:
        info["verdict"] = ("žádná vhodná sázka – " + (f"žádný kurz v pásmu {cfg.min_odds:.2f}–{cfg.max_odds:.2f}" if not info["checked"]
                                                     else f"žádný z {info['checked']} kurzů v pásmu {cfg.min_odds:.2f}–{cfg.max_odds:.2f} nesplnil pravidla"))
        return None, info
    pick = best[1]
    full = {k: evaluate(pick.market, pick.selection, pick.odds, m) for k, m in sc.items()}
    evs = {k: v["ev"] for k, v in full.items()}
    lams = scenarios(lam, mu, model, cfg)
    pick.scen = {"lam": round(lam, 3), "mu": round(mu, 3), "fit_err": round(err, 4), "fit_markets": n_mk,
                 "weights": dict(cfg.weights), "sims": cfg.sims, "seed": zlib.crc32(match_id.encode()),
                 "scenarios": {k: {"lam": round(lams[k][0], 3), "mu": round(lams[k][1], 3),
                                   **{x: round(v[x], 4) for x in ("ev", "win", "push", "loss")}} for k, v in full.items()},
                 "score": round(pick.score, 4), "worst_ev": round(pick.worst_ev, 4)}
    pick.detail = (
        f"Trh čeká {lam:.2f} : {mu:.2f} gólu ({n_mk} trhů). Simulace {cfg.sims}×: "
        + ", ".join(f"{s_} {p * 100:.0f} %" for s_, p in info["top"])
        + f". Výhra {pick.win * 100:.0f} %"
        + (f", vrácení {pick.push * 100:.0f} %" if pick.push > 0.005 else "")
        + f", prohra {pick.loss * 100:.0f} %. Čekaný zisk: "
        + ", ".join(f"{k} {v * 100:+.1f} %" for k, v in evs.items())
        + f" (váženě {pick.score * 100:+.1f} %)."
    )
    if pick.score < 0:
        pick.flags.append("bez ověřené výhody – sázka zhruba za férovou cenu")
    if err > cfg.max_fit_error:
        pick.flags.append("kurzy kanceláří si odporují, ověř zadání")
    info["verdict"] = f"sázka {pick.market} {pick.selection} @ {pick.odds:.2f}" + (f" ({', '.join(pick.flags)})" if pick.flags else "")
    return pick, info


def lobo_market(match_id: str, model: dict | None, entries: list, pick: SimPick, cfg: SimConfig = DEFAULT) -> dict:
    """Experiment Faze 3.2.1 (jen paralelni mereni, vyber Profilu 3 se nemeni): ocekavane goly se fitujou BEZ trhu
    hodnoceneho vyberu (jeho vlastni kurz tedy referenci neovlivni) a EV vyberu se prepocita proti tomuto
    nezavislemu odhadu. Kdyz je `ev_lobo` systematicky nizsi nez `ev_own`, kladne EV scenare „trh“ vznikalo jen
    z toho, ze se kurz porovnaval sam se sebou (kruhovost)."""
    rest = [e for e in entries if e.market != pick.market]
    prior = None
    if model and model.get("lambda_home") and model.get("lambda_away"):
        prior = (float(model["lambda_home"]), float(model["lambda_away"]))
    fit = fit_lambdas(rest, prior, cfg) if rest else None
    out = {"ev_own": round(pick.ev, 4), "markets_used": sorted({e.market for e in rest})}
    if fit is None or fit[3] < 2:
        return {**out, "status": "NEOVĚŘENO", "why": "bez hodnoceného trhu nezbývají aspoň 2 trhy pro odhad gólů"}
    lam, mu, err, n_mk = fit
    ev = evaluate(pick.market, pick.selection, pick.odds, score_matrix(lam, mu, RHO, MAX_GOALS))
    return {**out, "status": "OK", "lam": round(lam, 3), "mu": round(mu, 3), "fit_err": round(err, 4),
            "fit_markets": n_mk, "ev_lobo": round(ev["ev"], 4) if ev else None,
            "loss_lobo": round(ev["loss"], 4) if ev else None}


def sim_stability(lam: float, mu: float, ns: tuple[int, ...] = (1000, 10000), seeds: int = 30) -> list[dict]:
    """Numericka stabilita simulace: rozptyl podilu nejcastejsiho skore mezi seedy pro ruzny pocet simulaci.
    Sance a EV Profilu 3 se pocitaji presne ze skorove matice – simulace ovlivnuje jen zobrazene nejcastejsi
    vysledky, ne vyber ani poradi kandidatu."""
    m = score_matrix(lam, mu, RHO, MAX_GOALS)
    i, j = np.unravel_index(int(np.argmax(m)), m.shape)
    exact = float(m[i, j] / m.sum())
    out = []
    for n in ns:
        shares = []
        for s in range(seeds):
            top = dict(simulate(lam, mu, n, s))
            shares.append(top.get(f"{i}:{j}", 0.0))
        shares = np.array(shares)
        out.append({"sims": n, "score": f"{i}:{j}", "exact": round(exact, 4), "mean": round(float(shares.mean()), 4),
                    "sd": round(float(shares.std(ddof=1)), 4), "max_err": round(float(np.abs(shares - exact).max()), 4)})
    return out


def best_pick(match_id: str, model: dict | None, entries: list, cfg: SimConfig = DEFAULT) -> SimPick | None:
    return analyse(match_id, model, entries, cfg)[0]
