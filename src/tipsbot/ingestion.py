"""Sber dat. Zatim: historicke vysledky a kurzy fotbalu z football-data.co.uk (zdarma, CSV).

Pokryva top 5 lig a Liga Portugal. Chance Liga v tomto zdroji neni.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from .config import SETTINGS

log = logging.getLogger(__name__)

FOOTBALL_DATA_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
FOOTBALL_DATA_CODES = {
    "ENG1": "E0",
    "ESP1": "SP1",
    "GER1": "D1",
    "ITA1": "I1",
    "FRA1": "F1",
    "POR1": "P1",
}

# cilovy sloupec -> mozne zdrojove sloupce (prvni nalezeny vyhrava)
_ODDS_COLUMNS = {
    "sharp_1": ("PSCH", "PSH", "PH"),  # Pinnacle: zaviraci, jinak predzapasovy
    "sharp_x": ("PSCD", "PSD", "PD"),
    "sharp_2": ("PSCA", "PSA", "PA"),
    "odds_1": ("AvgH", "BbAvH", "B365H"),  # prumer trhu jako odhad bezne kancelare
    "odds_x": ("AvgD", "BbAvD", "B365D"),
    "odds_2": ("AvgA", "BbAvA", "B365A"),
    "max_1": ("MaxH", "BbMxH"),
    "max_x": ("MaxD", "BbMxD"),
    "max_2": ("MaxA", "BbMxA"),
}


def season_code(start_year: int) -> str:
    """2025 -> '2526' (sezona 2025/26)."""
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def current_season_start_year(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 7 else today.year - 1


def football_csv_path(league: str, start_year: int, data_dir: Path | None = None) -> Path:
    base = Path(data_dir or SETTINGS.data_dir) / "football"
    return base / f"{league}_{season_code(start_year)}.csv"


def download_football_season(league: str, start_year: int, data_dir: Path | None = None, timeout: int = 30) -> Path:
    """Stahne CSV jedne sezony a ulozi ho do datove slozky."""
    if league not in FOOTBALL_DATA_CODES:
        raise ValueError(f"Liga {league} neni v tomto zdroji dostupna.")
    url = FOOTBALL_DATA_URL.format(season=season_code(start_year), code=FOOTBALL_DATA_CODES[league])
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    path = football_csv_path(league, start_year, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    log.info("Stazeno %s -> %s", url, path)
    return path


def parse_football_csv(path: Path | str, league: str = "") -> pd.DataFrame:
    """Prevede CSV z football-data.co.uk na jednotny format bota."""
    try:  # pohary uklada bot v UTF-8, stare soubory football-data.co.uk jsou latin-1
        raw = pd.read_csv(path, encoding="utf-8-sig", on_bad_lines="skip")
    except UnicodeDecodeError:
        raw = pd.read_csv(path, encoding="latin-1", on_bad_lines="skip")
    raw = raw.rename(columns={c: c.strip().lstrip("﻿").lstrip("ï»¿") for c in raw.columns})
    needed = ("Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG")
    missing = [c for c in needed if c not in raw.columns]
    if missing:
        raise ValueError(f"{path}: chybi sloupce {missing}")
    raw = raw.dropna(subset=list(needed))
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["Date"], dayfirst=True, format="mixed").dt.date,
            "home": raw["HomeTeam"].astype(str).str.strip(),
            "away": raw["AwayTeam"].astype(str).str.strip(),
            "home_goals": raw["FTHG"].astype(int),
            "away_goals": raw["FTAG"].astype(int),
        }
    )
    for target, sources in _ODDS_COLUMNS.items():
        src = next((c for c in sources if c in raw.columns), None)
        if src is not None:
            out[target] = pd.to_numeric(raw[src], errors="coerce")
    out["league"] = league
    return out.sort_values("date").reset_index(drop=True)


def load_football(league: str, data_dir: Path | None = None) -> pd.DataFrame:
    """Nacte vsechny stazene sezony jedne ligy."""
    base = Path(data_dir or SETTINGS.data_dir) / "football"
    files = sorted(base.glob(f"{league}_*.csv"))
    if not files:
        raise FileNotFoundError(f"Zadna data pro {league} v {base}. Spust: tipsbot fetch-football --league {league}")
    frames = [parse_football_csv(f, league) for f in files]
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)


def fetch_football(league: str, seasons: int = 4, data_dir: Path | None = None) -> list[Path]:
    """Stahne poslednich `seasons` sezon (vcetne aktualni)."""
    last = current_season_start_year()
    paths = []
    for year in range(last - seasons + 1, last + 1):
        try:
            paths.append(download_football_season(league, year, data_dir))
        except requests.RequestException as e:
            log.warning("Sezonu %s se nepodarilo stahnout: %s", season_code(year), e)
    return paths
