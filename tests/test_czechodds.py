from datetime import datetime, timedelta

import pytest

from tipsbot import czechodds
from tipsbot.oddsapi import Event, MarketOdds
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 14, 12, 0)


def _event():
    return Event(
        "x:1", "UNL", "Germany", "Italy", NOW + timedelta(hours=5),
        {"1X2": MarketOdds("1X2", ("1", "X", "2"), {"pinnacle": {"1": 2.0, "X": 3.4, "2": 3.8}})},
    )


def test_parse_line_czech_names_and_decimal_comma():
    e = czechodds.parse_line("Tipsport | Německo - Itálie | 1x2 | 2,10 3.30 3.40", NOW)
    assert (e.book, e.market, e.odds) == ("tipsport", "1X2", (2.1, 3.3, 3.4))


@pytest.mark.parametrize("line", ["a | b", "foo | A - B | 1X2 | 2 3 4", "tipsport | A - B | 1X2 | 2 x 4", "tipsport | A - B | 1X2 | 1.0 3 4"])
def test_parse_line_rejects_bad_input(line):
    with pytest.raises(ValueError):
        czechodds.parse_line(line, NOW)


def test_apply_adds_book_to_matching_event_and_reports_gaps():
    ev = _event()
    entries, _ = czechodds.parse_text(
        "tipsport | Německo - Itálie | 1X2 | 2.10 3.30 3.40\n"
        "tipsport | Německo - Itálie | BTTS | 1.80 1.95\n"
        "fortuna | Německo - Itálie | 1X2 | 2.1 3.3\n"
        "betano | Francie - Španělsko | 1X2 | 2 3 4",
        NOW,
    )
    notes = czechodds.apply([ev], entries)
    assert ev.markets["1X2"].books["tipsport"] == {"1": 2.1, "X": 3.3, "2": 3.4}
    assert "fortuna" not in ev.markets["1X2"].books
    assert len(notes) == 2


def test_store_replaces_same_entry_and_expires():
    session = make_session_factory("sqlite://")()
    led = Ledger(session)
    a, _ = czechodds.parse_text("tipsport | A - B | 1X2 | 2 3 4", NOW)
    b, _ = czechodds.parse_text("tipsport | A - B | 1X2 | 2.5 3 4", NOW)
    czechodds.add(led, a, NOW)
    czechodds.add(led, b, NOW)
    assert [e.odds[0] for e in czechodds.load(led, NOW)] == [2.5]
    assert czechodds.load(led, NOW + timedelta(hours=49)) == []
