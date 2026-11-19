"""Korelace a kombinovane sazky.

Sazky ze STEJNEHO zapasu nejsou nezavisle (vyhra domacich + over 2.5, BTTS + over 2.5 ...). Spolecna
pravdepodobnost a EV kombinace se proto pocitaji ze spolecne skorove matice: navrat kombinace v kazdem skore
je soucin navratu jednotlivych sazek (vraceni = kurz 1, pulvyhra/pulprohra presne). Prosty soucin
pravdepodobnosti se pouzije jen pro sazky z RUZNYCH zapasu a je tak oznacen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import mathcore


@dataclass(frozen=True)
class Pair:
    a: str
    b: str
    p_a: float
    p_b: float
    p_both: float  # obe plne vyhrou
    p_independent: float  # p_a * p_b – jen pro srovnani
    phi: float  # korelace vyhry (-1..1)
    p_both_lose: float

    @property
    def lift(self) -> float:
        return self.p_both / self.p_independent if self.p_independent > 0 else math.nan


def pair(mat: np.ndarray, a: tuple[str, str], b: tuple[str, str]) -> Pair:
    wa, la = mathcore.outcome_vector(a[0], a[1], mat)
    wb, lb = mathcore.outcome_vector(b[0], b[1], mat)
    pa, pb = float(mat[wa].sum()), float(mat[wb].sum())
    pab = float(mat[wa & wb].sum())
    den = math.sqrt(pa * (1 - pa) * pb * (1 - pb))
    phi = (pab - pa * pb) / den if den > 0 else 0.0
    return Pair(f"{a[0]}:{a[1]}", f"{b[0]}:{b[1]}", pa, pb, pab, pa * pb, phi, float(mat[la & lb].sum()))


def same_match_combo(mat: np.ndarray, legs: list[tuple[str, str, float]]) -> dict:
    """EV kombinace sazek z jednoho zapasu (kurz kombinace = soucin kurzu, jak ho pocita kancelar)."""
    if len(legs) < 2:
        raise ValueError("kombinace potřebuje aspoň dva výběry")
    ret = np.ones_like(mat)
    for mk, sel, odds in legs:
        ret = ret * profile_returns(mk, sel, odds)
    combo_odds = float(np.prod([o for _, _, o in legs]))
    wins = np.ones_like(mat, dtype=bool)
    for mk, sel, _ in legs:
        wins &= mathcore.outcome_vector(mk, sel, mat)[0]
    p_all = float(mat[wins].sum())
    p_ind = float(np.prod([mathcore.evaluate(mk, sel, mat).p_win for mk, sel, _ in legs]))
    return {"odds": combo_odds, "ev": float((mat * ret).sum() - 1.0), "p_all_win": p_all,
            "p_if_independent": p_ind, "ev_if_independent_wrong": p_ind * combo_odds - 1.0,
            "note": "počítáno ze společné skórové matice (závislé výběry)"}


def cross_match_combo(legs: list[tuple[float, float]]) -> dict:
    """Vybery z RUZNYCH zapasu: (pravdepodobnost vyhry, kurz). Nezavislost je predpoklad, ne overeny fakt."""
    p = float(np.prod([x for x, _ in legs]))
    o = float(np.prod([y for _, y in legs]))
    return {"odds": o, "p_all_win": p, "ev": p * o - 1.0,
            "note": "různé zápasy – předpoklad nezávislosti (sdílené faktory jako počasí nebo den nejsou zahrnuty)"}


def profile_returns(market: str, selection: str, odds: float) -> np.ndarray:
    from .. import profile2

    return profile2.returns(market, selection, odds)
