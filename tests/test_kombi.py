"""Sledovani kombi tiketu: kazdy navrzeny tiket se zapise a vyhodnoti po nohach (fotbal i NHL)."""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from test_automation import SCORES
from tipsbot import combo, kombi, nhl_settle, paper
from tipsbot.analysis import MatchView, Row
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 19, 12, 0)


def _view(name, odds, p, league="ENG1", ko="2026-11-20T15:00:00", market="1X2", sel="1"):
    r = Row(market, sel, f"{sel} {name}", p, p, p, 1 / p, odds, "pinnacle", (p * odds - 1) * 100, "tip")
    return MatchView(name, league, name, ko, True, [r], 0, "TIP")


def _results(rows):
    return pd.DataFrame(rows, columns=["date", "home", "away", "home_goals", "away_goals"])


def test_every_suggested_combo_is_recorded_once_and_settled_by_legs():
    led = Ledger(make_session_factory("sqlite://")())
    views = [_view("Arsenal - Chelsea", 1.6, 0.70), _view("Leeds - Fulham", 1.7, 0.66), _view("Spurs - Wolves", 1.65, 0.68)]
    combos = combo.best_combos(views, 10000)
    leagues = {v.name: v.league for v in views}
    assert kombi.record(led, combos, NOW, leagues) == len(combos) >= 2
    assert kombi.record(led, combos, NOW + timedelta(hours=1), leagues) == 0  # stejny navrh = zadna duplicita
    ks = [p for p in paper.load(led) if p.profile == "kombi"]
    assert "doporučený tiket (nejlepší)" in ks[0].flags and all(p.legs for p in ks)
    # navrh zmizel pred vykopem -> zustava, jen s poznamkou
    kombi.record(led, combos[:1], NOW + timedelta(hours=2), leagues)
    assert sum(kombi.WITHDRAWN in p.flags for p in paper.load(led) if p.profile == "kombi") == len(combos) - 1
    d = date(2026, 11, 20)
    res = _results([(d, "Arsenal", "Chelsea", 2, 0), (d, "Leeds", "Fulham", 1, 0), (d, "Spurs", "Wolves", 0, 1)])
    assert not paper.settle(led, res)  # obycejne Sledovani kombi tikety nevyhodnocuje
    notes = kombi.settle(led, res)
    done = {p.name: p for p in paper.load(led) if p.profile == "kombi"}
    assert notes and all(p.result for p in done.values())
    for p in done.values():
        won = all(x["result"] == "won" for x in p.legs)
        assert p.result == ("won" if won else "lost")
        assert p.profit == pytest.approx(p.odds - 1 if won else -1, abs=0.02)


def test_combo_waits_for_all_legs_and_handles_void_and_nhl():
    led = Ledger(make_session_factory("sqlite://")())
    views = [_view("Arsenal - Chelsea", 1.6, 0.70, market="DNB", sel="home"),
             _view("Toronto Maple Leafs - Los Angeles Kings", 1.9, 0.58, league="NHL", ko="2026-11-20T00:00:00",
                   market="MONEYLINE", sel="away")]
    kombi.record(led, combo.best_combos(views, 10000), NOW, {v.name: v.league for v in views})
    (k,) = [p for p in paper.load(led) if p.profile == "kombi"]
    kombi.settle(led, _results([(date(2026, 11, 20), "Arsenal", "Chelsea", 1, 1)]))  # DNB remiza = vraceno
    (k,) = [p for p in paper.load(led) if p.profile == "kombi"]
    assert k.result is None and [x["result"] for x in k.legs if x["league"] != "NHL"] == ["void"]  # ceka na NHL
    nhl_settle.settle_paper(led, datetime(2026, 11, 20, 12, 0), fetch=lambda d: SCORES)  # LAK vyhral v najezdech
    (k,) = [p for p in paper.load(led) if p.profile == "kombi"]
    assert k.result == "won" and k.profit == pytest.approx(0.9)  # jen kurz NHL nohy 1.90


def test_kombi_profile_in_panel(tmp_path):
    from tipsbot.storage import utcnow
    from tipsbot.web import create_app

    now = utcnow()
    ko = (now + timedelta(days=1)).isoformat()
    url = f"sqlite:///{tmp_path / 'k.db'}"
    with make_session_factory(url)() as s:
        led = Ledger(s)
        views = [_view("Arsenal - Chelsea", 1.6, 0.70, ko=ko), _view("Leeds - Fulham", 1.7, 0.66, ko=ko)]
        kombi.record(led, combo.best_combos(views, 10000), now, {v.name: v.league for v in views})
        s.commit()
    html = create_app(url).test_client().get("/?tab=sledovani&profile=kombi").get_data(as_text=True)
    assert "Kombi tikety" in html and "1 Arsenal - Chelsea" in html and "Leeds - Fulham" in html
