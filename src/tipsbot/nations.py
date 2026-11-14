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


# Podil zapasu reprezentaci s danym vysledkem (Liga narodu a kvalifikace od 9/2024, 976 zapasu, neutralni vynechany).
GOAL_BASE_RATES = {
    "OU_1.5:over": 0.717, "OU_2.5:over": 0.496, "OU_3.5:over": 0.301, "BTTS:yes": 0.405,
    "TTH_0.5:over": 0.732, "TTH_1.5:over": 0.449, "TTH_2.5:over": 0.234,
    "TTA_0.5:over": 0.586, "TTA_1.5:over": 0.276, "TTA_2.5:over": 0.119,
}


def shrink_goal_markets(probs: dict[str, float], k: float) -> dict[str, float]:
    """Trhy na goly (over/under, oba skoruji, tymove totaly) se pritahnou k prumeru reprezentaci.

    Zpetny test (975 zapasu): model v prumeru sedi, ale jeho predikce jsou prilis extremni
    (predpoved 24 % -> skutecnost 35 %, predpoved 78 % -> skutecnost 64 %; sklon cca 0,54)
    a nerozlisuje lepe nez konstantni sazka. Proto k < 1."""
    out = dict(probs)
    for key, base in GOAL_BASE_RATES.items():
        if key in out:
            p = base + k * (out[key] - base)
            out[key] = p
            other = key.replace(":over", ":under").replace(":yes", ":no")
            out[other] = 1.0 - p
    return out
