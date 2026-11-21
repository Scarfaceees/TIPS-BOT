"""Profil 3 (simulace trhu): rozbor se ulozi u kazdeho zapasu i bez sazky, dalsi trhy z API-Football jen pro nej."""

from datetime import datetime, timedelta

from tipsbot import marketsim, paper
from tipsbot.czechodds import Entry
from tipsbot.oddsapi import Event
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 21, 8, 0)


def _ev(mid="m1", home="Inter Milan", away="Genoa"):
    return Event(mid, "ITA1", home, away, NOW + timedelta(hours=10), {})


def _e(market, odds):
    return Entry("zahraničí (medián)", "Inter Milan", "Genoa", market, odds, NOW)


def test_analysis_is_kept_with_reason_when_no_bet_is_found():
    led = Ledger(make_session_factory("sqlite://")())
    ev = _ev()
    entries = [_e("1X2", (2.70, 3.30, 2.60)), _e("OU_2.5", (1.92, 1.88))]  # vyrovnany zapas, nic v pasmu 1.15-1.75
    paper.track(led, "ITA1", [(ev, None, entries)], NOW)
    assert not [p for p in paper.load(led) if p.profile == "profil3"]
    a = paper.load_analyses(led)["m1"]
    assert a["verdict"].startswith("žádná vhodná sázka") and a["lam"] > 0 and a["top"] and a["name"] == "Inter Milan - Genoa"


def test_extra_markets_reach_only_market_simulation_profile():
    led = Ledger(make_session_factory("sqlite://")())
    ev = _ev()
    entries = [_e("1X2", (1.25, 6.5, 12.0)), _e("OU_2.5", (1.55, 2.45))]
    extra = [_e("DC", (1.04, 1.18, 3.60)), _e("OU_4.5", (3.40, 1.30)), _e("ROHY_9.5", (1.40, 2.80))]
    seen = []
    orig = marketsim.analyse

    def spy(mid, model, ents, cfg=marketsim.DEFAULT):
        seen.append(sorted(e.market for e in ents))
        return orig(mid, model, ents, cfg)

    marketsim.analyse = spy
    try:
        paper.track(led, "ITA1", [(ev, None, entries)], NOW, extra={"m1": extra})
    finally:
        marketsim.analyse = orig
    assert seen == [["1X2", "DC", "OU_2.5", "OU_4.5"]]  # rohy z API-Football ne, ty ma profil rohy
    a = paper.load_analyses(led)["m1"]
    assert "DC" in a["markets"] and a["checked"] >= 1


def test_old_analyses_are_pruned():
    led = Ledger(make_session_factory("sqlite://")())
    entries = [_e("1X2", (2.70, 3.30, 2.60)), _e("OU_2.5", (1.92, 1.88))]
    paper.track(led, "ITA1", [(_ev("old"), None, entries)], NOW)
    later = NOW + timedelta(days=3)
    ev2 = Event("new", "ITA1", "Inter Milan", "Genoa", later + timedelta(hours=5), {})
    paper.track(led, "ITA1", [(ev2, None, entries)], later)
    assert set(paper.load_analyses(led)) == {"new"}


def test_panel_lists_profil3_analyses(tmp_path):
    from tipsbot.storage import utcnow
    from tipsbot.web import create_app

    now = utcnow()
    url = f"sqlite:///{tmp_path / 'p.db'}"
    with make_session_factory(url)() as s:
        led = Ledger(s)
        ev = Event("m1", "ITA1", "Inter Milan", "Genoa", now + timedelta(days=1), {})
        paper.track(led, "ITA1", [(ev, None, [_e("1X2", (2.70, 3.30, 2.60)), _e("OU_2.5", (1.92, 1.88))])], now)
        s.commit()
    html = create_app(url).test_client().get("/?tab=sledovani&profile=profil3").get_data(as_text=True)
    assert "Rozbory zápasů (1)" in html and "žádná vhodná sázka" in html and "Inter Milan - Genoa" in html
