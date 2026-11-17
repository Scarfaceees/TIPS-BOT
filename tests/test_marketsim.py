from datetime import datetime, timedelta

import pytest

from tipsbot import czechodds, marketsim, paper
from tipsbot.models.football import score_matrix
from tipsbot.oddsapi import Event
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 17, 0, 0)

# Kurzy Tipsportu a Fortuny na Wales - Portugalsko (Liga narodu, 17. 11. 2026), ze ktereho profil vznikl.
WP = """tipsport | Wales - Portugalsko | 1X2 | 6.90 5.00 1.43
fortuna | Wales - Portugalsko | 1X2 | 6.80 5.20 1.43
tipsport | Wales - Portugalsko | OU_1.5 | 1.16 5.35
tipsport | Wales - Portugalsko | OU_2.0 | 1.22 4.34
tipsport | Wales - Portugalsko | OU_2.5 | 1.52 2.55
tipsport | Wales - Portugalsko | OU_3.5 | 2.34 1.62
tipsport | Wales - Portugalsko | OU_4.0 | 3.30 1.33
tipsport | Wales - Portugalsko | OU_4.5 | 3.95 1.25
tipsport | Wales - Portugalsko | OU_5.5 | 7.07 1.09
tipsport | Wales - Portugalsko | BTTS | 1.65 2.11
tipsport | Wales - Portugalsko | TTH_0.5 | 1.50 2.39
tipsport | Wales - Portugalsko | TTA_0.5 | 1.06 7.51
tipsport | Wales - Portugalsko | TTA_1.5 | 1.38 2.78"""


def _entries():
    entries, errors = czechodds.parse_text(WP, NOW)
    assert not errors
    return entries


def test_fit_matches_market_expectation():
    lam, mu, err, n = marketsim.fit_lambdas(_entries())
    assert lam == pytest.approx(0.95, abs=0.1) and mu == pytest.approx(2.3, abs=0.2)
    assert err < 0.03 and n >= 10  # cele linie (2.0, 4.0) se do fitu neberou


def test_push_lines_count_returned_stake():
    m = score_matrix(0.95, 2.3, marketsim.RHO)
    r = marketsim.evaluate("OU_2.0", "over", 1.22, m)
    assert r["push"] == pytest.approx(float(sum(m[i, 2 - i] for i in range(3))))
    assert r["ev"] == pytest.approx(r["win"] * 0.22 - r["loss"])
    q = marketsim.evaluate("OU_2.25", "over", 1.50, m)  # ctvrtinova linie: pri 2 golech pul prohra
    assert q["ev"] < r["ev"] * 0 + 1 and q["loss"] > r["loss"]


def test_wales_portugal_pick_is_robust_low_odds_goal_bet():
    p = marketsim.best_pick("x:wp", {"lambda_home": 0.92, "lambda_away": 1.98}, _entries())
    assert (p.market, p.selection, p.odds) == ("OU_4.0", "under", 1.33)
    assert p.loss <= 0.25 and p.worst_ev >= -0.15
    assert "Simulace 1000×" in p.detail and "0:2" in p.detail
    assert p.flags == ["bez ověřené výhody – sázka zhruba za férovou cenu"]


def test_better_price_elsewhere_becomes_value():
    extra, _ = czechodds.parse_text("betano | Wales - Portugalsko | OU_4.0 | 3.10 1.45", NOW)
    p = marketsim.best_pick("x:wp", None, _entries() + extra)
    assert (p.book, p.odds) == ("betano", 1.45) and p.score > 0 and not p.flags


def test_simulation_is_reproducible():
    a = marketsim.simulate(0.95, 2.3, 1000, 7)
    assert a == marketsim.simulate(0.95, 2.3, 1000, 7)
    assert abs(sum(s for _, s in a) - sum(s for _, s in marketsim.simulate(0.95, 2.3, 1000, 8))) < 0.15


def test_profile3_tracks_without_bot_model():
    led = Ledger(make_session_factory("sqlite://")())
    ev = Event("x:wp", "UNL", "Wales", "Portugal", NOW + timedelta(hours=20))
    paper.track(led, "UNL", [(ev, None, _entries())], NOW)
    (p,) = paper.load(led)
    assert p.profile == "profil3" and p.detail and (p.market, p.selection) == ("OU_2.0", "over")  # bez modelu bota vede trh
    assert p.ev == pytest.approx(marketsim.best_pick("x:wp", None, _entries()).ev, abs=1e-3)
