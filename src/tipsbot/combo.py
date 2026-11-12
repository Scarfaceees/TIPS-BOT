"""Kombinovane tikety: z tipu a value prilezitosti posklada nejlepsi dvojice (vyjimecne trojice).

Pravidla: max. 3 prilezitosti na tiket (standardne 2), kazda z jineho zapasu (nezavisle udalosti),
kazda musi mit sama o sobe hodnotu. Spolecna pravdepodobnost = soucin pravdepodobnosti jednotlivych
sazek, spolecny kurz = soucin kurzu; EV a vklad se pocitaji z nich.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import combinations

from . import risk
from .analysis import MatchView, Row
from .config import SETTINGS, Settings


@dataclass(frozen=True)
class Leg:
    match: str
    kickoff: str
    row: Row


@dataclass(frozen=True)
class Combo:
    legs: tuple[Leg, ...]
    odds: float
    prob: float
    ev_pct: float
    stake: float
    min_odds: float  # spolecny kurz, pod ktery se sazka uz nevyplati
    growth: float = 0.0  # ocekavany logaritmicky rust bankrollu pri optimalnim vkladu (radici kriterium)

    @property
    def fair_odds(self) -> float:
        return 1.0 / self.prob


def candidate_legs(views: list[MatchView]) -> list[Leg]:
    """Jedna nejlepsi prilezitost na zapas (doporuceny radek), jen s hodnotou a overenym kurzem."""
    legs = []
    for v in views:
        if v.recommended is None:
            continue
        r = v.rows[v.recommended]
        if r.status in ("tip", "value") and r.best_odds > 0 and r.p_final >= SETTINGS.tickets.min_leg_prob:
            legs.append(Leg(v.name, v.kickoff, r))
        elif v.target is not None:  # cilovy tip: kurz = minimalni kurz, od ktereho se vyplati sazet
            t = v.rows[v.target]
            legs.append(Leg(v.name, v.kickoff, replace(t, best_odds=t.min_odds, best_book="cíl – ověř u kanceláře", ev_pct=(t.p_model * t.min_odds - 1) * 100)))
    return legs


def _combo(legs: tuple[Leg, ...], bankroll: float, exposure: float, settings: Settings) -> Combo | None:
    odds = prob = 1.0
    for lg in legs:
        odds *= lg.row.best_odds
        prob *= lg.row.p_final
    if odds > settings.tickets.max_combined_odds or prob < settings.tickets.min_combined_prob:
        return None
    ev = prob * odds - 1.0
    if ev <= 0:
        return None
    st = risk.size_stake(prob, odds, bankroll, open_exposure_today=exposure, bets_on_match=0, paused=False, cfg=settings.risk)
    min_odds = 1.0 / prob * (1.0 + settings.value.ev_min[next(iter(settings.value.ev_min))])
    b = odds - 1.0
    growth = prob * math.log(prob * odds) + (1 - prob) * math.log((1 - prob) * odds / b) if 0 < prob < 1 and b > 0 else 0.0
    return Combo(legs, odds, prob, ev * 100.0, st.stake if st.allowed else 0.0, min_odds, growth)


def best_combos(views: list[MatchView], bankroll: float, exposure: float = 0.0, settings: Settings = SETTINGS) -> list[Combo]:
    """Nejlepsi tikety: nejdriv dvojice (standard), trojice jen pokud maji vyssi EV nez nejlepsi dvojice."""
    legs = candidate_legs(views)
    out: list[Combo] = []
    for n in range(2, settings.tickets.max_legs + 1):
        for group in combinations(legs, n):
            c = _combo(group, bankroll, exposure, settings)
            if c:
                out.append(c)
    pairs = sorted((c for c in out if len(c.legs) == 2), key=lambda c: -c.growth)
    triples = sorted((c for c in out if len(c.legs) == 3), key=lambda c: -c.growth)
    top = pairs[: settings.tickets.top_n]
    if triples and (not pairs or triples[0].growth > pairs[0].growth):
        top = top[:-1] + [triples[0]] if len(top) >= settings.tickets.top_n else top + [triples[0]]
    return sorted(top, key=lambda c: -c.growth)


def partner_for(match_id_or_name: str, views: list[MatchView], bankroll: float, exposure: float = 0.0,
                settings: Settings = SETTINGS) -> Combo | None:
    """Nejlepsi druha sazka k doplneni tiketu k dane prilezitosti."""
    for c in best_combos([v for v in views], bankroll, exposure, settings):
        if len(c.legs) == 2 and any(lg.match == match_id_or_name for lg in c.legs):
            return c
    legs = [lg for lg in candidate_legs(views)]
    mine = [lg for lg in legs if lg.match == match_id_or_name]
    best = None
    for lg in mine:
        for other in legs:
            if other.match == lg.match:
                continue
            c = _combo((lg, other), bankroll, exposure, settings)
            if c and (best is None or c.growth > best.growth):
                best = c
    return best
