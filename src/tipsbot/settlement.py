"""Automaticke vyhodnoceni tiketu z vysledku (fotbal): 1X2, dvojita sance, DNB, over/under, oba skoruji.

Trhy, kterym bot nerozumi (handicap, hraci, poločasy...), zustanou k rucnimu vyhodnoceni.
Zaviraci kurz (CLV) se uklada pri skenech pred vykopem (viz pipeline.update_closing).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, timedelta

import pandas as pd

from .names import match_team

log = logging.getLogger(__name__)


def _ou_single(total: int, line: float, over: bool) -> str:
    diff = total - line
    if abs(diff) < 1e-9:
        return "void"
    return "won" if (diff > 0) == over else "lost"


_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_SCORE = re.compile(r"^\s*(\d{1,2})\s*[:\-–]\s*(\d{1,2})\s*$")
_BTTS = {"btts", "gg", "oba daji gol", "oba tymy daji gol", "oba skoruji", "both teams to score"}
_DC = {"dc", "dvojita sance", "double chance", "neprohra"}
_1X2 = {"1x2", "vysledek", "zapas", "vitez", "vysledek zapasu"}


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9.,/]+", " ", text).split())


def normalize_bet(market: str, selection: str) -> tuple[str, str]:
    """Rucne zapsany trh/vyber -> tvar, kterému rozumi `grade` (napr. 'under' + '1,5' -> 'OU_1.5', 'under')."""
    m, s = _plain(market), _plain(selection)
    words = set(f"{m} {s}".split())
    side = "over" if words & {"over", "nad", "vice"} else "under" if words & {"under", "pod", "mene"} else None
    num = _NUM.search(f"{market} {selection}")
    if side and num:
        return f"OU_{float(num.group().replace(',', '.')):g}", side
    if m in _BTTS:
        return "BTTS", {"ano": "yes", "a": "yes", "ne": "no", "n": "no"}.get(s, s)
    if m in _DC:
        return "DC", {"10": "1X", "02": "X2", "1x": "1X", "x2": "X2"}.get(s, s.upper())
    if m in _1X2:
        return "1X2", {"0": "X", "remiza": "X", "domaci": "1", "hoste": "2"}.get(s, s.upper())
    return market.strip(), selection.strip()


def parse_score(text: str) -> tuple[int, int]:
    """'2:1', '2-1' -> (2, 1)."""
    m = _SCORE.match(text or "")
    if not m:
        raise ValueError("Skóre zapiš jako 2:1.")
    return int(m.group(1)), int(m.group(2))


def grade(market: str, selection: str, hg: int, ag: int) -> str | None:
    """Vysledek sazky po 90 minutach; None = trh neumime."""
    market, selection = normalize_bet(market, selection)
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


def settle_everything(ledger) -> list[str]:
    """Stahne aktualni vysledky (ligy, poharty, reprezentace) a vyhodnoti tikety i sledovane vybery."""
    import sys

    from . import paper
    from .cups import DOMESTIC, fetch_cups
    from .ingestion import fetch_football
    from .teamreport import load_all_results

    for lg in DOMESTIC:  # aktualni sezona (football-data.co.uk se dopln jednou denne)
        fetch_football(lg, seasons=1)
    fetch_cups(ledger.api_football_key(), ledger.football_data_key(), seasons=1)
    results = load_all_results()
    intl_last = None
    try:  # reprezentace (Liga narodu), vcetne neutralnich zapasu
        from .nations import download_results, load_international

        download_results()
        intl = load_international(neutral=True)
        intl_last = max(intl["date"]) if len(intl) else None
        results = pd.concat([results, intl[list(results.columns)]], ignore_index=True)
    except Exception as e:  # noqa: BLE001 - bez nich se vyhodnoti aspon kluby
        print(f"Vysledky reprezentaci nejsou k dispozici: {e}", file=sys.stderr)
    notes = settle_open_tickets(ledger, results) + paper.settle(ledger, results)
    return notes or [nothing_to_settle(ledger, intl_last)]


def nothing_to_settle(ledger, intl_last=None) -> str:
    """Proc se nic nevyhodnotilo: kolik veci ceka a do kdy jsou vysledky v datech."""
    from sqlalchemy import func, select

    from . import paper
    from .storage import TICKET_OPEN, Ticket

    n_t = ledger.s.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == TICKET_OPEN)) or 0
    n_p = sum(p.result is None for p in paper.load(ledger))
    if not n_t and not n_p:
        return "Nic k vyhodnocení: žádné otevřené tikety ani sledované výběry (už jsou vyhodnocené)."
    msg = f"Nic se nevyhodnotilo: čeká {n_t} tiketů a {n_p} sledovaných výběrů, jejich výsledky zatím nejsou ve stažených datech."
    if intl_last is not None:
        msg += f" Výsledky reprezentací jsou v datech jen do {intl_last:%d.%m.%Y}."
    return msg + " Skóre můžeš zadat ručně (Zadat výsledek zápasu)."


def settle_with_score(ledger, match_name: str, day: date, hg: int, ag: int) -> list[str]:
    """Rucne zadane skore zapasu vyhodnoti vsechny otevrene tikety i sledovane vybery toho zapasu."""
    from . import paper
    from .czechodds import _en

    if " - " not in match_name:
        raise ValueError("Zápas zapiš jako 'Domácí - Hosté'.")
    home, away = (p.strip() for p in match_name.split(" - ", 1))
    pairs = {(home, away), (_en(home), _en(away))}  # tiket muze byt cesky, sledovany vyber anglicky
    df = pd.DataFrame([{"date": day, "home": h, "away": a, "home_goals": hg, "away_goals": ag} for h, a in pairs])
    notes = settle_open_tickets(ledger, df) + paper.settle(ledger, df)
    if not notes:
        raise ValueError(f"U zápasu {match_name} ({day:%d.%m.%Y}) není nic otevřeného.")
    return notes
