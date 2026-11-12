from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from tipsbot import nhl_data, oddsapi, pipeline
from tipsbot.models import nhl
from tipsbot.names import match_team, nhl_abbr, normalize

NOW = datetime(2026, 11, 14, 12, 0)


def _book(key, h2h, totals=None):
    markets = [{"key": "h2h", "outcomes": [{"name": n, "price": p} for n, p in h2h.items()]}]
    if totals:
        markets.append({"key": "totals", "outcomes": [
            {"name": "Over", "price": totals[0], "point": 2.5}, {"name": "Under", "price": totals[1], "point": 2.5}]})
    return {"key": key, "title": key, "markets": markets}


def _payload(kick=NOW + timedelta(minutes=90), soft_home=2.25):
    h, a = "Arsenal", "Manchester United"
    return [{
        "id": "abc", "sport_key": "soccer_epl", "commence_time": kick.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "home_team": h, "away_team": a,
        "bookmakers": [
            _book("pinnacle", {h: 2.10, a: 3.60, "Draw": 3.40}, (1.95, 1.95)),
            _book("betfair_ex_eu", {h: 2.14, a: 3.70, "Draw": 3.50}),
            _book("unibet_eu", {h: soft_home, a: 3.30, "Draw": 3.20}, (1.90, 1.90)),
            _book("williamhill", {h: soft_home - 0.05, a: 3.40, "Draw": 3.25}, (1.88, 1.92)),
            _book("betsson", {h: soft_home, a: 3.30, "Draw": 3.20}, (1.90, 1.90)),
            _book("broken", {h: 2.0}),
        ],
    }]


def test_parse_events_and_split():
    (ev,) = oddsapi.parse_events(_payload(), "ENG1")
    assert ev.match_id == "oddsapi:abc" and ev.kickoff == NOW + timedelta(minutes=90)
    assert set(ev.markets) == {"1X2", "OU_2.5"}
    m = ev.markets["1X2"]
    assert m.books["pinnacle"] == {"1": 2.10, "X": 3.40, "2": 3.60}
    assert "broken" not in m.books
    sharp, offers = oddsapi.sharp_and_offers(m)
    assert sharp == {"pinnacle": [2.10, 3.40, 3.60], "betfair_exchange": [2.14, 3.50, 3.70]}
    assert ("unibet_eu", 2.25) in offers["1"] and all(b != "pinnacle" for b, _ in offers["1"])


def test_parse_nhl_moneyline():
    payload = [{"id": "n1", "commence_time": "2026-11-15T00:00:00Z", "home_team": "Boston Bruins",
                "away_team": "Toronto Maple Leafs",
                "bookmakers": [_book("pinnacle", {"Boston Bruins": 1.95, "Toronto Maple Leafs": 1.95})]}]
    (ev,) = oddsapi.parse_events(payload, "NHL")
    assert ev.markets["MONEYLINE"].books["pinnacle"] == {"home": 1.95, "away": 1.95}


def test_missing_key_raises():
    with pytest.raises(oddsapi.OddsApiError):
        oddsapi.fetch_raw("ENG1")


class _NoModel:
    note = "bez modelu"
    unmatched: set = set()

    def probabilities(self, ev):
        return None


def test_scan_issues_tip_then_cancels_when_value_disappears(ledger):
    events = oddsapi.parse_events(_payload(), "ENG1")
    rep = pipeline.scan_league("ENG1", ledger, now=NOW, events=events, provider=_NoModel())
    assert rep.events == 1 and rep.with_fair_line == 1 and rep.with_model == 0
    assert len(rep.new_tips) == 1
    tip = rep.new_tips[0]
    assert tip.bookmaker.startswith("medián 3") and tip.selection == "1" and tip.raw_odds == 2.25
    assert tip.min_odds < 2.25 and "bez modelu" in rep.summary()

    # stejny stav o 2 minuty pozdeji: nic noveho
    rep2 = pipeline.scan_league("ENG1", ledger, now=NOW + timedelta(minutes=2), events=events, provider=_NoModel())
    assert not rep2.new_tips and rep2.cancelled == 0

    # kurz spadl -> tip se zrusi
    dropped = oddsapi.parse_events(_payload(soft_home=2.08), "ENG1")
    rep3 = pipeline.scan_league("ENG1", ledger, now=NOW + timedelta(minutes=4), events=dropped, provider=_NoModel())
    assert rep3.cancelled == 1 and tip.status == "cancelled"


def test_scan_ignores_matches_outside_horizon(ledger):
    far = oddsapi.parse_events(_payload(kick=NOW + timedelta(days=3)), "ENG1")
    rep = pipeline.scan_league("ENG1", ledger, now=NOW, events=far, provider=_NoModel())
    assert rep.events == 0 and not rep.new_tips


def test_team_name_matching():
    known = ["Man United", "Man City", "Arsenal", "Wolves", "Nott'm Forest", "Brighton", "Newcastle", "Ath Madrid",
             "Sociedad", "Bayern Munich", "M'gladbach", "Paris SG", "Sp Lisbon", "Real Madrid", "Barcelona"]
    assert match_team("Manchester United", known) == "Man United"
    assert match_team("Wolverhampton Wanderers", known) == "Wolves"
    assert match_team("Brighton and Hove Albion", known) == "Brighton"
    assert match_team("Atlético Madrid", known) == "Ath Madrid"
    assert match_team("Borussia Monchengladbach", known) == "M'gladbach"
    assert match_team("Paris Saint Germain", known) == "Paris SG"
    assert match_team("FC Barcelona", known) == "Barcelona"
    assert match_team("Real Madrid", known) == "Real Madrid"
    assert match_team("Arsenal", known) == "Arsenal"
    assert match_team("Neznamy Klub", known) is None
    assert normalize("1. FC Köln") == "koln"
    assert nhl_abbr("Boston Bruins") == "BOS" and nhl_abbr("Nikdo") is None


def _nhl_rows():
    rows = []
    teams = {"BOS": (3.4, 2.6), "TOR": (3.0, 3.0), "L.A": (2.6, 3.2)}
    for season, n in ((2025, 30), (2026, 12)):
        for g in range(n):
            d = date(season, 10, 8) + timedelta(days=2 * g)
            for t, (xf, xa) in teams.items():
                opp = "TOR" if t != "TOR" else "BOS"
                for sit, k in (("all", 1.0), ("5on5", 0.8)):
                    rows.append({"team": t, "season": season, "gameId": g, "opposingTeam": opp,
                                 "home_or_away": "HOME" if g % 2 else "AWAY", "gameDate": int(d.strftime("%Y%m%d")),
                                 "situation": sit, "iceTime": 3600 * k, "xGoalsFor": xf * k, "xGoalsAgainst": xa * k,
                                 "unblockedShotAttemptsFor": 42 * k, "unblockedShotAttemptsAgainst": 42 * k,
                                 "goalsFor": 3, "goalsAgainst": 3, "extra": 1})
    return pd.DataFrame(rows)


def test_nhl_ratings_from_moneypuck_format(tmp_path):
    f = tmp_path / "all_teams.csv"
    _nhl_rows().to_csv(f, index=False)
    df = nhl_data.load_nhl(f)
    assert set(df["situation"]) == {"all"} and "LAK" in set(df["team"])
    r = nhl_data.NHLRatings(df, date(2026, 11, 14))
    assert r.ratings["BOS"].xgf60 == pytest.approx(3.4) and r.ratings["BOS"].games == 12
    assert r.league_xg60 == pytest.approx(3.0, abs=0.05)
    ctx = r.context("BOS", "LAK", date(2026, 11, 14))
    lam_h, lam_a = nhl.expected_goals(ctx)
    assert lam_h > lam_a
    assert r.context("BOS", "XXX", date(2026, 11, 14)) is None
    with pytest.raises(FileNotFoundError):
        nhl_data.load_nhl(tmp_path / "nic.csv")


def test_b2b_requires_time_zone_change(tmp_path):
    f = tmp_path / "all_teams.csv"
    _nhl_rows().to_csv(f, index=False)
    df = nhl_data.load_nhl(f)
    r = nhl_data.NHLRatings(df, date(2026, 11, 14))
    last = max(d for d in df[df["team"] == "LAK"]["date"])
    row = df[(df["team"] == "LAK") & (df["date"] == last)].iloc[-1]
    prev_venue = "LAK" if row["home_or_away"] == "HOME" else row["opposingTeam"]
    nxt = last + timedelta(days=1)
    same_zone = "ANA" if prev_venue == "LAK" else "BOS" if prev_venue == "TOR" else prev_venue
    other_zone = "BOS" if prev_venue == "LAK" else "LAK"
    assert not r.b2b_travel("LAK", nxt, same_zone)
    assert r.b2b_travel("LAK", nxt, other_zone)
    assert not r.b2b_travel("LAK", nxt + timedelta(days=3), other_zone)


def test_league_without_matches_is_skipped_to_save_credits(ledger, monkeypatch):
    calls = []

    def fake_fetch(league, settings=None, api_key=None):
        calls.append(league)
        return oddsapi.FetchResult(oddsapi.parse_events(_payload(kick=NOW + timedelta(days=3)), "ENG1"), 480, 20)

    monkeypatch.setattr(oddsapi, "fetch_events", fake_fetch)
    pipeline.scan_league("ENG1", ledger, now=NOW, provider=_NoModel())
    rep = pipeline.scan_league("ENG1", ledger, now=NOW + timedelta(hours=5), provider=_NoModel())
    assert calls == ["ENG1"] and "preskoceno" in rep.summary()
    # den pred zapasem se uz stahuje znovu
    pipeline.scan_league("ENG1", ledger, now=NOW + timedelta(days=2, hours=2), provider=_NoModel())
    assert len(calls) == 2


def test_puckline_parsed_and_total_anchor():
    h, a = "Boston Bruins", "Toronto Maple Leafs"
    spreads = {"key": "spreads", "outcomes": [
        {"name": h, "price": 2.5, "point": -1.5}, {"name": a, "price": 1.55, "point": 1.5}]}
    spreads2 = {"key": "spreads", "outcomes": [
        {"name": h, "price": 1.5, "point": 1.5}, {"name": a, "price": 2.6, "point": -1.5}]}
    totals = {"key": "totals", "outcomes": [
        {"name": "Over", "price": 1.95, "point": 6.5}, {"name": "Under", "price": 1.95, "point": 6.5}]}
    payload = [{"id": "n1", "commence_time": "2026-11-15T00:00:00Z", "home_team": h, "away_team": a,
                "bookmakers": [{"key": "pinnacle", "markets": [spreads, spreads2, totals]}]}]
    (ev,) = oddsapi.parse_events(payload, "NHL")
    assert ev.markets["PUCKLINE_1.5"].books["pinnacle"] == {"home-1.5": 2.5, "away+1.5": 1.55}
    assert ev.markets["PUCKLINE_1.5R"].books["pinnacle"] == {"home+1.5": 1.5, "away-1.5": 2.6}
    key, p = pipeline._total_anchor(ev, pipeline.SETTINGS)
    assert key == "OU_6.5:over" and abs(p - 0.5) < 1e-9
    ctx = nhl.GameContext(nhl.TeamRating(3.6, 3.0), nhl.TeamRating(3.5, 3.1), 3.0)
    free = nhl.market_probabilities(ctx, seed=1)
    anch = nhl.market_probabilities(ctx, total_anchor=(key, 0.42), seed=1)
    assert free["OU_6.5:over"] > 0.5 and abs(anch["OU_6.5:over"] - 0.42) < 0.02


def test_scan_stores_analysis_and_panel_renders(ledger):
    from tipsbot import analysis, web

    kick = NOW + timedelta(hours=30)
    events = oddsapi.parse_events(_payload(kick=kick), "ENG1")
    pipeline.scan_league("ENG1", ledger, now=NOW, events=events, provider=pipeline.ModelProvider("ENG1", NOW.date()))
    views = analysis.loads(ledger.analysis("ENG1"))
    assert views == [] or all(v.rows for v in views)  # mimo 24 h horizont se nezobrazuje
    events = oddsapi.parse_events(_payload(kick=NOW + timedelta(hours=5)), "ENG1")
    pipeline.scan_league("ENG1", ledger, now=NOW, events=events, provider=pipeline.ModelProvider("ENG1", NOW.date()))
    (v,) = analysis.loads(ledger.analysis("ENG1"))
    assert v.rows and v.recommended is not None and v.verdict
    assert {r.market for r in v.rows} >= {"1X2", "OU_2.5"}


def test_context_parsers_and_panel_detail(ledger):
    from tipsbot import context

    inj = context.parse_espn_injuries({"injuries": [{"displayName": "Boston Bruins", "injuries": [
        {"athlete": {"displayName": "A. Player", "position": {"abbreviation": "C"}}, "status": "Out", "shortComment": "knee"}]}]})
    assert inj[0].line() == "Boston Bruins: A. Player (C) – Out (knee)" and context.severity(inj) == 1
    assert context.for_match(inj, "Boston Bruins", "Toronto Maple Leafs") == inj
    af = context.parse_api_football_injuries({"response": [{"player": {"name": "X", "type": "Missing Fixture", "reason": "Knee"}, "team": {"name": "Arsenal"}}]})
    assert af[0].line() == "Arsenal: X – Missing Fixture (Knee)"
    hourly = {"time": ["2026-11-14T15:00"], "temperature_2m": [8], "precipitation": [3.0], "precipitation_probability": [90], "wind_speed_10m": [20]}
    assert "těžký déšť" in context.describe_weather(hourly, "2026-11-14T15:00")
    rss = "<rss><channel><item><title>Star out for derby</title></item></channel></rss>"
    assert context.parse_news_rss(rss) == ["Star out for derby"]


def test_at_least_ten_markets_per_match(ledger):
    from tipsbot import analysis

    events = oddsapi.parse_events(_payload(kick=NOW + timedelta(hours=5)), "ENG1")
    pipeline.scan_league("ENG1", ledger, now=NOW, events=events, provider=pipeline.ModelProvider("ENG1", NOW.date()))
    probs = {"1X2:1": 0.45, "1X2:X": 0.27, "1X2:2": 0.28, "DC:1X": 0.72, "DC:X2": 0.55, "DC:12": 0.73,
             "DNB:home": 0.62, "DNB:away": 0.38, "OU_1.5:over": 0.78, "OU_1.5:under": 0.22, "OU_2.5:over": 0.52,
             "OU_2.5:under": 0.48, "BTTS:yes": 0.55, "BTTS:no": 0.45, "TTH_1.5:over": 0.45}
    rows = analysis.model_rows(probs, set(), "A", "B", 0.025)
    assert len(rows) >= 10 and all(r.min_odds >= 1.45 and r.status == "model" for r in rows)
    assert rows[0].p_model >= rows[-1].p_model


def test_whole_number_total_line_does_not_crash_scan():
    h, a = "Boston Bruins", "Toronto Maple Leafs"
    totals = {"key": "totals", "outcomes": [
        {"name": "Over", "price": 1.95, "point": 6.0}, {"name": "Under", "price": 1.95, "point": 6.0}]}
    h2h = {"key": "h2h", "outcomes": [{"name": h, "price": 1.9}, {"name": a, "price": 1.95}]}
    payload = [{"id": "n2", "commence_time": "2026-11-15T00:00:00Z", "home_team": h, "away_team": a,
                "bookmakers": [{"key": "pinnacle", "markets": [h2h, totals]}]}]
    (ev,) = oddsapi.parse_events(payload, "NHL")
    assert pipeline._total_anchor(ev, pipeline.SETTINGS) is None


def test_odds_cache_saves_credits_and_force_refetches(ledger, monkeypatch):
    calls = []

    def fake_fetch(league, settings=None, api_key=None):
        calls.append(league)
        pl = _payload(kick=NOW + timedelta(hours=5))
        return oddsapi.FetchResult(oddsapi.parse_events(pl, "ENG1"), 480, 3, pl)

    monkeypatch.setattr(oddsapi, "fetch_events", fake_fetch)
    pipeline.scan_league("ENG1", ledger, now=NOW, provider=_NoModel())
    rep = pipeline.scan_league("ENG1", ledger, now=NOW + timedelta(minutes=3), provider=_NoModel())
    assert len(calls) == 1 and "z cache" in rep.summary() and rep.events == 1
    pipeline.scan_league("ENG1", ledger, now=NOW + timedelta(minutes=4), provider=_NoModel(), force=True)
    pipeline.scan_league("ENG1", ledger, now=NOW + timedelta(minutes=30), provider=_NoModel())
    assert len(calls) == 3


def test_scan_all_survives_one_league_failing(ledger, monkeypatch):
    def boom(league, settings=None, api_key=None):
        raise RuntimeError("spadlo")

    monkeypatch.setattr(oddsapi, "fetch_events", boom)
    reps = pipeline.scan_all(ledger, ["ENG1", "ESP1"])
    assert len(reps) == 2 and all("chyba skenu" in r.summary() for r in reps)


def test_team_goalies_from_moneypuck_format(tmp_path):
    from tipsbot import nhl_data
    from tipsbot.models.nhl import goalie_gsax60

    csv = tmp_path / "goalies.csv"
    pd.DataFrame([
        {"name": "A", "team": "BOS", "situation": "all", "games_played": 20, "xGoals": 50.0, "goals": 40.0, "ongoal": 600},
        {"name": "B", "team": "BOS", "situation": "all", "games_played": 10, "xGoals": 25.0, "goals": 28.0, "ongoal": 300},
        {"name": "C", "team": "T.B", "situation": "all", "games_played": 30, "xGoals": 80.0, "goals": 90.0, "ongoal": 900},
        {"name": "A", "team": "BOS", "situation": "5on5", "games_played": 20, "xGoals": 1.0, "goals": 9.0, "ongoal": 1},
    ]).to_csv(csv, index=False)
    g = nhl_data.team_goalies(csv)
    assert set(g) == {"BOS", "TBL"} and g["BOS"].gsax > 0 > g["TBL"].gsax
    assert goalie_gsax60(g["BOS"]) > 0
    assert nhl_data.team_goalies(tmp_path / "none.csv") == {}
    assert nhl_data.current_goalie_season(date(2026, 11, 11)) == 2026 and nhl_data.current_goalie_season(date(2027, 2, 1)) == 2026


def test_nhl_full_scan_end_to_end(tmp_path, ledger, monkeypatch):
    """Cely NHL beh: data -> model s brankari -> rozbor se sekcemi a doplnkovymi trhy."""
    from dataclasses import replace

    from tipsbot import analysis, nhl_data

    (tmp_path / "nhl").mkdir()
    _nhl_rows().to_csv(tmp_path / "nhl" / "all_teams.csv", index=False)
    pd.DataFrame([{"name": "G", "team": "BOS", "situation": "all", "games_played": 12, "xGoals": 30.0, "goals": 25.0, "ongoal": 350},
                  {"name": "H", "team": "LAK", "situation": "all", "games_played": 12, "xGoals": 30.0, "goals": 33.0, "ongoal": 340}]
                 ).to_csv(tmp_path / "nhl" / "goalies.csv", index=False)
    st = replace(pipeline.SETTINGS, data_dir=tmp_path)
    monkeypatch.setattr(nhl_data, "SETTINGS", st)
    h, a = "Boston Bruins", "Los Angeles Kings"
    kick = (NOW + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def book(key, hp, ap, op, up):
        return {"key": key, "markets": [
            {"key": "h2h", "outcomes": [{"name": h, "price": hp}, {"name": a, "price": ap}]},
            {"key": "totals", "outcomes": [{"name": "Over", "price": op, "point": 6.5}, {"name": "Under", "price": up, "point": 6.5}]}]}

    payload = [{"id": "x", "commence_time": kick, "home_team": h, "away_team": a, "bookmakers": [
        book("pinnacle", 1.80, 2.10, 1.95, 1.95), book("unibet_eu", 1.85, 2.05, 1.95, 1.9), book("betsson", 1.86, 2.0, 1.9, 1.95)]}]
    events = oddsapi.parse_events(payload, "NHL")
    provider = pipeline.ModelProvider("NHL", NOW.date(), st)
    assert provider.ratings is not None and provider.ratings.goalies["BOS"].gsax > 0
    pipeline.scan_league("NHL", ledger, now=NOW, events=events, provider=provider, settings=st)
    (v,) = analysis.loads(ledger.analysis("NHL"))
    titles = [s["title"] for s in v.sections]
    assert v.has_model and any("Brankáři" in t for t in titles) and any("Síla týmů" in t for t in titles)
    assert len(v.rows) >= 10 and v.verdict


def test_football_full_scan_end_to_end(ledger, monkeypatch):
    """Cely fotbalovy beh se syntetickou ligou: model, kalibrace, rozbor, doplnkove trhy, cilovy tip."""
    from tests.conftest import simulate_league
    from tipsbot import analysis, calibration, ingestion

    df, truth = simulate_league(n_teams=6, rounds=6, seed=2, start=date(2026, 5, 20))
    names = {truth["teams"][0]: "Arsenal", truth["teams"][1]: "Man United", truth["teams"][2]: "Chelsea",
             truth["teams"][3]: "Liverpool", truth["teams"][4]: "Everton", truth["teams"][5]: "Fulham"}
    df["home"], df["away"] = df["home"].map(names), df["away"].map(names)
    monkeypatch.setattr(ingestion, "load_football", lambda league, data_dir=None: df)
    sc = {k: calibration.PlattScaler(0.9, 0.0) for k in calibration.OUTCOMES}
    monkeypatch.setattr(calibration, "load_scalers", lambda path: sc)
    provider = pipeline.ModelProvider("ENG1", NOW.date())
    assert provider.params is not None and provider.scalers is sc
    events = oddsapi.parse_events(_payload(kick=NOW + timedelta(hours=5)), "ENG1")
    pipeline.scan_league("ENG1", ledger, now=NOW, events=events, provider=provider)
    (v,) = analysis.loads(ledger.analysis("ENG1"))
    assert v.has_model and len(v.rows) >= 10 and v.sections
    assert any("Forma" in s["title"] for s in v.sections) and any("vzájemné" in s["title"] for s in v.sections)
    p = provider.probabilities(events[0])
    assert abs(p["1X2:1"] + p["1X2:X"] + p["1X2:2"] - 1) < 1e-9
