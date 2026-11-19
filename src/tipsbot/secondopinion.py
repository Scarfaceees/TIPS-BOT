"""Druhy pohled na fotbal: nezavisly Dixon-Coles z knihovny penaltyblog (MIT).

Slouzi jen jako kontrola naseho modelu: kdyz se oba vyrazne rozchazeji, je to varovani.
Knihovna je volitelna (pip install penaltyblog); bez ni se druhy pohled vynecha.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import pandas as pd

log = logging.getLogger(__name__)

GAP = 0.07  # rozdil pravdepodobnosti obou modelu, od ktereho se zobrazi varovani
MIN_MATCHES = 100
_CACHE: dict = {}  # odhad trva az desitky sekund (reprezentace), v ramci behu se pouzije znovu


def available() -> bool:
    try:
        import penaltyblog  # noqa: F401
    except ImportError:
        return False
    return True


def fit(matches: pd.DataFrame, as_of, xi: float, history_seasons: int, model: str = "dc"):
    """Odhad k datu `as_of` ze stejnych zapasu jako nas model. Vraci None, kdyz to nejde."""
    if not available():
        return None
    import penaltyblog as pb

    df = matches.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    as_of = pd.Timestamp(as_of).date()
    df = df[(df["date"] < as_of) & (df["date"] >= as_of - timedelta(days=365 * history_seasons))]
    if len(df) < MIN_MATCHES:
        return None
    key = (len(df), df["date"].max(), int(df["home_goals"].sum()), as_of, xi, history_seasons, model)
    if key in _CACHE:
        return _CACHE[key]
    weights = pb.models.dixon_coles_weights(df["date"], xi)
    if "match_weight" in df.columns:
        weights = weights * df["match_weight"].to_numpy()
    neutral = df["neutral"].astype(int) if "neutral" in df.columns else None
    cls = {"dc": pb.models.DixonColesGoalModel, "bp": pb.models.BivariatePoissonGoalModel,
           "nb": pb.models.NegativeBinomialGoalModel}[model]
    try:
        m = cls(df["home_goals"], df["away_goals"], df["home"], df["away"], weights, neutral)
        m.fit()
    except Exception as e:  # noqa: BLE001 - druhy pohled nesmi shodit sken
        log.warning("druhy model se nepodarilo odhadnout: %s", e)
        return None
    m.known_teams = set(df["home"]) | set(df["away"])
    _CACHE.clear()
    _CACHE[key] = m
    return m


def probabilities(model, home: str, away: str, neutral: bool = False) -> dict[str, float] | None:
    """Pravdepodobnosti hlavnich trhu ve stejnych klicich jako nas model."""
    if model is None or home not in model.known_teams or away not in model.known_teams:
        return None
    try:
        g = model.predict(home, away, neutral_venue=neutral)
    except Exception as e:  # noqa: BLE001
        log.warning("druhy model %s - %s: %s", home, away, e)
        return None
    p1, px, p2 = (float(x) for x in g.home_draw_away)
    out = {"1X2:1": p1, "1X2:X": px, "1X2:2": p2, "DC:1X": p1 + px, "DC:X2": px + p2, "DC:12": p1 + p2,
           "DNB:home": p1 / (p1 + p2), "DNB:away": p2 / (p1 + p2)}
    for line in (1.5, 2.5, 3.5, 4.5):
        over = float(g.total_goals("over", line))
        out[f"OU_{line:g}:over"], out[f"OU_{line:g}:under"] = over, 1.0 - over
    yes = float(g.btts_yes)
    out["BTTS:yes"], out["BTTS:no"] = yes, 1.0 - yes
    return out
