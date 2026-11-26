"""Overeni zaviraciho kurzu pres zahranicni kancelare (The Odds API), kdyz API-Football kurzy pred vykopem neobnovilo.

API-Football aktualizuje predzapasove kurzy jen obcas, takze jeho posledni kurz pred vykopem byva stary nekolik hodin
(history._closing ho pak oznaci jako zastaraly a CLV se nepocita). The Odds API ma u kazde kancelare cas posledni
zmeny (`last_update`) a Pinnacle aktualizuje prubezne – jeho kurz tesne pred vykopem je standardni zaviraci kurz.

Kredity kurzoveho API jsou mesicni a malo jich je (tarif zdarma 500 / mesic), proto:
- jen zapasy se SCHVALENYM tipem, jen v poslednim bodu pred vykopem a jen kdyz API-Football kurz neobnovilo;
- seznam udalosti (/events) je zdarma, kurzy jedne udalosti stoji (pocet trhu x 1 region) kreditu – jen trhy tipu;
- pod rezervou kreditu (`auto_credit_reserve`) se nestahuje nic.
Kurzy se ukladaji do `odds_quotes` se zdrojem "oddsapi" a casem posledni zmeny u kancelare.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from .odds import OddsQuote

SOURCE = "oddsapi"
REFERENCE_BOOKS = ("pinnacle", "betfair_ex_eu", "matchbook")  # poradi duveryhodnosti zaviraciho kurzu


def api_market(market: str) -> str | None:
    """Trh Profilu 4 -> klic trhu v The Odds API (None = kurzove API ho primo nenabizi)."""
    if market == "1X2":
        return "h2h"
    if market.startswith("OU_"):
        return "totals"
    if market.startswith("AH_"):
        return "spreads"
    if market == "BTTS":
        return "btts"
    return None


def _t(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_event_odds(payload: dict, fixture_id: int, match: str, kickoff: datetime, now: datetime) -> list[OddsQuote]:
    """Odpoved /events/{id}/odds -> kurzy v zapisu Profilu 4 (AH: cislo je handicap domacich pro obe strany)."""
    home, away = payload.get("home_team"), payload.get("away_team")
    side = {home: "home", away: "away"}
    out = []
    for bm in payload.get("bookmakers", []):
        book = bm.get("key", "")
        for m in bm.get("markets", []):
            when = _t(m.get("last_update") or bm.get("last_update"))
            key = m.get("key")
            for o in m.get("outcomes", []):
                name, price, pt = o.get("name"), o.get("price"), o.get("point")
                if price is None:
                    continue
                mk = sel = None
                if key == "h2h":
                    mk, sel = "1X2", {home: "1", away: "2", "Draw": "X"}.get(name)
                elif key == "totals" and pt is not None and name in ("Over", "Under"):
                    mk, sel = f"OU_{float(pt):g}", name.lower()
                elif key == "spreads" and pt is not None and name in side:
                    hp = float(pt) if side[name] == "home" else -float(pt)
                    mk, sel = f"AH_{hp:g}", side[name]
                elif key == "btts" and name in ("Yes", "No"):
                    mk, sel = "BTTS", name.lower()
                if mk and sel:
                    out.append(OddsQuote(fixture_id, match, book, mk, sel, float(price), now, SOURCE, kickoff, when,
                                         note="The Odds API"))
    return out


def find_event(events: list[dict], home: str, away: str, kickoff: datetime) -> dict | None:
    from ..czechodds import _en
    from ..names import match_team

    for ev in events:
        start = _t(ev.get("commence_time"))
        if start is None or abs(start - kickoff) > timedelta(hours=3):
            continue
        if match_team(_en(home), [_en(ev.get("home_team", ""))]) and match_team(_en(away), [_en(ev.get("away_team", ""))]):
            return ev
    return None


def fetch(sport: str, key: str, home: str, away: str, kickoff: datetime, markets: list[str], fixture_id: int,
          match: str, now: datetime, base_url: str, timeout: int = 20, get=requests.get) -> tuple[list[OddsQuote], dict, str]:
    """(kurzy, kvota {'remaining','used'}, poznamka). Chyba = prazdny seznam a duvod v poznamce."""
    from .. import oddsapi

    r = oddsapi.request(base_url, f"/sports/{sport}/events", {"apiKey": key, "dateFormat": "iso"}, purpose="foreign_events",
                        sport=sport, timeout=timeout, fixture_id=fixture_id, get=get)
    if r.status_code != 200:
        return [], {}, f"kurzové API (seznam zápasů) vrátilo {r.status_code}"
    ev = find_event(r.json(), home, away, kickoff)
    if ev is None:
        return [], {}, "zápas v kurzovém API nenalezen"
    r = oddsapi.request(base_url, f"/sports/{sport}/events/{ev['id']}/odds",
                        {"apiKey": key, "regions": "eu", "markets": ",".join(markets), "oddsFormat": "decimal",
                         "dateFormat": "iso"}, purpose="foreign_clv", sport=sport, timeout=timeout,
                        fixture_id=fixture_id, event_id=str(ev["id"]), get=get)

    def num(h: str) -> int | None:
        try:
            return int(float(r.headers.get(h, "")))
        except ValueError:
            return None

    quota = {"remaining": num("x-requests-remaining"), "used": num("x-requests-used")}
    if r.status_code != 200:
        return [], quota, f"kurzové API vrátilo {r.status_code}"
    quotes = parse_event_odds(r.json(), fixture_id, match, kickoff, now)
    return quotes, quota, "" if quotes else "kurzové API nevrátilo žádné kurzy"


def reference(quotes: list[OddsQuote], market: str, sel: str, kickoff: datetime, after: datetime | None):
    """Zaviraci kurz zahranicni kancelare pro presne tento vyber: posledni kurz nejduveryhodnejsi kancelare
    (Pinnacle, burzy), jinak prvni dalsi kancelare s kurzem. Vraci (kurz, ferovy kurz, cas, kancelar) nebo None."""
    from .. import profile2
    from . import mathcore

    mine = [q for q in quotes if q.source == SOURCE and q.market == market and q.stamp < kickoff
            and (after is None or q.stamp > after)]
    if not mine:
        return None
    sels = profile2.selections(market)
    for book in REFERENCE_BOOKS + tuple(sorted({q.bookmaker for q in mine} - set(REFERENCE_BOOKS))):
        last = max((q for q in mine if q.bookmaker == book and q.selection == sel), key=lambda q: q.stamp, default=None)
        if last is None:
            continue
        per = {q.selection: q.odds for q in mine if q.bookmaker == book and q.stamp == last.stamp}
        fair = None
        if sels and all(s in per for s in sels):
            _, probs = mathcore.devig([per[s] for s in sels])
            fair = 1.0 / probs[sels.index(sel)]
        return last.odds, fair, last.stamp, book
    return None
