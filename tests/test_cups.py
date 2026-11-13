from datetime import date

import numpy as np
import pandas as pd

from tipsbot import cups
from tipsbot.config import FootballModelConfig
from tipsbot.models import football


def test_parse_api_football_uses_90_minutes_and_skips_unfinished():
    payload = {"response": [
        {"fixture": {"date": "2025-03-01T20:00:00+00:00", "status": {"short": "PEN"}},
         "teams": {"home": {"name": "A"}, "away": {"name": "B"}}, "score": {"fulltime": {"home": 1, "away": 1}}},
        {"fixture": {"date": "2025-03-02T20:00:00+00:00", "status": {"short": "NS"}},
         "teams": {"home": {"name": "C"}, "away": {"name": "D"}}, "score": {"fulltime": {"home": None, "away": None}}},
    ]}
    df = cups.parse_api_football_fixtures(payload)
    assert len(df) == 1 and df.iloc[0]["FTHG"] == 1 and df.iloc[0]["FTAG"] == 1


def test_parse_football_data_org_prefers_regular_time():
    payload = {"matches": [{"status": "FINISHED", "utcDate": "2025-03-01T20:00:00Z",
                            "homeTeam": {"name": "A"}, "awayTeam": {"name": "B"},
                            "score": {"fullTime": {"home": 3, "away": 2}, "regularTime": {"home": 1, "away": 1}}}]}
    df = cups.parse_football_data_org(payload)
    assert (df.iloc[0]["FTHG"], df.iloc[0]["FTAG"]) == (1, 1)


def _league(rng, teams, strength, n=240):
    rows = []
    for i in range(n):
        h, a = rng.choice(teams, 2, replace=False)
        lam = np.exp(0.2 + 0.25 + strength[h] - strength[a] * 0.5)
        mu = np.exp(0.2 + strength[a] - strength[h] * 0.5)
        rows.append((date(2026, 1, 1) + pd.Timedelta(days=i % 200), h, a, rng.poisson(lam), rng.poisson(mu)))
    return pd.DataFrame(rows, columns=["date", "home", "away", "home_goals", "away_goals"])


def test_joint_fit_recovers_stronger_league():
    rng = np.random.default_rng(1)
    strong = [f"S{i}" for i in range(6)]
    weak = [f"W{i}" for i in range(6)]
    strength = {t: 0.0 for t in strong + weak}
    for t in strong:
        strength[t] += 0.5  # silna liga je silnejsi jako celek
    domestic = {"STR": _league(rng, strong, strength), "WEA": _league(rng, weak, strength)}
    cup_rows = []
    for i in range(160):
        h, a = rng.choice(strong + weak, 2, replace=False)
        lam = np.exp(0.2 + 0.25 + strength[h] - strength[a] * 0.5)
        mu = np.exp(0.2 + strength[a] - strength[h] * 0.5)
        cup_rows.append((date(2026, 2, 1) + pd.Timedelta(days=i % 150), h, a, rng.poisson(lam), rng.poisson(mu)))
    cups_df = pd.DataFrame(cup_rows, columns=["date", "home", "away", "home_goals", "away_goals"])
    params, offsets = football.fit_joint(domestic, cups_df, date(2026, 9, 1), FootballModelConfig(xi=0.0))
    assert offsets["STR"] > offsets["WEA"]
    pr = football.market_probabilities(params, "S0", "W0")
    assert pr["1X2:1"] > pr["1X2:2"]


def test_utf8_cup_csv_keeps_accents(tmp_path):
    import pandas as pd
    from tipsbot import ingestion
    p = tmp_path / "UCL_2026.csv"
    pd.DataFrame([{"Date": "2026-11-04", "HomeTeam": "Bayern München", "AwayTeam": "Baník Ostrava", "FTHG": 2, "FTAG": 0}]
                 ).to_csv(p, index=False)
    assert ingestion.parse_football_csv(p, "UCL")["home"].iloc[0] == "Bayern München"
