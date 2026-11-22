"""Profil 4 – Faze 3.1 (Market Integrity & Ranking Validation).

Referencni tabulky settlementu jsou psane rucne (nezavisle na mathcore i integrity), ostatni testy pouzivaji
zaznam odpovedi API-Football a synteticka data s pevnym seedem – nic se nestahuje."""

import argparse
import io
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from tipsbot.profil4 import decision as D, history, integrity as IN, intelligence as IG, mathcore, odds as O
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store, iso

UTC = timezone.utc
W, HW, P, HL, L = mathcore.WON, mathcore.HALF_WON, mathcore.PUSH, mathcore.HALF_LOST, mathcore.LOST

# (trh, vyber, skore domaci, skore hoste, ocekavany vysledek) – rucne podle pravidel kancelari
REFERENCE = [
    # 1X2, DNB, BTTS
    ("1X2", "1", 2, 1, W), ("1X2", "X", 1, 1, W), ("1X2", "2", 1, 1, L),
    ("DNB", "home", 1, 1, P), ("DNB", "away", 0, 2, W), ("DNB", "home", 0, 1, L),
    ("BTTS", "yes", 1, 1, W), ("BTTS", "no", 2, 0, W),
    # nulovy handicap = DNB
    ("AH_0.0", "home", 1, 1, P), ("AH_0.0", "away", 0, 1, W),
    # cele linie (linie z pohledu domacich): domaci -1, hoste +1
    ("AH_-1.0", "home", 2, 1, P), ("AH_-1.0", "home", 3, 1, W), ("AH_-1.0", "away", 2, 1, P), ("AH_-1.0", "away", 1, 1, W),
    ("AH_1.0", "home", 0, 1, P), ("AH_1.0", "away", 0, 2, W), ("AH_1.0", "away", 0, 1, P),
    # pulove linie
    ("AH_-0.5", "home", 1, 0, W), ("AH_-0.5", "home", 1, 1, L), ("AH_-0.5", "away", 1, 1, W),
    ("AH_1.5", "home", 0, 1, W), ("AH_1.5", "away", 0, 1, L), ("AH_1.5", "away", 0, 2, W), ("AH_-1.5", "away", 1, 0, W),
    # ctvrtinove linie: -0.25 = pul 0 a pul -0.5; -0.75 = pul -0.5 a pul -1
    ("AH_-0.25", "home", 1, 1, HL), ("AH_-0.25", "away", 1, 1, HW), ("AH_-0.25", "home", 1, 0, W),
    ("AH_0.25", "home", 1, 1, HW), ("AH_0.25", "away", 1, 1, HL), ("AH_0.25", "away", 0, 1, W),
    ("AH_-0.75", "home", 1, 0, HW), ("AH_-0.75", "away", 1, 0, HL), ("AH_-0.75", "home", 2, 0, W),
    ("AH_0.75", "away", 0, 1, HW), ("AH_0.75", "home", 0, 1, HL), ("AH_-1.25", "home", 2, 1, HL),
    ("AH_-1.75", "home", 2, 0, HW), ("AH_1.75", "away", 0, 2, HW),
    # over/under vcetne ctvrtinovych
    ("OU_2.5", "over", 2, 1, W), ("OU_2.0", "over", 1, 1, P), ("OU_2.25", "over", 1, 1, HL), ("OU_2.25", "under", 1, 1, HW),
    ("OU_2.75", "over", 2, 1, HW), ("OU_2.75", "under", 2, 1, HL), ("TTH_1.0", "over", 1, 3, P), ("TTA_0.5", "over", 2, 1, W),
]


@pytest.mark.parametrize("market,sel,h,a,expected", REFERENCE)
def test_reference_settlement_production_and_independent(market, sel, h, a, expected):
    assert mathcore.settle(market, sel, h, a) == expected  # produkcni settlement
    assert IG.independent_settle(market, sel, h, a) == expected  # nezavisla implementace (integrity.outcome)


@pytest.mark.parametrize("value,market,sel", [
    ("Home -1.5", "AH_-1.5", "home"), ("Away -1.5", "AH_-1.5", "away"),  # cislo = handicap domacich u obou stran
    ("Home +1", "AH_1.0", "home"), ("Away +1", "AH_1.0", "away"), ("Home 0", "AH_0.0", "home"), ("Away 0", "AH_0.0", "away"),
    ("Home -0.5,-1", "AH_-0.75", "home"), ("Away -0.5,-1", "AH_-0.75", "away"), ("Home +0.25", "AH_0.25", "home"),
    ("Away -0.25", "AH_-0.25", "away"), ("Home -1.75", "AH_-1.75", "home"),
])
def test_api_handicap_mapping_never_flips_sign(value, market, sel):
    assert O._ah(value) == (market, sel)


def test_quarter_line_ev_splits_stake_between_neighbouring_lines():
    """Rucne: domaci -0.25 pri p(vyhra) 0.45, p(remiza) 0.30, p(prohra) 0.25 a kurzu 2.00.
    Pul vkladu na 0 (vyhra / vraceni / prohra), pul na -0.5 (vyhra / prohra / prohra)."""
    mat = np.zeros((11, 11))
    mat[1, 0], mat[1, 1], mat[0, 1] = 0.45, 0.30, 0.25
    ev_hand = 0.5 * (0.45 * 2.0 + 0.30 * 1.0 - 1) + 0.5 * (0.45 * 2.0 - 1)
    e = mathcore.evaluate("AH_-0.25", "home", mat, 2.0)
    ind = IN.recompute("AH_-0.25", "home", mat, 2.0)
    assert e.ev == pytest.approx(ev_hand) and ind["ev"] == pytest.approx(ev_hand)
    assert e.p_win == pytest.approx(0.45) and e.p_half_loss == pytest.approx(0.30)  # remiza = pulprohra, ne prohra
    assert e.fair_odds == pytest.approx((1 - 0.15) / 0.45) and ind["fair"] == pytest.approx(e.fair_odds)
    # binarni zjednoduseni (p = 0.45, EV = 0.45*2 - 1) by dalo jine EV – settlement se nezjednodusuje
    assert abs(e.ev - (0.45 * 2.0 - 1)) > 0.05


def test_binary_check_for_nhl_catches_wrong_ev():
    assert IN.binary_check(0.5, 2.1, 0.05, 2.0, 2.2, 0.10) == []
    bad = IN.binary_check(0.5, 2.1, 0.25, 2.0, 2.2, 0.10)
    assert bad and "EV nesouhlasí" in bad[0]


def test_independent_mismatch_is_no_bet_with_integrity_label():
    from test_profil4_decision import cand

    c = D.decide(cand(validation=(["EV nesouhlasí s nezávislým výpočtem (0.3 vs 0.05)"], [])), P4.decision)
    assert c.decision == D.NO_BET
    assert any(r.startswith(D.INTEGRITY_FAIL) for r in c.reasons)
    oi = {"suspicious": False, "flags": []}
    assert IG.integrity_audit(c, oi)["verdict"] == IG.INT_FAIL
    assert IG.integrity_audit(c, oi)["independent"] == "nesouhlasí"


def test_integrity_audit_verdicts():
    from test_profil4_decision import cand

    oi = {"suspicious": False, "flags": [], "median": 2.0, "books": 5}
    assert IG.integrity_audit(cand(), oi)["verdict"] == IG.INT_OK
    assert IG.integrity_audit(cand(validation=None), oi)["verdict"] == IG.INT_NONE
    part = IG.integrity_audit(cand(validation=([], ["pinnacle nenabízí protistranu (X)"])), oi)
    assert part["verdict"] == IG.INT_PARTIAL and "protistranu" in part["reasons"][0]
    assert IG.integrity_audit(cand(covered=False), oi)["verdict"] == IG.INT_PARTIAL
    sus = IG.integrity_audit(cand(), {**oi, "suspicious": True, "flags": ["extrémně odlišný kurz: +30 % nad mediánem 5"]})
    assert sus["verdict"] == IG.INT_SUSPECT
    ah = IG.integrity_audit(cand(key=("AH_-1.5", "away")), oi)
    assert ah["line"] == -1.5 and ah["line_side"] == "hosté" and "domácích" in ah["line_note"]


def test_suspicious_and_unverified_odds_never_approved():
    from test_profil4_decision import cand

    for c in (cand(validation=None), cand(covered=False), cand(validation=([], ["protistrana z jiné aktualizace"])),
              cand(odds=2.6, ev_c=0.3, confirmed=())):
        assert D.decide(c, P4.decision).decision != D.APPROVED


# --- Quality of Data -------------------------------------------------------------------------------------

STATUSES = ("AVAILABLE", "PARTIAL", "NOT_YET_AVAILABLE", "UNAVAILABLE")


@pytest.mark.parametrize("stats", STATUSES)
@pytest.mark.parametrize("lineups", STATUSES)
@pytest.mark.parametrize("injuries", STATUSES)
def test_quality_groups_every_combination(stats, lineups, injuries):
    from test_profil4_integrity import BAY, NOW, _cand, quotes
    from tipsbot.profil4.coverage import CoverageReport

    qs = quotes(BAY)
    c = _cand(qs, BAY, "1X2", "1")
    cov = CoverageReport(BAY)
    for name, st in (("fixture", "AVAILABLE"), ("model_data", stats), ("lineups", lineups), ("injuries", injuries),
                     ("standings", "AVAILABLE")):
        cov.set(name, st, NOW, "test")
    q1, q2 = IG.quality_of_data(c, qs, cov, NOW, P4), IG.quality_of_data(c, qs, cov, NOW, P4)
    assert q1 == q2  # deterministicke
    assert set(q1["groups"]) == {IG.Q_ODDS, IG.Q_SPORT, IG.Q_SOURCES}
    pts = sum(p["points"] for p in q1["parts"])
    assert q1["score"] == pytest.approx(round(pts, 0), abs=1)
    det = q1["details"]
    assert det["lineups"] == lineups and det["injuries"] == injuries and det["stats"] == stats
    assert det["books"] >= 1 and det["counterside"] is True and det["source"] == "api"
    assert ("lineups" in det["missing"]) == (lineups != "AVAILABLE")
    if lineups != "AVAILABLE":
        assert "sestavy nepotvrzené" in q1["uncertain"]  # nejistota, ne negativni informace
    if stats == lineups == injuries == "AVAILABLE":
        assert q1["groups"][IG.Q_SPORT] == 100


def test_quality_without_coverage_and_counterside_is_unknown_not_zero():
    from test_profil4_integrity import BAY, NOW, _cand, quotes

    qs = quotes(BAY)
    c = _cand(qs, BAY, "1X2", "1")
    c.market = "EXPR"  # trh bez protistrany
    q = IG.quality_of_data(c, qs, None, NOW, P4)
    assert q["details"]["counterside"] is None and q["details"]["available"] == [] and q["details"]["missing"] == []


def _tip_row(fid, kickoff, intel=None, **kw):
    base = {"fixture_id": fid, "match": f"H{fid} - A{fid}", "league_id": 39, "league": "Premier League",
            "kickoff": iso(kickoff), "market": "1X2", "selection": "1", "label": "domácí", "probs": json.dumps({"C": 0.5}),
            "fair_odds": 2.0, "odds": 2.1, "bookmaker": "pinnacle", "odds_source": "api",
            "odds_time": iso(kickoff - timedelta(hours=5)), "ev": 0.05, "ev_conservative": 0.02, "robustness": 70,
            "decision": "WATCH", "category": "", "predicted_at": iso(kickoff - timedelta(hours=4)),
            "model_version": "p4", "config_hash": "x", "data_sources": json.dumps({"model_data": "AVAILABLE",
                                                                                  "lineups": "NOT_YET_AVAILABLE"}),
            "odds_fetched_at": iso(kickoff - timedelta(hours=4, minutes=50)), "odds_age_min": 70.0,
            "odds_status": "AKTUÁLNÍ", "p_market": 0.48, "intel": json.dumps(intel) if intel else None, "result": None}
    base.update(kw)
    return base


def _insert_tip(st, row):
    cols = ", ".join(row)
    st.execute(f"INSERT INTO tips({cols}) VALUES({', '.join('?' * len(row))})", list(row.values()))


def test_excel_quality_columns_map_by_name_and_keep_null(tmp_path):
    from openpyxl import load_workbook

    from tipsbot import export
    from tipsbot.storage import make_session_factory

    ko = datetime(2030, 1, 5, 15, tzinfo=UTC)
    st = Store(tmp_path / "p4.db")
    intel = {"quality": {"score": 77.0, "groups": {IG.Q_ODDS: 80.0, IG.Q_SPORT: 70.0, IG.Q_SOURCES: 100.0},
                         "uncertain": ["sestavy nepotvrzené"], "parts": [],
                         "details": {"books": 7, "counterside": True, "age_min": 70.0}}}
    _insert_tip(st, _tip_row(1, ko, intel))
    _insert_tip(st, _tip_row(2, ko, None, odds_age_min=None))  # starsi tip bez Faze 3
    st.close()
    with make_session_factory(f"sqlite:///{tmp_path / 'm.db'}")() as s:
        d = export.collect(s, tmp_path / "p4.db", now=datetime(2030, 1, 1, tzinfo=UTC))
    ws = load_workbook(io.BytesIO(export.to_xlsx(d)))["Kvalita dat"]
    head = [c.value for c in ws[6]]
    rows = {ws.cell(r, 1).value: {h: ws.cell(r, j + 1).value for j, h in enumerate(head)} for r in (7, 8)}
    new, old = rows["H1 - A1"], rows["H2 - A2"]
    assert new["Kanceláří"] == 7 and new["Protistrana"] == "ověřena" and new["Kvalita dat (0–100)"] == 77
    assert new["Kvalita kurzu (0–100)"] == 80 and new["Stav kurzu"] == "AKTUÁLNÍ" and new["Stáří kurzu (min)"] == 70
    assert isinstance(new["Poslední aktualizace u zdroje"], datetime) and isinstance(new["Čas stažení"], datetime)
    assert new["Statistiky"] == "AVAILABLE" and new["Sestavy"] == "NOT_YET_AVAILABLE" and new["Chybějící zdroje"] == "lineups"
    assert old["Kanceláří"] is None and old["Kvalita dat (0–100)"] is None and old["Stáří kurzu (min)"] is None  # NULL != 0
    assert "před Fází 3" in old["Původ hodnot"]


# --- Opportunity Ranking ---------------------------------------------------------------------------------

def _analysis():
    import test_profil4_flow as t
    from tipsbot.profil4 import hunter

    prov, store, _, _ = t.make()
    return hunter.analyze_fixture(prov, t.FID, t.NOW, P4, simulations=2_000), store


def test_rankings_saved_for_all_candidates_deduped_and_updated_on_change():
    an, store = _analysis()
    ranked = [c for c in an.candidates if (c.intel.get("rank") or {}).get("score") is not None]
    assert ranked and all(c.intel["version"] == IG.RANK_VERSION and c.intel["integrity"]["verdict"] for c in ranked)
    store.execute("DELETE FROM rankings")
    n1 = history.record_rankings(store, an)
    assert n1 == len(ranked) == len(store.query("SELECT 1 FROM rankings"))  # vsichni kandidati, ne jen ulozene tipy
    assert history.record_rankings(store, replace(an, created=an.created + timedelta(minutes=20))) == 0  # beze zmeny
    c = ranked[0]
    c.quote = replace(c.quote, odds=round(c.odds + 0.05, 2))
    assert history.record_rankings(store, replace(an, created=an.created + timedelta(minutes=40))) == 1  # zmena kurzu
    row = store.query("SELECT * FROM rankings ORDER BY id DESC LIMIT 1")[0]
    assert row["version"] == IG.RANK_VERSION and row["computed_at"] and json.loads(row["detail"])["reasons"]
    late = replace(an, created=an.fixture.kickoff + timedelta(minutes=1))
    assert history.record_rankings(store, late) == 0  # po vykopu nic


def test_ranked_opportunities_only_valid_prematch_and_rank_does_not_decide():
    an, store = _analysis()
    history.record(store, an, P4)
    rows = IG.load_rankings(store.query)
    opp = IG.opportunities_ranked(rows, an.created)
    valid = {(r["market"], r["selection"]) for r in rows if r["valid"]}
    assert opp and all((o["market"], o["selection"]) in valid for o in opp)
    assert [o["rank"] for o in opp] == sorted((o["rank"] for o in opp), reverse=True)
    assert IG.opportunities_ranked(rows, an.fixture.kickoff + timedelta(minutes=1)) == []  # zapas zacal
    by = {c.key: c for c in an.candidates}
    for o in opp:
        assert o["decision"] == by[f"{o['market']}:{o['selection']}"].decision  # ranking rozhodnuti nemeni
    for c in an.candidates:  # ani nejvyssi ranking NO BET neprepise
        if c.decision == D.NO_BET:
            c.intel["rank"]["score"] = 100.0
            assert D.decide(replace(c, reasons=[], risks=[], decision=D.NO_BET), P4.decision).decision == D.NO_BET
            break


def _rank_rows(n_matches, seed=5, good=True, version=IG.RANK_VERSION):
    """Synteticke rankingy + vysledky: pri good=True vyhravaji vysoce hodnocene vybery casteji."""
    rng = np.random.default_rng(seed)
    ranks, preds = [], []
    t0 = datetime(2026, 12, 1, 15, tzinfo=UTC)
    for f in range(n_matches):
        ko = t0 + timedelta(days=f)
        for sel, rk in (("1", float(rng.uniform(65, 95))), ("2", float(rng.uniform(5, 35)))):
            p_win = (0.62 if rk >= 60 else 0.38) if good else 0.5
            won = rng.random() < p_win
            ranks.append({"id": len(ranks) + 1, "fixture_id": f, "match": f"H{f} - A{f}", "league": "Premier League",
                          "kickoff": iso(ko), "market": "1X2", "selection": sel, "odds": 2.0, "rank": rk,
                          "computed_at": iso(ko - timedelta(hours=3)), "version": version, "valid": 1,
                          "decision": "WATCH", "ev_conservative": 0.01, "quality": 80.0,
                          "detail": json.dumps({"p": {"C": 0.5}})})
            preds.append({"fixture_id": f, "market": "1X2", "selection": sel, "model": "C",
                          "result": W if won else L, "outcome": 1.0 if won else 0.0})
    return ranks, preds


def test_ranking_validation_small_sample_is_unproven():
    ranks, preds = _rank_rows(20)
    rv = IG.ranking_validation(ranks, preds, [], P4)
    assert rv["status"] == IG.RANKING_UNPROVEN and rv["matches"] == 20
    assert [b["band"] for b in rv["bands"]] == ["0–39", "40–59", "60–79", "80–100"]
    assert all(b["status"] == IG.RANKING_UNPROVEN for b in rv["bands"])
    low = rv["bands"][0]
    assert low["n"] == 20 and low["matches"] == 20 and low["brier"] == pytest.approx(0.25)


def test_ranking_validation_detects_working_ranking_and_ignores_leakage_and_other_versions():
    ranks, preds = _rank_rows(160, good=True)
    rv = IG.ranking_validation(ranks, preds, [], P4)
    assert rv["matches"] == 160 and rv["status"].startswith("vyšší ranking průkazně lepší")
    assert rv["hi_minus_lo"]["diff"] > 0 and rv["hi_minus_lo"]["ci95"][0] > 0
    assert len(rv["periods"]) == 2 and sum(p["matches"] for p in rv["periods"]) == 160
    # ranking spocteny az po vykopu se do validace nesmi dostat (unik informaci)
    late = [{**r, "computed_at": iso(datetime.fromisoformat(r["kickoff"]) + timedelta(minutes=5))} for r in ranks]
    assert IG.ranking_validation(late, preds, [], P4)["n"] == 0
    # jina verze algoritmu se nemicha (vahy se neladi na stejnych datech)
    other = [{**r, "version": "rank-v0"} for r in ranks]
    assert IG.ranking_validation(other, preds, [], P4)["n"] == 0
    flat = IG.ranking_validation(*_rank_rows(160, good=False), [], P4)
    assert not flat["status"].startswith("vyšší ranking průkazně lepší")


def test_export_ranking_and_integrity_sheets_have_rows(tmp_path):
    from openpyxl import load_workbook

    from tipsbot import export
    from tipsbot.storage import make_session_factory

    an, store = _analysis()
    p4 = tmp_path / "p4.db"
    disk = Store(p4)
    history.record_rankings(disk, an)
    disk.close()
    with make_session_factory(f"sqlite:///{tmp_path / 'm.db'}")() as s:
        d = export.collect(s, p4, now=an.created)
    wb = load_workbook(io.BytesIO(export.to_xlsx(d)))
    ws = wb["Opportunity Ranking"]
    assert ws.cell(7, 1).value == 1 and ws.cell(7, 2).value is not None and ws.cell(7, 15).value == IG.RANK_VERSION
    assert isinstance(ws.cell(7, 14).value, datetime)
    mi = wb["Market Integrity"]
    assert mi.cell(7, 14).value in (IG.INT_OK, IG.INT_PARTIAL, IG.INT_SUSPECT, IG.INT_FAIL, IG.INT_NONE)
    assert wb["Ranking Validation"].cell(7, 1).value == "0–39"


# --- Portfolio, chyby, panel, CLI ------------------------------------------------------------------------

def test_portfolio_flags_correlated_and_opposite_selections():
    ko = datetime(2030, 1, 5, 15, tzinfo=UTC)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    rows = [{**_tip_row(1, ko, selection="1"), "id": 1}, {**_tip_row(1, ko, selection="2"), "id": 2},
            {**_tip_row(1, ko, market="OU_2.5", selection="over"), "id": 3}, {**_tip_row(2, ko), "id": 4}]
    pf = IG.portfolio(rows, now)
    assert pf["pending"] == 4 and pf["pending_matches"] == 2
    corr = pf["correlated"]
    assert len(corr) == 1 and corr[0]["tips"] == 3 and corr[0]["opposite"] == ["1X2"]
    assert "virtuální" in pf["note"].lower()


def test_portfolio_sheet_separates_virtual_and_real_money(tmp_path):
    from datetime import date

    from openpyxl import load_workbook

    from tipsbot import export
    from tipsbot.storage import Ticket, make_session_factory

    st = Store(tmp_path / "p4.db")
    _insert_tip(st, _tip_row(1, datetime(2030, 1, 5, 15, tzinfo=UTC), decision="APPROVED BET"))
    st.close()
    with make_session_factory(f"sqlite:///{tmp_path / 'm.db'}")() as s:
        s.add(Ticket(match_id="m1", match_name="X - Y", league="Premier League", match_day=date(2030, 1, 6), market="1X2",
                     selection="1", bookmaker="tipsport",
                     odds=2.0, stake=100.0))
        s.flush()
        d = export.collect(s, tmp_path / "p4.db", now=datetime(2030, 1, 1, tzinfo=UTC))
        s.rollback()
    ws = load_workbook(io.BytesIO(export.to_xlsx(d)))["Portfolio Risk"]
    head = [c.value for c in ws[6]]
    rows = {ws.cell(r, 1).value: {h: ws.cell(r, j + 1).value for j, h in enumerate(head)} for r in range(7, 12)}
    official = rows["1. Oficiální tipy bota (APPROVED, virtuálně 1 j.)"]
    real = rows["3. Reálně vsazené tikety (Kč)"]
    assert official["Virtuálně ve hře (j.)"] == 1 and official["Skutečně ve hře (Kč)"] is None
    assert real["Skutečně ve hře (Kč)"] == 100 and real["Virtuálně ve hře (j.)"] is None


def test_error_classification_needs_evidence():
    ko = datetime(2026, 11, 1, 15, tzinfo=UTC)
    lost = _tip_row(1, ko, result="lost", score="0:1")
    assert IG.classify_error(lost)["primary"] == "běžná náhodná prohra"
    assert IG.classify_error({**lost, "result": "won", "score": "1:0"})["primary"] == "bez chyby (výhra)"
    wrong = IG.classify_error({**lost, "result": "won", "score": "0:1"})  # ulozeno vyhra, skore rika prohra
    assert wrong["primary"] == "chyba settlementu" and "nezávislý výpočet lost" in wrong["items"][0]["evidence"]
    ah = IG.classify_error({**lost, "market": "AH_-1.5", "selection": "away", "result": "void", "score": None})
    assert ah["primary"] == "chyba mapování" and "test_profil4_integrity" in ah["items"][0]["test"]
    optimistic = IG.classify_error({**lost, "probs": json.dumps({"C": 0.65}), "p_market": 0.45})
    assert optimistic["primary"] == "příliš optimistický odhad"
    stale = IG.classify_error({**lost, "odds_status": "ZASTARALÝ"})
    assert stale["primary"] == "zastaralý kurz"
    assert all(k["test"] and k["fixed"].startswith("ano") for k in IG.KNOWN_ISSUES)


def test_panel_shows_phase31_sections(tmp_path, monkeypatch):
    from tipsbot.web import create_app

    monkeypatch.setattr(type(P4), "db_path", property(lambda self: tmp_path / "p4.db"))
    an, store = _analysis()
    disk = Store(tmp_path / "p4.db")
    history.record_rankings(disk, an)
    IG.store_intel(disk, an.created)
    disk.close()
    html = create_app(f"sqlite:///{tmp_path / 'm.db'}").test_client().get("/?tab=profil4").get_data(as_text=True)
    for text in ("Market Integrity", "Ranking Validation", "Portfolio &amp; riziko", "Známé a opravené chyby",
                 IG.RANKING_UNPROVEN, "Integrita:"):
        assert text in html, text


def test_cli_sections_and_report(tmp_path, monkeypatch):
    from tipsbot.profil4 import cli

    st = Store()
    monkeypatch.setattr(cli, "build", lambda db=None, cfg=P4: (st, None))
    out = tmp_path / "r.json"
    p = argparse.ArgumentParser()
    cli.add_parser(p.add_subparsers(dest="cmd"))
    args = p.parse_args(["profil4", "intel", "ranking", "--out", str(out)])
    args.db = None
    assert cli.run(args) == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["ranking_validation"]["status"] == IG.RANKING_UNPROVEN
    text = IG.format_text(rep, "integrity")
    assert "MARKET INTEGRITY" in text and "KALIBRACE" not in text
    assert "VALIDACE RANKINGU" in IG.format_text(rep, "ranking") and "PORTFOLIO" in IG.format_text(rep, "portfolio")
    assert "KVALITA DAT" in IG.format_text(rep, "data-quality") and "ZNÁMÉ A OPRAVENÉ" in IG.format_text(rep, "errors")
    with pytest.raises(SystemExit):
        p.parse_args(["profil4", "intel", "neexistuje"])
