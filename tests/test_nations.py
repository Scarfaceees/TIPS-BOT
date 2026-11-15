from datetime import date

import pandas as pd

from tipsbot import nations
from tipsbot.config import SETTINGS
from tipsbot.models import football


def _csv(tmp_path):
    rows = []
    teams = ["Alpha", "Beta", "Gamma", "Delta"]
    d0 = date(2026, 1, 1)
    for i in range(48):  # Alpha silny, Delta slaby
        h, a = teams[i % 4], teams[(i + 1 + i // 4) % 4]
        if h == a:
            continue
        rows.append((str(d0 + pd.Timedelta(days=i * 5)), h, a, 3 if h == "Alpha" else 1 if a != "Alpha" else 0,
                     0 if h == "Alpha" else 1, "UEFA Nations League", "X", "Y", "FALSE"))
    rows.append(("2026-02-01", "Alpha", "Delta", 5, 0, "FIFA World Cup", "X", "Y", "TRUE"))  # neutralni
    p = tmp_path / "results.csv"
    pd.DataFrame(rows, columns=["date", "home_team", "away_team", "home_score", "away_score", "tournament",
                                "city", "country", "neutral"]).to_csv(p, index=False)
    return p


def test_loader_drops_neutral_unless_asked(tmp_path):
    p = _csv(tmp_path)
    assert (nations.load_international(p, neutral=True)["tournament"] == "FIFA World Cup").sum() == 1
    assert (nations.load_international(p)["tournament"] == "FIFA World Cup").sum() == 0


def test_national_model_fits_and_ranks(tmp_path):
    df = nations.load_international(_csv(tmp_path))
    from dataclasses import replace
    cfg = replace(SETTINGS.football, xi=SETTINGS.football.national_xi, max_history_seasons=SETTINGS.football.national_history_seasons)
    params = football.fit(df, date(2026, 11, 13), cfg=cfg)
    ph, pdr, pa = football.probs_1x2(football.match_matrix(params, "Alpha", "Delta"))
    assert ph > pa and abs(ph + pdr + pa - 1) < 1e-6


def test_neutral_matches_do_not_inflate_home_advantage(tmp_path):
    from dataclasses import replace
    rows = []
    for i in range(40):  # same vysledky, ale pri neutralni pude se domaci vyhoda nema pocitat
        rows += [(str(date(2026, 1, 1) + pd.Timedelta(days=i)), "Alpha", "Beta", 2, 0, "FIFA World Cup", "X", "Y", "TRUE"),
                 (str(date(2026, 1, 1) + pd.Timedelta(days=i)), "Beta", "Alpha", 1, 1, "Friendly", "X", "Y", "FALSE")]
    p = tmp_path / "r.csv"
    pd.DataFrame(rows, columns=["date", "home_team", "away_team", "home_score", "away_score", "tournament", "city",
                                "country", "neutral"]).to_csv(p, index=False)
    df = nations.load_international(p, neutral=True)
    assert df.loc[df.tournament == "Friendly", "match_weight"].iloc[0] == nations.FRIENDLY_WEIGHT
    cfg = replace(SETTINGS.football, xi=0.0019, max_history_seasons=4)
    with_flag = football.fit(df, date(2026, 11, 1), cfg=cfg)
    no_flag = football.fit(df.assign(neutral=False), date(2026, 11, 1), cfg=cfg)
    assert with_flag.attack["Alpha"] > no_flag.attack["Alpha"]  # neutralni vyhry Alphy nejsou domaci vyhoda, je tedy silnejsi


def test_goal_markets_shrink_towards_base_rate():
    probs = {"OU_2.5:over": 0.80, "OU_2.5:under": 0.20, "BTTS:yes": 0.10, "BTTS:no": 0.90, "1X2:1": 0.6}
    out = nations.shrink_goal_markets(probs, 0.5)
    assert 0.496 < out["OU_2.5:over"] < 0.80 and abs(out["OU_2.5:over"] + out["OU_2.5:under"] - 1) < 1e-9
    assert out["BTTS:yes"] > 0.10 and out["1X2:1"] == 0.6


def test_goal_base_rates_and_custom_base():
    import pandas as pd

    df = pd.DataFrame({"home_goals": [2, 0, 1, 3], "away_goals": [1, 0, 1, 0]})
    base = nations.goal_base_rates(df)
    assert base["OU_2.5:over"] == 0.5 and base["BTTS:yes"] == 0.5 and base["TTA_0.5:over"] == 0.5
    out = nations.shrink_goal_markets({"OU_2.5:over": 0.9, "OU_2.5:under": 0.1}, 0.5, base)
    assert out["OU_2.5:over"] == 0.7 and abs(out["OU_2.5:under"] - 0.3) < 1e-9
