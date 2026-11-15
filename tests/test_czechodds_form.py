from datetime import datetime

import pytest

from tipsbot import czechodds

NOW = datetime(2026, 11, 15, 10)


def test_from_form_keeps_complete_markets_only():
    entries, errors = czechodds.from_form("Tipsport", "Belgium - Italy", {
        "1X2:1": "2,16", "1X2:X": "3.81", "1X2:2": "3.30", "OU_2.5:over": "1.9", "OU_2.5:under": "",
        "TTH_0.5:over": "1.25", "TTH_0.5:under": "3.6"}, NOW)
    assert [(e.market, e.odds) for e in entries] == [("1X2", (2.16, 3.81, 3.30)), ("TTH_0.5", (1.25, 3.6))]
    assert errors and "OU_2.5" in errors[0]
    with pytest.raises(ValueError):
        czechodds.from_form("bet365", "Belgium - Italy", {}, NOW)


def test_for_match_best_price_and_czech_names():
    entries = [czechodds.Entry("tipsport", "Belgie", "Itálie", "BTTS", (1.9, 1.85), NOW),
               czechodds.Entry("fortuna", "Belgium", "Italy", "BTTS", (1.95, 1.80), NOW)]
    best = czechodds.for_match(entries, "Belgium - Italy")
    assert best[("BTTS", "yes")] == (1.95, "fortuna") and best[("BTTS", "no")] == (1.85, "tipsport")
    assert czechodds.for_match(entries, "Spain - England") == {}
