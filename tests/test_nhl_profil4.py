"""NHL v Profilu 4, zahranicni overeni zaviraciho kurzu, vyhodnoceni tiketu NHL, brankari, kvota API a Profil 2
se zahranicnimi kurzy. Odpovedi API jsou ve tvaru skutecnych odpovedi (zkracene), nic se nestahuje."""

from datetime import date, datetime, timedelta, timezone

import pytest

from test_automation import SCORES
from tipsbot import nhl_data, nhl_settle, profile2
from tipsbot.profil4 import closing, foreign, history, nhl, odds as O
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store, iso
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
KO = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
GID = 2025020345


def _book(key, upd, ml=(1.90, 2.00), ou=(1.95, 1.95), pl=None):
    mk = [{"key": "h2h", "last_update": upd, "outcomes": [
        {"name": "Toronto Maple Leafs", "price": ml[0]}, {"name": "Los Angeles Kings", "price": ml[1]}]},
        {"key": "totals", "last_update": upd, "outcomes": [
            {"name": "Over", "price": ou[0], "point": 6.5}, {"name": "Under", "price": ou[1], "point": 6.5}]}]
    if pl:
        mk.append({"key": "spreads", "last_update": upd, "outcomes": [
            {"name": "Toronto Maple Leafs", "price": pl[0], "point": -1.5}, {"name": "Los Angeles Kings", "price": pl[1], "point": 1.5}]})
    return {"key": key, "last_update": upd, "markets": mk}


def event(upd="2026-11-21T11:30:00Z", soft_ml=(2.30, 1.70)):
    return {"id": "abc123", "sport_key": "icehockey_nhl", "commence_time": "2026-11-22T00:00:00Z",
            "home_team": "Toronto Maple Leafs", "away_team": "Los Angeles Kings",
            "bookmakers": [_book("pinnacle", upd, (1.95, 1.95), pl=(2.80, 1.45)), _book("williamhill", upd, (1.90, 1.90)),
                           _book("unibet_nl", upd, soft_ml), _book("matchbook", upd, (2.40, 1.75))]}


SCHEDULE = {"gameWeek": [{"date": "2026-11-21", "games": [
    {"id": GID, "startTimeUTC": "2026-11-22T00:00:00Z", "homeTeam": {"abbrev": "TOR"}, "awayTeam": {"abbrev": "LAK"}}]}]}


class FakeProvider:
    note = None

    def __init__(self, p_home=0.56):
        self.p = p_home

    def probabilities(self, ev):
        return {"MONEYLINE:home": self.p, "MONEYLINE:away": 1 - self.p, "OU_6.5:over": 0.5, "OU_6.5:under": 0.5,
                "PUCKLINE_1.5:home-1.5": 0.34, "PUCKLINE_1.5:away+1.5": 0.66}


def ledger_with(payload, when=NOW):
    led = Ledger(make_session_factory("sqlite://")())
    led.set_odds_cache("NHL", when.replace(tzinfo=None), payload, 400)
    return led


# --- kurzy a modely -------------------------------------------------------------

def test_quotes_keep_bookmaker_update_time_and_markets():
    qs = nhl.quotes_from_event(event(), GID, NOW)
    keys = {(q.bookmaker, q.key) for q in qs}
    assert ("pinnacle", "MONEYLINE:home") in keys and ("pinnacle", "OU_6.5:over") in keys
    assert ("pinnacle", "PUCKLINE_1.5:home-1.5") in keys
    assert all(q.stamp == datetime(2026, 11, 21, 11, 30, tzinfo=timezone.utc) and q.source == "oddsapi" for q in qs)


def test_market_b_prefers_pinnacle_without_margin():
    b = nhl.market_b(nhl.quotes_from_event(event(), GID, NOW))
    assert b["MONEYLINE:home"] == (pytest.approx(0.5), "Pinnacle bez marže")


def test_value_on_soft_book_is_approved_with_full_integrity():
    """Model A 56 %, trh 50 %, unibet 2.30 => EV C ~ +16 % nad trhem, potvrzeno burzou (bez provize se nesazi)."""
    led = ledger_with([event()])
    store = Store()
    scan = nhl.run(led, store, NOW, P4, schedule=lambda d: SCHEDULE, provider=FakeProvider(0.56), injuries=True)
    an = scan.analyses[0]
    c = next(c for c in an.candidates if c.key == "MONEYLINE:home")
    assert c.quote.bookmaker == "unibet_nl" and c.p["B"] == pytest.approx(0.5)
    assert c.p["C"] == pytest.approx(0.15 * 0.56 + 0.85 * 0.5)  # vaha modelu NHL ze zpetneho testu
    assert c.validation == ([], [])
    assert c.ev_market > P4.decision.max_market_edge and not c.confirmed_by  # matchbook je burza => nepotvrzuje
    assert c.decision != "APPROVED BET" and any("nezávisle" in r for r in c.reasons)
    rows = store.query("SELECT * FROM tips")
    assert rows and all(r["fixture_id"] == GID and r["league"] == "NHL" for r in rows)
    assert store.query("SELECT COUNT(*) n FROM predictions")[0]["n"] > 0


def test_approved_when_value_is_moderate_and_fresh():
    led = ledger_with([event(soft_ml=(2.12, 1.80))])
    store = Store()
    scan = nhl.run(led, store, NOW, P4, schedule=lambda d: SCHEDULE, provider=FakeProvider(0.56), injuries=True)
    c = next(c for c in scan.analyses[0].candidates if c.key == "MONEYLINE:home")
    assert c.decision == "APPROVED BET", c.reasons
    assert any("brankáři" in r for r in c.risks)


def test_book_below_100_percent_is_hard_error():
    ev = event()
    ev["bookmakers"][1] = _book("williamhill", "2026-11-21T11:30:00Z", (2.30, 2.30))
    store = Store()
    scan = nhl.run(ledger_with([ev]), store, NOW, P4, schedule=lambda d: SCHEDULE, provider=FakeProvider(0.5), injuries=True)
    bad = [c for c in scan.analyses[0].candidates if c.quote.bookmaker == "williamhill"]
    assert bad and all(c.validation[0] and c.decision == "NO BET" for c in bad)


def test_stale_cache_odds_are_never_approved():
    old = (NOW - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    scan = nhl.run(ledger_with([event(upd=old, soft_ml=(2.12, 1.80))]), Store(), NOW, P4, schedule=lambda d: SCHEDULE,
                   provider=FakeProvider(0.56), injuries=True)
    assert scan.analyses and not any(c.decision == "APPROVED BET" for c in scan.analyses[0].candidates)


def test_settle_nhl_tips_and_predictions_from_nhl_api():
    store = Store()
    nhl.run(ledger_with([event(soft_ml=(2.12, 1.80))]), store, NOW, P4, schedule=lambda d: SCHEDULE,
            provider=FakeProvider(0.56), injuries=True)
    games = {"games": [{**SCORES["games"][0], "id": GID, "startTimeUTC": "2026-11-22T00:00:00Z"}]}  # TOR-LAK 3:4 SO
    msgs = nhl.settle(store, KO + timedelta(hours=5), fetch=lambda d: games)
    rows = {r["selection"]: dict(r) for r in store.query("SELECT * FROM tips WHERE market='MONEYLINE'")}
    assert msgs and rows["home"]["result"] == "lost" and rows["home"]["score"].startswith("3:4")
    assert all(r["result"] for r in store.query("SELECT result FROM predictions WHERE market='MONEYLINE'"))


def test_football_settle_ignores_nhl_rows():
    store = Store()
    nhl.run(ledger_with([event()]), store, NOW, P4, schedule=lambda d: SCHEDULE, provider=FakeProvider(), injuries=True)

    class NoCall:
        def fixtures_by_ids(self, ids, priority=""):
            raise AssertionError(f"API-Football dotaz na NHL {ids}")

    assert history.settle(store, NoCall(), KO + timedelta(hours=5)) == []


def test_observe_nhl_fetches_event_only_for_approved_tip_in_last_slot():
    store = Store()
    led_factory = make_session_factory("sqlite://")
    with led_factory() as s:
        led = Ledger(s)
        old = (NOW - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
        led.set_odds_cache("NHL", NOW.replace(tzinfo=None), [event(upd=old)], 400)
        led.set_api_key("TEST")
        s.commit()
    store.execute("INSERT INTO tips(fixture_id, match, league_id, league, kickoff, market, selection, label, probs, odds, "
                  "bookmaker, odds_source, decision, predicted_at, model_version, config_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (GID, "Toronto Maple Leafs - Los Angeles Kings", 0, "NHL", iso(KO), "MONEYLINE", "home", "x", "{}",
                   2.1, "unibet_nl", "oddsapi", "APPROVED BET", iso(NOW), "v", "h"))
    calls = []
    fresh = event(upd=(KO - timedelta(minutes=12)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def fetch(sport, key, eid, markets, base):
        calls.append((sport, eid, markets))
        return fresh, {"remaining": 380, "used": 120}, ""

    r = {"fixture_id": GID, "match": "Toronto Maple Leafs - Los Angeles Kings", "kickoff": iso(KO), "league": "NHL",
         "league_id": 0, "approved": 1, "slots": [30]}
    nhl.observe(store, r, KO - timedelta(minutes=30), P4, ledger_factory=led_factory, fetch=fetch)
    assert not calls  # bod 30 min: jen kurzy z cache, zadne kredity
    nhl.observe(store, {**r, "slots": [10]}, KO - timedelta(minutes=10), P4, ledger_factory=led_factory, fetch=fetch)
    assert calls == [("icehockey_nhl", "abc123", ["h2h"])]
    with led_factory() as s:
        assert Ledger(s).odds_api_quota()["remaining"] == 380
    t = dict(store.query("SELECT * FROM tips")[0])
    cl = history._closing(store, GID, "unibet_nl", "MONEYLINE", "home", KO, NOW)
    assert cl["kind"] == history.CK_CLOSING and cl["odds"] == 2.30, cl
    assert t["result"] is None


def test_observe_nhl_respects_credit_reserve():
    store = Store()
    led_factory = make_session_factory("sqlite://")
    with led_factory() as s:
        led = Ledger(s)
        led.set_odds_cache("NHL", NOW.replace(tzinfo=None), [event(upd="2026-11-21T00:00:00Z")], 10)
        led.set_odds_api_quota(10, 490, NOW.replace(tzinfo=None))
        led.set_api_key("TEST")
        s.commit()
    store.execute("INSERT INTO tips(fixture_id, match, league_id, league, kickoff, market, selection, label, probs, odds, "
                  "bookmaker, odds_source, decision, predicted_at, model_version, config_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (GID, "Toronto Maple Leafs - Los Angeles Kings", 0, "NHL", iso(KO), "MONEYLINE", "home", "x", "{}",
                   2.1, "unibet_nl", "oddsapi", "APPROVED BET", iso(NOW), "v", "h"))
    r = {"fixture_id": GID, "match": "Toronto Maple Leafs - Los Angeles Kings", "kickoff": iso(KO), "league": "NHL",
         "league_id": 0, "approved": 1, "slots": [10]}
    msg = nhl.observe(store, r, KO - timedelta(minutes=10), P4, ledger_factory=led_factory,
                      fetch=lambda *a: pytest.fail("pod rezervou se nestahuje"))
    assert "rezerva" in msg
    assert store.query("SELECT status FROM odds_observations")[0]["status"] == "SKIPPED_BUDGET"


# --- zahranicni overeni zaviraciho kurzu (fotbal) ----------------------------------

SOCCER_EVENT = {"id": "ev1", "home_team": "Bayern Munich", "away_team": "FC Koln", "commence_time": "2026-11-22T00:00:00Z",
                "bookmakers": [{"key": "pinnacle", "markets": [
                    {"key": "h2h", "last_update": "2026-11-21T23:52:00Z", "outcomes": [
                        {"name": "Bayern Munich", "price": 1.30}, {"name": "FC Koln", "price": 9.5}, {"name": "Draw", "price": 6.0}]},
                    {"key": "totals", "last_update": "2026-11-21T23:52:00Z", "outcomes": [
                        {"name": "Over", "price": 1.80, "point": 3.5}, {"name": "Under", "price": 2.05, "point": 3.5}]},
                    {"key": "spreads", "last_update": "2026-11-21T23:52:00Z", "outcomes": [
                        {"name": "Bayern Munich", "price": 1.95, "point": -1.75}, {"name": "FC Koln", "price": 1.90, "point": 1.75}]}]}]}


def test_parse_soccer_event_odds_uses_home_handicap_convention():
    qs = {q.key: q for q in foreign.parse_event_odds(SOCCER_EVENT, 1, "Bayern - Köln", KO, NOW)}
    assert qs["1X2:1"].odds == 1.30 and qs["1X2:X"].odds == 6.0 and qs["OU_3.5:under"].odds == 2.05
    assert qs["AH_-1.75:home"].odds == 1.95 and qs["AH_-1.75:away"].odds == 1.90  # cislo = handicap domacich


def test_stale_api_football_closing_is_verified_by_pinnacle():
    store = Store()
    fid, rec = 55, KO - timedelta(hours=6)
    O.save(store, [O.OddsQuote(fid, "x", "bet365", "1X2", s, o, rec, "api", KO, rec)
                   for s, o in (("1", 1.40), ("X", 5.0), ("2", 8.0))])
    store.execute("INSERT INTO odds_observations(fixture_id, kickoff, slot_min, observed_at, status, n_quotes, latest_update, "
                  "note) VALUES(?,?,?,?,?,?,?,?)", (fid, iso(KO), 10, iso(KO - timedelta(minutes=10)), "AVAILABLE", 3, iso(rec), ""))
    assert history._closing(store, fid, "bet365", "1X2", "1", KO, rec)["kind"] == history.CK_STALE
    O.save(store, foreign.parse_event_odds(SOCCER_EVENT, fid, "x", KO, NOW))
    cl = history._closing(store, fid, "bet365", "1X2", "1", KO, rec)
    assert cl["kind"] == history.CK_FOREIGN and cl["odds"] == 1.30 and "pinnacle" in cl["reason"]
    assert history._clv(1.40, cl) > 0


def test_verify_foreign_only_for_approved_last_slot_and_stale_source():
    store = Store()
    store.execute("INSERT INTO tips(fixture_id, match, league_id, league, kickoff, market, selection, label, probs, odds, "
                  "bookmaker, odds_source, decision, predicted_at, model_version, config_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (55, "Bayern - Köln", 78, "Bundesliga", iso(KO), "AH_-1.75", "home", "x", "{}", 2.0, "bet365", "api",
                   "APPROVED BET", iso(NOW), "v", "h"))
    led_factory = make_session_factory("sqlite://")
    with led_factory() as s:
        Ledger(s).set_api_key("TEST")
        s.commit()
    calls = []

    def fetch(sport, key, home, away, ko, markets, fid, match, now, base):
        calls.append((sport, markets))
        return foreign.parse_event_odds(SOCCER_EVENT, fid, match, ko, now), {"remaining": 300, "used": 200}, ""

    r = {"fixture_id": 55, "match": "Bayern - Köln", "league_id": 78, "approved": 1, "slots": [10]}
    fresh = KO - timedelta(minutes=20)
    assert closing.verify_foreign(store, r, KO, KO, fresh, P4, fetch, led_factory) == ""  # API-Football cerstve
    assert closing.verify_foreign(store, {**r, "slots": [30]}, KO, KO, None, P4, fetch, led_factory) == ""
    note = closing.verify_foreign(store, r, KO, KO, KO - timedelta(hours=5), P4, fetch, led_factory)
    assert calls == [("soccer_germany_bundesliga", ["spreads"])] and "3 kurzů" not in note and "nových" in note


# --- tikety NHL, brankari, kvota, Profil 2 ----------------------------------------

def test_manual_nhl_tickets_are_settled_from_nhl_api():
    led = Ledger(make_session_factory("sqlite://")())
    led.deposit(1000)
    day = date(2026, 11, 20)  # cesky den; zapas zacal 20.11. 00:00 UTC
    t1 = led.add_ticket(bookmaker="tipsport", odds=2.0, stake=100, match_id="m1", market="1X2", selection="X",
                        match_day=day, match_name="Toronto Maple Leafs - Los Angeles Kings")
    t2 = led.add_ticket(bookmaker="tipsport", odds=1.8, stake=100, match_id="m2", market="MONEYLINE", selection="home",
                        match_day=day, match_name="Boston Bruins - Montréal Canadiens")
    t3 = led.add_ticket(bookmaker="tipsport", odds=1.9, stake=100, match_id="m3", market="OU60_4.5", selection="under",
                        match_day=day, match_name="Edmonton Oilers - Calgary Flames")
    notes = nhl_settle.settle_tickets(led, datetime(2026, 11, 20, 12), fetch=lambda d: SCORES)
    assert {t.id: t.status for t in (t1, t2, t3)} == {t1.id: "won", t2.id: "won", t3.id: "lost"}
    assert len(notes) == 3 and "SO" in notes[0]


def test_panel_nhl_ticket_markets_are_understood():
    from tipsbot.web import _manual_bet

    assert _manual_bet("MONEYLINE", "", "away") == ("MONEYLINE", "away")
    assert _manual_bet("NHL_OU60", "5.5", "over") == ("OU60_5.5", "over")
    assert _manual_bet("PUCKLINE", "", "away-1.5") == ("PUCKLINE_1.5R", "away-1.5")
    assert _manual_bet("REG", "", "X") == ("REG", "X")
    with pytest.raises(ValueError):
        _manual_bet("MONEYLINE", "", "draw")


def test_goalie_override_uses_chosen_goalie(tmp_path):
    p = tmp_path / "goalies.csv"
    p.write_text("name,team,situation,games_played,xGoals,goals,ongoal\n"
                 "Star,TOR,all,15,40,30,400\nBackup,TOR,all,5,12,16,130\nStar,TOR,5on5,15,30,22,300\n", encoding="utf-8")
    base = nhl_data.team_goalies(p)["TOR"]
    chosen = nhl_data.team_goalies(p, {"TOR": "Backup"})["TOR"]
    assert chosen.gsax == pytest.approx(-4.0) and chosen.shots == pytest.approx(130) and base.gsax > 0
    assert nhl_data.goalie_roster(p)["TOR"] == [("Star", 15), ("Backup", 5)]
    led = Ledger(make_session_factory("sqlite://")())
    led.set_goalie_override("TOR", "Backup")
    assert led.goalie_overrides() == {"TOR": "Backup"}
    led.set_goalie_override("TOR", None)
    assert led.goalie_overrides() == {}


def test_odds_api_quota_today_usage():
    led = Ledger(make_session_factory("sqlite://")())
    led.set_odds_api_quota(400, 100, datetime(2026, 11, 20, 20))
    led.set_odds_api_quota(380, 120, datetime(2026, 11, 21, 8))
    led.set_odds_api_quota(372, None, datetime(2026, 11, 21, 9))  # vysledky z kurzoveho API davaji jen zbytek
    q = led.odds_api_quota()
    assert q["remaining"] == 372 and q["used"] == 120 and q["today"] == 20


def test_profile2_uses_foreign_odds_without_czech_input():
    led = Ledger(make_session_factory("sqlite://")())
    payload = [{"id": "e1", "home_team": "Arsenal", "away_team": "Chelsea", "commence_time": "2026-11-22T15:00:00Z",
                "bookmakers": [{"key": k, "markets": [
                    {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": h}, {"name": "Chelsea", "price": 4.0},
                                                 {"name": "Draw", "price": 3.6}]},
                    {"key": "totals", "outcomes": [{"name": "Over", "price": 1.9, "point": 2.5},
                                                   {"name": "Under", "price": 1.95, "point": 2.5}]}]}
                    for k, h in (("pinnacle", 1.95), ("williamhill", 1.90), ("unibet_nl", 2.0))]}]
    led.set_odds_cache("ENG1", datetime(2026, 11, 21, 9), payload, 400)
    entries, src = profile2.foreign_entries(led, "Arsenal - Chelsea", datetime(2026, 11, 21, 10))
    by = {e.market: e for e in entries}
    assert by["1X2"].odds == (1.95, 3.6, 4.0) and by["OU_2.5"].odds == (1.9, 1.95)
    assert "kurzové API" in src and all(e.book.startswith("zahraničí") for e in entries)
    a = profile2.analyse("Arsenal - Chelsea", entries, (1.6, 1.0), "test")
    assert a.offers


def test_espn_injuries_use_reachable_host():
    from tipsbot import context

    assert context.ESPN_NHL_INJURIES.startswith("https://site.web.api.espn.com/")


def test_panel_goalie_override_and_pages_render(tmp_path, monkeypatch):
    from tipsbot.web import create_app

    monkeypatch.setattr(nhl_data, "goalie_roster", lambda path=None: {"TOR": [("Star", 15), ("Backup", 5)]})
    url = f"sqlite:///{tmp_path / 'p.db'}"
    c = create_app(url).test_client()
    r = c.post("/nhl/goalie", data={"tab": "nastaveni", "team": "TOR", "name": "Backup"})
    assert r.status_code == 200 and "Backup" in r.get_data(as_text=True)
    assert c.post("/nhl/goalie", data={"tab": "nastaveni", "team": "TOR", "name": "Nikdo"}).status_code == 400
    with make_session_factory(url)() as s:
        assert Ledger(s).goalie_overrides() == {"TOR": "Backup"}
    for tab in ("prehled", "profil4", "tikety", "profil2"):
        page = c.get(f"/?tab={tab}")
        assert page.status_code == 200, tab
    assert "Spotřeba API" in c.get("/?tab=prehled").get_data(as_text=True)
    assert "NHL" in c.get("/?tab=profil4").get_data(as_text=True)
