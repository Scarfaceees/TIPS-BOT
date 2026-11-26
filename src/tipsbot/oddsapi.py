"""Zive kurzy z The Odds API (the-odds-api.com): stazeni a prevod na jednotny format."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import pandas as pd
import requests

from .config import SETTINGS, Settings

log = logging.getLogger(__name__)


class OddsApiError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Evidence jednotlivych pozadavku (Phase 3.4 / B1)
# ---------------------------------------------------------------------------
# Kazdy skutecny HTTP pozadavek na The Odds API jde pres `request()`. Ta po odpovedi (nebo chybe site) preda
# zaznam `CallRecord` prijemci nastavenemu pres `recording(sink)` – typicky Ledger.log_odds_api_call, ktery ho
# zapise do tabulky odds_api_calls. Bez prijemce se nic nezapisuje a chovani je stejne jako drive. Evidence sama
# nikdy neposila zadny dalsi pozadavek a jeji chyba nikdy neprerusi stahovani.

_SINK: ContextVar[Callable[["CallRecord"], None] | None] = ContextVar("odds_api_call_sink", default=None)


@dataclass
class CallRecord:
    ts: datetime  # UTC bez tzinfo, cas odeslani pozadavku
    endpoint: str  # cesta bez zakladni adresy a bez klice, napr. /sports/icehockey_nhl/odds
    purpose: str  # refresh_league | scores | foreign_events | foreign_clv | nhl_clv
    sport: str
    league: str | None
    params: dict  # parametry bez apiKey
    http: int | None = None  # None = odpoved neprisla (chyba site)
    ok: bool = False
    error: str | None = None
    remaining: int | None = None  # x-requests-remaining
    used: int | None = None  # x-requests-used
    last: int | None = None  # x-requests-last = naklad tohoto pozadavku podle API
    fixture_id: int | None = None
    event_id: str | None = None


@contextmanager
def recording(sink: Callable[[CallRecord], None] | None):
    """Pozadavky uvnitr bloku se predaji prijemci `sink` (None = neevidovat)."""
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def _header_int(headers, name: str) -> int | None:
    try:
        return int(float((headers or {}).get(name, "")))
    except (TypeError, ValueError):
        return None


def league_of(sport: str, settings: Settings = SETTINGS) -> str | None:
    return next((lg for lg, sk in settings.odds_api.sport_keys.items() if sk == sport), None)


def request(base_url: str, path: str, params: dict, *, purpose: str, sport: str, timeout: int = 30,
            league: str | None = None, fixture_id: int | None = None, event_id: str | None = None, get=None):
    """requests.get na The Odds API s evidenci pozadavku. Vraci odpoved nebo vyhodi puvodni vyjimku."""
    get = get or requests.get
    rec = CallRecord(datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0), path, purpose, sport,
                     league or league_of(sport), {k: v for k, v in params.items() if k.lower() != "apikey"},
                     fixture_id=fixture_id, event_id=event_id)
    try:
        resp = get(f"{base_url}{path}", params=params, timeout=timeout)
    except Exception as e:
        rec.error = redact(f"{type(e).__name__}: {e}")[:300]
        _emit(rec)
        raise
    rec.http = resp.status_code
    rec.ok = resp.status_code == 200
    headers = getattr(resp, "headers", None) or {}
    rec.remaining, rec.used, rec.last = (_header_int(headers, h) for h in
                                         ("x-requests-remaining", "x-requests-used", "x-requests-last"))
    if not rec.ok:
        rec.error = redact(str(getattr(resp, "text", "") or "")[:300]) or None
    _emit(rec)
    return resp


def expected_cost(rec: CallRecord) -> int | None:
    """Naklad podle dokumentace The Odds API: kurzy = trhy x regiony, /events zdarma, scores s daysFrom 2, jinak 1."""
    p = rec.params
    if rec.purpose == "foreign_events":
        return 0
    if rec.purpose == "scores":
        return 2 if p.get("daysFrom") else 1
    if "markets" in p:
        markets = [m for m in str(p["markets"]).split(",") if m]
        regions = [r for r in str(p.get("regions", "")).split(",") if r] or [""]
        return len(markets) * len(regions)
    return None


def _emit(rec: CallRecord) -> None:
    sink = _SINK.get()
    if sink is None:
        return
    try:
        sink(rec)
    except Exception as e:  # noqa: BLE001 - evidence nesmi shodit stahovani kurzu
        log.warning("evidence pozadavku kurzoveho API selhala: %s", redact(str(e)))


@dataclass(frozen=True)
class MarketOdds:
    """Kurzy jednoho trhu u vsech kancelari: kancelar -> {vyber: kurz}."""

    market: str  # napr. "1X2", "MONEYLINE", "OU_2.5"
    selections: tuple[str, ...]  # poradi vysledku trhu
    books: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Event:
    match_id: str
    league: str
    home: str
    away: str
    kickoff: datetime  # UTC bez tzinfo
    markets: dict[str, MarketOdds] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.home} - {self.away}"


@dataclass(frozen=True)
class FetchResult:
    events: list[Event]
    credits_remaining: int | None
    credits_used: int | None
    payload: list | None = None  # surova odpoved (pro cache)


def fetch_raw(
    league: str, settings: Settings = SETTINGS, timeout: int = 30, api_key: str | None = None
) -> tuple[list[dict], dict]:
    cfg = settings.odds_api
    key = api_key or cfg.api_key
    if not key:
        raise OddsApiError("Chybi API klic. Zadej ho v panelu v zalozce Nastaveni.")
    sport = cfg.sport_keys.get(league)
    if sport is None:
        raise OddsApiError(f"Liga {league} neni v kurzovem API dostupna.")
    resp = request(
        cfg.base_url, f"/sports/{sport}/odds", purpose="refresh_league", sport=sport, league=league, timeout=timeout,
        params={
            "apiKey": key,
            "regions": cfg.regions,
            "markets": ",".join(cfg.markets + (("spreads",) if league == "NHL" else ())),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
    )
    if resp.status_code != 200:
        raise OddsApiError(f"Kurzove API vratilo {resp.status_code}: {resp.text[:200]}")
    return resp.json(), dict(resp.headers)


_SECRET = re.compile(r"(?i)\b(api_?key|key|token)=[^&\s'\"]+")


def redact(text: str) -> str:
    """Zprava bez klicu (chyby knihovny requests obsahuji celou adresu i s apiKey)."""
    return _SECRET.sub(r"\1=***", str(text))


def fetch_scores(league: str, api_key: str, days: int = 3, settings: Settings = SETTINGS,
                 timeout: int = 30) -> tuple[pd.DataFrame, int | None]:
    """Vysledky odehranych zapasu souteze za posledni `days` dny (2 kredity). Sloupce jako u ostatnich vysledku."""
    sport = settings.odds_api.sport_keys.get(league)
    if sport is None or not api_key:
        raise OddsApiError(f"Výsledky {league} z kurzového API nejsou dostupné.")
    resp = request(settings.odds_api.base_url, f"/sports/{sport}/scores",
                   {"apiKey": api_key, "daysFrom": days, "dateFormat": "iso"}, purpose="scores", sport=sport, league=league,
                   timeout=timeout)
    if resp.status_code != 200:
        raise OddsApiError(f"Kurzove API vratilo {resp.status_code}: {resp.text[:200]}")
    rows = []
    for ev in resp.json():
        sc = {s.get("name"): s.get("score") for s in ev.get("scores") or []}
        home, away = ev.get("home_team"), ev.get("away_team")
        if not ev.get("completed") or sc.get(home) is None or sc.get(away) is None:
            continue
        try:
            hg, ag = int(sc[home]), int(sc[away])
        except (TypeError, ValueError):
            continue
        rows.append({"date": _parse_time(ev["commence_time"]).date(), "home": home, "away": away,
                     "home_goals": hg, "away_goals": ag})
    left = resp.headers.get("x-requests-remaining")
    df = pd.DataFrame(rows, columns=["date", "home", "away", "home_goals", "away_goals"])
    return df, int(float(left)) if left not in (None, "") else None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)


def _parse_puckline(ev: dict, home: str, away: str):
    """Puckline +-1.5: dva dvoucestne trhy. PUCKLINE_1.5 (home-1.5 / away+1.5), PUCKLINE_1.5R (home+1.5 / away-1.5)."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    for bm in ev.get("bookmakers", []):
        for m in bm.get("markets", []):
            if m.get("key") != "spreads":
                continue
            prices: dict[str, float] = {}
            for o in m.get("outcomes", []):
                pt = o.get("point")
                if pt is None or abs(float(pt)) != 1.5:
                    continue
                side = "home" if o.get("name") == home else "away" if o.get("name") == away else None
                if side:
                    prices[f"{side}{float(pt):+g}"] = float(o["price"])
            if {"home-1.5", "away+1.5"} <= set(prices):
                out.setdefault("PUCKLINE_1.5", {})[bm["key"]] = {k: prices[k] for k in ("home-1.5", "away+1.5")}
            if {"home+1.5", "away-1.5"} <= set(prices):
                out.setdefault("PUCKLINE_1.5R", {})[bm["key"]] = {k: prices[k] for k in ("home+1.5", "away-1.5")}
    sels = {"PUCKLINE_1.5": ("home-1.5", "away+1.5"), "PUCKLINE_1.5R": ("home+1.5", "away-1.5")}
    return {m: (sels[m], b) for m, b in out.items()}


def parse_events(payload: list[dict], league: str, settings: Settings = SETTINGS) -> list[Event]:
    """Prevede odpoved API na udalosti. Fotbal: 1X2 a OU_2.5; NHL: MONEYLINE, OU_5.5, OU_6.5."""
    is_nhl = league == "NHL"
    events = []
    for ev in payload:
        home, away = ev.get("home_team"), ev.get("away_team")
        if not home or not away:
            continue
        main = "MONEYLINE" if is_nhl else "1X2"
        main_sel = ("home", "away") if is_nhl else ("1", "X", "2")
        name_map = {home: main_sel[0], away: main_sel[-1], "Draw": "X"}
        books_main: dict[str, dict[str, float]] = {}
        totals: dict[str, dict[str, dict[str, float]]] = {}
        for bm in ev.get("bookmakers", []):
            key = bm.get("key", "")
            for m in bm.get("markets", []):
                if m.get("key") == "h2h":
                    prices = {name_map[o["name"]]: float(o["price"]) for o in m.get("outcomes", []) if o.get("name") in name_map}
                    if set(prices) == set(main_sel):
                        books_main[key] = prices
                elif m.get("key") == "totals":
                    by_point: dict[float, dict[str, float]] = {}
                    for o in m.get("outcomes", []):
                        if o.get("point") is None or o.get("name") not in ("Over", "Under"):
                            continue
                        by_point.setdefault(float(o["point"]), {})[o["name"].lower()] = float(o["price"])
                    for point, prices in by_point.items():
                        if set(prices) == {"over", "under"}:
                            totals.setdefault(f"OU_{point:g}", {})[key] = prices
        markets: dict[str, MarketOdds] = {}
        if is_nhl:
            for mkt, (sels, books) in _parse_puckline(ev, home, away).items():
                markets[mkt] = MarketOdds(mkt, sels, books)
        if books_main:
            markets[main] = MarketOdds(main, main_sel, books_main)
        for mkt, books in totals.items():
            markets[mkt] = MarketOdds(mkt, ("over", "under"), books)
        if markets:
            events.append(
                Event(
                    match_id=f"oddsapi:{ev.get('id')}",
                    league=league,
                    home=home,
                    away=away,
                    kickoff=_parse_time(ev["commence_time"]),
                    markets=markets,
                )
            )
    return events


def fetch_events(league: str, settings: Settings = SETTINGS, api_key: str | None = None) -> FetchResult:
    payload, headers = fetch_raw(league, settings, api_key=api_key)

    def num(name: str) -> int | None:
        try:
            return int(float(headers.get(name, "")))
        except ValueError:
            return None

    return FetchResult(
        parse_events(payload, league, settings), num("x-requests-remaining"), num("x-requests-used"), payload
    )


def sharp_and_offers(market: MarketOdds, settings: Settings = SETTINGS):
    """Rozdeli kancelare na ostre zdroje (pro ferovou linii) a ostatni (nabidky).

    Vraci (sharp, offers): sharp = {zdroj: [kurzy v poradi vyberu]},
    offers = {vyber: [(kancelar, kurz), ...]}.
    """
    sharp_keys = settings.odds_api.sharp_keys
    sharp: dict[str, list[float]] = {}
    offers: dict[str, list[tuple[str, float]]] = {s: [] for s in market.selections}
    for book, prices in market.books.items():
        if book in sharp_keys:
            sharp.setdefault(sharp_keys[book], [prices[s] for s in market.selections])
        else:
            for s in market.selections:
                offers[s].append((book, prices[s]))
    return sharp, offers
