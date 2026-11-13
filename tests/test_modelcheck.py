from datetime import date, datetime

import pandas as pd

from tipsbot import modelcheck
from tipsbot.config import SETTINGS
from tipsbot.tips import Candidate, Offer

NOW = datetime(2026, 11, 13, 12, 0)


def _cands(pm):
    return {"m1": [Candidate(match_id="m1", league="UNL", match_name="Spain - England", kickoff=datetime(2026, 11, 15, 19, 45),
                             market="1X2", selection=s, p_model=pm[s], p_market=pk, offers=(Offer("pinnacle", 2.0),))
                   for s, pk in (("1", 0.5), ("X", 0.25), ("2", 0.25))]}


def test_log_and_evaluate_picks_better_weight(ledger):
    assert modelcheck.log_predictions(ledger, "UNL", _cands({"1": 0.7, "X": 0.2, "2": 0.1}), NOW) == 1
    results = pd.DataFrame([(date(2026, 11, 15), "Spain", "England", 2, 0)], columns=["date", "home", "away", "home_goals", "away_goals"])
    n, ll = modelcheck.evaluate(modelcheck.load(ledger, "UNL"), results)
    assert n == 1 and ll[1.0] < ll[0.0]  # model mel pravdu, trh mene
    assert "nejlepší" in " ".join(modelcheck.report(ledger, "UNL", results))


def test_started_match_not_logged_and_unmatched_result_skipped(ledger):
    assert modelcheck.log_predictions(ledger, "UNL", _cands({"1": 0.5, "X": 0.25, "2": 0.25}), datetime(2026, 11, 15, 20, 0)) == 0
    assert modelcheck.evaluate({}, pd.DataFrame(columns=["date", "home", "away", "home_goals", "away_goals"]))[0] == 0


def test_league_model_weight():
    v = SETTINGS.value
    assert v.for_league("UNL").model_weight == 0.25 and v.for_league("ENG1").model_weight == v.model_weight
