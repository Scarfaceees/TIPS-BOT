from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tipsbot import backtest, secondopinion


def _preds(n=200, seed=1):
    rng = np.random.default_rng(seed)
    hg, ag = rng.poisson(1.6, n), rng.poisson(1.1, n)
    return pd.DataFrame({
        "home_goals": hg, "away_goals": ag,
        "p1": 0.45, "px": 0.27, "p2": 0.28, "p_over": 0.52,
        "q1": 0.47, "qx": 0.26, "q2": 0.27, "q_over": 0.50,
        "sharp_1": 2.10, "sharp_x": 3.50, "sharp_2": 3.60, "sharp_o25": 1.90, "sharp_u25": 1.95,
    })


def test_compare_lists_all_sources_on_same_matches():
    rows = backtest.compare(_preds())
    by_market = {m: [r["source"] for r in rows if r["market"] == m] for m in ("1X2", "OU_2.5")}
    expected = [backtest.NO_MODEL, backtest.OURS, backtest.SECOND, backtest.BOTH, backtest.MARKET, backtest.BLEND]
    assert by_market == {"1X2": expected, "OU_2.5": expected}
    assert {r["n"] for r in rows} == {200}
    assert all(0 < r["logloss"] < 2 and 0 < r["brier"] < 1 for r in rows)


def test_compare_without_odds_or_second_model():
    preds = _preds().drop(columns=["q1", "qx", "q2", "q_over", "sharp_1", "sharp_x", "sharp_2", "sharp_o25", "sharp_u25"])
    assert {r["source"] for r in backtest.compare(preds)} == {backtest.NO_MODEL, backtest.OURS}


def test_perfect_forecast_beats_frequencies():
    preds = _preds()
    over = (preds["home_goals"] + preds["away_goals"] > 2.5).astype(float)
    preds["p_over"] = over * 0.9 + 0.05
    rows = {r["source"]: r["logloss"] for r in backtest.compare(preds) if r["market"] == "OU_2.5"}
    assert rows[backtest.OURS] < rows[backtest.NO_MODEL]


def test_nhl_compare_skips_shootouts_for_winner():
    preds = pd.DataFrame({"home_goals": [3, 2, 4, 1] * 10, "away_goals": [2, 2, 1, 3] * 10, "p_home": 0.55,
                          "p_over55": 0.5, "p_over65": 0.3, "exp_total": 6.0})
    rows = backtest.nhl_compare(preds)
    n = {r["market"]: r["n"] for r in rows}
    assert n == {"MONEYLINE": 30, "OU_5.5": 40, "OU_6.5": 40}


def test_group_marks_best_source():
    g = backtest._group("x", "", backtest.compare(_preds()))
    best = [r for r in g["rows"] if r.get("best")]
    assert len(best) == 2 and all(r["source"] != backtest.NO_MODEL for r in best)


def test_second_opinion_probabilities():
    pytest.importorskip("penaltyblog")
    rng = np.random.default_rng(3)
    teams = [f"T{i}" for i in range(8)]
    start = date(2025, 8, 1)
    rows = []
    for k in range(240):
        h, a = rng.choice(len(teams), 2, replace=False)
        rows.append({"date": start + timedelta(days=k), "home": teams[h], "away": teams[a],
                     "home_goals": int(rng.poisson(1.7 if h < 3 else 1.2)), "away_goals": int(rng.poisson(1.0))})
    df = pd.DataFrame(rows)
    model = secondopinion.fit(df, start + timedelta(days=300), 0.0038, 2)
    p = secondopinion.probabilities(model, "T0", "T7")
    assert p is not None
    assert abs(p["1X2:1"] + p["1X2:X"] + p["1X2:2"] - 1) < 1e-6
    assert abs(p["OU_2.5:over"] + p["OU_2.5:under"] - 1) < 1e-9
    assert secondopinion.probabilities(model, "T0", "Neznamy") is None
    assert secondopinion.probabilities(None, "T0", "T7") is None


def test_chances_blend_into_training_goals():
    from tipsbot.models import football

    df = pd.DataFrame({"home_goals": [2, 0, 1], "away_goals": [0, 0, 3], "home_sot": [4.0, 6.0, np.nan],
                       "away_sot": [2.0, 0.0, np.nan], "home_xg": [np.nan, 1.5, np.nan], "away_xg": [np.nan, 0.5, np.nan]})
    out = football._blend_chances(df.copy(), 0.5)
    # 2 goly z 12 strel na branku -> 1/6 golu na strelu; druhy zapas ma xG, treti nema nic
    assert out["home_goals"].round(4).tolist() == [round(0.5 * 2 + 0.5 * 4 / 6, 4), 0.75, 1.0]
    assert out["away_goals"].round(4).tolist() == [round(0.5 * 2 / 6, 4), 0.25, 3.0]
    assert football._blend_chances(df.copy(), 0.0)["home_goals"].tolist() == [2, 0, 1]
    plain = df[["home_goals", "away_goals"]]
    assert football._blend_chances(plain.copy(), 0.5).equals(plain)


def test_compare_scores_all_goal_markets_and_panel_summary():
    import numpy as np

    from tipsbot import backtest, web

    rng = np.random.default_rng(3)
    n = 400
    hg, ag = rng.poisson(1.5, n), rng.poisson(1.1, n)
    preds = pd.DataFrame({"home_goals": hg, "away_goals": ag, "p1": 0.45, "px": 0.27, "p2": 0.28, "p_over": 0.5})
    for k in backtest.EVAL_KEYS:  # model, ktery skutecnost zna trochu predem -> musi porazit cetnosti
        truth = backtest._outcome(k, hg, ag) == 0
        preds[f"p:{k}"] = np.where(truth, 0.7, 0.3)
    rows = backtest.compare(preds)
    markets = {r["market"] for r in rows}
    assert {"OU_1.5", "OU_3.5", "OU_4.5", "BTTS", "TTH_0.5", "TTA_1.5"} <= markets
    btts = {r["source"]: r["logloss"] for r in rows if r["market"] == "BTTS"}
    assert btts[backtest.OURS] < btts[backtest.NO_MODEL]
    view = web._bt_view({"groups": [backtest._group("Bundesliga", web.LEAGUE_NOTE, rows)]})
    s = {m["market"]: m for m in view["summary"]}
    assert s["BTTS"]["skill"] > 0.05 and s["BTTS"]["vs_market"] is None and s["BTTS"]["verdict"] == "model pomáhá"
    assert view["summary"][0]["market"] == "1X2" and view["summary_name"] == "1 lig dohromady"
