"""Monte Carlo simulace zapasu (vychozi 50 000, reprodukovatelny seed).

Kazda simulace losuje skore ze skorove matice kombinovaneho modelu S NEJISTOTOU parametru (gama smes),
takze simulace zahrnuje nahodnost vysledku i nejistotu ocekavanych golu. Vystup ma simulacni chybu (SE)
a interval; analyticky vysledek ze stejne matice slouzi jako kontrola.

Pozor: vic simulaci zmensuje jen simulacni chybu. Presnost vstupniho modelu tim neroste.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import mathcore


@dataclass(frozen=True)
class SimMarket:
    key: str
    prob: float  # podil plnych vyher v simulaci
    se: float  # simulacni chyba pravdepodobnosti
    ci_low: float
    ci_high: float
    fair_odds: float  # z analyticke matice (presny vzorec vc. vraceni)
    ev: float | None = None
    ev_se: float | None = None


@dataclass
class SimResult:
    n: int
    seed: int
    markets: dict[str, SimMarket]
    expected: tuple[float, float]
    top_scores: list[tuple[str, float]]
    total_goals: dict[str, float]
    one_x_two: tuple[float, float, float]


def wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def sample_scores(mat: np.ndarray, n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    flat = mat.ravel() / mat.sum()
    idx = rng.choice(flat.size, size=n, p=flat)
    return np.stack(np.unravel_index(idx, mat.shape), axis=1)


def simulate(mat: np.ndarray, selections: list[tuple[str, str, float | None]], n: int = 50_000, seed: int = 1,
             z: float = 1.96) -> SimResult:
    """`selections` = [(trh, vyber, kurz nebo None)]."""
    if n <= 0:
        raise ValueError("počet simulací musí být kladný")
    scores = sample_scores(mat, n, seed)
    h, a = scores[:, 0], scores[:, 1]
    markets = {}
    for mk, sel, odds in selections:
        A, B = mathcore.coefficients(mk, sel)
        a_s, b_s = A[h, a], B[h, a]
        p = float((a_s > 0.99).mean())
        se = math.sqrt(max(p * (1 - p), 0.0) / n)
        lo, hi = wilson(p, n, z)
        exact = mathcore.evaluate(mk, sel, mat, odds)
        ev = ev_se = None
        if odds is not None:
            r = a_s * odds + b_s - 1.0
            ev, ev_se = float(r.mean()), float(r.std(ddof=1) / math.sqrt(n))
        markets[f"{mk}:{sel}"] = SimMarket(f"{mk}:{sel}", p, se, lo, hi, exact.fair_odds, ev, ev_se)
    keys, counts = np.unique(h * 100 + a, return_counts=True)
    order = np.argsort(-counts)[:10]
    top = [(f"{int(keys[i] // 100)}:{int(keys[i] % 100)}", float(counts[i] / n)) for i in order]
    tot = h + a
    dist = {str(g): float((tot == g).mean()) for g in range(6)}
    dist["6+"] = float((tot >= 6).mean())
    return SimResult(n, seed, markets, (float(h.mean()), float(a.mean())), top, dist,
                     (float((h > a).mean()), float((h == a).mean()), float((h < a).mean())))
