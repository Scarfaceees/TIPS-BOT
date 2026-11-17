from datetime import date, timedelta

import numpy as np
import pandas as pd

from tipsbot import analysis, czechodds, paper, settlement
from tipsbot.models import props


def _league(n=600, seed=1):
    """Syntetika: tym A ma hodne rohu, rozhodci 'X Strict' piska dvojnasobek karet."""
    rng = np.random.default_rng(seed)
    teams = ["A", "B", "C", "D", "E", "F"]
    rows = []
    for i in range(n):
        h, a = rng.choice(teams, 2, replace=False)
        ref = "Xavier Strict, Spain" if i % 5 == 0 else "Y Mild"
        lam_c = 5.0 * (2.0 if h == "A" else 1.0)
        rows.append({"date": date(2025, 1, 1) + timedelta(days=i // 3), "home": h, "away": a,
                     "home_goals": 1, "away_goals": 1, "home_corners": rng.poisson(lam_c), "away_corners": rng.poisson(4.5 * (2.0 if a == "A" else 1.0)),
                     "home_cards": rng.poisson(2.0 * (2 if ref.startswith("X") else 1)), "away_cards": rng.poisson(2.2 * (2 if ref.startswith("X") else 1)),
                     "referee": ref})
    return pd.DataFrame(rows)


def test_fit_learns_team_and_referee():
    df = _league()
    models = props.fit_all(df, date(2026, 1, 1))
    assert set(models) == {"ROHY", "KARTY"}
    c = models["ROHY"]
    assert c.own["A"] > 1.3 and c.own["A"] / c.own["B"] > 1.5
    k = models["KARTY"]
    assert k.ref[props.referee_key("X Strict")] / k.ref[props.referee_key("Y Mild")] > 1.5
    p = props.probabilities(models, "A", "B")
    assert p["ROHY_9.5:over"] > props.probabilities(models, "B", "C")["ROHY_9.5:over"]
    assert abs(p["KARTY_4.5:over"] + p["KARTY_4.5:under"] - 1) < 1e-9
    strict = props.probabilities(models, "B", "C", "Xavier Strict")["KARTY_4.5:over"]
    assert strict > props.probabilities(models, "B", "C", "Y Mild")["KARTY_4.5:over"]


def test_referee_key_unifies_sources():
    assert props.referee_key("Michael Oliver, England") == props.referee_key("M Oliver") == "m oliver"
    assert props.referee_key(None) == "" and props.referee_key("Çakır") == "cakir"


def test_walk_forward_has_totals():
    wf = props.walk_forward(_league(), date(2025, 5, 1))
    assert len(wf) and {"ROHY_9.5:over", "KARTY_total", "ROHY_mean"} <= set(wf.columns)


def test_settle_and_labels_for_counts():
    df = pd.DataFrame([{"date": date(2026, 11, 20), "home": "Arsenal", "away": "Chelsea", "home_goals": 2, "away_goals": 0,
                        "home_corners": 7, "away_corners": 4, "home_cards": 2, "away_cards": 3}])
    assert settlement.settle_bet(df, "Arsenal - Chelsea", date(2026, 11, 20), "ROHY_10.5", "over") == ("2:0, 11 rohů", "won")
    assert settlement.settle_bet(df, "Arsenal - Chelsea", date(2026, 11, 20), "KARTY_4.5", "pod")[1] == "lost"
    assert settlement.settle_bet(df, "Arsenal - Chelsea", date(2026, 11, 20), "1X2", "1") == ("2:0", "won")
    no_counts = df.drop(columns=["home_corners", "away_corners"])
    assert settlement.settle_bet(no_counts, "Arsenal - Chelsea", date(2026, 11, 20), "ROHY_10.5", "over") is None
    assert analysis.label("KARTY_4.5", "under", "A", "B") == "Karty: míň než 4.5"
    assert paper.selections("ROHY_9.5") == ("over", "under")
    assert any(m == "ROHY_9.5" for m, _, _ in czechodds.FORM_MARKETS)
