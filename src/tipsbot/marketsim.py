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

    @property
    def p_equiv(self) -> float:
        """Pravdepodobnost, ktera pri kurzu da stejny cekany zisk (pro trhy s vracenim vkladu)."""
        return (self.ev + 1.0) / self.odds


def best_pick(match_id: str, model: dict | None, entries: list, cfg: SimConfig = DEFAULT) -> SimPick | None:
    prior = None
    if model and model.get("lambda_home") and model.get("lambda_away"):
        prior = (float(model["lambda_home"]), float(model["lambda_away"]))
    fit = fit_lambdas(entries, prior, cfg)
    if fit is None:
        return None
    lam, mu, err, n_mk = fit
    sc = {k: score_matrix(a, b, RHO, MAX_GOALS) for k, (a, b) in scenarios(lam, mu, model, cfg).items()}
    best: tuple[tuple[float, float], SimPick] | None = None
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
            base = res["trh"]
            worst = min(r["ev"] for r in res.values())
            w = {k: v for k, v in cfg.weights if k in res}
            score = sum(res[k]["ev"] * v for k, v in w.items()) / sum(w.values())
            if base["loss"] > cfg.max_loss or worst < cfg.worst_ev or score < cfg.min_ev:
                continue
            key = (score, -base["loss"])
            if best is None or key > best[0]:
                best = (key, SimPick(e.market, sel, odds, e.book, base["ev"], score, worst, base["win"], base["push"],
                                     base["loss"], "", []))
    if best is None:
        return None
    pick = best[1]
    top = simulate(lam, mu, cfg.sims, zlib.crc32(match_id.encode()))
    evs = {k: evaluate(pick.market, pick.selection, pick.odds, m)["ev"] for k, m in sc.items()}
    pick.detail = (
        f"Trh čeká {lam:.2f} : {mu:.2f} gólu ({n_mk} trhů). Simulace {cfg.sims}×: "
        + ", ".join(f"{s} {p * 100:.0f} %" for s, p in top)
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
    return pick
