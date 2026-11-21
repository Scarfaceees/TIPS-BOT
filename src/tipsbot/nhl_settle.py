"""Automaticke vyhodnoceni NHL z oficialniho verejneho NHL API (api-web.nhle.com, zdarma, bez klice).

Do ted se NHL nevyhodnocovalo vubec: vysledky pro vyhodnoceni byly jen fotbalove a MoneyPuck nerozlisuje
zakladni hraci dobu a prodlouzeni. NHL API ma u kazdeho golu periodu a prubezne skore, takze se da presne urcit:
- skore po 60 minutach (trhy REG = 1X2 v zakladni dobe, OU60 = goly za 60 min),
- konecne skore vcetne prodlouzeni a rozhodujiciho golu z najezdu (MONEYLINE, PUCKLINE, OU, goly tymu) –
  stejne jako ho pocita model (models.nhl) i kancelare.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import requests

from .names import nhl_abbr

log = logging.getLogger(__name__)

NHL_API = "https://api-web.nhle.com/v1"


def fetch_scores(day: date, timeout: int = 20) -> dict:
    r = requests.get(f"{NHL_API}/score/{day.isoformat()}", headers={"User-Agent": "tipsbot"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def parse_scores(payload: dict) -> list[dict]:
    """Odehrane zapasy: zkratky tymu, start (UTC), konecne skore, skore po 60 min a jak zapas skoncil."""
    out = []
    for g in payload.get("games", []):
        if g.get("gameState") not in ("OFF", "FINAL"):
            continue
        h, a = g.get("homeTeam", {}), g.get("awayTeam", {})
        if "score" not in h or "score" not in a:
            continue
        reg = (0, 0)
        for goal in g.get("goals", []) or []:
            if int(goal.get("period", 0)) <= 3:
                reg = (int(goal["homeScore"]), int(goal["awayScore"]))
        start = g.get("startTimeUTC")
        out.append({"id": g.get("id"), "home": h.get("abbrev"), "away": a.get("abbrev"),
                    "start": _utc(start.replace("Z", "+00:00")) if start else None,
                    "final": (int(h["score"]), int(a["score"])), "reg": reg,
                    "end": (g.get("gameOutcome") or {}).get("lastPeriodType", "REG")})
    return out


def _ou(total: float, line: float, over: bool) -> str:
    if total == line:
        return "void"
    return "won" if (total > line) == over else "lost"


def grade(market: str, selection: str, g: dict) -> str | None:
    """Vysledek sazky NHL; None = trh neznam."""
    rh, ra = g["reg"]
    fh, fa = g["final"]
    sel = selection.strip()
    kind, _, line = market.partition("_")
    if market == "REG":
        actual = "1" if rh > ra else "X" if rh == ra else "2"
        return ("won" if sel.upper() == actual else "lost") if sel.upper() in ("1", "X", "2") else None
    if market == "MONEYLINE":
        return ("won" if (fh > fa) == (sel == "home") else "lost") if sel in ("home", "away") else None
    if market == "OT":
        tie = rh == ra
        return ("won" if tie == (sel == "yes") else "lost") if sel in ("yes", "no") else None
    if kind == "PUCKLINE":  # PUCKLINE_1.5: home-1.5 / away+1.5; PUCKLINE_1.5R: home+1.5 / away-1.5
        margin = fh - fa
        wins = {"home-1.5": margin >= 2, "away+1.5": margin < 2, "home+1.5": margin > -2, "away-1.5": margin <= -2}
        return None if sel not in wins else ("won" if wins[sel] else "lost")
    if kind in ("OU", "OU60", "TTH", "TTA") and sel in ("over", "under"):
        try:
            x = float(line)
        except ValueError:
            return None
        total = {"OU": fh + fa, "OU60": rh + ra, "TTH": fh, "TTA": fa}[kind]
        return _ou(total, x, sel == "over")
    return None


def find_game(games: list[dict], name: str, kickoff: datetime) -> dict | None:
    """Zapas 'Domaci - Hoste' (plne nazvy) podle zkratek a casu vykopu (UTC, tolerance 12 h)."""
    home_n, _, away_n = name.partition(" - ")
    h, a = nhl_abbr(home_n.strip()), nhl_abbr(away_n.strip())
    if not h or not a:
        return None
    for g in games:
        if g["home"] == h and g["away"] == a and (g["start"] is None or abs(g["start"] - kickoff) <= timedelta(hours=12)):
            return g
    return None


def settle_paper(ledger, now: datetime, fetch=fetch_scores) -> list[str]:
    """Vyhodnoti sledovane vybery NHL, jejichz zapas zacal pred vic nez 3,5 h (1 dotaz na herni den)."""
    from . import kombi, paper

    picks = paper.load(ledger)
    todo = [p for p in picks if p.league == "NHL" and p.result is None
            and now - timedelta(days=10) < _utc(p.kickoff) < now - timedelta(hours=3, minutes=30)]
    legs = [x for x in kombi.open_legs(ledger) if x["league"] == "NHL"
            and now - timedelta(days=10) < _utc(x["kickoff"]) < now - timedelta(hours=3, minutes=30)]
    if not todo and not legs:
        return []
    # herni den NHL je americke datum: zapas v 01:00 UTC patri k predchozimu dni
    days = sorted({(_utc(k) - timedelta(hours=d)).date() for k in [p.kickoff for p in todo] + [x["kickoff"] for x in legs]
                   for d in (0, 12)})
    games, notes = [], []
    for d in days:
        try:
            games += parse_scores(fetch(d))
        except Exception as e:  # noqa: BLE001 - chyba zdroje nesmi zastavit automat
            notes.append(f"výsledky NHL {d:%d.%m.}: chyba ({e})")
    for p in todo:
        g = find_game(games, p.name, _utc(p.kickoff))
        if g is None:
            continue
        res = grade(p.market, p.selection, g)
        if res is None:
            continue
        p.score = f"{g['final'][0]}:{g['final'][1]}" + ("" if g["end"] == "REG" else f" ({g['end']}, po 60 min "
                                                         f"{g['reg'][0]}:{g['reg'][1]})")
        p.result, p.profit = res, paper.unit_profit(res, p.odds)
        notes.append(f"Sledovaný výběr NHL {p.name} {p.score}: {p.market} {p.selection} @ {p.odds:.2f} -> {res}")
    if any(p.result is not None for p in todo):
        paper.save(ledger, picks)
    return notes + (kombi.settle_nhl(ledger, games) if legs else [])


def is_nhl_match(name: str) -> bool:
    home, _, away = (name or "").partition(" - ")
    return bool(nhl_abbr(home.strip()) and nhl_abbr(away.strip()))


def ticket_market(market: str, selection: str) -> tuple[str, str]:
    """Tiket NHL -> trh pro `grade`. Trojcestny vysledek (1X2) je u hokeje vzdy po 60 minutach (REG)."""
    m, sel = (market or "").strip().upper(), (selection or "").strip()
    if m == "1X2":
        return "REG", sel.upper()
    if m == "MONEYLINE" or m.startswith(("PUCKLINE_", "OU_", "OU60_", "TTH_", "TTA_")):
        return m, sel.lower() if not m.startswith("PUCKLINE") else sel
    return m, sel


def _game_for_day(games: list[dict], name: str, day: date) -> dict | None:
    """Zapas tiketu podle zkratek tymu; den tiketu je cesky den zapasu (vecerni zapas NHL je v Cesku az po pulnoci),
    proto se bere zapas se startem nejblize poledni dne tiketu (do 36 h)."""
    home_n, _, away_n = name.partition(" - ")
    h, a = nhl_abbr(home_n.strip()), nhl_abbr(away_n.strip())
    mid = datetime(day.year, day.month, day.day, 12)
    hits = [g for g in games if g["home"] == h and g["away"] == a and g["start"] is not None
            and abs(g["start"] - mid) <= timedelta(hours=36)]
    return min(hits, key=lambda g: abs(g["start"] - mid)) if hits else None


def settle_tickets(ledger, now: datetime, fetch=fetch_scores) -> list[str]:
    """Vyhodnoti otevrene tikety zapasu NHL (rucne zapsane i z tipu) z oficialniho NHL API."""
    from sqlalchemy import select

    from .storage import TICKET_OPEN, Ticket

    today = now.date()
    todo = [t for t in ledger.s.scalars(select(Ticket).where(Ticket.status == TICKET_OPEN))
            if t.match_day and t.match_day <= today and (t.league == "NHL" or is_nhl_match(t.match_name))]
    if not todo:
        return []
    days = sorted({t.match_day - timedelta(days=d) for t in todo for d in (0, 1)})
    games, notes = [], []
    for d in days:
        try:
            games += parse_scores(fetch(d))
        except Exception as e:  # noqa: BLE001 - chyba zdroje nesmi zastavit automat
            notes.append(f"výsledky NHL {d:%d.%m.}: chyba ({e})")
    for t in todo:
        g = _game_for_day(games, t.match_name, t.match_day)
        if g is None:
            continue
        market, sel = ticket_market(t.market, t.selection)
        res = grade(market, sel, g)
        score = f"{g['final'][0]}:{g['final'][1]}" + ("" if g["end"] == "REG" else f" ({g['end']}, po 60 min {g['reg'][0]}:{g['reg'][1]})")
        if res is None:
            notes.append(f"Tiket {t.id} ({t.match_name} {score}, {t.market}:{t.selection}): trh neumím vyhodnotit, udělej to ručně")
            continue
        ledger.settle_ticket(t.id, res, fair_closing_odds=t.closing_odds)
        notes.append(f"Tiket {t.id}: {t.match_name} {score} -> {res}")
    return notes


def _utc(iso: str) -> datetime:
    """ISO cas -> naivni UTC (ulozene casy byvaji s pasmem i bez nej)."""
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)
