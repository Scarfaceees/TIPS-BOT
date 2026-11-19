from datetime import timedelta

import pytest

from p4_fakes import NOW, Clock, FakeTransport, fixture_item, odds_item, ok
from tipsbot.profil4 import odds as p4odds
from tipsbot.profil4.apiclient import ApiFootballClient
from tipsbot.profil4.config import BudgetConfig, P4
from tipsbot.profil4.coverage import CoverageReport
from tipsbot.profil4.provider import AVAILABLE, NOT_YET, SKIPPED, UNAVAILABLE, DataProvider
from tipsbot.profil4.store import Store


def make(routes=None, cfg=P4, limit=100, key="SECRETKEY"):
    store, clock, tr, sleeps = Store(), Clock(), FakeTransport(routes, limit), []
    client = ApiFootballClient(store, cfg, key=key, transport=tr, sleep=sleeps.append, clock=clock)
    return client, tr, clock, sleeps


def test_cache_and_dedup():
    client, tr, clock, _ = make({"standings": lambda p: ok([{"league": {"standings": []}}])})
    a = client.get("standings", {"league": 39, "season": 2026})
    b = client.get("standings", {"season": 2026, "league": 39})
    assert a.status == "ok" and b.status in ("ok", "cached") and len(tr.calls) == 1
    client._memo.clear()
    assert client.get("standings", {"league": 39, "season": 2026}).status == "cached"
    clock.advance(hours=13)  # TTL tabulky 12 h
    client._memo.clear()
    client.get("standings", {"league": 39, "season": 2026})
    assert len(tr.calls) == 2


def test_empty_response_short_ttl():
    client, tr, clock, _ = make({"fixtures/lineups": lambda p: ok([])})
    assert client.get("fixtures/lineups", {"fixture": 1}).status == "empty"
    client._memo.clear()
    clock.advance(minutes=16)
    client.get("fixtures/lineups", {"fixture": 1})
    assert len(tr.calls) == 2


def test_retry_with_backoff_then_success():
    state = {"n": 0}

    def flaky(p):
        state["n"] += 1
        if state["n"] < 3:
            return 503, {"errors": [], "response": []}
        return ok([{"x": 1}])

    client, tr, _, sleeps = make({"teams": flaky})
    res = client.get("teams", {"id": 1})
    assert res.status == "ok" and len(tr.calls) == 3
    assert sleeps == [1.5, 3.0]


def test_network_error_gives_error_not_crash():
    client, tr, _, sleeps = make({"teams": lambda p: ConnectionError("down SECRETKEY")})
    res = client.get("teams", {"id": 1})
    assert res.status == "error" and "SECRETKEY" not in " ".join(res.errors)
    assert len(tr.calls) == P4.api.max_retries + 1


def test_missing_key():
    client, tr, _, _ = make(key="")
    import os

    os.environ.pop(P4.api.key_env, None)
    res = client.get("teams", {"id": 1})
    assert res.status == "no_key" and not tr.calls


def test_quota_exhausted_in_body():
    client, tr, _, _ = make({"teams": lambda p: (200, {"errors": {"requests": "You have reached the request limit for the day"}, "response": []})})
    res = client.get("teams", {"id": 1})
    assert res.status == "budget"
    assert client.budget.remaining() == 0
    assert client.get("teams", {"id": 2}).status == "budget" and len(tr.calls) == 1


def test_budget_priorities_and_fixture_cap():
    cfg = P4.with_(budget=BudgetConfig(daily_limit_fallback=100, max_per_fixture_per_day=2, max_per_run=50))
    client, tr, _, _ = make({}, cfg, limit=100)
    for i in range(2):
        assert client.get("odds", {"fixture": 7, "page": i + 1}, fixture_id=7).ok
    assert client.get("odds", {"fixture": 7, "page": 3}, fixture_id=7).status == "budget"
    # zbyva 98 z 100: LOW potrebuje rezervu 40 % -> projde; po vycerpani na 50 uz ne
    tr.used = 61
    client.get("teams", {"id": 1}, priority="MEDIUM")
    assert client.get("teams", {"id": 2}, priority="LOW").status == "budget"
    assert client.get("teams", {"id": 3}, priority="HIGH").ok


def test_run_limit():
    cfg = P4.with_(budget=BudgetConfig(max_per_run=2))
    client, _, _, _ = make({}, cfg)
    client.reset_run()
    assert client.get("a", {"x": 1}, priority="HIGH").ok and client.get("a", {"x": 2}, priority="HIGH").ok
    assert client.get("a", {"x": 3}, priority="HIGH").status == "budget"


def test_pagination():
    pages = {1: [{"p": 1}], 2: [{"p": 2}], 3: [{"p": 3}]}
    client, tr, _, _ = make({"odds": lambda p: ok(pages[int(p.get("page", 1))], 3, int(p.get("page", 1)))})
    res = client.get_all("odds", {"league": 39, "season": 2026})
    assert [r["p"] for r in res.response] == [1, 2, 3] and len(tr.calls) == 3
    client._memo.clear()
    again = client.get_all("odds", {"league": 39, "season": 2026})
    assert len(again.response) == 3 and len(tr.calls) == 3  # vse z cache


def test_api_log_has_no_key():
    client, _, _, _ = make({"teams": lambda p: ok([1])})
    client.get("teams", {"id": 1})
    rows = client.store.query("SELECT * FROM api_log")
    assert rows and all("SECRETKEY" not in str(dict(r)) for r in rows)


def test_provider_fixtures_filtered_and_status():
    ko = NOW + timedelta(hours=5)
    items = [fixture_item(1, "Arsenal", "Chelsea", ko), fixture_item(2, "X", "Y", ko, league_id=9999)]
    client, _, _, _ = make({"fixtures": lambda p: ok(items)})
    prov = DataProvider(client)
    got = prov.fixtures_on(NOW.date())
    assert got.status == AVAILABLE and [f.id for f in got.data] == [1]


def test_provider_lineups_not_yet_and_odds_not_yet():
    ko = NOW + timedelta(days=20)
    client, _, _, _ = make({"fixtures/lineups": lambda p: ok([]), "odds": lambda p: ok([])})
    prov = DataProvider(client)
    from tipsbot.profil4.provider import parse_fixture

    fx = parse_fixture(fixture_item(5, "A", "B", ko))
    assert prov.lineups(fx).status == NOT_YET
    assert prov.odds_fixture(fx).status == NOT_YET


def test_provider_budget_skip_status():
    cfg = P4.with_(budget=BudgetConfig(max_per_run=0))
    client, _, _, _ = make({}, cfg)
    client.reset_run(0)
    prov = DataProvider(client)
    assert prov.standings(39, 2026).status == SKIPPED


def test_competition_toggle():
    client, _, _, _ = make()
    prov = DataProvider(client)
    prov.set_competition(39, enabled=False)
    prov.set_competition(4321, enabled=True, priority=1, name="Moje liga")
    comps = {c.league_id: c for c in prov.competitions()}
    assert not comps[39].enabled and comps[4321].priority == 1


def test_coverage_report_quality():
    cov = CoverageReport(1)
    assert cov.quality() == 0.0
    for n in ("fixture", "model_data", "odds", "injuries", "lineups", "h2h", "predictions", "team_stats", "standings"):
        cov.set(n, AVAILABLE)
    assert cov.quality() == 1.0
    cov.set("lineups", NOT_YET)
    cov.set("odds", UNAVAILABLE)
    assert 0.5 < cov.quality() < 0.7 and "Kurzy" in cov.missing()


# --- kurzy ---------------------------------------------------------------

def test_parse_api_odds_markets_and_books():
    ko = NOW + timedelta(hours=6)
    item = odds_item(1, ko, {
        "Pinnacle": {"Match Winner": {"Home": 2.1, "Draw": 3.4, "Away": 3.6},
                     "Goals Over/Under": {"Over 2.5": 1.95, "Under 2.5": 1.9},
                     "Asian Handicap": {"Home -0.75": 2.05, "Away +0.75": 1.85, "Home -0.5,-1": 2.04},
                     "Both Teams Score": {"Yes": 1.8, "No": 2.0}, "Exotic": {"foo": 3.0}},
        "Tipsport": {"Match Winner": {"Home": 2.05, "Draw": 3.3, "Away": 3.5}},
    })
    q = p4odds.parse_api_odds([item], NOW, {1: "A - B"})
    keys = {(x.bookmaker, x.key) for x in q}
    assert ("pinnacle", "1X2:1") in keys and ("pinnacle", "OU_2.5:over") in keys
    assert ("pinnacle", "AH_-0.75:home") in keys and ("pinnacle", "AH_-0.75:away") in keys
    assert ("pinnacle", "BTTS:yes") in keys
    tip = [x for x in q if x.bookmaker == "tipsport"]
    assert tip and all("jinou zemskou verzi" in x.note for x in tip)  # API Tipsport neni automaticky cesky


def test_manual_csv_json_import_and_validation():
    ko = NOW + timedelta(hours=6)
    q, err = p4odds.parse_manual("1X2 | 2.10 3.30 3.60\nfortuna | OU_2.5 | 1.90 1.95\nU4.0 | 1.33\nBTTS | 0.9 1.2",
                                 7, "A - B", NOW, ko)
    assert len(q) == 3 + 2 + 1 and err and all(x.source == "manual" for x in q)
    assert {x.bookmaker for x in q} == {"tipsport", "fortuna"}
    csv_text = "fixture_id;bookmaker;market;selection;odds\n7;Tipsport;1X2;1;2,15\n7;Tipsport;OU_2.5;over;-3\n7;x;NEZNAMY;a;2"
    q2, err2 = p4odds.parse_csv(csv_text, NOW)
    assert len(q2) == 1 and q2[0].odds == 2.15 and len(err2) == 2
    q3, err3 = p4odds.parse_json('{"fixture_id": 7, "quotes": [{"bookmaker": "tipsport", "market": "BTTS", "selection": "yes", "odds": 1.8}]}', NOW)
    assert len(q3) == 1 and not err3
    _, err4 = p4odds.parse_json("{nope", NOW)
    assert err4


def test_quote_freshness_states():
    ko = NOW + timedelta(hours=3)
    api = p4odds.OddsQuote(1, "A - B", "pinnacle", "1X2", "1", 2.0, NOW, "api", ko, NOW - timedelta(hours=7))
    man = p4odds.OddsQuote(1, "A - B", "tipsport", "1X2", "1", 2.0, NOW, "manual", ko, NOW - timedelta(minutes=30))
    assert api.status(NOW, P4.odds) == p4odds.STALE
    assert man.status(NOW, P4.odds) == p4odds.CURRENT
    assert man.status(ko + timedelta(minutes=1), P4.odds) == p4odds.HISTORICAL
    best = p4odds.best_prices([api, man], NOW, P4.odds)
    assert best["1X2:1"].bookmaker == "tipsport"  # zastaraly kurz se jako aktualni nepouzije


def test_store_latest_and_before():
    st = Store()
    a = p4odds.OddsQuote(1, "m", "pinnacle", "1X2", "1", 2.0, NOW, "api", None, NOW - timedelta(hours=2))
    b = p4odds.OddsQuote(1, "m", "pinnacle", "1X2", "1", 1.9, NOW, "api", None, NOW - timedelta(hours=1))
    p4odds.save(st, [a, b])
    assert p4odds.latest(st, 1)[0].odds == 1.9
    assert p4odds.latest(st, 1, before=NOW - timedelta(minutes=90))[0].odds == 2.0


@pytest.mark.parametrize("bad", [1.0, 0.5, float("nan"), float("inf")])
def test_invalid_odds_rejected(bad):
    with pytest.raises(ValueError):
        p4odds.validate("1X2", "1", bad)
