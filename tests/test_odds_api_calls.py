"""Phase 3.4 / B1: evidence jednotlivych pozadavku na The Odds API. Jen mocky, zadne skutecne HTTP volani."""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from tipsbot import auto, oddsapi, pipeline
from tipsbot.config import SETTINGS
from tipsbot.profil4 import foreign, nhl
from tipsbot.storage import OddsApiCall

KEY = "SECRETKEY123"


class Resp:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body if body is not None else [], headers or {}, text

    def json(self):
        return self._body


class FakeGet:
    """Nahrada requests.get: vraci pripravene odpovedi a pocita pozadavky."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {})))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def H(remaining=None, used=None, last=None):
    h = {}
    for k, v in (("x-requests-remaining", remaining), ("x-requests-used", used), ("x-requests-last", last)):
        if v is not None:
            h[k] = str(v)
    return h


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Pojistka: skutecny requests.get v techto testech nesmi probehnout."""
    def boom(*a, **k):
        raise AssertionError("skutečné HTTP volání v testu")
    monkeypatch.setattr(requests, "get", boom)


def rows(ledger):
    ledger.s.flush()
    return ledger.odds_api_calls()


def no_secret(row):
    return all(KEY not in str(getattr(row, c)) for c in ("endpoint", "params", "error"))


# --- zakladni zaznam ---------------------------------------------------------

def test_success_with_cost_header_is_known(ledger):
    get = FakeGet(Resp(200, [], H(remaining=440, used=60, last=2)))
    with oddsapi.recording(ledger.log_odds_api_call):
        oddsapi.request("https://x", "/sports/soccer_epl/odds", {"apiKey": KEY, "markets": "h2h,totals", "regions": "eu"},
                        purpose="refresh_league", sport="soccer_epl", get=get)
    (r,) = rows(ledger)
    assert (r.purpose, r.league, r.http, r.ok, r.remaining, r.used) == ("refresh_league", "ENG1", 200, True, 440, 60)
    assert (r.cost, r.cost_kind, r.cost_expected) == (2, "header", 2)
    assert "apiKey" not in r.params and no_secret(r) and len(get.calls) == 1


def test_http_error_is_recorded_without_key(ledger):
    get = FakeGet(Resp(401, {}, {}, text=f"invalid apiKey={KEY}"))
    with oddsapi.recording(ledger.log_odds_api_call):
        resp = oddsapi.request("https://x", "/sports/icehockey_nhl/odds", {"apiKey": KEY, "markets": "h2h"},
                               purpose="refresh_league", sport="icehockey_nhl", get=get)
    assert resp.status_code == 401
    (r,) = rows(ledger)
    assert (r.http, r.ok, r.cost, r.cost_kind, r.league) == (401, False, None, "unknown", "NHL")
    assert r.error and no_secret(r)


def test_network_error_is_recorded_and_reraised(ledger):
    get = FakeGet(requests.ConnectionError(f"https://x/?apiKey={KEY} timed out"))
    with oddsapi.recording(ledger.log_odds_api_call), pytest.raises(requests.ConnectionError):
        oddsapi.request("https://x", "/sports/soccer_epl/scores", {"apiKey": KEY, "daysFrom": 3}, purpose="scores",
                        sport="soccer_epl", get=get)
    (r,) = rows(ledger)
    assert (r.http, r.ok, r.cost, r.cost_kind, r.cost_expected) == (None, False, None, "unknown", 2)
    assert "ConnectionError" in r.error and no_secret(r)


# --- chybejici udaje o spotrebe ----------------------------------------------

def test_missing_headers_cost_unknown(ledger):
    get = FakeGet(Resp(200, [], {}))
    with oddsapi.recording(ledger.log_odds_api_call):
        oddsapi.request("https://x", "/sports/soccer_epl/odds", {"markets": "h2h"}, purpose="refresh_league",
                        sport="soccer_epl", get=get)
    (r,) = rows(ledger)
    assert (r.remaining, r.used, r.cost, r.cost_kind) == (None, None, None, "unknown")


def test_cost_from_used_delta_is_marked_as_estimate(ledger):
    get = FakeGet(Resp(200, [], H(450, 50)), Resp(200, [], H(447, 53)))
    with oddsapi.recording(ledger.log_odds_api_call):
        for _ in range(2):
            oddsapi.request("https://x", "/sports/icehockey_nhl/odds", {"markets": "h2h,totals,spreads"},
                            purpose="refresh_league", sport="icehockey_nhl", get=get)
    first, second = rows(ledger)
    assert (first.cost, first.cost_kind) == (None, "unknown")  # nema s cim srovnat
    assert (second.cost, second.cost_kind, second.cost_expected) == (3, "delta", 3)


def test_delta_not_computed_across_monthly_reset(ledger):
    ledger.s.add(OddsApiCall(ts=datetime(2026, 11, 30, 18), endpoint="/e", purpose="refresh_league", sport="s",
                             used=490, cost_kind="header"))
    rec = oddsapi.CallRecord(datetime(2026, 12, 1, 6), "/e", "refresh_league", "s", None, {}, http=200, ok=True,
                             remaining=497, used=3)
    r = ledger.log_odds_api_call(rec)
    assert (r.cost, r.cost_kind) == (None, "unknown")


def test_used_lower_than_previous_gives_unknown(ledger):
    for used in (100, 4):
        r = ledger.log_odds_api_call(oddsapi.CallRecord(datetime(2026, 12, 2), "/e", "scores", "s", None, {},
                                                        http=200, ok=True, used=used))
    assert (r.cost, r.cost_kind) == (None, "unknown")


# --- evidence nic nestahuje a nic nerozbije ------------------------------------

def test_without_sink_nothing_is_recorded(ledger):
    get = FakeGet(Resp(200, [], H(1, 1, 1)))
    oddsapi.request("https://x", "/e", {}, purpose="scores", sport="s", get=get)
    with oddsapi.recording(None):
        get.responses.append(Resp(200, [], {}))
        oddsapi.request("https://x", "/e", {}, purpose="scores", sport="s", get=get)
    assert rows(ledger) == [] and len(get.calls) == 2


def test_sink_failure_does_not_break_request():
    def bad_sink(rec):
        raise RuntimeError("db locked")

    get = FakeGet(Resp(200, ["ok"], H(10, 1, 1)))
    with oddsapi.recording(bad_sink):
        resp = oddsapi.request("https://x", "/e", {}, purpose="scores", sport="s", get=get)
    assert resp.json() == ["ok"] and len(get.calls) == 1


def test_logging_makes_no_extra_requests(ledger, monkeypatch):
    """Zapis evidence nevola zadne HTTP: zakladni requests.get by v tomto testu selhal (fixture no_network)."""
    rec = oddsapi.CallRecord(datetime(2026, 12, 1, 7), "/e", "nhl_clv", "icehockey_nhl", "NHL", {"markets": "h2h"},
                             http=200, ok=True, remaining=480, used=20, last=1)
    r = ledger.log_odds_api_call(rec)
    assert r.cost == 1 and r.cost_expected == 1


# --- vsechna mista volani ------------------------------------------------------

def _payload(kick):
    return [{"id": "e1", "commence_time": kick.strftime("%Y-%m-%dT%H:%M:%SZ"), "home_team": "Arsenal",
             "away_team": "Chelsea", "bookmakers": []}]


def test_fetch_events_site_records_refresh_league(ledger, monkeypatch):
    kick = datetime.now(timezone.utc) + timedelta(hours=5)
    get = FakeGet(Resp(200, _payload(kick), H(480, 20, 2)))
    monkeypatch.setattr(requests, "get", get)
    ledger.set_api_key(KEY)

    class NoModel:
        note = ""
        unmatched: set = set()

        def probabilities(self, ev):
            return None

    pipeline.scan_league("ENG1", ledger, now=datetime.now(timezone.utc).replace(tzinfo=None), provider=NoModel())
    calls = rows(ledger)
    assert len(get.calls) == 1 and len(calls) == 1
    assert (calls[0].purpose, calls[0].league, calls[0].cost, calls[0].cost_kind) == ("refresh_league", "ENG1", 2, "header")
    assert no_secret(calls[0])


def test_fetch_scores_records_scores(ledger, monkeypatch):
    get = FakeGet(Resp(200, [], H(470, 30, 2)))
    monkeypatch.setattr(requests, "get", get)
    with oddsapi.recording(ledger.log_odds_api_call):
        df, left = oddsapi.fetch_scores("ENG1", KEY)
    (r,) = rows(ledger)
    assert left == 470 and (r.purpose, r.league, r.cost, r.cost_expected) == ("scores", "ENG1", 2, 2)


def test_foreign_fetch_records_both_requests(ledger):
    ko = datetime(2026, 12, 2, 20, tzinfo=timezone.utc)
    events = [{"id": "ev9", "home_team": "Arsenal", "away_team": "Chelsea", "commence_time": "2026-12-02T20:00:00Z"}]
    get = FakeGet(Resp(200, events, H(400, 100)), Resp(200, {"bookmakers": []}, H(399, 101, 1)))
    with oddsapi.recording(ledger.log_odds_api_call):
        foreign.fetch("soccer_epl", KEY, "Arsenal", "Chelsea", ko, ["h2h"], 77, "Arsenal - Chelsea", ko, "https://x", get=get)
    lst, odds = rows(ledger)
    assert len(get.calls) == 2
    assert (lst.purpose, lst.cost_expected, lst.fixture_id) == ("foreign_events", 0, 77)
    assert (odds.purpose, odds.event_id, odds.cost, odds.cost_kind, odds.league) == ("foreign_clv", "ev9", 1, "header", "ENG1")


def test_nhl_event_fetch_records_nhl_clv(ledger, monkeypatch):
    get = FakeGet(Resp(200, {"bookmakers": []}, H(300, 200, 2)))
    monkeypatch.setattr(requests, "get", get)
    with oddsapi.recording(ledger.log_odds_api_call):
        body, quota, note = nhl._fetch_event("icehockey_nhl", KEY, "abc", ["h2h", "totals"], "https://x")
    (r,) = rows(ledger)
    assert quota == {"remaining": 300, "used": 200} and note == ""
    assert (r.purpose, r.league, r.event_id, r.cost, r.cost_expected) == ("nhl_clv", "NHL", "abc", 2, 2)


# --- rezerva a cache-only se nemeni -------------------------------------------

@pytest.mark.parametrize("credits, expect_calls", [(None, 1), (SETTINGS.timing.auto_credit_reserve, 1),
                                                    (SETTINGS.timing.auto_credit_reserve - 1, 0)])
def test_scores_reserve_unchanged(ledger, monkeypatch, credits, expect_calls):
    """auto.settle_from_scores: pod rezervou kreditu se nestahuje (ani pozadavek, ani zaznam), jinak jeden pozadavek
    a jeden zaznam – stejna hranice jako pred zmenou."""
    from tipsbot import paper

    get = FakeGet(*[Resp(200, [], H(400, 100, 2)) for _ in range(expect_calls)])
    monkeypatch.setattr(requests, "get", get)
    ledger.set_api_key(KEY)
    now = datetime(2026, 12, 3, 12)
    ledger.set_odds_cache("ENG1", now - timedelta(hours=1), [], credits)
    pending = type("P", (), {"league": "ENG1", "result": None, "kickoff": (now - timedelta(hours=5)).isoformat()})()
    monkeypatch.setattr(paper, "load", lambda led: [pending])
    monkeypatch.setattr(paper, "settle", lambda led, df: [])
    monkeypatch.setattr("tipsbot.settlement.settle_p5", lambda led, df: None)
    auto.settle_from_scores(ledger, now, SETTINGS)
    calls = rows(ledger)
    assert len(get.calls) == expect_calls and len(calls) == expect_calls
    if calls:
        assert (calls[0].purpose, calls[0].league, calls[0].cost) == ("scores", "ENG1", 2)
