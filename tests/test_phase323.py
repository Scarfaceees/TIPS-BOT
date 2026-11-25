"""Faze 3.2.3 – audit mereni: nekruhovost Profilu 3x, unikatni odmitnuti Profilu 5, rozsah CLV tabulek, pokryti
Market Integrity a shoda Excel / panel / CLI. Vse jen cte – produkcni vybery, EV ani pravidla se nemeni."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import re
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import openpyxl
import pytest
from test_freshness import db  # noqa: F401 - fixture databaze Profilu 4 (Store)
from test_marketsim import _entries

from tipsbot import export, freshness, marketcheck as MC, marketsim, paper, profil5, strategy
from tipsbot.models.football import score_matrix
from tipsbot.profil4 import history as H
from tipsbot.profil4.store import Store
from tipsbot.storage import Ledger, P5Candidate, make_session_factory

UTC = timezone.utc
_REAL_REPORT = freshness.report
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
KO = NOW + timedelta(hours=8)
CFG = marketsim.SimConfig(min_odds=1.01, max_odds=10, min_ev=-1, worst_ev=-1, max_loss=1)


def _sp(market, sel, odds):
    return marketsim.SimPick(market, sel, odds, "b", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, "", [])


def _with(entries, market, odds):
    return [dataclasses.replace(e, odds=odds) if e.market == market else e for e in entries]


# --- 1. Profil 3x: nezavislost odhadu golu -------------------------------------------------------------------------
def test_evaluated_market_price_does_not_move_lobo_reference_but_moves_production():
    """Zmena kurzu hodnoceneho trhu (OU_2.5, je ve fitu) nemeni nezavisly odhad golu; produkcni fit se pohne."""
    ent = _entries()
    o = next(e for e in ent if e.market == "OU_2.5").odds
    moved = _with(ent, "OU_2.5", (o[0] * 1.25, o[1] * 0.85))
    a = marketsim.lobo_market("x", None, ent, _sp("OU_2.5", "under", 1.6), CFG)
    b = marketsim.lobo_market("x", None, moved, _sp("OU_2.5", "under", 1.6), CFG)
    assert "OU_2.5" not in a["markets_used"]
    assert (a["lam"], a["mu"], a["ev_lobo"]) == (b["lam"], b["mu"], b["ev_lobo"])
    assert marketsim.fit_lambdas(ent)[:2] != marketsim.fit_lambdas(moved)[:2]


def test_other_markets_can_move_lobo_reference():
    ent = _entries()
    o = next(e for e in ent if e.market == "1X2").odds
    moved = _with(ent, "1X2", (o[0] * 0.8, o[1], o[2] * 1.3))
    a = marketsim.lobo_market("x", None, ent, _sp("OU_2.5", "under", 1.6), CFG)
    b = marketsim.lobo_market("x", None, moved, _sp("OU_2.5", "under", 1.6), CFG)
    assert (a["lam"], a["mu"]) != (b["lam"], b["mu"])


def test_whole_line_is_never_in_fit_so_lobo_ev_equals_production_ev():
    """Pricina shodneho EV na listu Profile 3 LOBO: cele/ctvrtinove linie se do odhadu golu nepouzivaji ani v
    produkci, takze vynechani hodnoceneho trhu nic nemeni. Polovicni linie se do fitu berou a EV se lisi."""
    assert not marketsim.in_fit("TTH_2.0") and not marketsim.in_fit("OU_2.25") and not marketsim.in_fit("DC")
    assert marketsim.in_fit("OU_2.5") and marketsim.in_fit("TTH_1.5") and marketsim.in_fit("1X2")
    ent = _entries()
    lam, mu, *_ = marketsim.fit_lambdas(ent)
    own = marketsim.evaluate("OU_4.0", "under", 1.33, score_matrix(lam, mu, marketsim.RHO, marketsim.MAX_GOALS))["ev"]
    lb = marketsim.lobo_market("x", None, ent, _sp("OU_4.0", "under", 1.33), CFG)
    assert lb["ev_lobo"] == pytest.approx(round(own, 4), abs=1e-4)
    own_h = marketsim.evaluate("OU_2.5", "under", 1.6, score_matrix(lam, mu, marketsim.RHO, marketsim.MAX_GOALS))["ev"]
    assert marketsim.lobo_market("x", None, ent, _sp("OU_2.5", "under", 1.6), CFG)["ev_lobo"] != pytest.approx(own_h, abs=1e-4)


def test_lobo_ev_uses_independent_probability():
    """EV 3x = p(nezavisly odhad golu) × kurz − 1 (polovicni linie: jen vyhra / prohra)."""
    ent = _entries()
    lb = marketsim.lobo_market("x", None, ent, _sp("OU_2.5", "under", 1.6), CFG)
    m = score_matrix(lb["lam"], lb["mu"], marketsim.RHO, marketsim.MAX_GOALS)
    p_under = float(sum(m[i, j] for i in range(m.shape[0]) for j in range(m.shape[1]) if i + j < 3))
    assert lb["ev_lobo"] == pytest.approx(p_under * 1.6 - 1, abs=2e-4)


def test_lobo_without_enough_markets_is_unverified_not_zero():
    ent = list({e.market: e for e in _entries() if e.market in ("OU_2.5", "1X2")}.values())  # 1 trh po vynechani
    lb = marketsim.lobo_market("x", None, ent, _sp("OU_2.5", "under", 1.6), CFG)
    assert lb["status"] == "NEOVĚŘENO" and "ev_lobo" not in lb


# --- 1b. reference kancelari (leave-one-bookmaker-out) ----------------------------------------------------------
def _books(**over):
    b = {"pinnacle": {"1": 1.97, "X": 3.45, "2": 3.95}, "bet365": {"1": 1.95, "X": 3.40, "2": 3.90},
         "williamhill": {"1": 1.93, "X": 3.40, "2": 3.85}, "unibet_se": {"1": 1.96, "X": 3.35, "2": 3.80},
         "unibet_nl": {"1": 1.96, "X": 3.35, "2": 3.80}, "marathonbet": {"1": 1.98, "X": 3.40, "2": 3.90}}
    b.update(over)
    return b


SELS = ("1", "X", "2")


def test_evaluated_book_and_its_group_are_not_in_reference():
    r = MC.consensus(_books(), SELS, "1", NOW, None, "unibet_se")
    assert "unibet_se" not in r["included"] and "unibet_nl" not in r["included"]
    assert r["excluded"]["unibet_se"] == "hodnocená kancelář"
    assert r["excluded"]["unibet_nl"] == "stejná skupina jako hodnocená kancelář"
    assert r["n"] == 4


def test_evaluated_book_price_does_not_change_reference_other_books_do():
    base = MC.consensus(_books(), SELS, "1", NOW, None, "unibet_se")
    moved = MC.consensus(_books(unibet_se={"1": 2.40, "X": 3.0, "2": 3.4}, unibet_nl={"1": 2.40, "X": 3.0, "2": 3.4}),
                         SELS, "1", NOW, None, "unibet_se")
    assert moved["p"] == base["p"]
    other = MC.consensus(_books(pinnacle={"1": 1.70, "X": 3.9, "2": 4.8}, bet365={"1": 1.72, "X": 3.9, "2": 4.7}),
                         SELS, "1", NOW, None, "unibet_se")
    assert other["p"] != base["p"]


def _p3pick(market="TTH_2.0", lobo=None, integrity=None, mid="m1", profile="profil3", meta=None, odds=1.36,
            book="zahraničí (API-Football, 2 kanc.)"):
    return paper.Pick(id=f"{profile}-{mid}", league="UCL", match_id=mid, name=f"A{mid} - B{mid}", day="2026-11-24",
                      kickoff=KO.isoformat(), market=market, selection="under", odds=odds,
                      book=book, p_model=0.7, source="model",
                      created=NOW.replace(tzinfo=None).isoformat(), profile=profile,
                      meta=meta if meta is not None else {"p3": {}, "p3_lobo": lobo or {}}, integrity=integrity or {})


OK_LOBO = {"ev_own": -0.0185, "ev_lobo": -0.0185, "status": "OK", "markets_used": ["1X2", "OU_2.5"]}
NO_MARKET = {"status": MC.UNVERIFIED, "reference": MC.UNVERIFIED, "reasons": ["trh není v kurzech kanceláří (nelze ověřit)"]}


def test_p3_audit_states_and_empty_ev_with_reason():
    weak = {"status": MC.UNVERIFIED, "reference": MC.UNVERIFIED, "ref_n": 2, "edge": 0.07, "validity": MC.OK,
            "ref_excluded": {"bet365": "hodnocená kancelář"}, "reasons": ["málo nezávislých kanceláří"]}
    good = {"status": MC.OK, "reference": MC.OK, "ref_n": 4, "edge": 0.031, "validity": MC.OK,
            "ref_excluded": {"bet365": "hodnocená kancelář", "pinnacle": "burza"}, "reasons": []}
    picks = [
        _p3pick(lobo=OK_LOBO, integrity=NO_MARKET, mid="a"),
        _p3pick("OU_2.5", lobo={"ev_own": 0.01, "status": "NEOVĚŘENO", "why": "málo trhů", "markets_used": ["1X2"]},
                integrity=weak, mid="b"),
        _p3pick("OU_2.5", lobo={**OK_LOBO, "markets_used": ["OU_2.5", "1X2"]}, integrity=good, mid="c", book="betano"),
        _p3pick("OU_2.5", lobo=OK_LOBO, integrity=good, mid="f", book="zahraničí (medián)"),
        _p3pick(lobo={**OK_LOBO, "ev_lobo": None}, integrity={"status": MC.UNVERIFIED,
                                                              "reasons": ["kontrola selhala: x"]}, mid="d"),
        _p3pick(lobo=OK_LOBO, integrity={"status": MC.UNVERIFIED, "validity": "NESHODA", "reasons": ["výběr"]}, mid="e"),
    ]
    rows = {r["name"]: r for r in strategy.p3_audit(picks)["rows"]}
    a, b, c, d, e, f = (rows[f"A{m} - B{m}"] for m in "abcdef")
    assert (a["model_status"], a["books_status"], a["ev_books"]) == ("OK", "MISSING_MARKET", None)
    assert "celá/čtvrtinová" in a["model_why"] and a["in_fit"] == "ne"
    assert (b["model_status"], b["ev_lobo"]) == ("MISSING_MARKET", None)  # nedostupny vypocet neni EV 0
    assert (b["books_status"], b["ev_books"], b["groups"]) == ("INSUFFICIENT_BOOKMAKERS", None, 2)
    assert "bet365" in b["left_out"]
    assert c["model_status"] == "CIRCULAR_REFERENCE" and c["ev_lobo"] is None
    assert (c["books_status"], c["ev_books"]) == ("OK", 0.031) and "pinnacle" not in c["left_out"]
    assert (d["model_status"], d["books_status"]) == ("CALCULATION_ERROR", "CALCULATION_ERROR")
    assert e["books_status"] == "INVALID_LINE"
    # kurz = median kancelari, reference z tychz kancelari -> neni nezavisla, EV proti nim se nevykazuje
    assert (f["books_status"], f["ev_books"]) == ("CIRCULAR_REFERENCE", None)


def test_p3x_pick_is_compared_on_same_match_and_only_p3_split_by_start():
    old = _p3pick(lobo=None, meta={"p3": {}}, mid="old")
    old.kickoff = (NOW - timedelta(days=1)).isoformat()
    p3 = _p3pick(lobo=OK_LOBO, integrity=NO_MARKET, mid="a")
    x = _p3pick(profile="profil3x", mid="a", meta={"p3": {"scenarios": {"trh": {"ev": -0.0185}}}})
    au = strategy.p3_audit([old, p3, x])
    r = au["rows"][0]
    assert (r["ev_p3x"], r["p3x_bet"]) == (-0.0185, "stejná sázka")
    assert (au["only_p3_before_start"], au["only_p3_after_start"], au["equal_ev"], au["not_in_fit"]) == (1, 0, 1, 1)


# --- 2. Profil 5: unikatni nabidky a prekryvajici se duvody --------------------------------------------------------
def _row(status, book, reasons, mid="e1", sel="2"):
    return SimpleNamespace(status=status, match_id=mid, market="1X2", selection=sel, book=book,
                           reasons=json.dumps(reasons, ensure_ascii=False))


def test_p5_counts_unique_offers_and_overlapping_reasons():
    rej = [_row("rejected", "unibet_se", ["kurz 8.00 mimo pásmo 1.40–4.20", "kurz o +18 % nad mediánem 17 kanceláří"]),
           _row("rejected", "unibet_nl", ["kurz 8.00 mimo pásmo 1.40–4.20", "kurz o +18 % nad mediánem 17 kanceláří",
                                          "kurz o +10 % nad férovou cenou trhu a nepotvrzen"]),
           _row("rejected", "marathonbet", ["kurz o +12 % nad mediánem 8 kanceláří"], sel="1")]
    cand = [_row("candidate", "gtbets", [], sel="X")]
    c = profil5.counts(cand, rej, [{profil5.NO_REF: 5, "posouzeno nabídek": 9}, {"posouzeno nabídek": 3}])
    assert (c["candidates"], c["rejected"], c["rejected_groups"]) == (1, 3, 2)  # unibet_se + unibet_nl = 1 skupina
    assert (c["reason_instances"], c["multi_reason"]) == (6, 2)
    med = c["reasons"]["cenový extrém (nad mediánem)"]
    assert (med["offers"], med["instances"], med["overlap"]) == (3, 3, 2)
    assert c["reasons"]["kurz mimo pásmo"]["offers"] == 2
    assert sum(v["offers"] for v in c["reasons"].values()) > c["rejected"]  # duvody se prekryvaji, nabidky ne
    assert (c["no_reference_last_scan"], c["assessed_last_scan"], c["overlap_cand_rej"]) == (5, 12, 0)


def test_p5_repeated_scans_do_not_duplicate_offers():
    from tipsbot.oddsapi import Event, MarketOdds

    led = Ledger(make_session_factory("sqlite://")())
    books = _books(unibet_se={"1": 1.70, "X": 3.6, "2": 6.0}, unibet_nl={"1": 1.70, "X": 3.6, "2": 6.0})
    now = NOW.replace(tzinfo=None)
    ev = Event("oddsapi:e1", "ENG1", "Arsenal", "Chelsea", now + timedelta(hours=6), {"1X2": MarketOdds("1X2", SELS, books)})
    stamps = {"oddsapi:e1": {b: {"h2h": now - timedelta(minutes=10)} for b in books}}
    profil5.scan(led, "ENG1", [ev], now, stamps)
    first = profil5.report(led.s)["counts"]
    profil5.scan(led, "ENG1", [ev], now + timedelta(minutes=30), stamps)
    second = profil5.report(led.s)["counts"]
    assert first["rejected"] >= 2 and first == {**second, "no_reference_last_scan": first["no_reference_last_scan"],
                                                "assessed_last_scan": first["assessed_last_scan"]}
    assert led.s.query(P5Candidate).count() == first["candidate_rows"] + first["rejected_rows"]
    assert first["rejected_groups"] < first["rejected"]  # unibet_se a unibet_nl jsou jedna skupina


# --- 3. CLV: stejny rozsah obou tabulek, presne odvozeni ------------------------------------------------------------
def _tip(st, fid, sel, kind, result="won", decision="APPROVED BET", after=False, clv=None):
    ko = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    pt = ko + timedelta(minutes=5) if after else ko - timedelta(hours=8)
    st.execute("INSERT INTO tips(fixture_id, match, league, kickoff, market, selection, probs, odds, bookmaker, odds_source, "
               "decision, predicted_at, model_version, config_hash, result, profit, closing_kind, clv) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (fid, f"A{fid} - B{fid}", "Premier League", ko.isoformat(), "1X2", sel, "{}", 2.0, "pinnacle", "api",
                decision, pt.isoformat(), "t", "h", result, None if result is None else 1.0, kind, clv))


@pytest.fixture
def clvdb(tmp_path):
    path = tmp_path / "clv.db"
    st = Store(path)
    _tip(st, 1, "1", H.CK_CLOSING, clv=0.02)  # zapocitana sazka
    _tip(st, 1, "1", H.CK_STALE)  # opakovane ulozeny tip (dalsi sken) – jen jednou
    _tip(st, 2, "1", H.CK_STALE, after=True)  # ulozen az po vykopu
    _tip(st, 3, "1", None, result="void")  # neplatna sazka
    _tip(st, 4, "1", None, result=None)  # zatim nevyhodnoceno
    st.close()
    return path


def test_clv_scope_counts_every_excluded_tip_with_reason(clvdb):
    import sqlite3

    con = sqlite3.connect(clvdb)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM tips ORDER BY id")]
    tips, ex = H.clv_scope(rows)
    assert [t["fixture_id"] for t in tips] == [1, 4]
    assert (ex["rows"], ex["duplicates"], ex["after_kickoff"], ex["void"]) == (5, 1, 1, 1)
    assert (ex["settled_rows"], ex["settled_duplicates"], ex["settled_after_kickoff"]) == (4, 1, 1)


def test_clv_performance_and_odds_freshness_share_scope(clvdb):
    import sqlite3

    from tipsbot.profil4.performance import clv_performance

    con = sqlite3.connect(clvdb)
    con.row_factory = sqlite3.Row
    perf = clv_performance([dict(r) for r in con.execute("SELECT * FROM tips")], [], NOW)
    fr = freshness.report(clvdb, NOW)
    assert perf["tips"] == 2 and perf["classes"] == {"skutečný závěrečný kurz (stejná kancelář)": 1,
                                                     "zatím nevyhodnoceno": 1}
    assert fr["clv_scope"]["settled"] == perf["settled"] == 1
    fb = next(r for r in fr["clv"] if r["scope"] == "všechny vyhodnocené tipy")
    assert fb["tips"] == 1 and fb[freshness.CLV_OK_API] == 1 and fb[freshness.CLV_STALE] == 0  # duplicita nepocitana
    a = {"clv_perf": {"included": perf["tips"]}, "clv_fresh": fr["clv_scope"]}
    assert export.clv_reconcile(a) == ("Vyhodnocených záznamů v databázi Profilu 4: 4 − 1 opakovaně uložených − 1 "
                                       "uložených po výkopu − 1 neplatných = 1 vyhodnocených sázek (Odds Freshness). "
                                       "CLV Performance = 1 vyhodnocených + 1 zatím nevyhodnocených = 2.")


def test_stale_and_missing_closing_are_never_clv_zero(clvdb):
    import sqlite3

    from tipsbot.profil4.performance import clv_performance

    fr = freshness.report(clvdb, NOW)
    assert all(r["verified"] == r[freshness.CLV_OK_API] + r[freshness.CLV_OK_ODDSAPI] for r in fr["clv"])
    con = sqlite3.connect(clvdb)
    con.row_factory = sqlite3.Row
    perf = clv_performance([dict(r) for r in con.execute("SELECT * FROM tips")], [], NOW)
    assert perf["valid"] == 1 and perf["overall"]["clv_n"] == 1  # zastaraly ani chybejici kurz neni CLV 0


# --- 4. Market Integrity: pokryti ---------------------------------------------------------------------------------
def test_integrity_coverage_separates_unchecked_from_ok_and_counts_only_comparable_offers():
    ok = {"status": MC.OK, "t": "2026-11-24T12:00"}
    picks = [_p3pick(mid="a", profile="zaklad", meta={}, integrity=ok),
             _p3pick(mid="a", profile="profil2", meta={}, integrity=ok),  # stejna nabidka u 2 profilu = porovnatelna
             _p3pick(mid="b", profile="zaklad", meta={}, integrity={"status": MC.UNVERIFIED, "t": "x"}),
             _p3pick(mid="c", profile="zaklad", meta={}), _p3pick(mid="d", profile="profil2", meta={})]
    m = strategy.integrity_coverage(picks)
    assert (m["selections"], m["checked"], m["unchecked"], m["ok"], m["unverified"]) == (5, 3, 2, 2, 1)
    assert (m["comparable"], m["mismatches"], m["coverage"], m["verified_coverage"]) == (1, 0, 0.6, 0.4)
    picks[1].integrity = {"status": MC.SUSPECT, "t": "2026-11-24T12:00"}
    m = strategy.integrity_coverage(picks)
    assert (m["comparable"], m["mismatches"], m["suspect"]) == (1, 1, 1)


# --- 5. shoda Excel / panel / CLI ---------------------------------------------------------------------------------
def _excel_audit(wb) -> dict:
    keys = {v: k for k, v in export.AUDIT_TITLES.items()}
    out = {}
    for key, name in export.AUDIT_SHEETS.items():
        rows = list(wb[name].iter_rows(min_row=6))
        i = next(i for i, r in enumerate(rows) if r[0].value == export.AUDIT_TITLES[key])
        head = [c.value for c in rows[i + 1] if c.value is not None]
        body, j = [], i + 2
        while j < len(rows) and rows[j][0].value is not None and rows[j][0].value not in keys:
            body.append([c.value for c in rows[j][:len(head)]])
            j += 1
        out[key] = (head, body)
    return out


@pytest.fixture
def world(tmp_path, db, monkeypatch):  # noqa: F811
    from tipsbot.oddsapi import Event, MarketOdds
    from tipsbot.profil4 import config as P4C

    url = f"sqlite:///{tmp_path / 'w.db'}"
    s = make_session_factory(url)()
    led = Ledger(s)
    good = {"status": MC.OK, "reference": MC.OK, "ref_n": 4, "edge": 0.031, "validity": MC.OK, "t": "t",
            "ref_excluded": {"bet365": "hodnocená kancelář"}, "reasons": []}
    paper.save(led, [
        _p3pick(lobo=OK_LOBO, integrity={**NO_MARKET, "t": "t"}, mid="a"),
        _p3pick("OU_2.5", lobo={"ev_own": 0.01, "status": "NEOVĚŘENO", "why": "málo trhů", "markets_used": []},
                integrity=good, mid="b"),
        _p3pick(profile="profil3x", mid="a", meta={"p3": {"scenarios": {"trh": {"ev": -0.0185}}}},
                integrity={**NO_MARKET, "t": "t"}),
        _p3pick(profile="zaklad", mid="c", meta={})])
    books = _books(unibet_se={"1": 1.70, "X": 3.6, "2": 6.0}, unibet_nl={"1": 1.70, "X": 3.6, "2": 6.0})
    now = NOW.replace(tzinfo=None)
    ev = Event("oddsapi:e1", "ENG1", "Arsenal", "Chelsea", now + timedelta(hours=6), {"1X2": MarketOdds("1X2", SELS, books)})
    profil5.scan(led, "ENG1", [ev], now, {"oddsapi:e1": {b: {"h2h": now} for b in books}})
    s.commit()
    monkeypatch.setattr(P4C, "P4", dataclasses.replace(P4C.P4, data_dir=db.parent, db_name=db.name))
    real = freshness.report
    monkeypatch.setattr(freshness, "report", lambda p, now=None: real(db, NOW))
    return SimpleNamespace(url=url, s=s, db=db)


def test_excel_panel_and_cli_show_same_audit_values(world):
    from tipsbot.profilecli import run
    from tipsbot.web import create_app

    d = export.collect(world.s, world.db, NOW)
    wb = openpyxl.load_workbook(io.BytesIO(export.to_xlsx(d)))
    assert len(wb.sheetnames) == 30  # zadny novy list, puvodni nazvy a poradi
    xl = _excel_audit(wb)
    tables = export.audit_tables(d.audit)
    assert set(xl) == set(tables) and all(xl[k][1] for k in xl)
    html = create_app(world.url).test_client().get("/?tab=sledovani&audit=1").get_data(as_text=True)
    assert "Audit měření" in html
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert run(Namespace(what="measure", action="report", db=world.url)) == 0
    cli = buf.getvalue().splitlines()
    for key, (head, body) in xl.items():
        fmt = export.AUDIT_FMT[key]
        assert head == tables[key][0], key
        m = re.search(rf'<table class="audit" data-key="{key}">(.*?)</table>', html, re.S)
        assert m, key
        cells = [re.findall(r"<td>(.*?)</td>", tr, re.S) for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S)[1:]]
        assert len(cells) == len(body), key
        for prow, xrow in zip(cells, body):
            want = [export.fcell(v, fmt.get(j)) for j, v in enumerate(xrow, 1)]
            assert [c.replace("&amp;", "&").replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">") for c in prow] == want, key
            assert " | ".join(want) in cli, key
    assert export.clv_reconcile(d.audit) in cli


def test_unavailable_values_stay_empty_with_reason_not_zero(world):
    d = export.collect(world.s, world.db, NOW)
    wb = openpyxl.load_workbook(io.BytesIO(export.to_xlsx(d)))
    head, body = _excel_audit(wb)["p3"]
    row = next(r for r in body if r[1] == "OU_2.5")
    assert row[head.index("EV bez hodnoceného trhu (3x)")] is None
    assert row[head.index("Stav nekruhového odhadu")] == "MISSING_MARKET"
    first = next(r for r in body if r[1] == "TTH_2.0")
    assert first[head.index("EV proti kancelářím (bez hodnocené)")] is None
    assert first[head.index("Stav reference kanceláří")] == "MISSING_MARKET" and first[head.index("Důvod reference")]
    assert first[head.index("Nezávislých skupin")] is None  # ne 0
    assert export.fcell(None, export.EVF) == "–"
    mi = dict((r[0], r[1]) for r in _excel_audit(wb)["mi"][1])
    assert mi["Neověřeno – bez záznamu kontroly (starší výběry)"] == 1 and mi["OVĚŘENO (OK)"] == 1


# --- 6. datovy snapshot: Excel, panel i CLI ctou stejna data ---------------------------------------------------------
def test_snapshot_is_shared_by_excel_panel_and_cli_and_ignores_later_scan(world, tmp_path, monkeypatch):
    from tipsbot import snapshot
    from tipsbot.profilecli import run
    from tipsbot.web import create_app

    monkeypatch.setattr(freshness, "report", _REAL_REPORT)  # snapshot cte vlastni kopii Profilu 4
    monkeypatch.setenv("TIPSBOT_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    live = tmp_path / "w.db"
    meta = snapshot.create(live, world.db, snapshot.root())
    assert re.fullmatch(r"S\d{8}-\d{6}-[0-9a-f]{6}", meta["id"])
    assert snapshot.get(snapshot.root(), "../w.db") is None  # jen ID ze seznamu, zadne cesty
    # „automaticky sken“ po snapshotu: novy vyber v zive databazi
    led = Ledger(world.s)
    paper.save(led, paper.load(led) + [_p3pick(profile="zaklad", mid="z", meta={}, integrity={"status": MC.OK, "t": "t"})])
    world.s.commit()

    client = create_app(world.url).test_client()
    resp = client.get(f"/export/xlsx?snapshot={meta['id']}")
    assert resp.status_code == 200 and meta["id"] in resp.headers["Content-Disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(resp.data))
    assert meta["id"] in str(wb["Souhrn"]["A2"].value)
    xl = _excel_audit(wb)
    html = client.get(f"/?tab=sledovani&audit=1&snapshot={meta['id']}").get_data(as_text=True)
    assert meta["id"] in html
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert run(Namespace(what="measure", action="report", db=world.url, snapshot=meta["id"])) == 0
    cli = buf.getvalue()
    assert meta["id"] in cli
    mi = dict((r[0], r[1]) for r in xl["mi"][1])
    assert mi["Výběrů celkem (bez kombi)"] == 4  # snapshot: novy vyber z pozdejsiho „skenu“ v nem neni
    for key, (_, body) in xl.items():
        fmt = export.AUDIT_FMT[key]
        for xrow in body:
            assert " | ".join(export.fcell(v, fmt.get(j)) for j, v in enumerate(xrow, 1)) in cli, key
    assert "<td>4</td>" in html.split('data-key="mi"')[1].split("</table>")[0]
    live_html = client.get("/?tab=sledovani&audit=1").get_data(as_text=True)
    assert "<td>5</td>" in live_html.split('data-key="mi"')[1].split("</table>")[0]  # ziva databaze uz ma 5
    assert "Živá databáze bez snapshotu" in live_html


def test_snapshot_of_same_data_has_same_fingerprint(db, tmp_path):  # noqa: F811
    from tipsbot import snapshot

    s = make_session_factory(f"sqlite:///{tmp_path / 'a.db'}")()
    paper.save(Ledger(s), [_p3pick(profile="zaklad", mid="z", meta={})])
    s.commit()
    s.close()
    a = snapshot.create(tmp_path / "a.db", db, tmp_path / "snaps", datetime(2026, 11, 24, 18, 0, tzinfo=UTC))
    b = snapshot.create(tmp_path / "a.db", db, tmp_path / "snaps", datetime(2026, 11, 24, 18, 1, tzinfo=UTC))
    assert a["id"][-6:] == b["id"][-6:] and a["id"] != b["id"]
    assert [m["id"] for m in snapshot.items(tmp_path / "snaps")] == [b["id"], a["id"]]


def test_snapshot_is_opened_read_only_and_stays_unchanged(world, tmp_path, monkeypatch):
    import hashlib

    from sqlalchemy.exc import OperationalError

    from tipsbot import snapshot
    from tipsbot.profilecli import run
    from tipsbot.web import create_app

    monkeypatch.setattr(freshness, "report", _REAL_REPORT)
    monkeypatch.setenv("TIPSBOT_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    meta = snapshot.create(tmp_path / "w.db", world.db, snapshot.root())
    d = snapshot.root() / meta["id"]
    digest = lambda: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.glob("*.db")}  # noqa: E731
    before = digest()
    url, _, _ = snapshot.paths(snapshot.root(), meta)
    with snapshot.session_factory(url)() as s:  # zapis do snapshotu selze
        with pytest.raises(OperationalError):
            s.execute(__import__("sqlalchemy").text("CREATE TABLE x(a)"))
    client = create_app(world.url).test_client()
    assert client.get(f"/export/xlsx?snapshot={meta['id']}").status_code == 200
    client.get(f"/?tab=sledovani&audit=1&snapshot={meta['id']}")
    with contextlib.redirect_stdout(io.StringIO()):
        run(Namespace(what="measure", action="report", db=world.url, snapshot=meta["id"]))
    assert digest() == before


def test_old_meta_v1_snapshot_works_in_cli_panel_excel_and_failed_is_never_published(world, tmp_path, monkeypatch):
    """Snapshot ve formatu do 25. 11. (meta.json bez auditnich poli) funguje v CLI, panelu i Excelu; zaznam
    neuspechu ve failed/ se nikde nevystavi jako snapshot."""
    import json

    from tipsbot import snapshot
    from tipsbot.profilecli import run
    from tipsbot.web import create_app

    monkeypatch.setattr(freshness, "report", _REAL_REPORT)
    monkeypatch.setenv("TIPSBOT_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    meta = snapshot.create(tmp_path / "w.db", world.db, snapshot.root())
    old = {k: meta[k] for k in ("id", "created", "copied_until", "files", "attempts", "sizes")}
    (snapshot.root() / meta["id"] / "meta.json").write_text(json.dumps(old), encoding="utf-8")
    with pytest.raises(snapshot.SnapshotBusy):  # neuspesny pokus -> failed/F*.json
        snapshot.create(tmp_path / "w.db", world.db, snapshot.root(), deadline_s=-1, chunk=1)
    (fail,) = (snapshot.root() / snapshot.FAILED_DIR).glob("F*.json")
    assert [m["id"] for m in snapshot.items(snapshot.root())] == [old["id"]]
    for sid in (snapshot.FAILED_DIR, fail.stem, fail.name, f"{snapshot.FAILED_DIR}/{fail.name}"):
        assert snapshot.get(snapshot.root(), sid) is None

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert run(Namespace(what="snapshot", action="list", db=world.url)) == 0
        assert run(Namespace(what="measure", action="report", db=world.url, snapshot=old["id"])) == 0
    cli = buf.getvalue()
    assert f"{old['id']}  data k" in cli and "starší snapshot bez auditních metadat" in cli
    assert "Zdroj dat: Snapshot " + old["id"] in cli and fail.stem not in cli

    client = create_app(world.url).test_client()
    resp = client.get(f"/export/xlsx?snapshot={old['id']}")
    assert resp.status_code == 200 and old["id"] in resp.headers["Content-Disposition"]
    assert old["id"] in str(openpyxl.load_workbook(io.BytesIO(resp.data))["Souhrn"]["A2"].value)
    assert old["id"] in client.get(f"/?tab=sledovani&audit=1&snapshot={old['id']}").get_data(as_text=True)
    tab = client.get("/?tab=export").get_data(as_text=True)
    assert old["id"] in tab and fail.stem not in tab
    for sid in (snapshot.FAILED_DIR, fail.stem):  # zaznam neuspechu nejde pouzit jako zdroj dat
        r = client.get(f"/export/xlsx?snapshot={sid}", follow_redirects=True)  # zpet na panel se zpravou
        assert r.headers["Content-Type"].startswith("text/html") and "Content-Disposition" not in r.headers
        assert f"snapshot {sid} neexistuje" in r.get_data(as_text=True)
        html = client.get(f"/?tab=sledovani&audit=1&snapshot={sid}", follow_redirects=True).get_data(as_text=True)
        assert "neexistuje" in html


def _big_db(path, rows=20000):
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t(v) VALUES(?)", (("x" * 200,) for _ in range(rows)))
    con.commit()
    con.close()


def test_snapshot_does_not_block_writer(tmp_path):
    """Automat zapisuje behem snapshotu: zapisovac s kratkym timeoutem nesmi dostat „database is locked“
    (24. 11. 2026 v Dockeru: kopie v jednom kroku drzela zamek a Profil 4 selhal)."""
    import sqlite3
    import threading
    import time as _t

    from tipsbot import snapshot

    a, b = tmp_path / "a.db", tmp_path / "b.db"
    _big_db(a)
    _big_db(b)
    errors, done = [], threading.Event()

    def writer():
        con = sqlite3.connect(b, timeout=1.0)
        while not done.is_set():
            try:
                con.execute("INSERT INTO t(v) VALUES('w')")
                con.commit()
            except sqlite3.OperationalError as e:
                errors.append(str(e))
            _t.sleep(0.02)
        con.close()

    th = threading.Thread(target=writer)
    th.start()
    try:
        try:
            snapshot.create(a, b, tmp_path / "snaps", deadline_s=5, chunk=16)
        except snapshot.SnapshotBusy:
            pass  # povoleny vysledek pri neustalych zapisech: snapshot se vzda, zapisy maji prednost
    finally:
        done.set()
        th.join()
    assert errors == []
    assert not list((tmp_path / "snaps").glob(".tmp-*"))  # rozpracovana kopie se nenechava


def test_snapshot_gives_up_after_deadline_and_cleans_up(tmp_path):
    from tipsbot import snapshot

    a = tmp_path / "a.db"
    _big_db(a)
    with pytest.raises(snapshot.SnapshotBusy):
        snapshot.create(a, None, tmp_path / "snaps", deadline_s=-1, chunk=1)
    assert [x.name for x in (tmp_path / "snaps").iterdir()] == [snapshot.FAILED_DIR]  # jen diagnostika
    assert snapshot.items(tmp_path / "snaps") == []
