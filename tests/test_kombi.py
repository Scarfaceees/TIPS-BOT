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


def test_all_combos_count_and_panel_shows_evaluation(tmp_path):
    """27.11.2026: pravidlo „kazda sazka jen v prvnim tiketu“ zruseno – pocitaji se vsechny tikety, nezavislost
    resi interval po zapasech; panel ukaze tri pohledy a kontrolu dat."""
    from tipsbot.storage import utcnow
    from tipsbot.web import create_app

    now = utcnow()
    ko = (now + timedelta(days=1)).isoformat()
    url = f"sqlite:///{tmp_path / 'k.db'}"
    with make_session_factory(url)() as s:
        led = Ledger(s)
        views = [_view("Arsenal - Chelsea", 1.6, 0.70, ko=ko), _view("Leeds - Fulham", 1.7, 0.66, ko=ko),
                 _view("Spurs - Wolves", 1.65, 0.68, ko=ko)]
        combos = combo.best_combos(views, 10000)
        kombi.record(led, combos, now, {v.name: v.league for v in views})
        ks = [p for p in paper.load(led) if p.profile == "kombi"]
        assert len(ks) >= 2 and paper.stats(ks)["open"] == len(ks)  # zadny tiket se nevyhazuje
        ev = kombi.evaluation(ks)
        assert ev["all"]["open"] == len(ks) and ev["rec"]["open"] == 1 and ev["issues"] == []
        assert ev["legs"]["unique"] == 3 and ev["legs"]["open"] == 3
        s.commit()
    html = create_app(url).test_client().get("/?tab=sledovani&profile=kombi").get_data(as_text=True)
    assert "nezapočítán" not in html  # karta Vyhodnoceni kombi az s vyhodnocenymi tikety (test na kopii dat)


def _pk(pid, legs, result=None, profit=0.0, flags=(), odds=None):
    """legs = [(zapas, vyber, kurz, vysledek)] – vsechny nohy OU_2.5 stejneho dne."""
    lg = [{"name": n, "day": "2026-11-22", "market": "OU_2.5", "selection": s, "odds": o, "result": r, "score": "1:0"}
          for n, s, o, r in legs]
    import math

    return paper.Pick(id=pid, league="KOMBI", match_id=pid, name=" + ".join(x["name"] for x in lg), day="2026-11-22",
                      kickoff="2026-11-22T15:00", market="KOMBI", selection=f"{len(lg)} sázky",
                      odds=odds or round(math.prod(x["odds"] for x in lg), 2), book="kombinace", p_model=None,
                      source="model", created="", profile="kombi", legs=lg, flags=list(flags), result=result, profit=profit)


def test_evaluation_counts_every_ticket_once_and_each_bet_once_as_single():
    a = _pk("A", [("X - Y", "under", 1.5, "won"), ("Z - W", "under", 1.6, "won")], "won", 1.5 * 1.6 - 1, [kombi.REC])
    b = _pk("B", [("X - Y", "under", 1.5, "won"), ("Q - R", "over", 1.8, "lost")], "lost", -1.0)
    c = _pk("C", [("Q - R", "over", 1.8, "lost"), ("S - T", "under", 2.0, "won")], "lost", -1.0)
    ev = kombi.evaluation([a, b, c])
    assert ev["all"]["settled"] == 3 and ev["all"]["profit"] == pytest.approx(1.4 - 2) and ev["all"]["matches"] == 4
    assert ev["rec"]["settled"] == 1 and ev["rec"]["roi"] == pytest.approx(1.4)
    lg = ev["legs"]  # X-Y jen jednou (i kdyz je ve 2 tiketech)
    assert lg["unique"] == 4 and lg["settled"] == 4 and lg["hit"] == pytest.approx(0.75)
    assert lg["roi"] == pytest.approx((0.5 + 0.6 - 1 + 1.0) / 4)
    assert lg["implied"] == pytest.approx((1 / 1.5 + 1 / 1.6 + 1 / 1.8 + 1 / 2.0) / 4)
    assert ev["issues"] == []


def test_data_check_finds_ticket_leg_mismatches():
    ok = _pk("OK", [("X - Y", "under", 1.5, "void"), ("Z - W", "under", 1.6, "won")], "won", 0.6)  # vracena noha = kurz 1
    bad_profit = _pk("P", [("A - B", "under", 1.5, "won"), ("C - D", "under", 1.6, "won")], "won", 1.0)
    bad_odds = _pk("O", [("E - F", "under", 1.5, None), ("G - H", "under", 1.6, None)], odds=3.0)
    early = _pk("N", [("I - J", "under", 1.5, None), ("K - L", "under", 1.6, None)], "won", 1.4)
    same_match = _pk("S", [("M - N", "under", 1.5, None), ("M - N", "over", 1.6, None)])
    conflict = _pk("K", [("X - Y", "under", 1.5, "lost"), ("Q - R", "under", 1.7, None)], "lost", -1.0)
    assert kombi.ticket_issues(ok) == []
    assert "≠ podle nohou won +1.40" in kombi.ticket_issues(bad_profit)[0]
    assert "součin kurzů" in kombi.ticket_issues(bad_odds)[0]
    assert "nohy ještě nejsou rozhodnuté" in kombi.ticket_issues(early)[0]
    assert kombi.ticket_issues(same_match) == ["dvě nohy ze stejného zápasu"]
    ev = kombi.evaluation([ok, conflict])
    assert any("různé výsledky: lost, void" in i for _, i in ev["issues"])
    assert ev["legs"]["settled"] == 1  # sporna sazka se do singlu nepocita


def test_interval_accounts_for_shared_matches():
    import math
    import random

    r = random.Random(1)
    pr = [r.choice([1.2, -1.0]) for _ in range(60)]
    indep = [([f"a{i}", f"b{i}"], p) for i, p in enumerate(pr)]
    lo, hi = kombi.roi_ci(indep)
    mean = sum(pr) / 60
    sd = math.sqrt(sum((p - mean) ** 2 for p in pr) / 59)
    assert (hi - lo) / 2 == pytest.approx(1.96 * sd / math.sqrt(60))  # nezavisle tikety = bezny interval
    dup = [([f"a{i}", f"b{i}"], p) for i, p in enumerate(pr[:20])] * 3  # kazdy tiket 3x (sdileji oba zapasy)
    lo3, hi3 = kombi.roi_ci(dup)
    lo1, hi1 = kombi.roi_ci(dup[:20])
    assert hi3 - lo3 == pytest.approx((hi1 - lo1) * math.sqrt(57 / 59), rel=1e-9)  # kopie neprinaseji informaci
    assert kombi.roi_ci([(["m1"], 1.0)]) is None and kombi.roi_ci(dup) == kombi.roi_ci(dup)


def test_lost_combo_still_gets_results_of_other_legs():
    """23.11.2026: u prohraneho kombi tiketu se ostatni zapasy nevyhodnotily (⏳ navzdy)."""
    led = Ledger(make_session_factory("sqlite://")())
    views = [_view("Arsenal - Chelsea", 1.6, 0.70), _view("Leeds - Fulham", 1.7, 0.66, ko="2026-11-20T19:00:00")]
    kombi.record(led, combo.best_combos(views, 10000), NOW, {v.name: v.league for v in views})
    d = date(2026, 11, 20)
    kombi.settle(led, _results([(d, "Arsenal", "Chelsea", 0, 1)]))
    (k,) = [p for p in paper.load(led) if p.profile == "kombi"]
    assert k.result == "lost" and kombi.open_legs(led)
    kombi.settle(led, _results([(d, "Leeds", "Fulham", 2, 0)]))
    (k,) = [p for p in paper.load(led) if p.profile == "kombi"]
    assert [x["result"] for x in k.legs] == ["lost", "won"] and k.result == "lost" and k.profit == -1
    assert kombi.open_legs(led) == []
