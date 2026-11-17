from datetime import datetime, timedelta

from tipsbot import auto, paper, pipeline
from tipsbot.config import SETTINGS
from tipsbot.oddsapi import Event, MarketOdds
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 17, 12, 0)


def test_market_entries_median_without_exchanges():
    ev = Event("m1", "ENG1", "Arsenal", "Chelsea", NOW + timedelta(hours=30), {
        "1X2": MarketOdds("1X2", ("1", "X", "2"), {"a": {"1": 2.0, "X": 3.4, "2": 4.0}, "b": {"1": 2.2, "X": 3.6, "2": 3.6},
                                                   "c": {"1": 2.1, "X": 3.5, "2": 3.8}, "betfair_ex_eu": {"1": 9, "X": 9, "2": 9}}),
        "OU_2.5": MarketOdds("OU_2.5", ("over", "under"), {"a": {"over": 1.9, "under": 1.9}}),  # jen 1 kancelar
    })
    (e,) = pipeline.market_entries(ev, NOW)
    assert (e.market, e.odds, e.book) == ("1X2", (2.1, 3.5, 3.8), pipeline.MARKET_BOOK)
    assert paper.selections(e.market) == ("1", "X", "2")


def test_fresh_odds_respect_age_and_credit_reserve():
    led = Ledger(make_session_factory("sqlite://")())
    assert auto.wants_fresh_odds(led, "ENG1", NOW)
    led.set_odds_cache("ENG1", NOW - timedelta(hours=3), [], 400)
    assert not auto.wants_fresh_odds(led, "ENG1", NOW)  # stare jen 3 h
    assert auto.wants_fresh_odds(led, "ENG1", NOW + timedelta(hours=SETTINGS.timing.auto_odds_hours))
    led.set_odds_cache("ENG1", NOW - timedelta(days=3), [], SETTINGS.timing.auto_credit_reserve - 1)
    assert not auto.wants_fresh_odds(led, "ENG1", NOW)  # pod rezervou kreditu


def test_run_once_scans_all_leagues_and_records_status(monkeypatch):
    factory = make_session_factory("sqlite://")
    calls = []

    class Rep:
        def __init__(self, lg):
            self.lg = lg

        def summary(self):
            return f"{self.lg} ok"

    monkeypatch.setattr(pipeline, "refresh_data", lambda led: ["ligy: ok"])
    monkeypatch.setattr(pipeline, "scan_league", lambda lg, led, **kw: calls.append((lg, kw["cache_only"])) or Rep(lg))
    monkeypatch.setattr("tipsbot.settlement.settle_everything", lambda led: ["nic"])
    notes = auto.run_once(factory, NOW, verify=False)
    assert [c[0] for c in calls] == list(SETTINGS.odds_api.sport_keys)
    assert all(c[1] is False for c in calls)  # bez ulozenych kurzu se stahuje
    assert "data ligy: ok" in notes and "nic" in notes
    with factory() as s:
        st = auto.status(Ledger(s))
    assert st["t"] == NOW.isoformat() and "ENG1 ok" in st["notes"]
    auto.run_once(factory, NOW + timedelta(hours=6), verify=False)
    assert sum(n.startswith("data") for n in auto.run_once(factory, NOW + timedelta(hours=7), verify=False)) == 0


def test_redact_hides_keys():
    from tipsbot.oddsapi import redact

    msg = redact("Error for url: https://api.the-odds-api.com/v4/x?apiKey=abc123&regions=eu")
    assert "abc123" not in msg and "apiKey=***" in msg


def test_settle_from_scores_settles_pending_pick(monkeypatch):
    import pandas as pd

    from tipsbot import oddsapi

    led = Ledger(make_session_factory("sqlite://")())
    led.set_api_key("k")
    kick = NOW - timedelta(hours=5)
    paper.save(led, [paper.Pick(id="a1", league="UNL", match_id="m", name="Germany - Netherlands", day=kick.date().isoformat(),
                                kickoff=kick.isoformat(), market="BTTS", selection="no", odds=2.9, book="tipsport",
                                p_model=0.45, source="model", created=NOW.isoformat())])
    df = pd.DataFrame([{"date": kick.date(), "home": "Germany", "away": "Netherlands", "home_goals": 1, "away_goals": 4}])
    calls = []
    monkeypatch.setattr(oddsapi, "fetch_scores", lambda lg, key, **kw: calls.append(lg) or (df, 300))
    notes = auto.settle_from_scores(led, NOW)
    assert calls == ["UNL"] and any("lost" in n for n in notes)
    assert paper.load(led)[0].result == "lost"
    assert auto.settle_from_scores(led, NOW + timedelta(hours=1)) == []  # nic neceka -> zadne volani
    assert calls == ["UNL"]


def test_panel_button_requests_run(tmp_path):
    from tipsbot.web import create_app

    c = create_app(f"sqlite:///{tmp_path / 'p.db'}").test_client()
    r = c.post("/auto/run", data={})
    assert "spustí se do minuty" in r.get_data(as_text=True)
