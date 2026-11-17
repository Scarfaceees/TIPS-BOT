import json
from datetime import datetime, timedelta

import pytest

from tipsbot import paper, pipeline, propsodds
from tipsbot.oddsapi import Event
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 19, 12, 0)
FIXTURES = {"response": [{"fixture": {"id": 7, "date": "2026-11-20T19:30:00+00:00"},
                          "teams": {"home": {"name": "Bayern München"}, "away": {"name": "1. FC Köln"}}}]}


def _odds(bet_id, values, book="Pinnacle"):
    return {"response": [{"fixture": {"id": 7}, "bookmakers": [{"name": book, "bets": [
        {"id": bet_id, "name": "x", "values": [{"value": v, "odd": o} for v, o in values]}]}]}]}


@pytest.fixture
def led():
    return Ledger(make_session_factory("sqlite://")())


def test_parse_keeps_half_lines_and_maps_books():
    rows = propsodds.parse(FIXTURES, [
        _odds(45, [("Over 9.5", "1.49"), ("Under 9.5", "2.29"), ("Over 10", "1.61"), ("Under 10", "2.05")]),
        _odds(80, [("Over 4.5", "2.10"), ("Under 4.5", "1.67")], book="Betano"),
        _odds(80, [("Over 3.5", "1.5"), ("Under 3.5", "2.5")], book="Betfair"),
    ])
    assert [(r["market"], r["book"], r["over"], r["under"]) for r in rows] == [
        ("ROHY_9.5", "pinnacle", 1.49, 2.29), ("KARTY_4.5", "betano_int", 2.10, 1.67)]
    assert rows[0]["home"] == "Bayern München" and rows[0]["date"] == "2026-11-20"


def _cache(led, rows, at=NOW):
    led._set("propsodds:GER1", json.dumps({"at": at.isoformat(), "rows": rows}))


ROWS = [{"date": "2026-11-20", "home": "Bayern München", "away": "1. FC Köln", "market": "ROHY_9.5", "book": b,
         "over": o, "under": u} for b, o, u in (("pinnacle", 1.49, 2.29), ("bet365", 1.45, 2.40), ("1xbet", 1.41, 2.52))]


def test_attach_adds_markets_from_cache_without_request(led, monkeypatch):
    monkeypatch.setattr(propsodds, "fetch", lambda *a, **k: pytest.fail("nemelo se stahovat"))
    _cache(led, ROWS)
    ev = Event("x:1", "GER1", "Bayern Munich", "FC Koln", datetime(2026, 11, 20, 19, 30))
    notes = propsodds.attach([ev], "GER1", led, NOW + timedelta(hours=1), refresh=True)  # cache mladsi nez TTL
    assert set(ev.markets["ROHY_9.5"].books) == {"pinnacle", "bet365", "1xbet"}
    assert "u 1 z 1" in notes[0]
    assert propsodds.attach([ev], "UNL", led, NOW, refresh=True) == []  # jen ligy s modelem rohu a karet


def test_attach_refreshes_after_ttl_and_keeps_cache_on_error(led, monkeypatch):
    _cache(led, ROWS, at=NOW - timedelta(hours=4))
    calls = []
    monkeypatch.setattr(propsodds, "fetch", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(ValueError("x")))
    monkeypatch.setattr(Ledger, "api_football_key", lambda self: "k")
    ev = Event("x:1", "GER1", "Bayern Munich", "FC Koln", datetime(2026, 11, 20, 19, 30))
    notes = propsodds.attach([ev], "GER1", led, NOW, refresh=True)
    assert calls and "nepodařilo" in notes[0] and "ROHY_9.5" in ev.markets


def test_props_profile_picks_corners_and_other_profiles_ignore_them(led, monkeypatch):
    ev = Event("x:1", "GER1", "Bayern Munich", "FC Koln", NOW + timedelta(hours=30))
    _cache(led, ROWS)
    propsodds.attach([ev], "GER1", led, NOW, refresh=False)
    entries = pipeline.market_entries(ev, NOW)
    assert [(e.market, e.odds) for e in entries] == [("ROHY_9.5", (1.45, 2.4))]  # median kancelari
    model = {"ROHY_9.5:over": 0.55, "ROHY_9.5:under": 0.45}
    monkeypatch.setattr(paper, "PROFILES", tuple(p for p in paper.PROFILES if p.key in ("zaklad", "rohy")))
    paper.track(led, "GER1", [(ev, model, entries)], NOW)
    (p,) = paper.load(led)
    assert (p.profile, p.market, p.selection, p.odds) == ("rohy", "ROHY_9.5", "under", 2.4)  # 0.45*2.4 = +8 %

