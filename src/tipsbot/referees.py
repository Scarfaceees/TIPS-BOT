"""Rozhodci zapasu z API-Football (/fixtures: 1 dotaz na ligu a sezonu, obsahuje i naplanovane zapasy).

football-data.co.uk ma rozhodci jen u anglicke ligy; ostatnim ligam se doplni odsud. U naplanovanych zapasu
API rozhodciho uvadi, jakmile je nasazeny (obvykle par dni pred zapasem).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from .config import SETTINGS
from .context import API_FOOTBALL, API_FOOTBALL_LEAGUES
from .ingestion import current_season_start_year
from .names import match_team

log = logging.getLogger(__name__)
COLUMNS = ["date", "home", "away", "referee"]


def path(league: str, year: int, data_dir: Path | None = None) -> Path:
    return Path(data_dir or SETTINGS.data_dir) / "referees" / f"{league}_{year}.csv"


def parse_fixtures(payload: dict) -> pd.DataFrame:
    rows = []
    for item in payload.get("response", []):
        fx, teams = item.get("fixture", {}), item.get("teams", {})
        home, away = teams.get("home", {}).get("name"), teams.get("away", {}).get("name")
        if not (fx.get("date") and home and away):
            continue
        when = datetime.fromisoformat(fx["date"].replace("Z", "+00:00"))
        rows.append({"date": when.date(), "home": home, "away": away, "referee": fx.get("referee") or None})
    return pd.DataFrame(rows, columns=COLUMNS)


def fetch(api_key: str, leagues, seasons: int = 3, data_dir: Path | None = None, timeout: int = 30) -> list[str]:
    """Stahne rozhodci pro ligy a posledni `seasons` sezon. Vraci zpravy (chyba jedne ligy nevadi ostatnim)."""
    if not api_key:
        return ["rozhodčí: chybí klíč API-Football (Nastavení)"]
    notes = []
    last = current_season_start_year()
    for lg in leagues:
        lid = API_FOOTBALL_LEAGUES.get(lg)
        if lid is None:
            continue
        for year in range(last - seasons + 1, last + 1):
            out = path(lg, year, data_dir)
            if year < last and out.exists():
                continue  # uzavrena sezona uz se nemeni
            try:
                r = requests.get(f"{API_FOOTBALL}/fixtures", params={"league": lid, "season": year},
                                 headers={"x-apisports-key": api_key}, timeout=timeout)
                data = r.json()
                if data.get("errors"):
                    notes.append(f"rozhodčí {lg} {year}: API odmítlo dotaz")
                    continue
                df = parse_fixtures(data)
                out.parent.mkdir(parents=True, exist_ok=True)
                df.to_csv(out, index=False)
                notes.append(f"rozhodčí {lg} {year}: {int(df['referee'].notna().sum())} z {len(df)} zápasů")
            except Exception as e:  # noqa: BLE001
                log.warning("rozhodci %s %s: %s", lg, year, e)
                notes.append(f"rozhodčí {lg} {year}: nepodařilo se ({type(e).__name__})")
    return notes


def load(league: str, data_dir: Path | None = None) -> pd.DataFrame:
    files = sorted((Path(data_dir or SETTINGS.data_dir) / "referees").glob(f"{league}_*.csv"))
    if not files:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def _lookup(refs: pd.DataFrame, known: set[str]) -> dict[tuple, str]:
    """(datum, domaci, hoste) -> rozhodci; nazvy tymu prevedene na nazvy v datech ligy."""
    out = {}
    names = {n: match_team(n, known) for n in set(refs["home"]) | set(refs["away"])}
    for d, h, a, r in refs[refs["referee"].notna()][COLUMNS].itertuples(index=False):
        if names.get(h) and names.get(a):
            out[(d, names[h], names[a])] = r
    return out


def attach(df: pd.DataFrame, league: str, data_dir: Path | None = None) -> pd.DataFrame:
    """Doplni sloupec `referee` tam, kde chybi (datum se toleruje o den: API ma UTC)."""
    refs = load(league, data_dir)
    df = df.copy()
    if "referee" not in df:
        df["referee"] = None
    if refs.empty:
        return df
    table = _lookup(refs, set(df["home"]) | set(df["away"]))
    missing = df["referee"].isna()
    df.loc[missing, "referee"] = [
        next((table[k] for k in ((d + timedelta(days=s), h, a) for s in (0, -1, 1)) if k in table), None)
        for d, h, a in df.loc[missing, ["date", "home", "away"]].itertuples(index=False)
    ]
    return df


def upcoming(league: str, home: str, away: str, day: date, data_dir: Path | None = None) -> str | None:
    """Nasazeny rozhodci naplanovaneho zapasu (nazvy z kurzoveho API), jinak None."""
    refs = load(league, data_dir)
    if refs.empty:
        return None
    near = refs[(refs["date"] >= day - timedelta(days=1)) & (refs["date"] <= day + timedelta(days=1)) & refs["referee"].notna()]
    for h, a, r in near[["home", "away", "referee"]].itertuples(index=False):
        if match_team(home, [h]) and match_team(away, [a]):
            return r
    return None
