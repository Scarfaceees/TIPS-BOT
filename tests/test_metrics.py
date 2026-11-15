import pytest

from tipsbot.metrics import track_record


def test_drawdown_streak_yield():
    r = track_record([100, -50, -50, -30, 80, -10], [50] * 6)
    assert r["profit"] == 40 and r["max_drawdown"] == 130 and r["longest_losing"] == 3
    assert r["current_losing"] == 1 and r["yield_pct"] == pytest.approx(100 * 40 / 300)


def test_empty():
    r = track_record([], [])
    assert r["n"] == 0 and r["yield_pct"] is None and r["max_drawdown"] == 0
