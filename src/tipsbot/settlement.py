"""Automaticke vyhodnoceni tiketu z vysledku (fotbal): 1X2, dvojita sance, DNB, over/under, oba skoruji.

Trhy, kterym bot nerozumi (handicap, hraci, poločasy...), zustanou k rucnimu vyhodnoceni.
Zaviraci kurz (CLV) se uklada pri skenech pred vykopem (viz pipeline.update_closing).
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

from .names import match_team

log = logging.getLogger(__name__)


def _ou_single(total: int, line: float, over: bool) -> str:
    diff = total - line
    if abs(diff) < 1e-9:
        return "void"
    return "won" if (diff > 0) == over else "lost"


def grade(market: str, selection: str, hg: int, ag: int) -> str | None:
    """Vysledek sazky po 90 minutach; None = trh neumime."""
    sel = selection.strip()
    low = sel.lower()
    if market == "1X2":
        actual = "1" if hg > ag else "X" if hg == ag else "2"
        return "won" if sel.upper() == actual else "lost" if sel.upper() in ("1", "X", "2") else None
    if market == "DC":
        actual = "1" if hg > ag else "X" if hg == ag else "2"
        covers = {"1X": ("1", "X"), "X2": ("X", "2"), "12": ("1", "2")}.get(sel.upper())
        return None if covers is None else ("won" if actual in covers else "lost")
    if market == "DNB":
        if hg == ag:
            return "void"
        home_wins = hg > ag
        return "won" if (low in ("home", "1") and home_wins) or (low in ("away", "2") and not home_wins) else (
            "lost" if low in ("home", "1", "away", "2") else None)
    if market == "BTTS":
        both = hg > 0 and ag > 0
        return "won" if (low in ("yes", "ano") and both) or (low in ("no", "ne") and not both) else (
            "lost" if low in ("yes", "ano", "no", "ne") else None)
    if market.startswith("OU_") and low in ("over", "under"):
        try:
            line = float(market[3:])
        except ValueError:
            return None
        over, total = low == "over", hg + ag
        quarter = abs(line * 4 - round(line * 4)) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9
        if not quarter:
            return _ou_single(total, line, over)
        a, b = _ou_single(total, line - 0.25, over), _ou_single(total, line + 0.25, over)
        pair = {a, b}
        if pair == {"won"}:
            return "won"
        if pair == {"lost"}:
            return "lost"
        if pair == {"won", "void"}:
            return "half_won"
        if pair == {"lost", "void"}:
            return "half_lost"
        return None
    return None


def find_result(df: pd.DataFrame, match_name: str, match_day) -> tuple[int, int] | None:
    """Vysledek zapasu 'Domaci - Hoste' v okoli data (+-1 den), nazvy se paruji tolerantne."""
    if " - " not in match_name:
        return None
    home_n, away_n = (p.strip() for p in match_name.split(" - ", 1))
    known = set(df["home"]) | set(df["away"])
    h, a = match_team(home_n, known), match_team(away_n, known)
    if not h or not a:
        return None
    lo, hi = match_day - timedelta(days=1), match_day + timedelta(days=1)
    m = df[(df["home"] == h) & (df["away"] == a) & (df["date"] >= lo) & (df["date"] <= hi)]
    if m.empty:
        return None
    r = m.iloc[-1]
    return int(r["home_goals"]), int(r["away_goals"])


def settle_open_tickets(ledger, df: pd.DataFrame) -> list[str]:
    """Vyhodnoti otevrene tikety, ktere umi. Vraci zpravy o tom, co se stalo."""
    from sqlalchemy import select

    from .storage import TICKET_OPEN, Ticket

    notes = []
    for t in list(ledger.s.scalars(select(Ticket).where(Ticket.status == TICKET_OPEN))):
        res = find_result(df, t.match_name, t.match_day)
        if res is None:
            continue
        outcome = grade(t.market, t.selection, *res)
        if outcome is None:
            notes.append(f"Tiket {t.id} ({t.match_name}, {t.market}:{t.selection}): trh neumím vyhodnotit, udělej to ručně")
            continue
        ledger.settle_ticket(t.id, outcome, fair_closing_odds=t.closing_odds)
        notes.append(f"Tiket {t.id}: {t.match_name} {res[0]}:{res[1]} -> {outcome}")
    return notes
