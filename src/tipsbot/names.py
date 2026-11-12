"""Parovani nazvu tymu mezi zdroji (kurzove API vs. historicka data)."""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable

_DROP = {"fc", "cf", "ac", "as", "sc", "afc", "ssc", "us", "rc", "cd", "ud", "sv", "vfl", "vfb", "tsg", "fsv", "ogc", "aj", "de", "club", "calcio", "1", "05", "04", "1846", "1899", "1910"}

# normalizovany nazev z kurzoveho API -> nazev ve football-data.co.uk
_ALIASES = {
    "manchester united": "Man United",
    "manchester city": "Man City",
    "wolverhampton wanderers": "Wolves",
    "nottingham forest": "Nott'm Forest",
    "newcastle united": "Newcastle",
    "tottenham hotspur": "Tottenham",
    "west ham united": "West Ham",
    "brighton and hove albion": "Brighton",
    "leeds united": "Leeds",
    "leicester city": "Leicester",
    "atletico madrid": "Ath Madrid",
    "athletic bilbao": "Ath Bilbao",
    "athletic": "Ath Bilbao",
    "real betis": "Betis",
    "real sociedad": "Sociedad",
    "celta vigo": "Celta",
    "rayo vallecano": "Vallecano",
    "espanyol": "Espanol",
    "bayern munchen": "Bayern Munich",
    "borussia dortmund": "Dortmund",
    "borussia monchengladbach": "M'gladbach",
    "bayer leverkusen": "Leverkusen",
    "eintracht frankfurt": "Ein Frankfurt",
    "koln": "FC Koln",
    "cologne": "FC Koln",
    "st pauli": "St Pauli",
    "hamburger": "Hamburg",
    "inter milan": "Inter",
    "internazionale": "Inter",
    "hellas verona": "Verona",
    "paris saint germain": "Paris SG",
    "saint etienne": "St Etienne",
    "sporting lisbon": "Sp Lisbon",
    "sporting cp": "Sp Lisbon",
    "sporting": "Sp Lisbon",
    "braga": "Sp Braga",
    "vitoria": "Guimaraes",
    "vitoria guimaraes": "Guimaraes",
}

NHL_ABBR = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF", "Calgary Flames": "CGY",
    "Carolina Hurricanes": "CAR", "Chicago Blackhawks": "CHI", "Colorado Avalanche": "COL",
    "Columbus Blue Jackets": "CBJ", "Dallas Stars": "DAL", "Detroit Red Wings": "DET",
    "Edmonton Oilers": "EDM", "Florida Panthers": "FLA", "Los Angeles Kings": "LAK",
    "Minnesota Wild": "MIN", "Montreal Canadiens": "MTL", "Montréal Canadiens": "MTL",
    "Nashville Predators": "NSH", "New Jersey Devils": "NJD", "New York Islanders": "NYI",
    "New York Rangers": "NYR", "Ottawa Senators": "OTT", "Philadelphia Flyers": "PHI",
    "Pittsburgh Penguins": "PIT", "San Jose Sharks": "SJS", "Seattle Kraken": "SEA",
    "St Louis Blues": "STL", "St. Louis Blues": "STL", "Tampa Bay Lightning": "TBL",
    "Toronto Maple Leafs": "TOR", "Utah Mammoth": "UTA", "Utah Hockey Club": "UTA",
    "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK", "Washington Capitals": "WSH",
    "Winnipeg Jets": "WPG",
}

# posun mistniho casu domaciho stadionu vuci UTC (zimni cas), pro pravidlo back-to-back
NHL_UTC_OFFSET = {
    "BOS": -5, "BUF": -5, "CAR": -5, "CBJ": -5, "DET": -5, "FLA": -5, "MTL": -5, "NJD": -5, "NYI": -5,
    "NYR": -5, "OTT": -5, "PHI": -5, "PIT": -5, "TBL": -5, "TOR": -5, "WSH": -5,
    "CHI": -6, "DAL": -6, "MIN": -6, "NSH": -6, "STL": -6, "WPG": -6,
    "CGY": -7, "COL": -7, "EDM": -7, "UTA": -7,
    "ANA": -8, "LAK": -8, "SJS": -8, "SEA": -8, "VAN": -8, "VGK": -8,
}


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text.replace("&", " and "))
    return " ".join(t for t in text.split() if t not in _DROP)


def match_team(name: str, known: Iterable[str]) -> str | None:
    """Najde nazev tymu v seznamu `known` (nazvy z historickych dat)."""
    known = list(known)
    if name in known:
        return name
    norm = normalize(name)
    alias = _ALIASES.get(norm)
    if alias in known:
        return alias
    by_norm = {normalize(k): k for k in known}
    if norm in by_norm:
        return by_norm[norm]
    contained = [k for n, k in by_norm.items() if n and (n in norm.split() or norm.startswith(n + " ") or n.startswith(norm + " "))]
    if len(contained) == 1:
        return contained[0]
    close = difflib.get_close_matches(norm, list(by_norm), n=1, cutoff=0.8)
    return by_norm[close[0]] if close else None


def nhl_abbr(name: str) -> str | None:
    return NHL_ABBR.get(name)
