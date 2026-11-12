"""NHL data z MoneyPuck (zdarma pro nekomercni pouziti): tymove xG a strely po zapasech."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests

from .config import SETTINGS, NHLModelConfig
from .models.nhl import GameContext, Goalie, TeamRating, cold_start_rating, goalie_gsax60
from .names import NHL_UTC_OFFSET

log = logging.getLogger(__name__)

MONEYPUCK_TEAMS_URL = "https://moneypuck.com/moneypuck/playerData/careers/gameByGame/all_teams.csv"
_COLUMNS = [
    "team", "season", "gameId", "opposingTeam", "home_or_away", "gameDate", "situation", "iceTime",
    "xGoalsFor", "xGoalsAgainst", "unblockedShotAttemptsFor", "unblockedShotAttemptsAgainst",
    "goalsFor", "goalsAgainst",
]
_FIX = {"L.A": "LAK", "N.J": "NJD", "S.J": "SJS", "T.B": "TBL", "ARI": "UTA"}


def nhl_csv_path(data_dir: Path | None = None) -> Path:
    return Path(data_dir or SETTINGS.data_dir) / "nhl" / "all_teams.csv"


def download_nhl(data_dir: Path | None = None, timeout: int = 180) -> Path:
    """Stahne soubor se vsemi tymovymi zapasy (velky soubor, desitky MB)."""
    path = nhl_csv_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(MONEYPUCK_TEAMS_URL, stream=True, timeout=timeout, headers={"User-Agent": "tipsbot"}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path


MONEYPUCK_GOALIES_URL = "https://moneypuck.com/moneypuck/playerData/seasonSummary/{season}/regular/goalies.csv"


def goalies_csv_path(data_dir: Path | None = None) -> Path:
    return Path(data_dir or SETTINGS.data_dir) / "nhl" / "goalies.csv"


def current_goalie_season(today: date | None = None) -> int:
    today = today or date.today()
    return today.year if today.month >= 9 else today.year - 1


def download_goalies(data_dir: Path | None = None, today: date | None = None, timeout: int = 60) -> Path:
    """Stahne souhrn brankaru aktualni sezony z MoneyPuck (GSAx = xGoals - goals)."""
    path = goalies_csv_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    url = MONEYPUCK_GOALIES_URL.format(season=current_goalie_season(today))
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "tipsbot"})
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def team_goalies(path: Path | str | None = None) -> dict[str, Goalie]:
    """Kvalita brankare tymu: GSAx a strely vazene podilem odchytanych zapasu (starter neni predem znam)."""
    path = Path(path) if path else goalies_csv_path()
    if not path.exists():
        return {}
    df = pd.read_csv(path)
    need = {"team", "games_played", "xGoals", "goals", "ongoal"}
    if not need <= set(df.columns):
        raise ValueError(f"{path}: chybi sloupce {sorted(need - set(df.columns))}")
    if "situation" in df.columns:
        df = df[df["situation"] == "all"]
    df = df[df["games_played"] > 0].copy()
    df["team"] = df["team"].replace(_FIX)
    out: dict[str, Goalie] = {}
    for team, g in df.groupby("team"):
        share = g["games_played"] / g["games_played"].sum()
        out[team] = Goalie(
            gsax=float(((g["xGoals"] - g["goals"]) * share).sum()),
            shots=float((g["ongoal"] * share).sum()),
        )
    return out


def load_nhl(path: Path | str | None = None, min_season: int | None = None) -> pd.DataFrame:
    """Nacte zapasy tymu (vsechny herni situace dohromady)."""
    path = Path(path) if path else nhl_csv_path()
    if not path.exists():
        raise FileNotFoundError(f"Chybi {path}. Spust: tipsbot fetch-nhl")
    df = pd.read_csv(path, usecols=lambda c: c in _COLUMNS)
    missing = [c for c in _COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path}: chybi sloupce {missing}")
    df = df[df["situation"] == "all"].copy()
    if min_season is not None:
        df = df[df["season"] >= min_season]
    df["team"] = df["team"].replace(_FIX)
    df["opposingTeam"] = df["opposingTeam"].replace(_FIX)
    df["date"] = pd.to_datetime(df["gameDate"].astype(str), format="%Y%m%d").dt.date
    return df.sort_values("date").reset_index(drop=True)


def _rating(games: pd.DataFrame) -> TeamRating | None:
    hours = games["iceTime"].sum() / 3600.0
    if hours <= 0:
        return None
    return TeamRating(
        xgf60=float(games["xGoalsFor"].sum() / hours),
        xga60=float(games["xGoalsAgainst"].sum() / hours),
        ff60=float(games["unblockedShotAttemptsFor"].sum() / hours),
        fa60=float(games["unblockedShotAttemptsAgainst"].sum() / hours),
        games=int(len(games)),
    )


class NHLRatings:
    """Sily tymu k danemu dni: aktualni sezona, na zacatku michana s minulou."""

    def __init__(self, df: pd.DataFrame, as_of: date, cfg: NHLModelConfig | None = None, goalies: dict | None = None):
        self.cfg = cfg or SETTINGS.nhl
        self.goalies = goalies or {}
        self.as_of = as_of
        past = df[df["date"] < as_of]
        if past.empty:
            raise ValueError("Zadna NHL data pred zadanym dnem.")
        self.past = past
        season = int(past["season"].max())
        cur, prev = past[past["season"] == season], past[past["season"] == season - 1]
        base = cur if len(cur) >= 200 else pd.concat([prev, cur])
        hours = base["iceTime"].sum() / 3600.0
        self.league_xg60 = float(base["xGoalsFor"].sum() / hours)
        self.league_ff60 = float(base["unblockedShotAttemptsFor"].sum() / hours)
        self.ratings: dict[str, TeamRating] = {}
        for team in set(cur["team"]) | set(prev["team"]):
            now = _rating(cur[cur["team"] == team])
            before = _rating(prev[prev["team"] == team])
            if now and before:
                self.ratings[team] = cold_start_rating(before, now, self.cfg)
            elif now or before:
                self.ratings[team] = now or before  # type: ignore[assignment]

    def b2b_travel(self, team: str, game_day: date, venue: str) -> bool:
        """Druhy zapas ve dvou dnech a zaroven presun pres casove pasmo."""
        prev = self.past[(self.past["team"] == team) & (self.past["date"] == game_day - timedelta(days=1))]
        if prev.empty:
            return False
        row = prev.iloc[-1]
        prev_venue = team if row["home_or_away"] == "HOME" else row["opposingTeam"]
        a, b = NHL_UTC_OFFSET.get(prev_venue), NHL_UTC_OFFSET.get(venue)
        return a is not None and b is not None and a != b

    def context(self, home: str, away: str, game_day: date) -> GameContext | None:
        if home not in self.ratings or away not in self.ratings:
            return None
        return GameContext(
            home=self.ratings[home],
            away=self.ratings[away],
            league_xg60=self.league_xg60,
            league_ff60=self.league_ff60,
            home_goalie=self.goalies.get(home, Goalie()),
            away_goalie=self.goalies.get(away, Goalie()),
            home_b2b_travel=self.b2b_travel(home, game_day, home),
            away_b2b_travel=self.b2b_travel(away, game_day, home),
        )
