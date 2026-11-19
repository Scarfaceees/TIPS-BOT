"""Matematicke jadro Profilu 4: vyhodnoceni sazky na skorove matici, ferovy kurz, EV, settlement.

Pouziva spolecny vypocet vyher/vraceni/pulvyher z Profilu 2 (`profile2.returns`), takze cela, pulova i ctvrtinova
asijska linie se vyhodnocuji stejne v obou profilech. Vse je deterministicke.

Pro trh s vracenim (cela linie, DNB, ctvrtinove linie) je navrat sazky linearni v kurzu:
    R(o) = a * o + b,   a = vaha "vyhry" (vyhra 1, pulvyhra 0.5), b = vaha vraceni (vraceni 1, pulvyhra 0.5, pulprohra 0.5)
    EV(o) = E[R(o)] - 1 = a*o + b - 1
    ferovy kurz (EV = 0) = (1 - b) / a       – presne, ne jen 1/p
U binarniho trhu je b = 0 a ferovy kurz = 1/p.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .. import profile2

WON, HALF_WON, PUSH, HALF_LOST, LOST, VOID = "won", "half_won", "push", "half_lost", "lost", "void"
SETTLE_CZ = {WON: "výhra", HALF_WON: "poloviční výhra", PUSH: "vrácení vkladu", HALF_LOST: "poloviční prohra",
             LOST: "prohra", VOID: "neplatná sázka (vrácení)"}


@dataclass(frozen=True)
class BetEval:
    market: str
    selection: str
    odds: float | None
    p_win: float  # plna vyhra
    p_half_win: float
    p_push: float
    p_half_loss: float
    p_loss: float
    a: float  # E[R] = a*o + b
    b: float
    ev: float | None  # None bez kurzu

    @property
    def fair_odds(self) -> float:
        return fair_odds_ab(self.a, self.b)

    @property
    def p_effective(self) -> float:
        """Pravdepodobnost 'vyhry' pro binarni ekvivalent (ignoruje vraceni): a / (1 - b) kdyz b < 1."""
        return self.a / (1.0 - self.b) if self.b < 1.0 else 1.0

    @property
    def binary(self) -> bool:
        return self.p_push + self.p_half_win + self.p_half_loss < 1e-12


def fair_odds_ab(a: float, b: float) -> float:
    if a <= 0:
        return math.inf
    return (1.0 - b) / a


def ev_from_ab(a: float, b: float, odds: float) -> float:
    return a * odds + b - 1.0


def fair_odds(p: float) -> float:
    """Ferovy kurz binarni sazky: 1 / p."""
    if not 0 < p <= 1:
        return math.inf
    return 1.0 / p


def expected_value(p: float, odds: float) -> float:
    """EV binarni sazky na jednotku vkladu: p * kurz - 1."""
    return p * odds - 1.0


def min_odds_for_ev(a: float, b: float, ev_min: float) -> float:
    """Nejnizsi kurz, pri kterem EV >= ev_min (zaokrouhleno nahoru na setiny)."""
    if a <= 0:
        return math.inf
    return math.ceil(((1.0 + ev_min - b) / a) * 100 - 1e-9) / 100


def coefficients(market: str, selection: str) -> tuple[np.ndarray, np.ndarray]:
    """Matice koeficientu (A, B): navrat pri kurzu o je A*o + B pro kazde skore."""
    r0 = profile2.returns(market, selection, 0.0)
    r1 = profile2.returns(market, selection, 1.0)
    return r1 - r0, r0


def evaluate(market: str, selection: str, mat: np.ndarray, odds: float | None = None) -> BetEval:
    A, B = coefficients(market, selection)
    if A.shape != mat.shape:
        raise ValueError(f"matice {mat.shape} neodpovida mrizce {A.shape}")
    eps = 1e-9
    full_win = (A > 1 - eps)
    half_win = (A > 0.5 - eps) & (A < 0.5 + eps) & (B > 0.5 - eps)
    push = (A < eps) & (B > 1 - eps)
    half_loss = (A < eps) & (B > 0.5 - eps) & (B < 0.5 + eps)
    loss = (A < eps) & (B < eps)
    a = float((mat * A).sum())
    b = float((mat * B).sum())
    return BetEval(market, selection, odds, float(mat[full_win].sum()), float(mat[half_win].sum()),
                   float(mat[push].sum()), float(mat[half_loss].sum()), float(mat[loss].sum()), a, b,
                   None if odds is None else ev_from_ab(a, b, odds))


def settle(market: str, selection: str, home_goals: int, away_goals: int) -> str:
    """Vysledek sazky podle skore po 90 minutach (vcetne pul vyher a pul proher u ctvrtinovych linii)."""
    if home_goals < 0 or away_goals < 0:
        raise ValueError("neplatné skóre")
    hg, ag = min(home_goals, profile2.MAX_GOALS), min(away_goals, profile2.MAX_GOALS)
    if max(home_goals, away_goals) > profile2.MAX_GOALS:
        # mimo mrizku: vyhodnotit primo pres podminky (pocet golu nad 10 je extremne vzacny)
        raise ValueError("skóre mimo rozsah výpočtu (víc než 10 gólů jednoho týmu) – vyhodnoť ručně")
    A, B = coefficients(market, selection)
    a, b = float(A[hg, ag]), float(B[hg, ag])
    if a > 0.99:
        return WON
    if abs(a - 0.5) < 1e-9:
        return HALF_WON
    if b > 0.99:
        return PUSH
    if abs(b - 0.5) < 1e-9:
        return HALF_LOST
    return LOST


def unit_profit(outcome: str, odds: float) -> float:
    return {WON: odds - 1.0, HALF_WON: (odds - 1.0) / 2.0, PUSH: 0.0, VOID: 0.0, HALF_LOST: -0.5, LOST: -1.0}[outcome]


def outcome_vector(market: str, selection: str, mat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(maska plne vyhry, maska plne prohry) na skorove matici – pro korelace."""
    A, B = coefficients(market, selection)
    return A > 0.99, (A < 1e-9) & (B < 1e-9)


# ---------------------------------------------------------------------------
# Marze a ferova linie trhu
# ---------------------------------------------------------------------------

def devig(odds: list[float], method: str = "multiplicative") -> tuple[list[float], list[float]]:
    """(hrube implikovane pravdepodobnosti 1/kurz, ocistene). Metody: multiplicative (p_i / sum), shin, power."""
    from .. import devig as dv

    raw = dv.implied_probs(odds)
    if method == "multiplicative":
        fair = dv.devig_multiplicative(odds)
    elif method == "shin":
        fair = dv.devig_shin(odds)
    elif method == "power":
        fair = dv.devig_power(odds)
    else:
        raise ValueError(f"neznámá metoda {method}")
    return raw, list(fair)
