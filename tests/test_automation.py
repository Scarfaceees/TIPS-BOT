"""Samostatny provoz: ranni analyza na 48 h (hlavni bot i Profil 4) a automaticke vyhodnoceni NHL.

NHL odpoved je presny tvar odpovedi api-web.nhle.com/v1/score/{den} (zkraceno), nic se nestahuje.
"""

from datetime import datetime, timedelta, timezone
from dataclasses import replace

import pytest

from tipsbot import auto, nhl_settle, paper, pipeline
from tipsbot.config import SETTINGS
from tipsbot.storage import Ledger, make_session_factory

# 21.11.2026: 06:30 UTC = 07:30 v Praze
MORNING = datetime(2026, 11, 21, 6, 30)


def _goal(period, hs, as_):
    return {"period": period, "periodDescriptor": {"number": period}, "homeScore": hs, "awayScore": as_}


SCORES = {"games": [
    # TOR - LAK 3:4 po najezdech; po 60 min 3:3
    {"id": 1, "gameState": "OFF", "startTimeUTC": "2026-11-20T00:00:00Z", "gameOutcome": {"lastPeriodType": "SO"},
     "homeTeam": {"abbrev": "TOR", "score": 3}, "awayTeam": {"abbrev": "LAK", "score": 4},
     "goals": [_goal(1, 1, 0), _goal(1, 2, 0), _goal(2, 3, 0), _goal(2, 3, 1), _goal(2, 3, 2), _goal(3, 3, 3), _goal(5, 3, 4)]},
    # BOS - MTL 2:1 v prodlouzeni; po 60 min 1:1
    {"id": 2, "gameState": "FINAL", "startTimeUTC": "2026-11-20T00:30:00Z", "gameOutcome": {"lastPeriodType": "OT"},
     "homeTeam": {"abbrev": "BOS", "score": 2}, "awayTeam": {"abbrev": "MTL", "score": 1},
     "goals": [_goal(1, 1, 0), _goal(3, 1, 1), _goal(4, 2, 1)]},
    # EDM - CGY 4:1 v zakladni dobe
    {"id": 3, "gameState": "OFF", "startTimeUTC": "2026-11-20T02:00:00Z", "gameOutcome": {"lastPeriodType": "REG"},
     "homeTeam": {"abbrev": "EDM", "score": 4}, "awayTeam": {"abbrev": "CGY", "score": 1},
     "goals": [_goal(1, 1, 0), _goal(2, 2, 0), _goal(2, 2, 1), _goal(3, 3, 1), _goal(3, 4, 1)]},
    {"id": 4, "gameState": "FUT", "startTimeUTC": "2026-11-21T00:00:00Z", "homeTeam": {"abbrev": "NYR"},
     "awayTeam": {"abbrev": "NJD"}},
]}


# --- NHL vyhodnoceni ------------------------------------------------------------

def test_parse_scores_regulation_and_final():
    g = {x["home"]: x for x in nhl_settle.parse_scores(SCORES)}
    assert set(g) == {"TOR", "BOS", "EDM"}  # neodehrany zapas se vynecha
    assert g["TOR"]["final"] == (3, 4) and g["TOR"]["reg"] == (3, 3) and g["TOR"]["end"] == "SO"
    assert g["BOS"]["final"] == (2, 1) and g["BOS"]["reg"] == (1, 1)
    assert g["EDM"]["final"] == g["EDM"]["reg"] == (4, 1)


@pytest.mark.parametrize("home,market,sel,res", [
    ("TOR", "REG", "X", "won"), ("TOR", "REG", "2", "lost"),  # po 60 min remiza
    ("TOR", "MONEYLINE", "away", "won"), ("TOR", "MONEYLINE", "home", "lost"),  # najezdy vyhral LAK
    ("TOR", "OU60_6.5", "under", "won"), ("TOR", "OU_6.5", "over", "won"),  # 6 golu za 60 min, 7 vcetne najezdu
    ("TOR", "OT", "yes", "won"), ("EDM", "OT", "yes", "lost"),
    ("EDM", "PUCKLINE_1.5", "home-1.5", "won"), ("BOS", "PUCKLINE_1.5", "home-1.5", "lost"),
    ("BOS", "PUCKLINE_1.5", "away+1.5", "won"), ("EDM", "PUCKLINE_1.5R", "away-1.5", "lost"),
    ("EDM", "TTH_3.5", "over", "won"), ("EDM", "TTA_1.5", "under", "won"),
    ("EDM", "OU60_5.0", "over", "void"),  # cela linie: presne 5 golu = vraceni
    ("EDM", "1X2", "1", None),  # fotbalovy trh NHL vyhodnoceni nezna
])
def test_grade_all_nhl_markets(home, market, sel, res):
    g = {x["home"]: x for x in nhl_settle.parse_scores(SCORES)}[home]
    assert nhl_settle.grade(market, sel, g) == res


def _pick(name, market, sel, kickoff, odds=2.0):
    return paper.Pick(id=name[:3] + market, league="NHL", match_id="x", name=name, day=kickoff.date().isoformat(),
                      kickoff=kickoff.isoformat(), market=market, selection=sel, odds=odds, book="pinnacle",
                      p_model=0.5, source="model", created=kickoff.isoformat())


def test_settle_paper_nhl_from_api():
    led = Ledger(make_session_factory("sqlite://")())
    ko = datetime(2026, 11, 20, 0, 0)
    paper.save(led, [_pick("Toronto Maple Leafs - Los Angeles Kings", "MONEYLINE", "away", ko, 2.4),
                     _pick("Boston Bruins - Montréal Canadiens", "OU60_2.5", "under", ko + timedelta(minutes=30)),
                     _pick("Toronto Maple Leafs - Los Angeles Kings", "REG", "1", ko, 2.1),
                     _pick("New York Rangers - New Jersey Devils", "MONEYLINE", "home", ko + timedelta(days=1))])
    days = []
    notes = nhl_settle.settle_paper(led, ko + timedelta(hours=8), fetch=lambda d: days.append(d) or SCORES)
    picks = {(p.name[:6], p.market): p for p in paper.load(led)}
    assert picks[("Toront", "MONEYLINE")].result == "won" and picks[("Toront", "MONEYLINE")].profit == pytest.approx(1.4)
    assert picks[("Toront", "REG")].result == "lost" and "SO" in picks[("Toront", "REG")].score
    assert picks[("New Yo", "MONEYLINE")].result is None  # jeste se nehralo
    assert len(notes) >= 2 and days  # herni den NHL se hleda i den predem (americke datum)


def test_nhl_name_mapping_covers_montreal():
    from tipsbot.names import nhl_abbr

    for name in ("Boston Bruins", "Montréal Canadiens", "Toronto Maple Leafs", "Los Angeles Kings"):
        assert nhl_abbr(name), name


# --- ranni analyza ----------------------------------------------------------------

def test_morning_due_once_per_day_after_7_prague():
    led = Ledger(make_session_factory("sqlite://")())
    assert not auto.morning_due(led, datetime(2026, 11, 21, 5, 30))  # 06:30 v Praze
    assert auto.morning_due(led, MORNING)
    led._set(auto.MORNING_KEY, "2026-11-21")
    assert not auto.morning_due(led, MORNING + timedelta(hours=5))
    assert auto.morning_due(led, MORNING + timedelta(days=1))


def test_morning_pass_refreshes_data_and_odds_older_than_6h(monkeypatch):
    factory = make_session_factory("sqlite://")
    with factory() as s:
        led = Ledger(s)
        for lg in SETTINGS.odds_api.sport_keys:
            led.set_odds_cache(lg, MORNING - timedelta(hours=8), [], 400)  # 8 h stare
        led._set(auto.REFRESH_KEY, (MORNING - timedelta(hours=2)).isoformat())
        s.commit()
    calls, refresh = [], []
    monkeypatch.setattr(pipeline, "refresh_data", lambda led: refresh.append(1) or ["ligy: ok"])
    monkeypatch.setattr(pipeline, "scan_league", lambda lg, led, **kw: calls.append(kw["cache_only"]) or
                        type("R", (), {"summary": lambda self: "ok"})())
    monkeypatch.setattr("tipsbot.settlement.settle_everything", lambda led: [])
    monkeypatch.setattr(nhl_settle, "settle_paper", lambda led, now: [])
    auto.run_once(factory, MORNING, verify=False)
    assert all(calls) and not refresh  # bezny pruchod: 8 h stare kurzy se nestahuji, data se neobnovuji
    calls.clear()
    notes = auto.run_once(factory, MORNING, verify=False, morning=True)
    assert calls and not any(calls) and refresh  # ranni: nove kurzy i data
    assert notes[0].startswith("ranní analýza")
    with factory() as s:
        assert Ledger(s)._get(auto.MORNING_KEY, "") == "2026-11-21"


def test_run_once_settles_nhl(monkeypatch):
    factory = make_session_factory("sqlite://")
    monkeypatch.setattr(pipeline, "refresh_data", lambda led: [])
    monkeypatch.setattr(pipeline, "scan_league", lambda lg, led, **kw: type("R", (), {"summary": lambda self: "ok"})())
    monkeypatch.setattr("tipsbot.settlement.settle_everything", lambda led: [])
    monkeypatch.setattr(nhl_settle, "settle_paper", lambda led, now: ["NHL hotovo"])
    assert "NHL hotovo" in auto.run_once(factory, MORNING, verify=False)


def test_profil4_morning_scan_analyses_all_fixtures_once_a_day(tmp_path):
    import test_profil4_flow as t
    from tipsbot.profil4 import closing, config as c4
    from tipsbot.profil4.store import Store

    cfg = c4.Profil4Config(data_dir=tmp_path)
    prov, store, _, _ = t.make()
    store.close = lambda: None
    import tipsbot.profil4.closing as cl

    orig = cl.open_store
    cl.open_store = lambda cfg=None: store
    try:
        before = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)  # 06:00 v Praze
        assert closing.morning_scan(before, cfg, lambda: (store, prov)) == []
        now = t.NOW.replace(hour=7)  # 08:00 v Praze
        notes = closing.morning_scan(now, cfg, lambda: (store, prov))
        assert notes and "ranní sken 48 h" in notes[0]
        assert "hloubkově 2" in notes[0]  # oba zapasy okna, ne jen kandidati
        assert store.get("web_last_view") and store.get("web_last_report")
        assert len({r["fixture_id"] for r in store.query("SELECT fixture_id FROM predictions")}) == 2
        assert closing.morning_scan(now + timedelta(hours=2), cfg, lambda: (store, prov)) == []  # jen jednou denne
    finally:
        cl.open_store = orig
