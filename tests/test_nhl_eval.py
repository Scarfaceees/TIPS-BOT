"""Phase 3.5.1 / M3 – vykaz vyhodnoceni NHL mereni (profil4/nhl_eval.py): jen cteni, zadna sit.

Data se zakladaji pres M1/M2 (nhl_measure.record / record_result) do izolovanych docasnych databazi. Pravdepodobnosti
jsou synteticke se znamou pravdou, aby slo overit smer i prukaznost."""

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from scipy.stats import multivariate_normal as mvn, norm

from tipsbot.profil4 import nhl_eval as E, nhl_measure as M
from tipsbot.profil4.store import Store

T0 = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=30)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(E, "BOOT_REPS", 300)


def _snap(fid, i, computed, kickoff, code_rev="abc123", comparable=1):
    return {**dict.fromkeys(M._SNAP_COLS), "scan_id": computed.isoformat(), "fixture_id": fid, "match": "Toronto Maple Leafs - Los Angeles Kings",
            "kickoff": kickoff.isoformat(), "computed_at": computed.isoformat(), "odds_fetched_at": computed.isoformat(),
            "ref_age_min": 30.0, "anchored": 1, "missing": "[]", "comparable": comparable, "exclusion": "[]",
            "code_rev": code_rev, "measure_version": "m2-1", "input_hash": f"h{fid}-{i}"}


def _row(fid, market, sel, p_raw, p_mkt, p_used=None, comparable=1, reasons=None):
    reasons = reasons or []
    pb = M.devig.blend_probability(p_used or p_raw, p_mkt, 0.15) if hasattr(M, "devig") else None
    from tipsbot import devig

    pb = devig.blend_probability(p_used if p_used is not None else p_raw, p_mkt, 0.15)
    return {"fixture_id": fid, "market": market, "selection": sel, "market_family": market.split("_")[0],
            "p_raw": p_raw, "p_used": p_used if p_used is not None else p_raw, "independence_used": "ukotveno na tržní total",
            "p_mkt_sharp": p_mkt, "p_mkt_p4": p_mkt, "p_blend_main": pb, "blend_main_kind": "směs",
            "p_blend_p4": 0.15 * (p_used if p_used is not None else p_raw) + 0.85 * p_mkt, "p_cons_p4": None,
            "blend_p4_kind": "směs", "in_main": 1, "in_p4": 1, "comparable_sharp": comparable, "comparable_p4": comparable,
            "exclusion": json.dumps({"sharp": reasons, "p4": reasons}, ensure_ascii=False)}


def _result(store, fid, home_win, margin2=False, total_over=True):
    if home_win:
        final = (4, 1) if margin2 else (3, 2)
    else:
        final = (1, 4) if margin2 else (2, 3)
    if total_over and sum(final) < 7:
        final = (final[0] + 2, final[1] + 2) if final[0] > final[1] else (final[0] + 2, final[1] + 2)
    if not total_over and sum(final) > 6:
        final = (final[0] - 1, final[1] - 1)
    assert M.record_result(store, fid, {"final": final, "reg": final, "end": "REG"}, NOW)


def build_db(path, n, *, model="truth", seed=1, code_rev="abc123", ou_lines=1, start_id=1, noise=0.08):
    """n zapasu; trh = pravda + sum, cisty model = pravda (`truth`), trh (`market`) nebo sum (`noise`)."""
    rng = np.random.default_rng(seed)
    store = Store(path)
    for k in range(n):
        fid = start_id + k
        ko = T0 + timedelta(hours=k)
        p_true = float(rng.uniform(0.3, 0.7))
        p_mkt = float(np.clip(p_true + rng.normal(0, noise), 0.05, 0.95))
        p_raw = {"truth": p_true, "market": p_mkt, "noise": float(np.clip(p_mkt + rng.normal(0, 0.1), 0.05, 0.95))}[model]
        home = bool(rng.random() < p_true)
        rows = [_row(fid, "MONEYLINE", "home", p_raw, p_mkt), _row(fid, "MONEYLINE", "away", 1 - p_raw, 1 - p_mkt),
                _row(fid, "PUCKLINE_1.5", "home-1.5", p_raw * 0.6, p_mkt * 0.6)]
        q = float(rng.uniform(0.35, 0.65))  # totaly: stejna pravdepodobnost a stejny vysledek na vsech liniich zapasu
        rows += [_row(fid, f"OU_{5.5 + j}", "over", q, q, p_used=q) for j in range(ou_lines)]
        M.record(store, _snap(fid, 0, ko - timedelta(hours=3), ko, code_rev), rows)
        _result(store, fid, home, total_over=bool(rng.random() < q))
    store.close()
    return path


def _digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


# --- jen cteni a zadna sit -------------------------------------------------------------------------------------

def test_report_is_read_only_and_changes_nothing(tmp_path):
    p = build_db(tmp_path / "p4.db", 40)
    before, mtime = _digest(p), p.stat().st_mtime_ns
    E.report(p, NOW)
    E.format_text(E.report(p, NOW))
    assert _digest(p) == before and p.stat().st_mtime_ns == mtime
    assert not (tmp_path / "p4.db-journal").exists()
    conn = E.open_ro(p)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM nhl_results")
    conn.close()


def test_missing_database_is_not_created_and_tables_missing_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        E.report(tmp_path / "nic.db", NOW)
    assert not (tmp_path / "nic.db").exists()
    plain = tmp_path / "plain.db"
    sqlite3.connect(plain).execute("CREATE TABLE x(a)").connection.close()
    rep = E.report(plain, NOW)
    assert rep["stav"].startswith("měření v této databázi není") and "nhl_snapshots" in rep["stav"]


def test_report_makes_no_network_requests(tmp_path, monkeypatch):
    import socket

    import requests

    calls = []
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError("síť")))
    monkeypatch.setattr(requests.Session, "request", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError("síť")))
    E.format_text(E.report(build_db(tmp_path / "p4.db", 30), NOW))
    assert calls == []


def test_cli_prints_report_without_creating_store(tmp_path, capsys):
    from tipsbot import cli

    p = build_db(tmp_path / "p4.db", 20)
    before = _digest(p)
    assert cli.main(["profil4", "nhl-measure-report", "--p4db", str(p)]) == 0
    out = capsys.readouterr().out
    assert "NHL – výkaz měření" in out and "A – čistý model vs. trh" in out and _digest(p) == before
    assert cli.main(["profil4", "nhl-measure-report", "--p4db", str(p), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["stav"] == "ok"


# --- vyber vzorku ---------------------------------------------------------------------------------------------

def test_last_comparable_snapshot_before_kickoff_is_used(tmp_path):
    p = tmp_path / "p4.db"
    store = Store(p)
    ko = T0
    M.record(store, _snap(1, 0, ko - timedelta(hours=10), ko), [_row(1, "MONEYLINE", "home", 0.40, 0.45)])
    M.record(store, _snap(1, 1, ko - timedelta(hours=2), ko), [_row(1, "MONEYLINE", "home", 0.60, 0.55)])
    M.record(store, _snap(1, 2, ko - timedelta(hours=1), ko),
             [_row(1, "MONEYLINE", "home", 0.90, 0.90, comparable=0, reasons=["S3: ostrá linie starší než 180 min"])])
    M.record(store, _snap(1, 3, ko + timedelta(minutes=5), ko), [_row(1, "MONEYLINE", "home", 0.99, 0.99)])
    _result(store, 1, True)
    store.close()
    conn = E.open_ro(p)
    s = E.collect(conn, "sharp", NOW)
    (it,) = next(iter(s["groups"].values()))
    assert (it["p_raw"], it["p_mkt_sharp"], it["n_snaps"], it["first_raw"]) == (0.60, 0.55, 2, 0.40)


def test_exclusion_reasons_conflicts_corrections_and_missing_results(tmp_path):
    p = tmp_path / "p4.db"
    store = Store(p)
    for fid in (1, 2, 3, 4):
        ok = fid != 1
        M.record(store, _snap(fid, 0, T0 - timedelta(hours=2), T0),
                 [_row(fid, "MONEYLINE", "home", 0.5, 0.5, comparable=int(ok), reasons=[] if ok else ["S2: chybí ostrá férová linie"])])
    _result(store, 1, True)
    _result(store, 2, True)
    M.record_result(store, 2, {"final": (3, 5), "reg": (3, 5), "end": "REG"}, NOW)  # konflikt, nevyreseny
    _result(store, 3, True)
    M.record_result(store, 3, {"final": (2, 3), "reg": (2, 3), "end": "REG"}, NOW)  # konflikt ...
    M.correct_result(store, 3, (2, 3), (2, 3), "REG", "ověřeno v zápisu NHL", NOW)  # ... a vyslovna oprava
    store.close()  # zapas 4 nema vysledek
    rep = E.report(p, NOW)
    ex = rep["vyřazeno (ostrá reference)"]
    assert ex == {"S2: chybí ostrá férová linie": 1, "S6: nevyřešený konflikt výsledku": 1, "S6: výsledek není uložen": 1}
    s = E.collect(E.open_ro(p), "sharp", NOW)
    (it,) = next(iter(s["groups"].values()))
    assert it["fid"] == 3 and it["y"] == 0.0 and it["result_status"] == "opraveno"  # pouzita oprava (hoste vyhrali)
    assert rep["zápasy bez výsledku po 12 h (výběrové zkreslení)"] == 1


def test_anchored_ou_never_enters_pure_model_evaluation(tmp_path):
    p = tmp_path / "p4.db"
    store = Store(p)
    for fid in range(1, 31):
        M.record(store, _snap(fid, 0, T0 - timedelta(hours=2), T0 + timedelta(hours=fid)),
                 [_row(fid, "OU_6.5", "over", 0.30, 0.50, p_used=0.50)])
        _result(store, fid, True, total_over=fid % 2 == 0)
    store.close()
    a = E.report(p, NOW)["skupiny"]["m2-1 / kód abc123"]["A – čistý model vs. trh"]["OU"]
    cmp = a["srovnání log loss / Brier"]
    expected = np.mean([E.ll(0.30, float(f % 2 == 0)) for f in range(1, 31)])
    assert cmp["log loss"]["hodnocená"][0] == pytest.approx(expected)  # p_raw 0.30, ne ukotvene p_used 0.50
    assert "NENÍ" in a["poznámka"]


def test_unknown_code_version_is_reported_separately(tmp_path):
    p = tmp_path / "p4.db"
    build_db(p, 10, code_rev="abc123")
    build_db(p, 10, code_rev="unknown", start_id=100, seed=2)
    rep = E.report(p, NOW)
    assert set(rep["skupiny"]) == {"m2-1 / kód abc123", "m2-1 / kód unknown"}
    assert "VERZE KÓDU NEZNÁMÁ" in E.format_text(rep)


def test_integrity_links_are_checked(tmp_path):
    p = build_db(tmp_path / "p4.db", 5)
    c = sqlite3.connect(p)
    c.execute("INSERT INTO nhl_measurements(snapshot_id, fixture_id, market, selection, market_family, independence_used, "
              "blend_main_kind, blend_p4_kind, in_main, in_p4, comparable_sharp, comparable_p4, exclusion) "
              "VALUES(999, 1, 'MONEYLINE', 'home', 'MONEYLINE', 'x', 'x', 'x', 0, 0, 0, 0, '{}')")
    c.execute("INSERT INTO nhl_measurements(snapshot_id, fixture_id, market, selection, market_family, independence_used, "
              "blend_main_kind, blend_p4_kind, in_main, in_p4, comparable_sharp, comparable_p4, exclusion) "
              "VALUES(1, 777, 'MONEYLINE', 'away', 'MONEYLINE', 'x', 'x', 'x', 0, 0, 0, 0, '{}')")
    c.execute("INSERT INTO nhl_results VALUES(555, 1, 0, 1, 0, 'REG', 'x', 'x')")
    c.commit()
    c.close()
    i = E.report(p, NOW)["integrita"]
    assert i["radky_bez_snimku"] == 1 and i["radky_s_jinym_zapasem_nez_snimek"] == 1 and i["vysledky_bez_snimku"] == 1


# --- statistika a kontrolni body ------------------------------------------------------------------------------

def test_obrien_fleming_bounds_are_the_preregistered_ones():
    t = np.array(E.CHECKPOINTS) / E.CHECKPOINTS[-1]
    spent = [2 - 2 * norm.cdf(norm.ppf(0.975) / math.sqrt(x)) for x in t]
    c = [E.Z_BOUNDS[0]]
    assert 2 * (1 - norm.cdf(c[0])) == pytest.approx(spent[0], rel=1e-3)
    for k in (1, 2):
        ts = t[:k + 1]
        cov = np.array([[math.sqrt(min(a, b) / max(a, b)) for b in ts] for a in ts])
        cs = np.array(list(E.Z_BOUNDS[:k + 1]))
        cross = 1 - mvn(mean=np.zeros(k + 1), cov=cov).cdf(cs, lower_limit=-cs)
        assert cross == pytest.approx(spent[k], abs=2e-4)


def test_true_model_is_inconclusive_before_first_checkpoint_even_if_better(tmp_path):
    rep = E.report(build_db(tmp_path / "p4.db", 120, model="truth"), NOW)
    ml = rep["skupiny"]["m2-1 / kód abc123"]["A – čistý model vs. trh"]["MONEYLINE"]
    d = ml["srovnání log loss / Brier"]["log loss"]["rozdíl"]
    assert d[0] < 0  # bodove lepsi ...
    assert ml["test vnoření (primární)"]["verdikt"].startswith("NEPRŮKAZNÝ – před 1. kontrolním bodem")  # ... ale nerozhoduje se
    b = rep["skupiny"]["m2-1 / kód abc123"]["B – hybrid 15/85 vs. trh"]["MONEYLINE"]
    assert b["hlavní bot (logit 15/85) vs. ostrý trh – primární"]["verdikt"].startswith("NEPRŮKAZNÝ")


def test_true_model_at_checkpoint_600_is_detected_and_market_copy_is_not(tmp_path):
    """Na 300 zapasech nestaci ani dokonaly model proti sumivemu trhu (|z| ~ 3 < 3,488) – proto test v bode 600."""
    good = E.report(build_db(tmp_path / "a.db", 620, model="truth", seed=3, noise=0.2), NOW)
    enc = good["skupiny"]["m2-1 / kód abc123"]["A – čistý model vs. trh"]["MONEYLINE"]["test vnoření (primární)"]
    assert enc["β_model"][0] > 0 and enc["verdikt"].startswith("PRŮKAZNÝ ROZDÍL v kontrolním bodě 600")
    assert "přidává informaci" in enc["verdikt"]
    same = E.report(build_db(tmp_path / "b.db", 320, model="market", seed=3), NOW)
    cmp = same["skupiny"]["m2-1 / kód abc123"]["A – čistý model vs. trh"]["MONEYLINE"]["srovnání log loss / Brier"]
    assert cmp["log loss"]["rozdíl"][0] == pytest.approx(0.0, abs=1e-12)
    assert not cmp["verdikt"].startswith("PRŮKAZNÝ")
    assert cmp["verdikt"].startswith("SEKUNDÁRNÍ")  # srovnani log loss u A je sekundarni (primarni je test vnoreni)


def test_bootstrap_is_by_match_not_by_selection(tmp_path):
    """Tri linie OU stejneho zapasu nejsou tri nezavisla pozorovani: interval se nesmi zuzit jako u nezavislych."""
    one = E.report(build_db(tmp_path / "one.db", 80, model="noise", seed=5, ou_lines=1), NOW)
    three = E.report(build_db(tmp_path / "three.db", 80, model="noise", seed=5, ou_lines=3), NOW)

    def width(rep):
        ou = rep["skupiny"]["m2-1 / kód abc123"]["C – trh jako benchmark"]["OU"]["ostrý trh"]["log loss"]
        return ou[2] - ou[1]

    assert three["skupiny"]["m2-1 / kód abc123"]["C – trh jako benchmark"]["OU"]["ostrý trh"]["výběrů"] == 240
    assert width(three) > 0.8 * width(one)  # nezavisle vybery by daly ~ 1/sqrt(3) = 0.58 sirky


def test_report_does_not_touch_global_rng(tmp_path):
    p = build_db(tmp_path / "p4.db", 30)
    np.random.seed(11)
    st = np.random.get_state()[1].copy()
    E.report(p, NOW)
    assert (np.random.get_state()[1] == st).all()


def test_roi_and_clv_are_not_fabricated(tmp_path):
    rep = E.report(build_db(tmp_path / "p4.db", 10), NOW)
    assert rep["ROI / yield"].startswith("nelze z měřicích tabulek") and rep["CLV"].startswith("nelze z měřicích tabulek")
