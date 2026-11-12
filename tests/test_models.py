from datetime import date

import numpy as np
import pytest

from tipsbot.config import SETTINGS
from tipsbot.models import football, nhl


# ---------- fotbal ----------
def test_score_matrix_sums_to_one_and_tau_shifts_low_scores():
    base = football.score_matrix(1.5, 1.1, 0.0)
    dc = football.score_matrix(1.5, 1.1, -0.1)
    assert base.sum() == pytest.approx(1.0)
    assert dc.sum() == pytest.approx(1.0)
    # zaporne rho pridava remizy 0:0 a 1:1, ubira 1:0 a 0:1
    assert dc[0, 0] > base[0, 0] and dc[1, 1] > base[1, 1]
    assert dc[1, 0] < base[1, 0] and dc[0, 1] < base[0, 1]


def test_market_probs_consistent():
    m = football.score_matrix(1.6, 1.0, -0.05)
    p1, px, p2 = football.probs_1x2(m)
    assert p1 + px + p2 == pytest.approx(1.0)
    assert p1 > p2
    assert 0 < football.prob_over(m, 2.5) < 1
    assert football.prob_over(m, 1.5) > football.prob_over(m, 2.5)


def test_asian_handicap_ev():
    m = football.score_matrix(1.6, 1.0, -0.05)
    p1, px, p2 = football.probs_1x2(m)
    # -0.5 je totez co vyhra domacich
    assert football.asian_handicap_ev(m, -0.5, 2.0, "home") == pytest.approx(p1 * 2.0 - 1.0)
    # 0.0 (draw no bet): remiza vraci vklad
    assert football.asian_handicap_ev(m, 0.0, 1.5, "home") == pytest.approx(p1 * 0.5 - p2)
    # ctvrtinova linie je prumer sousednich
    q = football.asian_handicap_ev(m, -0.25, 1.8, "home")
    half = (football.asian_handicap_ev(m, -0.5, 1.8, "home") + football.asian_handicap_ev(m, 0.0, 1.8, "home")) / 2
    assert q == pytest.approx(half)
    # ferovy kurz na obou stranach -> EV se zrcadli
    assert football.asian_handicap_ev(m, +0.5, 1 / (1 - p1), "away") == pytest.approx(0.0, abs=1e-9)
    assert football.equivalent_probability(0.05, 2.1) == pytest.approx(0.5)


def test_fit_recovers_structure(league):
    df, truth = league
    params = football.fit(df, date(2026, 1, 1))
    teams = truth["teams"]
    est_att = np.array([params.attack[t] for t in teams])
    true_att = np.array([truth["att"][t] for t in teams])
    est_def = np.array([params.defence[t] for t in teams])
    true_def = np.array([truth["def"][t] for t in teams])
    assert np.corrcoef(est_att, true_att)[0, 1] > 0.85
    assert np.corrcoef(est_def, true_def)[0, 1] > 0.6
    assert params.gamma == pytest.approx(truth["gamma"], abs=0.12)
    assert -0.3 <= params.rho <= 0.3
    # tymova domaci vyhoda je pritazena k ligove hodnote
    spread = max(params.home_adv.values()) - min(params.home_adv.values())
    assert spread < 0.5

    probs = football.market_probabilities(params, teams[0], teams[1])
    assert probs["1X2:1"] + probs["1X2:X"] + probs["1X2:2"] == pytest.approx(1.0)
    with pytest.raises(football.ModelError):
        params.expected_goals("neexistuje", teams[0])


def test_fit_uses_only_past_and_drops_old(league):
    df, _ = league
    first = df["date"].min()
    with pytest.raises(football.ModelError):
        football.fit(df, first)  # zadne zapasy pred timto dnem
    with pytest.raises(football.ModelError):
        football.fit(df, date(2032, 1, 1))  # vse starsi nez 2 sezony


def test_cold_start_blends_towards_prior(league):
    df, truth = league
    days = sorted(df["date"].unique())
    season_start = days[len(days) - 4]  # v "nove sezone" ma kazdy tym jen par zapasu
    as_of = date(2026, 1, 1)
    full = football.fit(df, as_of)
    blended = football.fit(df, as_of, season_start=season_start)
    prior = football.fit(df[df["date"] < season_start], as_of)
    t = truth["teams"][0]
    w = SETTINGS.football.cold_start_prior_weight
    assert blended.attack[t] == pytest.approx(w * prior.attack[t] + (1 - w) * full.attack[t], abs=1e-9)


# ---------- NHL ----------
def _ctx(**kw):
    avg = nhl.TeamRating(xgf60=3.0, xga60=3.0, ff60=42.0, fa60=42.0)
    base = dict(home=avg, away=avg, league_xg60=3.0, league_ff60=42.0)
    base.update(kw)
    return nhl.GameContext(**base)


def test_expected_goals_home_ice_and_adjustments():
    lam_h, lam_a = nhl.expected_goals(_ctx())
    assert lam_h == pytest.approx(3.0 * SETTINGS.nhl.home_factor)
    assert lam_a == pytest.approx(3.0 * SETTINGS.nhl.away_factor)

    b2b_h, b2b_a = nhl.expected_goals(_ctx(away_b2b_travel=True))
    assert b2b_a == pytest.approx(lam_a * 0.95)
    assert b2b_h == pytest.approx(lam_h)

    elite = nhl.Goalie(gsax=20.0, shots=1500)
    g_h, g_a = nhl.expected_goals(_ctx(home_goalie=elite))
    assert g_a < lam_a and g_h == pytest.approx(lam_h)
    assert nhl.goalie_gsax60(nhl.Goalie()) == 0.0
    # maly vzorek se stahuje k nule
    assert nhl.goalie_gsax60(nhl.Goalie(2.0, 100)) < nhl.goalie_gsax60(nhl.Goalie(20.0, 1000))


def test_shot_volume_is_auxiliary_signal():
    shooter = nhl.TeamRating(xgf60=3.0, xga60=3.0, ff60=50.0, fa60=42.0)
    lam_h, _ = nhl.expected_goals(_ctx(home=shooter))
    base_h, _ = nhl.expected_goals(_ctx())
    assert base_h < lam_h < base_h * 50 / 42


def test_simulation_probabilities():
    rng = np.random.default_rng(7)
    out = nhl.simulate(3.3, 2.7, rng=rng)
    assert out["MONEYLINE:home"] + out["MONEYLINE:away"] == pytest.approx(1.0)
    assert out["REG:1"] + out["REG:X"] + out["REG:2"] == pytest.approx(1.0)
    assert out["MONEYLINE:home"] > 0.55
    assert out["MONEYLINE:home"] > out["REG:1"]
    assert 0.15 < out["REG:X"] < 0.30
    assert out["PUCKLINE_1.5:home-1.5"] + out["PUCKLINE_1.5:away+1.5"] == pytest.approx(1.0)
    assert out["PUCKLINE_1.5:home-1.5"] < out["MONEYLINE:home"]
    assert out["OU_5.5:over"] > out["OU_6.5:over"]
    assert out["OU_5.5:over"] + out["OU_5.5:under"] == pytest.approx(1.0)


def test_simulation_is_symmetric_for_equal_teams():
    cfg = SETTINGS.nhl.__class__(mc_iterations=40_000)
    out = nhl.simulate(3.0, 3.0, cfg=cfg, rng=np.random.default_rng(3))
    assert out["MONEYLINE:home"] == pytest.approx(0.5, abs=0.015)


def test_cold_start_rating():
    prior = nhl.TeamRating(3.4, 2.6, games=82)
    early = nhl.TeamRating(2.4, 3.6, games=3)
    mixed = nhl.cold_start_rating(prior, early)
    assert mixed.xgf60 == pytest.approx(0.6 * 3.4 + 0.4 * 2.4)
    late = nhl.TeamRating(2.4, 3.6, games=20)
    assert nhl.cold_start_rating(prior, late) is late


def test_calibration_fixes_overconfident_model_and_roundtrips(tmp_path):
    import numpy as np
    import pandas as pd

    from tipsbot import backtest, calibration
    from tipsbot.pipeline import apply_calibration

    rng = np.random.default_rng(3)
    n = 1500
    true = rng.dirichlet([4, 2.5, 3.5], n)
    res = np.array([rng.choice(3, p=p) for p in true])
    # prehnane sebevedomy model: pravdepodobnosti natlacene k extremum
    sharp = true ** 1.8
    sharp /= sharp.sum(axis=1, keepdims=True)
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n).date, "p1": sharp[:, 0], "px": sharp[:, 1], "p2": sharp[:, 2],
                       "home_goals": np.where(res == 0, 2, np.where(res == 1, 1, 0)), "away_goals": np.where(res == 2, 2, np.where(res == 1, 1, 0))})
    out = backtest.calibrate(df)
    assert out is not None
    scalers, before, after = out
    assert after < before and scalers["1"].a < 1.0
    path = tmp_path / "c" / "ENG1.json"
    calibration.save_scalers(path, scalers)
    loaded = calibration.load_scalers(path)
    probs = apply_calibration({"1X2:1": 0.6, "1X2:X": 0.25, "1X2:2": 0.15}, loaded)
    assert abs(probs["1X2:1"] + probs["1X2:X"] + probs["1X2:2"] - 1) < 1e-9 and abs(probs["DC:1X"] - (probs["1X2:1"] + probs["1X2:X"])) < 1e-9
    assert calibration.load_scalers(tmp_path / "missing.json") is None


def test_backtest_reports_market_benchmark():
    import numpy as np
    import pandas as pd

    from tipsbot import backtest

    rng = np.random.default_rng(5)
    n = 400
    true = rng.dirichlet([4, 2.5, 3.5], n)
    res = np.array([rng.choice(3, p=p) for p in true])
    odds = 1 / (true * 1.05)
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=n).date, "p1": true[:, 0], "px": true[:, 1], "p2": true[:, 2],
                       "home_goals": np.where(res == 0, 2, 0), "away_goals": np.where(res == 2, 2, 0),
                       "sharp_1": odds[:, 0], "sharp_x": odds[:, 1], "sharp_2": odds[:, 2]})
    df.loc[res == 1, ["home_goals", "away_goals"]] = 1
    rep = backtest.evaluate(df)
    assert rep.market_brier is not None and rep.blend_brier is not None and rep.baseline_brier > rep.brier
    assert "trh" in rep.summary()
