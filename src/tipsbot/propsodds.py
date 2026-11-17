"""Kurzy na rohy a karty z API-Football (/odds: sazka 45 = rohy, 80 = karty; Pinnacle, Bet365, Betano, 1xBet...).

Kurzove API (The Odds API) rohy ani karty nema. Stahuji se jen ligy, kde ma bot model rohu a karet a kde se
vybery daji vyhodnotit (cups.DOMESTIC). Karty kancelare vypisuji obvykle az 1-3 dny pred zapasem.
Ulozi se do stavu (`propsodds:<liga>`) a obnovi nejdriv po TTL, aby panel ani automat neplytvaly dotazy.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import requests

from .context import API_FOOTBALL, API_FOOTBALL_LEAGUES
from .cups import DOMESTIC
from .ingestion import current_season_start_year
from .models.props import KINDS
from .names import match_team
from .oddsapi import Event, MarketOdds

log = logging.getLogger(__name__)
BETS = {45: "ROHY", 80: "KARTY"}
TTL = timedelta(hours=3)
MAX_PAGES = 5
# nazev kancelare v API-Football -> klic v botovi; Pinnacle je ostry zdroj, Betano je mezinarodni (ne ceske)
BOOKS = {"Pinnacle": "pinnacle", "Betano": "betano_int", "Betfair": None}


def _key(league: str) -> str:
    return f"propsodds:{league}"


def book_key(name: str) -> str | None:
    return BOOKS[name] if name in BOOKS else name.lower().replace(" ", "")


def parse(fixtures: dict, odds_pages: list[dict]) -> list[dict]:
    """Radky {date, home, away, market, book, over, under} jen pro linie, ktere pocita model (x.5)."""
    teams = {}
    for f in fixtures.get("response", []):
        fx, t = f.get("fixture", {}), f.get("teams", {})
        if fx.get("id") and fx.get("date"):
            teams[fx["id"]] = (fx["date"][:10], t.get("home", {}).get("name"), t.get("away", {}).get("name"))
    rows = []
    for page in odds_pages:
        for item in page.get("response", []):
            fid = item.get("fixture", {}).get("id")
            if fid not in teams or not all(teams[fid]):
                continue
            day, home, away = teams[fid]
            for bk in item.get("bookmakers", []):
                book = book_key(bk.get("name", ""))
                if not book:
                    continue
                for bet in bk.get("bets", []):
                    kind = BETS.get(bet.get("id"))
                    if kind is None:
                        continue
                    prices: dict[float, dict[str, float]] = {}
                    for v in bet.get("values", []):
                        side, _, line = str(v.get("value", "")).partition(" ")
                        try:
                            prices.setdefault(float(line), {})[side.lower()] = float(v["odd"])
                        except (KeyError, ValueError):
                            continue
                    for line, p in prices.items():
                        if line in KINDS[kind][2] and {"over", "under"} <= set(p):
                            rows.append({"date": day, "home": home, "away": away, "market": f"{kind}_{line:g}",
                                         "book": book, "over": p["over"], "under": p["under"]})
    return rows


def fetch(league: str, api_key: str, today: date, days: int = 3, timeout: int = 30) -> list[dict]:
    lid = API_FOOTBALL_LEAGUES[league]
    season = current_season_start_year(today)
    head = {"x-apisports-key": api_key}
    fixtures = requests.get(f"{API_FOOTBALL}/fixtures", headers=head, timeout=timeout, params={
        "league": lid, "season": season, "from": today.isoformat(), "to": (today + timedelta(days=days)).isoformat()}).json()
    if fixtures.get("errors"):
        raise ValueError(f"API-Football odmítlo dotaz: {fixtures['errors']}")
    pages = []
    for bet in BETS:
        page, total = 1, 1
        while page <= min(total, MAX_PAGES):
            d = requests.get(f"{API_FOOTBALL}/odds", headers=head, timeout=timeout,
                             params={"league": lid, "season": season, "bet": bet, "page": page}).json()
            if d.get("errors"):
                raise ValueError(f"API-Football odmítlo dotaz: {d['errors']}")
            pages.append(d)
            total = int(d.get("paging", {}).get("total") or 1)
            page += 1
    return parse(fixtures, pages)


def load(ledger, league: str, now: datetime, refresh: bool) -> tuple[list[dict], str | None]:
    """Ulozene radky; pri `refresh` a starsich nez TTL je stahne znovu. Vraci (radky, poznamka)."""
    raw = ledger._get(_key(league), "")
    cached = json.loads(raw) if raw else None
    if cached and not (refresh and now - datetime.fromisoformat(cached["at"]) >= TTL):
        return cached["rows"], None
    key = ledger.api_football_key()
    if not refresh or not key:
        return (cached or {}).get("rows", []), None
    try:
        rows = fetch(league, key, now.date())
    except Exception as e:  # noqa: BLE001 - bez kurzu na rohy a karty sken bezi dal
        log.warning("kurzy rohu a karet %s: %s", league, e)
        return (cached or {}).get("rows", []), f"kurzy na rohy a karty se nepodařilo stáhnout ({type(e).__name__})"
    ledger._set(_key(league), json.dumps({"at": now.isoformat(), "rows": rows}))
    return rows, None


def attach(events: list[Event], league: str, ledger, now: datetime, refresh: bool) -> list[str]:
    """Prida udalostem trhy ROHY_x.5 a KARTY_x.5 (kancelar -> {over, under}). Vraci poznamky pro report."""
    if league not in DOMESTIC or not events:
        return []
    rows, note = load(ledger, league, now, refresh)
    hit = 0
    for ev in events:
        day = ev.kickoff.date()
        mine = [r for r in rows if abs((date.fromisoformat(r["date"]) - day).days) <= 1
                and match_team(ev.home, [r["home"]]) and match_team(ev.away, [r["away"]])]
        for r in mine:
            m = ev.markets.setdefault(r["market"], MarketOdds(r["market"], ("over", "under")))
            m.books[r["book"]] = {"over": r["over"], "under": r["under"]}
        hit += bool(mine)
    notes = [note] if note else []
    if hit:
        notes.append(f"kurzy na rohy a karty (API-Football) u {hit} z {len(events)} zápasů")
    return notes
