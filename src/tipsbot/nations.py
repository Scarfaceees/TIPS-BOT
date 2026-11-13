"""Reprezentace (Liga narodu): vysledky vsech mezinarodnich zapasu a data pro Dixon-Coles model.

Zdroj: verejny soubor vysledku mezinarodnich zapasu (martj42/international_results na GitHubu, zdarma).
Zapasy na neutralni pude se z odhadu vynechavaji (model pocita s domaci vyhodou).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import requests

from .config import SETTINGS

log = logging.getLogger(__name__)

# Prátelaky maji mensi vahu (zpetny test: mirne zlepseni), neutralni zapasy se do odhadu berou bez domaci vyhody.
FRIENDLY_WEIGHT = 0.5
RESULTS_URL = "https://raw.githubusercontent.com/martj42/international_results/master/results.csv"


def results_path(data_dir: Path | None = None) -> Path:
    return Path(data_dir or SETTINGS.data_dir) / "international" / "results.csv"


def download_results(data_dir: Path | None = None, timeout: int = 60) -> Path:
    path = results_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(RESULTS_URL, timeout=timeout, headers={"User-Agent": "tipsbot"})
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


def load_international(path: Path | str | None = None, neutral: bool = False) -> pd.DataFrame:
    """Vysledky v jednotnem formatu bota; zapasy bez skore jsou vynechany, neutralni jen kdyz `neutral` (vyhodnoceni tiketu)."""
    path = Path(path) if path else results_path()
    if not path.exists():
        raise FileNotFoundError(f"Chybi {path}. Spust: tipsbot fetch-intl")
    raw = pd.read_csv(path, encoding="utf-8-sig")
    raw = raw.dropna(subset=["home_score", "away_score"])
    if not neutral:
        raw = raw[~raw["neutral"].astype(str).str.lower().eq("true")]
    out = pd.DataFrame({
        "date": pd.to_datetime(raw["date"]).dt.date,
        "home": raw["home_team"].astype(str).str.strip(),
        "away": raw["away_team"].astype(str).str.strip(),
        "home_goals": raw["home_score"].astype(int),
        "away_goals": raw["away_score"].astype(int),
        "league": "UNL",
        "tournament": raw["tournament"].astype(str),
        "neutral": raw["neutral"].astype(str).str.lower().eq("true"),
        "match_weight": raw["tournament"].map(lambda t: FRIENDLY_WEIGHT if t == "Friendly" else 1.0),
    })
    return out.sort_values("date").reset_index(drop=True)
