"""Phase 3.5.1 / M2 – napojeni mereni NHL na sken (profil4/nhl.run) a na vyhodnoceni (profil4/nhl.settle).

Izolovane docasne databaze (Store() v pameti), skutecny model nad syntetickymi daty MoneyPuck, odpovedi API ve tvaru
skutecnych odpovedi. Nic se nestahuje."""

import json
import sqlite3
from datetime import timedelta

import numpy as np
import pytest

from test_automation import _goal
from test_nhl_measure import AWAY, HOME, payload, provider, st  # noqa: F401 - fixture st
from test_nhl_profil4 import GID, KO, NOW, SCHEDULE, ledger_with
from tipsbot import nhl_settle
from tipsbot.profil4 import nhl, nhl_measure as M
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store

LATER = NOW + timedelta(minutes=30)
AFTER = KO + timedelta(hours=5)


def evidence(led, ts, remaining=400, purpose="refresh_league", league="NHL", http=200, ok=True,
             endpoint="/sports/icehockey_nhl/odds"):
    """Zaznam evidence B1 (odds_api_calls) tak, jak ho zapise skutecne stazeni kurzu (oddsapi.request)."""
    from tipsbot.oddsapi import CallRecord

    led.log_odds_api_call(CallRecord(ts.replace(tzinfo=None, microsecond=0), endpoint, purpose, "icehockey_nhl", league,
                                     {"regions": "eu"}, http=http, ok=ok, remaining=remaining,
                                     used=None if remaining is None else 500 - remaining, last=3 if ok else None))
    led.s.commit()


def _run(st, store, pl=None, now=NOW, fetched=NOW - timedelta(minutes=5), schedule=None, ev_at=None):
    """Skutecny prubeh: cache nese cas ZACATKU pruchodu (`fetched`), pozadavek na kurzy NHL prisel o 20 s pozdeji."""
    led = ledger_with([pl or payload()], when=fetched)
    if ev_at is not False:
        evidence(led, ev_at or fetched + timedelta(seconds=20))
    return nhl.run(led, store, now, P4, schedule=schedule or (lambda d: SCHEDULE), provider=provider(st), injuries=True)


def _all_decisions(store):
    """Rozhodovaci vystupy Profilu 4: tipy, predikce (pravdepodobnosti, EV) a poradi – bez id a casu vypoctu poradi."""
    tips = [{k: v for k, v in dict(r).items() if k != "id"} for r in store.query("SELECT * FROM tips ORDER BY market, selection")]
    preds = [{k: v for k, v in dict(r).items() if k != "id"}
             for r in store.query("SELECT * FROM predictions ORDER BY market, selection, model")]
    ranks = [{k: v for k, v in dict(r).items() if k not in ("id", "computed_at")}
             for r in store.query("SELECT * FROM rankings ORDER BY market, selection")]
    return tips, preds, ranks


def _cands(scan):
    return [(c.key, c.p, c.ev_c, c.ev_d, c.robustness, c.decision, tuple(c.reasons), c.quote.bookmaker, c.quote.odds)
            for an in scan.analyses for c in an.candidates]


def _disabled(m):
    m.setattr(nhl.nhl_measure, "measure_scan", lambda *a, **k: None)
    m.setattr(nhl.nhl_measure, "measure_results", lambda *a, **k: 0)


def _n(store, table):
    return store.query(f"SELECT COUNT(*) n FROM {table}")[0]["n"]


# --- 1, 2: rozhodovani s merenim a bez nej ------------------------------------------------------------------

@pytest.mark.parametrize("pl", [payload(), payload(sharp_totals=False), payload(soft_ml=(2.30, 1.70))],
                         ids=["kotva", "bez-totalu", "mekky-kurz"])
def test_decisions_identical_with_and_without_measurement(st, monkeypatch, pl):
    on = Store()
    scan_on = _run(st, on, pl)
    assert _n(on, "nhl_snapshots") == 1
    with monkeypatch.context() as m:
        _disabled(m)
        off = Store()
        scan_off = _run(st, off, pl)
    assert _n(off, "nhl_snapshots") == 0
    assert _all_decisions(on) == _all_decisions(off) and _all_decisions(on)[0] and _all_decisions(on)[1]
    assert _cands(scan_on) == _cands(scan_off)


def test_scan_links_fixture_scan_market_and_stores_everything(st):
    store = Store()
    scan = _run(st, store, fetched=NOW - timedelta(minutes=15))
    snap = dict(store.query("SELECT * FROM nhl_snapshots")[0])
    assert snap["fixture_id"] == GID and snap["scan_id"] == "2026-11-21T12:00:00+00:00" and snap["match"] == f"{HOME} - {AWAY}"
    assert snap["odds_pass_started_at"] == "2026-11-21T11:45:00+00:00"  # cas cache = zacatek pruchodu
    assert snap["odds_fetched_at"] == "2026-11-21T11:45:20+00:00" and snap["odds_call_id"] == 1  # skutecny pozadavek B1
    assert json.loads(snap["odds_fetch_evidence"])["reason"].startswith("prokázáno") and snap["computed_at"] >= snap["scan_id"]
    assert snap["anchored"] == 1 and snap["config_hash"] == P4.config_hash() and snap["input_hash"]
    rows = {(r["market"], r["selection"]): dict(r) for r in store.query("SELECT * FROM nhl_measurements")}
    assert all(r["snapshot_id"] == snap["id"] and r["fixture_id"] == GID for r in rows.values())
    for c in scan.analyses[0].candidates:  # kazdy kandidat Profilu 4 ma radek mereni se stejnymi A, B, C, D
        r = rows[(c.market, c.selection)]
        assert (r["p_used"], r["p_mkt_p4"]) == (c.p["A"], c.p["B"]) and r["in_p4"] == 1
        assert r["p_blend_p4"] == pytest.approx(c.p["C"], abs=1e-15) and r["p_cons_p4"] == pytest.approx(c.p["D"], abs=1e-15)


# --- 3, 4: selhani zapisu a vypoctu -------------------------------------------------------------------------

def test_write_failure_is_logged_and_scan_unchanged(st, monkeypatch, caplog):
    ref = Store()
    _run(st, ref)

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(M, "record", boom)
    store = Store()
    with caplog.at_level("WARNING"):
        scan = _run(st, store)
    assert scan.analyses and _all_decisions(store) == _all_decisions(ref)
    assert any("měření NHL selhalo" in r.message and "database is locked" in r.message for r in caplog.records)
    assert _n(store, "nhl_snapshots") == 0


def test_compute_failure_is_logged_and_scan_unchanged(st, monkeypatch, caplog):
    ref = Store()
    _run(st, ref)
    monkeypatch.setattr(M, "build", lambda *a, **k: 1 / 0)
    store = Store()
    with caplog.at_level("WARNING"):
        _run(st, store)
    assert _all_decisions(store) == _all_decisions(ref)
    assert any("ZeroDivisionError" in r.message for r in caplog.records)


def test_results_failure_is_logged_and_settle_unchanged(st, monkeypatch, caplog):
    ref, _ = _settled(st, SO)
    monkeypatch.setattr(M, "record_results", lambda *a, **k: 1 / 0)
    with caplog.at_level("WARNING"):
        store, _ = _settled(st, SO)
    assert _all_decisions(store) == _all_decisions(ref) and _n(store, "nhl_results") == 0
    assert any("ukládání výsledků selhalo" in r.message for r in caplog.records)


# --- 5: opakovany sken --------------------------------------------------------------------------------------

def test_repeated_scan_does_not_duplicate_measurements(st):
    store = Store()
    _run(st, store)
    rows = _n(store, "nhl_measurements")
    _run(st, store)  # tentyz sken znovu (stejny scan_id)
    _run(st, store, now=LATER)  # pozdejsi sken nad stejnou cache = stejne vstupy
    assert _n(store, "nhl_snapshots") == 1 and _n(store, "nhl_measurements") == rows
    # nove stazeni kurzu = novy rozhodovaci okamzik = novy snimek
    _run(st, store, payload(upd="2026-11-21T12:20:00Z", soft_ml=(2.15, 1.78)), now=LATER, fetched=LATER - timedelta(minutes=5))
    snaps = store.query("SELECT id, odds_fetched_at FROM nhl_snapshots ORDER BY id")
    assert len(snaps) == 2 and snaps[0]["odds_fetched_at"] != snaps[1]["odds_fetched_at"]
    assert store.query("SELECT COUNT(DISTINCT snapshot_id) n FROM nhl_measurements")[0]["n"] == 2
    assert _n(store, "nhl_measurements") == 2 * rows


# --- 8: chybejici a nesrovnatelna data ----------------------------------------------------------------------

def test_missing_sharp_line_and_unscheduled_game(st):
    store = Store()
    pl = payload()
    pl["bookmakers"] = pl["bookmakers"][1:]  # bez Pinnacle (NHL vic nez 36 h pred zapasem)
    _run(st, store, pl)
    snap = dict(store.query("SELECT * FROM nhl_snapshots")[0])
    rows = [dict(r) for r in store.query("SELECT * FROM nhl_measurements")]
    assert snap["anchored"] == 0 and rows
    assert all(r["comparable_sharp"] == 0 and r["p_mkt_sharp"] is None and r["p_blend_main"] is None for r in rows)
    assert all(any(e.startswith("S2") for e in json.loads(r["exclusion"])["sharp"]) for r in rows)
    store2 = Store()  # zapas mimo rozpis NHL API se neanalyzuje, a tedy ani nemeri
    _run(st, store2, schedule=lambda d: {"gameWeek": []})
    assert _n(store2, "nhl_snapshots") == 0


def test_stale_cache_is_measured_but_not_comparable(st):
    store = Store()
    old = (NOW - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _run(st, store, payload(upd=old), fetched=NOW - timedelta(hours=4))
    rows = [dict(r) for r in store.query("SELECT * FROM nhl_measurements WHERE market='MONEYLINE'")]
    assert rows and all(r["comparable_sharp"] == 0 and r["comparable_p4"] == 0 for r in rows)
    assert dict(store.query("SELECT ref_age_min FROM nhl_snapshots")[0])["ref_age_min"] == pytest.approx(300, abs=1)


# --- 6, 7: vysledky -----------------------------------------------------------------------------------------

def _games(final, goals, end, gid=GID, home="TOR", away="LAK", start="2026-11-22T00:00:00Z", state="OFF"):
    return {"games": [{"id": gid, "gameState": state, "startTimeUTC": start, "gameOutcome": {"lastPeriodType": end},
                       "homeTeam": {"abbrev": home, "score": final[0]}, "awayTeam": {"abbrev": away, "score": final[1]},
                       "goals": goals}]}


SO = _games((3, 4), [_goal(1, 1, 0), _goal(2, 2, 0), _goal(2, 2, 1), _goal(3, 2, 2), _goal(3, 3, 3), _goal(5, 3, 4)], "SO")
OT = _games((2, 1), [_goal(1, 1, 0), _goal(3, 1, 1), _goal(4, 2, 1)], "OT")
REG = _games((4, 1), [_goal(1, 1, 0), _goal(2, 2, 0), _goal(2, 2, 1), _goal(3, 3, 1), _goal(3, 4, 1)], "REG")


def _settled(st, games, store=None):
    store = store or Store()
    _run(st, store)
    calls = []
    nhl.settle(store, AFTER, fetch=lambda d: calls.append(d) or games)
    return store, calls


@pytest.mark.parametrize("games,final,reg,end", [(SO, (3, 4), (3, 3), "SO"), (OT, (2, 1), (1, 1), "OT"),
                                                  (REG, (4, 1), (4, 1), "REG")],
                         ids=["najezdy", "prodlouzeni", "zakladni-doba"])
def test_results_store_final_and_60_minute_score(st, games, final, reg, end):
    store, _ = _settled(st, games)
    r = M.stored_result(store, GID)
    assert (tuple(r["final"]), tuple(r["reg"]), r["end"]) == (final, reg, end)
    assert r["source"] == "NHL API (nhl_settle)" and r["fetched_at"] == "2026-11-22T05:00:00+00:00"
    # vysledek patri spravnemu zapasu: stejne id jako snimky mereni
    assert {x["fixture_id"] for x in store.query("SELECT fixture_id FROM nhl_snapshots")} == {GID}


def test_repeated_settle_is_idempotent_and_conflict_never_overwrites(st, caplog):
    store, _ = _settled(st, SO)
    nhl.settle(store, AFTER + timedelta(hours=1), fetch=lambda d: SO)
    assert _n(store, "nhl_results") == 1 and _n(store, "nhl_result_corrections") == 0
    other = _games((4, 3), [_goal(1, 1, 0), _goal(2, 2, 0), _goal(2, 2, 1), _goal(3, 2, 2), _goal(3, 3, 3)], "SO")
    with caplog.at_level("WARNING"):
        for _ in range(2):  # zdroj pozdeji hlasi jiny vysledek – opakovane
            M.record_results(store, {g["id"]: g for g in nhl_settle.parse_scores(other)}, AFTER + timedelta(hours=2))
    assert tuple(M.stored_result(store, GID)["final"]) == (3, 4)  # prvni vysledek nepřepsan
    conf = store.query("SELECT * FROM nhl_result_corrections")
    assert len(conf) == 1 and conf[0]["kind"] == M.CONFLICT and json.loads(conf[0]["new"])["final"] == [4, 3]
    assert sum("konflikt" in r.message for r in caplog.records) == 1
    assert M.effective_result(store, GID)["final"] == [3, 4]  # konflikt se pro vyhodnoceni nepouzije


def test_explicit_correction_keeps_original(st):
    store, _ = _settled(st, SO)
    with pytest.raises(ValueError):
        M.correct_result(store, GID, (4, 3), (3, 3), "SO", " ", AFTER)  # bez duvodu
    with pytest.raises(ValueError):
        M.correct_result(store, GID, (5, 3), (3, 3), "SO", "chybny zapis", AFTER)  # nekonzistentni skore
    with pytest.raises(ValueError):
        M.correct_result(store, 999, (4, 3), (3, 3), "SO", "x", AFTER)  # zapas bez vysledku
    assert M.correct_result(store, GID, (4, 3), (3, 3), "SO", "NHL API opravilo vítěze nájezdů", AFTER) is True
    eff = M.effective_result(store, GID)
    assert eff["final"] == [4, 3] and eff["source"].startswith("oprava")
    assert tuple(M.stored_result(store, GID)["final"]) == (3, 4)  # puvodni zaznam zustava
    fix = dict(store.query("SELECT * FROM nhl_result_corrections WHERE kind=?", (M.CORRECTION,))[0])
    assert json.loads(fix["old"])["final"] == [3, 4] and fix["source"] == "ručně"


@pytest.mark.parametrize("games,why", [
    (_games((3, 2), [], "REG"), "nesedí"),  # chybi goly -> po 60 min 0:0 proti konecnemu 3:2
    (_games((3, 2), [_goal(1, 1, 0), _goal(1, 2, 0), _goal(2, 2, 1), _goal(3, 2, 2), _goal(4, 3, 2)], "OT"), None),
    (_games((3, 3), [_goal(1, 1, 0)], "OVER"), "neznámý konec"),
], ids=["chybi-goly", "ot-ok", "neznamy-konec"])
def test_only_consistent_final_results_are_stored(st, games, why, caplog):
    with caplog.at_level("WARNING"):
        store, _ = _settled(st, games)
    if why is None:
        assert M.stored_result(store, GID) is not None
    else:
        assert M.stored_result(store, GID) is None and any(why in r.message for r in caplog.records)


def test_unfinished_game_and_wrong_assignment_store_nothing(st, caplog):
    store, _ = _settled(st, _games((2, 1), [_goal(1, 1, 0), _goal(2, 2, 0), _goal(2, 2, 1)], "REG", state="LIVE"))
    assert M.stored_result(store, GID) is None  # neukonceny zapas (parse_scores ho vynecha)
    goals = [_goal(1, 1, 0), _goal(2, 2, 0), _goal(2, 2, 1), _goal(3, 3, 1), _goal(3, 4, 1)]
    with caplog.at_level("WARNING"):
        wrong = nhl_settle.parse_scores(_games((4, 1), goals, "REG", home="BOS", away="MTL"))
        assert M.record_results(store, {g["id"]: g for g in wrong}, AFTER) == 0
    assert M.stored_result(store, GID) is None and any("nepřiřazen" in r.message for r in caplog.records)
    late = nhl_settle.parse_scores(_games((4, 1), goals, "REG", start="2026-11-23T00:00:00Z"))
    assert M.record_results(store, {g["id"]: g for g in late}, AFTER) == 0 and M.stored_result(store, GID) is None


# --- 9: zadne dodatecne sitove pozadavky, globalni RNG ------------------------------------------------------

def test_settle_measurement_adds_no_requests(st, monkeypatch):
    _, calls_on = _settled(st, SO)
    with monkeypatch.context() as m:
        _disabled(m)
        _, calls_off = _settled(st, SO)
    assert calls_on == calls_off and calls_on  # stejne dny, stejny pocet dotazu na vysledky


def test_scan_and_settle_make_no_network_requests_and_keep_global_rng(st, monkeypatch):
    import requests

    calls = []

    def no(*a, **k):
        calls.append(a)
        raise AssertionError("síť")

    monkeypatch.setattr(requests, "get", no)
    monkeypatch.setattr(requests.Session, "request", no)
    np.random.seed(7)
    state = np.random.get_state()[1].copy()
    store = Store()
    _run(st, store)
    nhl.settle(store, AFTER, fetch=lambda d: SO)
    assert calls == [] and (np.random.get_state()[1] == state).all()
    assert _n(store, "nhl_results") == 1 and _n(store, "nhl_snapshots") == 1
