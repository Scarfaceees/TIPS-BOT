from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from tipsbot import czechodds, paper
from tipsbot.oddsapi import Event
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 14, 12, 0)


@pytest.fixture
def led():
    return Ledger(make_session_factory("sqlite://")())


def _event(hours=5):
    return Event("x:1", "UNL", "Germany", "Italy", NOW + timedelta(hours=hours))


def _offers(odds_ou="1.76 2.10", model=None):
    entries, _ = czechodds.parse_text(f"tipsport | Německo - Itálie | OU_2.5 | {odds_ou}", NOW)
    return [(_event(), model or {"OU_2.5:over": 0.40, "OU_2.5:under": 0.60}, entries)]


def test_track_picks_best_edge_and_updates_before_kickoff(led):
    assert paper.track(led, "UNL", _offers(), NOW) == 1
    (p,) = paper.load(led)
    assert (p.market, p.selection, p.odds) == ("OU_2.5", "under", 2.10)  # 0.60*2.10-1 = +26 %
    assert p.flags  # hrana nad 15 % je podezrela
    paper.track(led, "UNL", _offers(odds_ou="1.76 2.30"), NOW)
    assert [x.odds for x in paper.load(led)] == [2.30]  # aktualizace, ne duplikat


def test_track_ignores_low_edge_and_odds_band(led):
    assert paper.track(led, "UNL", _offers(model={"OU_2.5:over": 0.55, "OU_2.5:under": 0.45}), NOW) == 0
    assert paper.load(led) == []
    assert paper.track(led, "UNL", _offers(odds_ou="1.05 4.90", model={"OU_2.5:over": 0.99, "OU_2.5:under": 0.9}), NOW) == 0


def test_started_match_pick_is_frozen(led):
    paper.track(led, "UNL", _offers(), NOW)
    later = NOW + timedelta(hours=6)
    paper.track(led, "UNL", _offers(odds_ou="1.76 3.00"), later)
    assert [p.odds for p in paper.load(led)] == [2.10]


def test_settle_and_stats(led):
    paper.track(led, "UNL", _offers(), NOW)
    paper.add_manual(led, league="UNL", name="Germany - Italy", day="2026-11-14", market="1X2", selection="1",
                     odds=2.0, book="tipsport", now=NOW)
    results = pd.DataFrame([{"date": date(2026, 11, 14), "home": "Germany", "away": "Italy", "home_goals": 2, "away_goals": 1}])
    notes = paper.settle(led, results)
    assert len(notes) == 2
    st = paper.stats(paper.load(led))
    assert st["settled"] == 2 and st["hit"] == 0.5
    assert st["profit"] == pytest.approx(-1.0 + 1.0)  # under 2.5 prohra (3 goly), 1 vyhra @2.0
    assert st["by_market"]["OU_2.5"][0] == 1


def test_manual_validation(led):
    with pytest.raises(ValueError):
        paper.add_manual(led, league="", name="A - B", day="2026-11-14", market="OU_2.5", selection="yes", odds=2, book="x", now=NOW)
    with pytest.raises(ValueError):
        paper.add_manual(led, league="", name="AB", day="2026-11-14", market="1X2", selection="1", odds=2, book="x", now=NOW)


def test_profiles_track_separately(led, monkeypatch):
    narrow = paper.Profile("uzky", "Úzký", "jen kurzy do 2.0", min_odds=1.5, max_odds=2.0)
    monkeypatch.setattr(paper, "PROFILES", paper.PROFILES[:1] + (narrow, paper.Profile("vyp", "Vyp", "", active=False)))
    model = {"OU_2.5:over": 0.62, "OU_2.5:under": 0.60}
    assert paper.track(led, "UNL", _offers(model=model), NOW) == 2
    got = {p.profile: (p.selection, p.odds) for p in paper.load(led)}
    assert got == {"zaklad": ("under", 2.10), "uzky": ("over", 1.76)}  # uzky profil nesmi brat kurz 2.10
    paper.track(led, "UNL", _offers(odds_ou="1.80 2.10", model=model), NOW)
    assert len(paper.load(led)) == 2  # aktualizace v ramci profilu, ne duplikaty
    m = paper.add_manual(led, league="UNL", name="A - B", day="2026-11-14", market="1X2", selection="1",
                         odds=2.0, book="tipsport", now=NOW, profile_key="uzky")
    assert m.profile == "uzky" and paper.profile("neznamy").key == "zaklad"
