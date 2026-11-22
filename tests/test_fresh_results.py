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


def test_football_settles_between_passes_not_six_hours_later(tmp_path, monkeypatch):
    """22.11.2026: dohrany fotbal (i tikety AKU) cekal na dalsi sestihodinovy pruchod automatu."""
    from datetime import date

    from tipsbot import auto

    factory = make_session_factory(f"sqlite:///{tmp_path / 'f.db'}")
    with factory() as s:
        led = Ledger(s)
        led.set_api_football_key("k")
        led.deposit(1000)
        _pick(led, "Chelsea - Leeds United", "1X2", "2")
        t = led.add_combo_ticket(bookmaker="tipsport", odds=2.0, stake=25, legs=[
            {"match_name": "Chelsea - Leeds United", "match_day": date(2026, 11, 21), "market": "OU_2.5",
             "selection": "over", "odds": 1.4},
            {"match_name": "Newcastle United - Arsenal", "match_day": date(2026, 11, 21), "market": "OU_4.5",
             "selection": "under", "odds": 1.43}])
        tid = t.id
        s.commit()
    calls = []

    def fake(ledger, endpoint, params):
        calls.append(params)
        return [_fixture(1, "Chelsea", "Leeds", 1, 4), _fixture(2, "Newcastle", "Arsenal", 2, 2)]

    monkeypatch.setattr(settlement, "_api_get", lambda key, ep, params: fake(None, ep, params))
    assert auto.settle_football(factory, datetime(2026, 11, 21, 11, 0)) == [] and calls == []  # pred polednem nic
    notes = auto.settle_football(factory, datetime(2026, 11, 21, 17, 15))
    assert calls == [{"date": "2026-11-21", "timezone": "UTC"}]
    with factory() as s:
        led = Ledger(s)
        assert paper.load(led)[0].result == "won"
        from tipsbot.storage import Ticket

        assert s.get(Ticket, tid).status == "won" and any("celý tiket won" in n for n in notes)
    assert auto.settle_football(factory, datetime(2026, 11, 21, 17, 30)) == [] and len(calls) == 1  # uz nic neceka
