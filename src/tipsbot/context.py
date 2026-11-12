"""Doplnujici informace k zapasum: zraneni, absence, sestavy.

Zdroje (zdarma): ESPN (NHL zraneni, bez klice), API-Football (fotbal, klic zdarma, 100 pozadavku/den).
Vsechny funkce jsou tolerantni k chybam - kdyz zdroj neodpovi, vrati se prazdny seznam.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .names import match_team, normalize

log = logging.getLogger(__name__)

ESPN_NHL_INJURIES = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries"
API_FOOTBALL = "https://v3.football.api-sports.io"
API_FOOTBALL_LEAGUES = {"ENG1": 39, "ESP1": 140, "GER1": 78, "ITA1": 135, "FRA1": 61, "POR1": 94, "CZE1": 345}


@dataclass(frozen=True)
class Absence:
    team: str
    player: str
    status: str  # napr. Out, Day-To-Day, Missing Fixture, Questionable
    detail: str = ""

    def line(self) -> str:
        return f"{self.team}: {self.player} – {self.status}" + (f" ({self.detail})" if self.detail else "")


def parse_espn_injuries(payload: dict) -> list[Absence]:
    out = []
    for team in payload.get("injuries", []):
        name = team.get("displayName") or team.get("team", {}).get("displayName", "")
        for inj in team.get("injuries", []):
            ath = inj.get("athlete", {})
            player = ath.get("displayName") or ath.get("fullName")
            if not player:
                continue
            pos = ath.get("position", {}).get("abbreviation") if isinstance(ath.get("position"), dict) else ""
            comment = (inj.get("shortComment") or "").strip()
            out.append(Absence(name, f"{player}{f' ({pos})' if pos else ''}", inj.get("status", "?"), comment[:140]))
    return out


def fetch_nhl_injuries(timeout: int = 20) -> list[Absence]:
    try:
        r = requests.get(ESPN_NHL_INJURIES, timeout=timeout, headers={"User-Agent": "tipsbot"})
        r.raise_for_status()
        return parse_espn_injuries(r.json())
    except Exception as e:  # noqa: BLE001
        log.warning("NHL zraneni nejsou dostupna: %s", e)
        return []


def parse_api_football_injuries(payload: dict) -> list[Absence]:
    out = []
    for item in payload.get("response", []):
        p, t = item.get("player", {}), item.get("team", {})
        if p.get("name") and t.get("name"):
            out.append(Absence(t["name"], p["name"], p.get("type") or "Absence", p.get("reason") or ""))
    return out


def fetch_football_injuries(league: str, season: int, day: str, api_key: str, timeout: int = 20) -> list[Absence]:
    """Absence na zapasy jedne ligy v jeden den (1 pozadavek)."""
    lid = API_FOOTBALL_LEAGUES.get(league)
    if not api_key or lid is None:
        return []
    try:
        r = requests.get(
            f"{API_FOOTBALL}/injuries",
            params={"league": lid, "season": season, "date": day},
            headers={"x-apisports-key": api_key},
            timeout=timeout,
        )
        r.raise_for_status()
        return parse_api_football_injuries(r.json())
    except Exception as e:  # noqa: BLE001
        log.warning("Zraneni %s nejsou dostupna: %s", league, e)
        return []


def for_match(absences: list[Absence], home: str, away: str) -> list[Absence]:
    """Absence obou tymu (nazvy se paruji tolerantne)."""
    teams = {a.team for a in absences}
    h, a_ = match_team(home, teams), match_team(away, teams)
    return [x for x in absences if x.team in {h, a_}]


def severity(absences: list[Absence]) -> int:
    """Pocet jistych absenci (Out/Missing) - orientacni; model je zatim nezapocitava."""
    return sum(1 for a in absences if normalize(a.status) in {"out", "missing fixture", "injured reserve", "suspended"})


# --- pocasi (Open-Meteo, bez klice) a novinky (Google News RSS, bez klice) ---------------

OPEN_METEO_GEO = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


def fetch_venue_cities(league: str, season: int, day: str, api_key: str, timeout: int = 20) -> dict[str, str]:
    """Domaci tym -> mesto stadionu z API-Football (1 pozadavek na ligu a den)."""
    lid = API_FOOTBALL_LEAGUES.get(league)
    if not api_key or lid is None:
        return {}
    try:
        r = requests.get(f"{API_FOOTBALL}/fixtures", params={"league": lid, "season": season, "date": day},
                         headers={"x-apisports-key": api_key}, timeout=timeout)
        r.raise_for_status()
        return {
            f["teams"]["home"]["name"]: f["fixture"]["venue"]["city"]
            for f in r.json().get("response", [])
            if f.get("fixture", {}).get("venue", {}).get("city")
        }
    except Exception as e:  # noqa: BLE001
        log.warning("Mesta stadionu nejsou dostupna: %s", e)
        return {}


def describe_weather(hourly: dict, kickoff_hour_iso: str) -> str | None:
    """Text o pocasi z odpovedi Open-Meteo pro hodinu vykopu."""
    try:
        i = hourly["time"].index(kickoff_hour_iso)
        temp, rain, prob, wind = (hourly[k][i] for k in ("temperature_2m", "precipitation", "precipitation_probability", "wind_speed_10m"))
    except (KeyError, ValueError, IndexError):
        return None
    note = "těžký déšť, může snížit počet gólů" if rain >= 2.5 else "déšť" if rain >= 0.5 else "sucho"
    return f"{temp:.0f} °C, srážky {rain:.1f} mm (pravděpodobnost {prob:.0f} %), vítr {wind:.0f} km/h – {note}"


def fetch_weather(city: str, kickoff_utc, timeout: int = 20) -> str | None:
    try:
        g = requests.get(OPEN_METEO_GEO, params={"name": city, "count": 1}, timeout=timeout).json()
        loc = g["results"][0]
        day = kickoff_utc.strftime("%Y-%m-%d")
        w = requests.get(OPEN_METEO, params={
            "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "UTC",
            "hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m",
            "start_date": day, "end_date": day}, timeout=timeout).json()
        return describe_weather(w["hourly"], kickoff_utc.strftime("%Y-%m-%dT%H:00"))
    except Exception as e:  # noqa: BLE001
        log.warning("Pocasi pro %s neni dostupne: %s", city, e)
        return None


def parse_news_rss(xml_text: str, limit: int = 3) -> list[str]:
    import xml.etree.ElementTree as ET

    try:
        items = ET.fromstring(xml_text).findall(".//item")
    except ET.ParseError:
        return []
    return [(i.findtext("title") or "").strip() for i in items[:limit] if i.findtext("title")]


def fetch_news(team: str, extra: str = "injury lineup", timeout: int = 20) -> list[str]:
    """Nejnovejsi titulky o tymu (zraneni, sestava). Jen k orientaci - model je nezapocitava."""
    try:
        r = requests.get("https://news.google.com/rss/search", params={"q": f"{team} {extra} when:2d", "hl": "en"}, timeout=timeout)
        r.raise_for_status()
        return parse_news_rss(r.text)
    except Exception as e:  # noqa: BLE001
        log.warning("Novinky pro %s nejsou dostupne: %s", team, e)
        return []
