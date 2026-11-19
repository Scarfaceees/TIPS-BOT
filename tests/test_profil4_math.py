import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from p4_fakes import NOW, season_items
from tipsbot.models import football
from tipsbot.profil4 import correlation, mathcore, montecarlo, surebet
from tipsbot.profil4 import models as M
from tipsbot.profil4.config import P4
from tipsbot.profil4.odds import OddsQuote
from tipsbot.profil4.provider import parse_fixture
from tipsbot.profil4.stress import freshness_factor, robustness, stress_test


def single_score(h, a):
    m = np.zeros((11, 11))
    m[h, a] = 1.0
    return m


# --- de-vig, ferovy kurz, EV ------------------------------------------------

def test_devig_multiplicative_formula():
    raw, fair = mathcore.devig([2.0, 3.5, 4.0])
    assert raw == pytest.approx([0.5, 1 / 3.5, 0.25])
    s = sum(raw)
    assert fair == pytest.approx([r / s for r in raw]) and sum(fair) == pytest.approx(1.0)


def test_devig_other_methods_sum_to_one():
    for method in ("shin", "power"):
        _, fair = mathcore.devig([1.8, 3.6, 4.8], method)
        assert sum(fair) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        mathcore.devig([2.0, 2.0], "xyz")


def test_fair_odds_and_ev_example_from_spec():
    assert mathcore.fair_odds(0.60) == pytest.approx(1.6667, abs=1e-4)
    assert mathcore.expected_value(0.60, 1.85) == pytest.approx(0.11)
    assert math.isinf(mathcore.fair_odds(0.0))


def test_min_odds_for_ev():
    assert mathcore.min_odds_for_ev(0.5, 0.0, 0.03) == 2.06


# --- settlement a ctvrtinove linie ------------------------------------------

@pytest.mark.parametrize("market,sel,score,expected", [
    ("1X2", "1", (2, 1), "won"), ("1X2", "X", (2, 1), "lost"),
    ("OU_2.5", "over", (2, 1), "won"), ("OU_3.0", "over", (2, 1), "push"),
    ("OU_2.25", "over", (1, 1), "half_lost"), ("OU_2.25", "over", (2, 1), "won"),
    ("OU_2.75", "over", (2, 1), "half_won"), ("OU_2.75", "under", (2, 1), "half_lost"),
    ("AH_-0.25", "home", (1, 1), "half_lost"), ("AH_-0.25", "away", (1, 1), "half_won"),
    ("AH_-0.75", "home", (2, 1), "half_won"), ("AH_-0.75", "home", (3, 1), "won"),
    ("AH_-1.0", "home", (2, 1), "push"), ("AH_0.0", "away", (0, 0), "push"),
    ("AH_0.25", "home", (0, 0), "half_won"), ("DNB", "home", (1, 1), "push"),
    ("BTTS", "yes", (1, 1), "won"), ("TTA_0.5", "under", (3, 0), "won"),
])
def test_settlement(market, sel, score, expected):
    assert mathcore.settle(market, sel, *score) == expected


def test_settlement_invalid():
    with pytest.raises(ValueError):
        mathcore.settle("1X2", "1", -1, 0)
    with pytest.raises(ValueError):
        mathcore.settle("NESMYSL", "x", 1, 0)
    with pytest.raises(ValueError):
        mathcore.settle("1X2", "1", 12, 0)


def test_quarter_line_ev_exact_by_hand():
    # AH -0.75 domaci, kurz 2.00: vyhra o 2+ = plna vyhra, o 1 = pulvyhra, jinak prohra
    m = np.zeros((11, 11))
    m[2, 0], m[1, 0], m[0, 0] = 0.3, 0.4, 0.3
    ev = mathcore.evaluate("AH_-0.75", "home", m, 2.0)
    assert ev.p_win == pytest.approx(0.3) and ev.p_half_win == pytest.approx(0.4) and ev.p_loss == pytest.approx(0.3)
    # 0.3*1 + 0.4*0.5 - 0.3 = 0.2
    assert ev.ev == pytest.approx(0.3 * 1.0 + 0.4 * 0.5 - 0.3)
    # ferovy kurz: a = 0.3 + 0.2 = 0.5, b = 0.2  => (1 - 0.2) / 0.5 = 1.6
    assert ev.fair_odds == pytest.approx(1.6)
    assert mathcore.evaluate("AH_-0.75", "home", m, ev.fair_odds).ev == pytest.approx(0.0, abs=1e-12)


def test_push_market_fair_odds_not_one_over_p():
    m = np.zeros((11, 11))
    m[1, 0], m[0, 0], m[0, 1] = 0.4, 0.3, 0.3  # DNB: vyhra 0.4, vraceni 0.3
    ev = mathcore.evaluate("DNB", "home", m)
    assert ev.fair_odds == pytest.approx(0.7 / 0.4)  # ne 1/0.4
    assert ev.p_push == pytest.approx(0.3)


def test_evaluate_matches_profile2_returns_on_random_matrix():
    from tipsbot import profile2

    m = football.score_matrix(1.6, 1.1, -0.05, 10)
    for mk, sel in [("OU_2.25", "under"), ("AH_-1.25", "home"), ("AH_0.75", "away"), ("TTH_1.0", "over")]:
        e = mathcore.evaluate(mk, sel, m, 1.9)
        assert e.ev == pytest.approx(float((m * profile2.returns(mk, sel, 1.9)).sum() - 1.0))
        total = e.p_win + e.p_half_win + e.p_push + e.p_half_loss + e.p_loss
        assert total == pytest.approx(1.0)


# --- Poisson / model A a B ----------------------------------------------------

def test_poisson_matrix_and_market_lambda_fit_roundtrip():
    m = football.score_matrix(1.7, 1.0, 0.0, 10)
    assert m.sum() == pytest.approx(1.0)
    cons = {"1X2:1": float(np.tril(m, -1).sum()), "1X2:X": float(np.trace(m)), "1X2:2": float(np.triu(m, 1).sum()),
            "OU_2.5:over": mathcore.evaluate("OU_2.5", "over", m).p_win,
            "OU_2.5:under": mathcore.evaluate("OU_2.5", "under", m).p_win}
    lh, la = M.fit_market_lambdas(cons)
    assert lh == pytest.approx(1.7, abs=0.02) and la == pytest.approx(1.0, abs=0.02)


def _quotes(fid, book, mk, sels_odds, src="api", age_min=10):
    return [OddsQuote(fid, "A - B", book, mk, s, o, NOW, src, NOW + timedelta(hours=5), NOW - timedelta(minutes=age_min))
            for s, o in sels_odds]


def test_market_table_prefers_sharp_and_keeps_raw():
    q = (_quotes(1, "pinnacle", "1X2", [("1", 2.0), ("X", 3.5), ("2", 4.0)])
         + _quotes(1, "bet365", "1X2", [("1", 1.8), ("X", 3.3), ("2", 4.5)]))
    t = M.market_table(q)
    assert t["sources"]["1X2"] == ["pinnacle"]
    assert t["books"]["1X2"]["bet365"]["odds"] == [1.8, 3.3, 4.5]
    assert t["consensus"]["1X2:1"] == pytest.approx(0.5 / (0.5 + 1 / 3.5 + 0.25))


def test_model_b_unavailable_without_1x2():
    q = _quotes(1, "pinnacle", "OU_2.5", [("over", 1.9), ("under", 1.95)])
    assert not M.model_b(q, P4).available


def _history(teams=None):
    teams = teams or ["T%d" % i for i in range(10)]
    start = datetime(2026, 8, 1, 15, tzinfo=timezone.utc)
    return [parse_fixture(x) for x in season_items(teams, start)], teams


def test_model_a_from_api_results_and_no_leakage():
    hist, teams = _history()
    fx = parse_fixture({"fixture": {"id": 9, "date": "2027-01-10T15:00:00+00:00", "status": {"short": "NS"}},
                        "league": {"id": 39, "season": 2026}, "teams": {"home": {"id": 1, "name": teams[0]},
                                                                         "away": {"id": 10, "name": teams[-1]}}})
    a = M.model_a(fx, hist, P4)
    assert a.available and a.lam_home > a.lam_away and a.matrix.sum() == pytest.approx(1.0)
    # zapasy po vykopu se nesmi pouzit: predikce k datu uprostred sezony ma mene dat
    mid = parse_fixture({"fixture": {"id": 9, "date": hist[100].kickoff.isoformat(), "status": {"short": "NS"}},
                         "league": {"id": 39, "season": 2026}, "teams": {"home": {"id": 1, "name": teams[0]},
                                                                          "away": {"id": 10, "name": teams[-1]}}})
    assert M.model_a(mid, hist, P4).n_matches < a.n_matches


def test_model_a_unavailable_with_little_data():
    hist, teams = _history()
    fx = hist[-1]
    a = M.model_a(fx, hist[:20], P4)
    assert not a.available and "málo" in a.note


def test_model_c_mixture_and_degraded():
    a = M.ModelResult("A", True, football.score_matrix(1.5, 1.0, 0, 10), 1.5, 1.0)
    b = M.ModelResult("B", True, football.score_matrix(1.3, 1.2, 0, 10), 1.3, 1.2)
    c = M.model_c(a, b, 0.3, "test")
    assert np.allclose(c.matrix, 0.3 * a.matrix + 0.7 * b.matrix) and not c.degraded
    only = M.model_c(a, M.unavailable("B", "x"), 0.3, "test")
    assert only.degraded and np.allclose(only.matrix, a.matrix)
    assert not M.model_c(M.unavailable("A", "x"), M.unavailable("B", "y"), 0.5, "").available


def test_model_weight_from_backtest_or_config():
    assert M.model_weight(P4, "ENG1")[0] == pytest.approx(0.10)
    assert M.model_weight(P4, "")[0] == pytest.approx(0.5)


def test_uncertainty_widens_tails():
    base = football.score_matrix(1.4, 1.1, 0.0, 10)
    unc = M.uncertain_matrix(1.4, 1.1, 0.0, 9.0)
    assert unc.sum() == pytest.approx(1.0)
    p5 = lambda m: mathcore.evaluate("OU_4.5", "over", m).p_win  # noqa: E731
    assert p5(unc) > p5(base)
    assert M.gamma_k(P4, 1.0, 400) > M.gamma_k(P4, 0.4, 50)


def _modelset():
    a = M.ModelResult("A", True, football.score_matrix(1.8, 0.9, -0.05, 10), 1.8, 0.9, -0.05, n_matches=300)
    b = M.ModelResult("B", True, football.score_matrix(1.6, 1.0, 0.0, 10), 1.6, 1.0)
    c = M.model_c(a, b, 0.5, "")
    k = 16.0
    a_unc = M.uncertain_matrix(1.8, 0.9, -0.05, k)
    c_unc = 0.5 * a_unc + 0.5 * M.uncertain_matrix(1.6, 1.0, 0.0, k)
    return M.ModelSet(a, b, c, k, a_unc, c_unc, 0.5)


def test_model_d_is_not_above_c():
    ms = _modelset()
    for mk, sel in [("1X2", "1"), ("OU_2.5", "over"), ("AH_-0.75", "home"), ("BTTS", "no")]:
        d, _ = ms.evaluate_d(mk, sel, 2.0)
        assert d.ev <= mathcore.evaluate(mk, sel, ms.c.matrix, 2.0).ev + 1e-12


# --- Monte Carlo ------------------------------------------------------------

def test_monte_carlo_reproducible_and_close_to_exact():
    m = football.score_matrix(1.5, 1.1, -0.05, 10)
    sels = [("1X2", "1", 2.1), ("OU_2.5", "over", 1.9), ("AH_-0.25", "home", 1.95)]
    r1 = montecarlo.simulate(m, sels, n=50_000, seed=7)
    r2 = montecarlo.simulate(m, sels, n=50_000, seed=7)
    assert r1.markets["1X2:1"].prob == r2.markets["1X2:1"].prob
    exact = mathcore.evaluate("1X2", "1", m).p_win
    sm = r1.markets["1X2:1"]
    assert abs(sm.prob - exact) < 4 * sm.se and sm.ci_low < exact < sm.ci_high
    ev_exact = mathcore.evaluate("AH_-0.25", "home", m, 1.95).ev
    assert abs(r1.markets["AH_-0.25:home"].ev - ev_exact) < 4 * r1.markets["AH_-0.25:home"].ev_se
    assert sum(r1.one_x_two) == pytest.approx(1.0)
    assert r1.n == 50_000 and len(r1.top_scores) == 10
    with pytest.raises(ValueError):
        montecarlo.simulate(m, sels, n=0)


def test_monte_carlo_error_shrinks_with_n():
    m = football.score_matrix(1.5, 1.1, 0, 10)
    small = montecarlo.simulate(m, [("1X2", "1", None)], n=5_000, seed=1).markets["1X2:1"].se
    big = montecarlo.simulate(m, [("1X2", "1", None)], n=100_000, seed=1).markets["1X2:1"].se
    assert big < small / 3


# --- stress a robustnost ------------------------------------------------------

def test_stress_and_robustness_components():
    ms = _modelset()
    st = stress_test(ms, "1X2", "1", 2.10, P4, lineups_confirmed=False)
    names = [n for n, _ in st.scenarios]
    assert any(n.startswith("optimistický") for n in names) and any(n.startswith("konzervativní") for n in names)
    assert any(n.startswith("tržní") for n in names) and any("sestava" for n in names)
    assert 0 <= st.pass_rate <= 1 and st.worst_ev <= st.base_ev
    rb = robustness(ms, "1X2", "1", st, 0.9, 1.0, "pending", P4)
    assert 0 <= rb.score <= 100 and rb.score == pytest.approx(sum(rb.parts.values()), abs=0.2)
    bad = robustness(ms, "1X2", "1", st, 0.2, 0.0, "missing_near_kickoff", P4)
    assert bad.score < rb.score


def test_freshness_factor():
    assert freshness_factor(10, 120) == 1.0
    assert freshness_factor(500, 120) == 0.0
    assert 0 < freshness_factor(120, 120) < 1


# --- korelace -----------------------------------------------------------------

def test_correlation_home_win_and_over_not_independent():
    m = football.score_matrix(1.9, 0.9, -0.05, 10)
    p = correlation.pair(m, ("1X2", "1"), ("OU_2.5", "over"))
    assert p.phi > 0.1 and p.p_both > p.p_independent
    combo = correlation.same_match_combo(m, [("1X2", "1", 1.7), ("OU_2.5", "over", 1.9)])
    assert combo["p_all_win"] == pytest.approx(p.p_both)
    assert combo["ev"] != pytest.approx(combo["ev_if_independent_wrong"])
    btts = correlation.pair(m, ("BTTS", "yes"), ("OU_2.5", "over"))
    assert btts.phi > 0.3
    with pytest.raises(ValueError):
        correlation.same_match_combo(m, [("1X2", "1", 1.7)])


def test_combo_with_push_leg():
    m = single_score(1, 1)  # remiza 1:1 => DNB vraci, over 1.5 vyhrava
    c = correlation.same_match_combo(m, [("DNB", "home", 2.0), ("OU_1.5", "over", 1.5)])
    assert c["ev"] == pytest.approx(1.5 - 1.0)


# --- surebet -------------------------------------------------------------------

def test_surebet_requires_verification():
    q = (_quotes(1, "pinnacle", "OU_2.5", [("over", 2.10)]) + _quotes(1, "tipsport", "OU_2.5", [("under", 2.05)], "manual")
         + _quotes(1, "bet365", "1X2", [("1", 2.0), ("X", 3.0), ("2", 3.0)]))
    res = surebet.scan(q, NOW, P4.odds, 1000)
    assert len(res) == 1 and res[0].market == "OU_2.5"
    sb = res[0]
    assert sb.status != "SUREBET" and any("limit" in p for p in sb.problems)
    assert sum(sb.stakes) == pytest.approx(1000) and sb.margin > 0
    assert sb.stakes[0] * 2.10 == pytest.approx(sb.stakes[1] * 2.05)
    ok = surebet.scan(q, NOW, P4.odds, 1000, limits={"pinnacle": (10, 5000), "tipsport": (10, 5000)})
    assert ok[0].status == "SUREBET"


def test_surebet_stale_quote_blocks():
    q = (_quotes(1, "pinnacle", "OU_2.5", [("over", 2.10)], age_min=600)
         + _quotes(1, "tipsport", "OU_2.5", [("under", 2.05)], "manual"))
    res = surebet.scan(q, NOW, P4.odds, 100, limits={"pinnacle": (1, 1e6), "tipsport": (1, 1e6)})
    assert res[0].status != "SUREBET"


def test_surebet_ignores_whole_lines():
    q = _quotes(1, "a", "OU_3.0", [("over", 2.2)]) + _quotes(1, "b", "OU_3.0", [("under", 2.2)])
    assert surebet.scan(q, NOW, P4.odds) == []
