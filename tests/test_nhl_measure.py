"""Phase 3.5.1 / M1 – merici evidence NHL modelu (profil4/nhl_measure.py).

Skutecny NHL model (ModelProvider + NHLRatings) nad syntetickymi daty ve formatu MoneyPuck, kurzy ve tvaru
odpovedi kurzoveho API. Nic se nestahuje (pojistky _no_network a _no_paid_api v conftest.py)."""

import json
import sqlite3
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from test_nhl_profil4 import GID, KO, NOW, SCHEDULE, _book, ledger_with
from tipsbot import devig, nhl_data, oddsapi, pipeline
from tipsbot.models import nhl as NM
from tipsbot.profil4 import nhl, nhl_measure as M
from tipsbot.profil4.config import P4
from tipsbot.profil4.provider import Fixture
from tipsbot.profil4.store import Store

HOME, AWAY = "Toronto Maple Leafs", "Los Angeles Kings"
FETCHED = datetime(2026, 11, 21, 11, 45, tzinfo=timezone.utc)
UPD = "2026-11-21T11:30:00Z"


def _moneypuck(last=date(2026, 11, 19)):
    rows = []
    teams = {"TOR": (3.3, 2.8), "L.A": (2.7, 3.1), "BOS": (3.0, 3.0)}
    for season, start, n in ((2025, date(2025, 10, 8), 40), (2026, last - timedelta(days=2 * 23), 24)):
        for g in range(n):
            d = start + timedelta(days=2 * g)
            for t, (xf, xa) in teams.items():
                rows.append({"team": t, "season": season, "gameId": season * 1000 + g, "opposingTeam": "BOS" if t != "BOS" else "TOR",
                             "home_or_away": "HOME" if g % 2 else "AWAY", "gameDate": int(d.strftime("%Y%m%d")),
                             "situation": "all", "iceTime": 3600, "xGoalsFor": xf, "xGoalsAgainst": xa,
                             "unblockedShotAttemptsFor": 44, "unblockedShotAttemptsAgainst": 40,
                             "goalsFor": 3, "goalsAgainst": 3})
    return pd.DataFrame(rows)


@pytest.fixture
def st(tmp_path, monkeypatch):
    (tmp_path / "nhl").mkdir()
    _moneypuck().to_csv(tmp_path / "nhl" / "all_teams.csv", index=False)
    pd.DataFrame([{"name": "G", "team": "TOR", "situation": "all", "games_played": 20, "xGoals": 55.0, "goals": 48.0, "ongoal": 600}]
                 ).to_csv(tmp_path / "nhl" / "goalies.csv", index=False)
    s = replace(pipeline.SETTINGS, data_dir=tmp_path)
    monkeypatch.setattr(nhl_data, "SETTINGS", s)
    return s


def provider(st, overrides=None):
    return pipeline.ModelProvider("NHL", NOW.date(), st, goalie_overrides=overrides)


def payload(upd=UPD, sharp_totals=True, soft_ml=(2.12, 1.80), extra_books=()):
    pin = _book("pinnacle", upd, (1.95, 1.95), ou=(1.92, 1.98), pl=(2.80, 1.45))
    if not sharp_totals:
        pin["markets"] = [m for m in pin["markets"] if m["key"] != "totals"]
    books = [pin, _book("williamhill", upd, (1.90, 1.90)), _book("unibet_nl", upd, soft_ml), *extra_books]
    return {"id": "abc123", "sport_key": "icehockey_nhl", "commence_time": "2026-11-22T00:00:00Z",
            "home_team": HOME, "away_team": AWAY, "bookmakers": books}


FX = Fixture(GID, 0, "NHL", 0, KO, "NS", 0, HOME, 0, AWAY)


def snapshot(st, raw_ev=None, *, now=NOW, fetched=FETCHED, prov=None, fx=FX, **kw):
    raw_ev = raw_ev or payload()
    ev = oddsapi.parse_events([raw_ev], "NHL", st)[0]
    quotes = nhl.quotes_from_event(raw_ev, fx.id, now)
    prov = prov or provider(st)
    used = prov.probabilities(ev)
    snap, rows = M.build(fx, ev, quotes, prov, used, now=now, odds_fetched_at=fetched, scan_id="t1", weight_p4=0.15,
                         config_hash="h", settings=st, **kw)
    return snap, {(r["market"], r["selection"]): r for r in rows}, ev, used, prov


# --- tri typy pravdepodobnosti a ukotveni OU ------------------------------------------------------------------

def test_raw_used_market_and_blends_are_stored_side_by_side(st):
    snap, rows, ev, used, _ = snapshot(st)
    r = rows[("MONEYLINE", "home")]
    assert r["p_raw"] is not None and r["p_used"] == used["MONEYLINE:home"]
    line = devig.consensus_line(oddsapi.sharp_and_offers(ev.markets["MONEYLINE"], st)[0])
    assert r["p_mkt_sharp"] == pytest.approx(line.probs[0]) and json.loads(r["mkt_sharp_sources"]) == {"pinnacle": 1.0}
    assert r["p_mkt_p4"] == pytest.approx(0.5) and r["mkt_p4_source"] == "Pinnacle bez marže"
    assert r["p_blend_main"] == pytest.approx(devig.blend_probability(r["p_used"], r["p_mkt_sharp"], 0.15))
    assert r["p_blend_p4"] == pytest.approx(0.15 * r["p_used"] + 0.85 * r["p_mkt_p4"])
    assert r["p_cons_p4"] == pytest.approx(min(r["p_mkt_p4"], r["p_blend_p4"]))
    assert r["blend_main_kind"] == "směs" and r["blend_p4_kind"] == "směs"
    assert snap["model_weight_main"] == 0.15 and snap["model_weight_p4"] == 0.15


def test_ou_anchor_is_recorded_and_raw_stays_unanchored(st):
    snap, rows, ev, used, _ = snapshot(st)
    assert snap["anchored"] == 1 and snap["anchor_market"] == "OU_6.5:over"
    p_over_mkt = devig.consensus_line(oddsapi.sharp_and_offers(ev.markets["OU_6.5"], st)[0]).probs[0]
    assert snap["anchor_target"] == pytest.approx(p_over_mkt)
    assert 0.6 <= snap["anchor_k"] <= 1.6 and snap["anchor_k"] != 1.0
    ou = rows[("OU_6.5", "over")]
    assert ou["independence_used"] == "ukotveno na tržní total"
    assert ou["p_used"] == pytest.approx(p_over_mkt, abs=0.01)  # ukotveny model trefi trh (bisekce)
    assert abs(ou["p_raw"] - ou["p_used"]) > 0.01  # cisty model se od trhu lisi – neni nahrazen ukotvenym
    # cisty vypocet = stejne ocekavane goly a stejny seed bez kotvy; ukotveny = tytez goly x k
    lam = NM.expected_goals(provider(st).ratings.context("TOR", "LAK", ev.kickoff.date()), st.nhl)
    assert (snap["lambda_raw_h"], snap["lambda_raw_a"]) == lam
    again = NM.simulate(lam[0] * snap["anchor_k"], lam[1] * snap["anchor_k"], st.nhl, np.random.default_rng(snap["seed"]))
    assert again["OU_6.5:over"] == pytest.approx(used["OU_6.5:over"], abs=1e-12)
    assert again["MONEYLINE:home"] == pytest.approx(used["MONEYLINE:home"], abs=1e-12)


def test_without_market_total_raw_equals_used_exactly(st):
    snap, rows, *_ = snapshot(st, payload(sharp_totals=False))
    assert snap["anchored"] == 0 and snap["anchor_market"] is None and snap["anchor_k"] == 1.0
    assert all(r["p_raw"] == r["p_used"] for r in rows.values() if r["p_used"] is not None)
    assert rows[("MONEYLINE", "home")]["independence_used"] == "nezávislý"
    assert json.loads(snap["missing"]) == []


# --- seed, RNG a reprodukovatelnost ---------------------------------------------------------------------------

def test_same_seed_gives_identical_measurement(st):
    a = snapshot(st)
    b = snapshot(st)
    assert a[0] == b[0] and a[1] == b[1]
    assert a[0]["seed"] == pipeline.zlib.crc32(a[2].match_id.encode())


def test_extra_simulation_does_not_touch_production_rng_or_outputs(st):
    raw_ev = payload()
    ev = oddsapi.parse_events([raw_ev], "NHL", st)[0]
    prov = provider(st)
    before = prov.probabilities(ev)
    np.random.seed(123)
    state = np.random.get_state()[1].copy()
    M.build(FX, ev, nhl.quotes_from_event(raw_ev, GID, NOW), prov, before, now=NOW, odds_fetched_at=FETCHED,
            scan_id="t", weight_p4=0.15, settings=st)
    assert (np.random.get_state()[1] == state).all()  # globalni generator netknuty
    assert prov.probabilities(ev) == before  # produkcni vypocet po mereni stejny (bitove)


# --- casova konzistence a srovnatelnost ----------------------------------------------------------------------

def test_times_sources_and_age_are_recorded(st):
    snap, rows, *_ = snapshot(st)
    assert snap["computed_at"] == "2026-11-21T12:00:00+00:00" and snap["odds_fetched_at"] == "2026-11-21T11:45:00+00:00"
    assert snap["ref_sharp_stamp_max"] == "2026-11-21T11:30:00+00:00" and snap["ref_age_min"] == pytest.approx(30)
    assert json.loads(snap["ref_sharp_books"]) == ["pinnacle"]
    assert json.loads(snap["ref_p4_books"]) == ["pinnacle", "unibet_nl", "williamhill"]
    r = rows[("MONEYLINE", "home")]
    assert r["mkt_sharp_stamp"] == "2026-11-21T11:30:00+00:00" and r["best_book"] == "unibet_nl" and r["best_odds"] == 2.12
    assert r["comparable_sharp"] == 1 and r["comparable_p4"] == 1 and snap["comparable"] == 1


def test_stale_market_is_marked_not_comparable(st):
    old = (NOW - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _, rows, *_ = snapshot(st, payload(upd=old))
    ex = json.loads(rows[("MONEYLINE", "home")]["exclusion"])
    assert rows[("MONEYLINE", "home")]["comparable_sharp"] == 0 and any(e.startswith("S3") for e in ex["sharp"])
    assert any(e.startswith("S3") for e in ex["p4"])


def test_quotes_newer_than_fetch_mean_mixed_moments(st):
    snap, rows, *_ = snapshot(st, fetched=datetime(2026, 11, 21, 11, 0, tzinfo=timezone.utc))
    assert any("nejsou z jednoho okamžiku" in m for m in json.loads(snap["missing"]))
    assert snap["comparable"] == 0 and all(r["comparable_sharp"] == 0 for r in rows.values())


def test_missing_fetch_time_and_after_kickoff_are_excluded(st):
    snap, *_ = snapshot(st, fetched=None)
    assert "chybí čas stažení kurzů" in json.loads(snap["missing"]) and snap["comparable"] == 0
    late = KO + timedelta(minutes=5)
    snap, *_ = snapshot(st, now=late, fetched=late - timedelta(minutes=1))
    assert any(e.startswith("S1") for e in json.loads(snap["exclusion"]))


def test_no_sharp_line_is_explicit_and_not_substituted(st):
    raw_ev = payload()
    raw_ev["bookmakers"] = raw_ev["bookmakers"][1:]  # bez Pinnacle (jako NHL vic nez 36 h pred zapasem)
    snap, rows, *_ = snapshot(st, raw_ev)
    r = rows[("MONEYLINE", "home")]
    assert r["p_mkt_sharp"] is None and r["p_blend_main"] is None and r["in_main"] == 0
    assert r["blend_main_kind"].startswith("bez ostré linie")
    assert snap["anchored"] == 0 and r["p_raw"] == r["p_used"]
    ex = json.loads(r["exclusion"])
    assert any(e.startswith("S2") for e in ex["sharp"]) and any("2 kanceláří" in e for e in ex["p4"])


def test_unknown_team_and_missing_model_are_flagged_without_fallback_values(st):
    raw_ev = payload()
    raw_ev["home_team"] = "Atlantis Krakens"
    for b in raw_ev["bookmakers"]:
        for m in b["markets"]:
            for o in m["outcomes"]:
                if o["name"] == HOME:
                    o["name"] = "Atlantis Krakens"
    fx = replace(FX, home="Atlantis Krakens")
    snap, rows, *_ = snapshot(st, raw_ev, fx=fx)
    assert any("bez ratingu" in m for m in json.loads(snap["missing"])) and snap["comparable"] == 0
    r = rows[("MONEYLINE", "home")]
    assert r["p_raw"] is None and r["p_used"] is None and r["independence_used"] == "chybí"
    assert r["blend_p4_kind"] == "jen trh (model A chybí)"  # hodnota je to, co by pouzil system, s oznacenim

    class NoModel:
        note = "model neni k dispozici"
        ratings = None

        def probabilities(self, ev):
            return None

    snap, *_ = snapshot(st, prov=NoModel())
    assert "model NHL není k dispozici (chybí data MoneyPuck)" in json.loads(snap["missing"])


def test_data_quality_flags(st, tmp_path):
    snap, *_ = snapshot(st, overrides={"LAK": "Nobody"})
    assert snap["goalie_home_src"] == "weighted" and snap["goalie_away_src"] == "manual"
    assert snap["home_games"] == 24 and snap["cold_start_home"] == 0 and snap["b2b_home"] in (0, 1)
    assert snap["data_last_game"] == "2026-11-19"
    assert json.loads(snap["data_file_mtime"])["all_teams"] is not None
    # zastarala data MoneyPuck
    _moneypuck(last=date(2026, 11, 10)).to_csv(tmp_path / "nhl" / "all_teams.csv", index=False)
    snap, *_ = snapshot(st)
    assert any("data MoneyPuck končí" in m for m in json.loads(snap["missing"])) and snap["comparable"] == 0


# --- shoda s produkcnim rozhodovanim (regrese) ---------------------------------------------------------------

def _decisions(store):
    cols = ("fixture_id", "market", "selection", "probs", "fair_odds", "odds", "bookmaker", "ev", "ev_conservative",
            "robustness", "decision", "category")
    return ([tuple(r[c] for c in cols) for r in store.query("SELECT * FROM tips ORDER BY market, selection")],
            [tuple(r)[1:] for r in store.query("SELECT fixture_id, market, selection, model, p, p_market, odds, bookmaker, "
                                               "predicted_at FROM predictions ORDER BY market, selection, model")])


def test_measurement_matches_profil4_and_does_not_change_decisions(st):
    led = ledger_with([payload()])
    s1 = Store()
    scan = nhl.run(led, s1, NOW, P4, schedule=lambda d: SCHEDULE, provider=provider(st), injuries=True)
    base = _decisions(s1)
    _, rows, *_ = snapshot(st)
    for c in scan.analyses[0].candidates:  # A, B, C, D Profilu 4 = ulozene hodnoty mereni
        r = rows[(c.market, c.selection)]
        assert (r["p_used"], r["p_mkt_p4"]) == (c.p["A"], c.p["B"])
        assert r["p_blend_p4"] == pytest.approx(c.p["C"], abs=1e-15) and r["p_cons_p4"] == pytest.approx(c.p["D"], abs=1e-15)
    s2 = Store()
    nhl.run(led, s2, NOW, P4, schedule=lambda d: SCHEDULE, provider=provider(st), injuries=True)
    assert _decisions(s2) == base and base[0] and base[1]


def test_measurement_matches_main_bot_candidates(st):
    _, rows, ev, used, _ = snapshot(st)
    cands, has_line = pipeline.build_candidates(ev, used, st)
    assert has_line
    for c in cands:
        r = rows[(c.market, c.selection)]
        assert r["p_mkt_sharp"] == c.p_market and r["p_used"] == c.p_model and r["in_main"] == 1
        a = devig.assess_value(c.p_model, c.p_market, 2.0, 0.025, st.value.for_league("NHL"))
        assert r["p_blend_main"] == a.prob


# --- ukladani (jen pribyva) a vysledky -----------------------------------------------------------------------

def test_record_is_append_only_and_results_first_write_wins(st):
    store = Store()
    snap, rows, *_ = snapshot(st)
    sid1 = M.record(store, snap, list(rows.values()))
    before = [tuple(r) for r in store.query("SELECT * FROM nhl_measurements WHERE snapshot_id=?", (sid1,))]
    sid2 = M.record(store, {**snap, "scan_id": "t2"}, list(rows.values()))
    assert sid2 != sid1 and [tuple(r) for r in store.query("SELECT * FROM nhl_measurements WHERE snapshot_id=?", (sid1,))] == before
    assert store.query("SELECT COUNT(*) n FROM nhl_snapshots")[0]["n"] == 2
    got = dict(store.query("SELECT * FROM nhl_snapshots WHERE id=?", (sid1,))[0])
    assert got["anchored"] == 1 and got["measure_version"] == M.MEASURE_VERSION and got["missing"] == "[]"
    g = {"id": GID, "final": (3, 4), "reg": (3, 3), "end": "SO"}
    assert M.record_result(store, GID, g, NOW) is True
    assert M.record_result(store, GID, {**g, "final": (9, 9)}, NOW) is False
    assert tuple(store.query("SELECT final_h, final_a, reg_h, reg_a, \"end\" FROM nhl_results")[0]) == (3, 4, 3, 3, "SO")


def test_failed_record_leaves_nothing_half_written(st):
    store = Store()
    snap, rows, *_ = snapshot(st)
    bad = list(rows.values()) + [{"fixture_id": GID}]  # radek bez povinnych sloupcu
    with pytest.raises(sqlite3.IntegrityError):
        M.record(store, snap, bad)
    assert store.query("SELECT COUNT(*) n FROM nhl_snapshots")[0]["n"] == 0
    assert store.query("SELECT COUNT(*) n FROM nhl_measurements")[0]["n"] == 0


# --- schema a migrace ----------------------------------------------------------------------------------------

def _pre_m1_db(path):
    """Databaze ve stavu pred M1: cele dosavadni schema bez novych tabulek a s daty."""
    s = Store(path)
    for t in ("nhl_snapshots", "nhl_measurements", "nhl_results"):
        s.execute(f"DROP TABLE {t}")
    s.execute("INSERT INTO kv(key, value) VALUES('stary', 'zaznam')")
    s.execute("INSERT INTO predictions(fixture_id, market, selection, market_type, model, p, predicted_at, model_version, "
              "config_hash) VALUES(1, 'MONEYLINE', 'home', 'MONEYLINE', 'A', 0.5, '2026-11-20T00:00:00+00:00', 'v', 'h')")
    s.close()


def _dump(path):
    c = sqlite3.connect(path)
    out = {t: c.execute(f'SELECT * FROM "{t}"').fetchall() for (t,) in
           c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'nhl_%'")}
    schema = sorted(c.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'nhl_%'").fetchall(), key=str)
    c.close()
    return out, schema


def test_new_tables_on_existing_database_keep_old_data(tmp_path):
    p = tmp_path / "old.db"
    _pre_m1_db(p)
    before = _dump(p)
    s = Store(p)
    names = {r["name"] for r in s.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"nhl_snapshots", "nhl_measurements", "nhl_results"} <= names
    s.close()
    Store(p).close()  # opakovany start nic nemeni
    assert _dump(p) == before  # dosavadni tabulky, schema i radky beze zmeny


def test_concurrent_open_of_existing_database_creates_tables_once(tmp_path):
    p, errors = tmp_path / "race.db", []
    _pre_m1_db(p)

    def opener():
        try:
            Store(p).close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    for _ in range(5):
        ts = [threading.Thread(target=opener) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert not errors
    assert {r[0] for r in sqlite3.connect(p).execute("SELECT name FROM sqlite_master WHERE name LIKE 'nhl_%' AND type='table'")} == {
        "nhl_snapshots", "nhl_measurements", "nhl_results"}


# --- zadne API pozadavky -------------------------------------------------------------------------------------

def test_build_makes_no_network_requests(st, monkeypatch):
    import requests

    calls = []
    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("sit")))
    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("sit")))
    snapshot(st)
    assert calls == []
