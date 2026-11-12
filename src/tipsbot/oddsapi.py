"""Zive kurzy z The Odds API (the-odds-api.com): stazeni a prevod na jednotny format."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

from .config import SETTINGS, Settings


class OddsApiError(RuntimeError):
    pass


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
    resp = requests.get(
        f"{cfg.base_url}/sports/{sport}/odds",
        params={
            "apiKey": key,
            "regions": cfg.regions,
            "markets": ",".join(cfg.markets + (("spreads",) if league == "NHL" else ())),
            "oddsFormat": "decimal",
            "dateFormat": "iso",
        },
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise OddsApiError(f"Kurzove API vratilo {resp.status_code}: {resp.text[:200]}")
    return resp.json(), dict(resp.headers)


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
