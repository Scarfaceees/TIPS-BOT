"""Pohary (LM, EL, ECL): stahovani vysledku a spolecny model s ligami.

Zdroje: API-Football (plan Free: sezony 2022-2024) a football-data.org (LM, aktualni sezona).
Soubory se ukladaji ve formatu football-data.co.uk, takze je cte `ingestion.load_football`.
Do vysledku se pocita jen 90 minut (bez prodlouzeni a penalt).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import requests

from .config import SETTINGS
from .ingestion import current_season_start_year, football_csv_path

log = logging.getLogger(__name__)

API_FOOTBALL = "https://v3.football.api-sports.io"
FOOTBALL_DATA_ORG = "https://api.football-data.org/v4"
CUP_API_FOOTBALL_IDS = {"UCL": 2, "UEL": 3, "UECL": 848}
CUP_FOOTBALL_DATA_ORG = {"UCL": "CL"}
DOMESTIC = ("ENG1", "ESP1", "GER1", "ITA1", "FRA1", "POR1")
FINISHED = {"FT", "AET", "PEN"}


def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    cols = ["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]
    df = pd.DataFrame(rows, columns=cols)
    return df.dropna().sort_values("Date").reset_index(drop=True)


def parse_api_football_fixtures(payload: dict) -> pd.DataFrame:
    rows = []
    for f in payload.get("response", []):
        if f.get("fixture", {}).get("status", {}).get("short") not in FINISHED:
            continue
        ft = f.get("score", {}).get("fulltime", {})
        if ft.get("home") is None or ft.get("away") is None:
            continue
        day = pd.Timestamp(f["fixture"]["date"]).strftime("%d/%m/%Y")
        rows.append([day, f["teams"]["home"]["name"], f["teams"]["away"]["name"], int(ft["home"]), int(ft["away"])])
    return _rows_to_frame(rows)


def parse_football_data_org(payload: dict) -> pd.DataFrame:
    rows = []
    for m in payload.get("matches", []):
        if m.get("status") != "FINISHED":
            continue
        score = m.get("score", {})
        # regularTime existuje jen pri prodlouzeni; jinak je fullTime vysledek po 90 minutach
        res = score.get("regularTime") or score.get("fullTime") or {}
        if res.get("home") is None or res.get("away") is None:
            continue
        day = pd.Timestamp(m["utcDate"]).strftime("%d/%m/%Y")
        rows.append([day, m["homeTeam"]["name"], m["awayTeam"]["name"], int(res["home"]), int(res["away"])])
    return _rows_to_frame(rows)


def _save(df: pd.DataFrame, league: str, start_year: int, data_dir: Path | None) -> Path:
    path = football_csv_path(league, start_year, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return path


def fetch_cups(api_football_key: str = "", football_data_key: str = "", seasons: int = 3,
               data_dir: Path | None = None, timeout: int = 30) -> list[str]:
    """Stahne pohary. Vraci zpravy o tom, co se podarilo a co ne (zadna chyba nezastavi zbytek)."""
    notes: list[str] = []
    last = current_season_start_year()
    for league in CUP_API_FOOTBALL_IDS:
        for year in range(last - seasons + 1, last + 1):
            got = False
            if api_football_key:
                try:
                    r = requests.get(f"{API_FOOTBALL}/fixtures", headers={"x-apisports-key": api_football_key},
                                     params={"league": CUP_API_FOOTBALL_IDS[league], "season": year}, timeout=timeout)
                    data = r.json()
                    if not data.get("errors"):
                        df = parse_api_football_fixtures(data)
                        if len(df):
                            _save(df, league, year, data_dir)
                            notes.append(f"{league} {year}: {len(df)} zapasu (API-Football)")
                            got = True
                except Exception as e:  # noqa: BLE001
                    log.warning("%s %s API-Football: %s", league, year, e)
            if not got and football_data_key and league in CUP_FOOTBALL_DATA_ORG:
                try:
                    r = requests.get(f"{FOOTBALL_DATA_ORG}/competitions/{CUP_FOOTBALL_DATA_ORG[league]}/matches",
                                     headers={"X-Auth-Token": football_data_key}, params={"season": year}, timeout=timeout)
                    if r.ok:
                        df = parse_football_data_org(r.json())
                        if len(df):
                            _save(df, league, year, data_dir)
                            notes.append(f"{league} {year}: {len(df)} zapasu (football-data.org)")
                            got = True
                except Exception as e:  # noqa: BLE001
                    log.warning("%s %s football-data.org: %s", league, year, e)
            if not got:
                notes.append(f"{league} {year}: nedostupne")
    return notes
