import math

import pytest

from tipsbot import devig
from tipsbot.config import LEAGUES, SETTINGS, Tier

ODDS_1X2 = [2.10, 3.40, 3.60]
ODDS_LONGSHOT = [1.25, 6.00, 12.00]
ODDS_2WAY = [1.80, 2.05]


@pytest.mark.parametrize(
    "fn", [devig.devig_multiplicative, devig.devig_power, devig.devig_shin]
)
@pytest.mark.parametrize("odds", [ODDS_1X2, ODDS_LONGSHOT, ODDS_2WAY])
def test_methods_sum_to_one_and_keep_order(fn, odds):
    p = fn(odds)
    assert sum(p) == pytest.approx(1.0, abs=1e-9)
    assert all(0 < x < 1 for x in p)
    # nizsi kurz = vyssi pravdepodobnost
    assert sorted(p, reverse=True) == [x for _, x in sorted(zip(odds, p))]


def test_fair_probs_below_implied():
    for fn in (devig.devig_power, devig.devig_shin):
        p = fn(ODDS_1X2)
        assert all(a < b for a, b in zip(p, devig.implied_probs(ODDS_1X2)))


def test_shin_takes_more_margin_from_longshots_than_multiplicative():
    mult = devig.devig_multiplicative(ODDS_LONGSHOT)
    shin = devig.devig_shin(ODDS_LONGSHOT)
    assert shin[2] < mult[2]  # outsider
    assert shin[0] > mult[0]  # favorit


def test_shin_two_way_equals_additive():
    q = devig.implied_probs(ODDS_2WAY)
    margin = sum(q) - 1
    additive = [x - margin / 2 for x in q]
    assert devig.devig_shin(ODDS_2WAY) == pytest.approx(additive, abs=1e-9)


def test_shin_z_in_range():
    z = devig.shin_z(ODDS_1X2)
    assert 0 < z < 0.2


def test_no_margin_market_is_unchanged():
    fair = [2.0, 4.0, 4.0]
    assert devig.devig_shin(fair) == pytest.approx([0.5, 0.25, 0.25])
    assert devig.devig_power(fair) == pytest.approx([0.5, 0.25, 0.25])


def test_invalid_odds_raise():
    with pytest.raises(devig.DevigError):
        devig.fair_line([1.0, 2.0])
    with pytest.raises(devig.DevigError):
        devig.fair_line([2.0])


def test_fair_line_longshot_check_is_conservative():
    line = devig.fair_line(ODDS_LONGSHOT)
    shin = devig.devig_shin(ODDS_LONGSHOT)
    assert sum(line.probs) == pytest.approx(1.0)
    assert line.probs[2] <= shin[2] + 1e-12
    assert line.method in ("shin", "shin+power")


def test_consensus_reweights_missing_sources():
    full = devig.consensus_line(
        {"pinnacle": ODDS_1X2, "betfair_exchange": [2.14, 3.5, 3.7], "sbobet": [2.08, 3.35, 3.55]}
    )
    assert full.weights == pytest.approx((0.5, 0.3, 0.2))
    assert sum(full.probs) == pytest.approx(1.0)

    partial = devig.consensus_line({"pinnacle": ODDS_1X2, "sbobet": [2.08, 3.35, 3.55]})
    assert partial.sources == ("pinnacle", "sbobet")
    assert partial.weights == pytest.approx((0.5 / 0.7, 0.2 / 0.7))

    only = devig.consensus_line({"pinnacle": ODDS_1X2})
    assert only.probs == pytest.approx(devig.fair_line(ODDS_1X2).probs)


def test_consensus_skips_bad_source_and_fails_without_any():
    c = devig.consensus_line({"pinnacle": ODDS_1X2, "sbobet": [0.9, 3.0, 3.0]})
    assert c.sources == ("pinnacle",)
    with pytest.raises(devig.DevigError):
        devig.consensus_line({"unknown_book": ODDS_1X2})


def test_exchange_commission_lowers_odds():
    assert devig.net_exchange_odds(2.0, 0.05) == pytest.approx(1.95)


def test_ev_and_min_odds():
    assert devig.expected_value(0.5, 2.10) == pytest.approx(0.05)
    m = devig.min_acceptable_odds(0.5, 0.025)
    assert m == pytest.approx(2.05)
    assert devig.expected_value(0.5, m) >= 0.025 - 1e-12


def test_blend_is_between_inputs():
    b = devig.blend_probability(0.60, 0.50)
    assert 0.50 < b < 0.60
    assert devig.blend_probability(0.55, 0.55) == pytest.approx(0.55)
    assert devig.blend_probability(0.60, 0.50, model_weight=1.0) == pytest.approx(0.60)


def test_arbitrage_yield():
    assert devig.arbitrage_yield([2.10, 2.10]) == pytest.approx(0.05)
    assert devig.arbitrage_yield(ODDS_2WAY) < 0


def test_assess_value_accepts_and_rejects():
    thr = SETTINGS.value.ev_threshold(Tier.MAJOR)
    ok = devig.assess_value(p_model=0.52, p_market=0.50, offered_odds=2.12, ev_threshold=thr)
    assert ok.is_value and not ok.needs_review
    assert ok.min_odds <= 2.12
    assert devig.expected_value(ok.prob, ok.min_odds) >= thr - 1e-9

    low_ev = devig.assess_value(0.50, 0.50, 2.00, thr)
    assert not low_ev.is_value and "EV pod prahem" in low_ev.reasons

    out_of_band = devig.assess_value(0.20, 0.19, 6.00, thr)
    assert "kurz mimo pasmo" in out_of_band.reasons

    gap = devig.assess_value(0.65, 0.50, 2.10, thr)
    assert "model a trh se prilis rozchazeji" in gap.reasons

    no_market_edge = devig.assess_value(0.56, 0.50, 1.98, thr)
    assert "bez hodnoty vuci ferove linii" in no_market_edge.reasons


def test_config_thresholds():
    v = SETTINGS.value
    assert v.ev_threshold(Tier.MAJOR) == pytest.approx(0.025)
    assert v.ev_threshold(Tier.MINOR) == pytest.approx(0.040)
    assert v.ev_threshold(Tier.MAJOR, early=True) == pytest.approx(0.05)
    assert v.ev_threshold(Tier.MINOR, early=True) == pytest.approx(0.08)
    assert LEAGUES["CZE1"].tier is Tier.MINOR and LEAGUES["NHL"].tier is Tier.MAJOR
    assert sum(SETTINGS.bookmakers.sharp_weights.values()) == pytest.approx(1.0)
    assert math.isclose(math.log(2) / SETTINGS.football.xi, 182.4, abs_tol=0.5)
