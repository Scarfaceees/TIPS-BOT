from datetime import date, datetime

import pandas as pd
import pytest

from tipsbot import pipeline, settlement
from tipsbot.tips import Candidate, Offer


@pytest.mark.parametrize("market,sel,hg,ag,expected", [
    ("1X2", "1", 2, 1, "won"), ("1X2", "X", 2, 1, "lost"), ("DC", "1X", 1, 1, "won"), ("DC", "12", 1, 1, "lost"),
    ("DNB", "home", 1, 1, "void"), ("DNB", "away", 0, 1, "won"),
    ("BTTS", "yes", 1, 1, "won"), ("BTTS", "no", 1, 1, "lost"),
    ("OU_2.5", "over", 2, 1, "won"), ("OU_2.5", "under", 2, 1, "lost"), ("OU_3", "over", 2, 1, "void"),
    ("OU_2.75", "over", 2, 1, "half_won"), ("OU_2.75", "under", 2, 1, "half_lost"), ("OU_2.25", "over", 1, 1, "half_lost"),
    ("ASIAN_HANDICAP", "home:-0.5", 1, 0, None),
])
def test_grade(market, sel, hg, ag, expected):
    assert settlement.grade(market, sel, hg, ag) == expected


def _df():
    return pd.DataFrame([(date(2026, 11, 7), "Arsenal", "Chelsea", 2, 1, "ENG1")],
                        columns=["date", "home", "away", "home_goals", "away_goals", "league"])


def test_find_result_tolerant_names_and_date():
    assert settlement.find_result(_df(), "Arsenal FC - Chelsea FC", date(2026, 11, 7)) == (2, 1)
    assert settlement.find_result(_df(), "Arsenal - Chelsea", date(2026, 11, 20)) is None


def test_auto_settle_and_clv(ledger):
    t = ledger.add_ticket(bookmaker="tipsport", odds=2.2, stake=100, match_id="m1", market="1X2", selection="1",
                          match_day=date(2026, 11, 7), league="ENG1", match_name="Arsenal - Chelsea")
    cand = Candidate(match_id="m1", league="ENG1", match_name="Arsenal - Chelsea", kickoff=datetime(2026, 11, 7, 17, 30),
                     market="1X2", selection="1", p_model=0.5, p_market=0.5, offers=(Offer("pinnacle", 2.0),))
    assert pipeline.update_closing(ledger, {"m1": [cand]}) == 1
    assert t.closing_odds == pytest.approx(2.0) and t.clv_pct == pytest.approx(10.0)
    notes = settlement.settle_open_tickets(ledger, _df())
    assert t.status == "won" and "won" in notes[0] and t.profit_loss == pytest.approx(120.0)
