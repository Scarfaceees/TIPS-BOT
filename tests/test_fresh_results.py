"""Cerstve vysledky z API-Football: odpoledni zapasy se vyhodnoti hned, ne az po aktualizaci football-data.co.uk."""

from datetime import datetime

from tipsbot import paper, settlement
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 21, 20, 0)


def _fixture(fid, home, away, hg, ag, status="FT", league=39):
    return {"fixture": {"id": fid, "date": "2026-11-21T15:00:00+00:00", "status": {"short": status}},
            "league": {"id": league}, "teams": {"home": {"name": home}, "away": {"name": away}},
            "score": {"fulltime": {"home": hg, "away": ag}}}


def _stats(corners, yellow):
    def team(c, y):
        return {"statistics": [{"type": "Total Shots", "value": 9}, {"type": "Corner Kicks", "value": c},
                               {"type": "Yellow Cards", "value": y}, {"type": "Red Cards", "value": None}]}
    return [team(corners[0], yellow[0]), team(corners[1], yellow[1])]


def _pick(led, name, market, sel, ko="2026-11-21T15:00:00", league="ENG1"):
    p = paper.Pick(id=f"{name}{market}", league=league, match_id=name, name=name, day=ko[:10], kickoff=ko, market=market,
                   selection=sel, odds=2.0, book="x", p_model=0.5, source="model", created=ko)
    paper.save(led, paper.load(led) + [p])


def test_afternoon_matches_settle_from_api_football_including_cards():
    led = Ledger(make_session_factory("sqlite://")())
    led.set_api_football_key("k")
    _pick(led, "Chelsea - Leeds United", "1X2", "2")
    _pick(led, "Newcastle United - Arsenal", "KARTY_5.5", "under")
    _pick(led, "Liverpool - Everton", "1X2", "1", ko="2026-11-21T19:30:00")  # jeste se hraje
    calls = []

    def get(ep, params):
        calls.append(ep)
        if ep == "fixtures":
            return [_fixture(1, "Chelsea", "Leeds", 1, 4), _fixture(2, "Newcastle", "Arsenal", 2, 2),
                    _fixture(3, "Liverpool", "Everton", 1, 0, status="2H"), _fixture(4, "Sparta", "Slavia", 1, 1, league=999)]
        assert params == {"fixture": 2}
        return _stats((5, 4), (None, 2))  # null = 0 karet

    df, notes = settlement.fresh_results(led, NOW, get=get)
    assert calls == ["fixtures", "fixtures/statistics"] and not notes
    assert list(df["home"]) == ["Chelsea", "Newcastle"]  # rozehrany zapas a cizi soutez se nepouziji
    done = paper.settle(led, df)
    res = {p.name: (p.result, p.score) for p in paper.load(led)}
    assert res["Chelsea - Leeds United"] == ("won", "1:4")
    assert res["Newcastle United - Arsenal"] == ("won", "2:2, 2 karet")
    assert res["Liverpool - Everton"] == (None, "") and len(done) == 2


def test_empty_statistics_do_not_settle_cards_as_zero():
    assert settlement._count_stats([]) == {}
    blank = {"statistics": [{"type": "Corner Kicks", "value": None}, {"type": "Yellow Cards", "value": None}]}
    assert settlement._count_stats([blank, blank]) == {}


def test_no_key_or_nothing_pending_makes_no_request():
    led = Ledger(make_session_factory("sqlite://")())
    _pick(led, "Chelsea - Leeds United", "1X2", "2")
    df, _ = settlement.fresh_results(led, NOW, get=lambda *a: 1 / 0)  # bez klice
    assert df.empty
    led.set_api_football_key("k")
    df, _ = settlement.fresh_results(led, datetime(2026, 11, 21, 16, 0), get=lambda *a: 1 / 0)  # zapas jeste nekonci
    assert df.empty
