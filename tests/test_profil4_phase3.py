"""Profil 4 – Faze 3 (Intelligence & Calibration): kalibrace na spolecnem vzorku, intervaly, ochrana proti
data leakage, kvalita dat, kurzova inteligence, EV po modelech, Opportunity Ranking, export a panel.

Vse na fixnich datech (zaznam odpovedi API, synteticke predikce s pevnym seedem) – nic se nestahuje."""

import io
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from tipsbot.profil4 import decision, history, intelligence as IG
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store, iso

UTC = timezone.utc
T0 = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


def _pred(fid, model, p, y, market="1X2", sel="1", league="Premier League", day=0, p_market=0.5, late=False,
          predicted=None, version="p4-1.0.0"):
    ko = T0 + timedelta(days=day)
    pt = predicted or (ko + timedelta(minutes=5) if late else ko - timedelta(hours=3))
    return {"fixture_id": fid, "match": f"H{fid} - A{fid}", "league": league, "kickoff": iso(ko), "market": market,
            "selection": sel, "market_type": market.split("_")[0], "model": model, "p": p, "p_market": p_market,
            "outcome": y, "odds": 2.0, "predicted_at": iso(pt), "settled_at": iso(ko + timedelta(hours=3)),
            "model_version": version}


def _sample(n_matches=40, seed=3, league="Premier League", shift=0.0, base=1000):
    """Synteticky vzorek: skutecna sance q, trh = q, modely = q + sum (C nejpresnejsi, A nejhorsi)."""
    rng = np.random.default_rng(seed)
    rows = []
    for f in range(n_matches):
        for sel in ("1", "X", "2"):
            q = float(rng.uniform(0.15, 0.7))
            y = float(rng.random() < q)
            noise = {"A": 0.15, "B": 0.03, "C": 0.05, "D": 0.08}
            for m, s in noise.items():
                p = float(np.clip(q + shift + rng.normal(0, s), 0.02, 0.98))
                rows.append(_pred(base + f, m, p, y, sel=sel, league=league, day=f, p_market=q))
    return rows


# --- metriky ---------------------------------------------------------------------------------------------

def test_brier_and_log_loss_are_exact():
    common = [{"fixture_id": 1, "kickoff": iso(T0), "outcome": 1.0, "p": {m: 0.8 for m in IG.MODELS}, "p_market": 0.6},
              {"fixture_id": 2, "kickoff": iso(T0), "outcome": 0.0, "p": {m: 0.3 for m in IG.MODELS}, "p_market": 0.5}]
    t = IG.model_table(common, P4)
    assert t["C"]["brier"] == pytest.approx(((0.8 - 1) ** 2 + 0.3 ** 2) / 2)
    assert t["C"]["log_loss"] == pytest.approx(-(math.log(0.8) + math.log(0.7)) / 2)
    assert t["trh"]["brier"] == pytest.approx((0.4 ** 2 + 0.5 ** 2) / 2)
    assert t["C"]["vs_market"]["diff"] == pytest.approx(
        ((-math.log(0.8)) - (-math.log(0.6)) + (-math.log(0.7)) - (-math.log(0.5))) / 2)


def test_models_are_compared_on_the_same_rows_only():
    rows = _sample(10)
    rows = [r for r in rows if not (r["model"] == "A" and r["fixture_id"] == 1000)]  # A chybi u jednoho zapasu
    rows.append(_pred(5000, "C", 0.9, 1.0))  # zapas jen s modelem C
    common = IG.common_sample(IG.clean_predictions(rows)[0])
    fids = {r["fixture_id"] for r in common}
    assert 1000 not in fids and 5000 not in fids and len(fids) == 9
    assert all(set(r["p"]) == set(IG.MODELS) and r["p_market"] is not None for r in common)


def test_cluster_bootstrap_is_by_match_and_reproducible():
    rows = _sample(40)
    common = IG.common_sample(IG.clean_predictions(rows)[0])
    a = IG.model_table(common, P4)
    b = IG.model_table(common, P4)
    assert a["C"]["brier_ci95"] == b["C"]["brier_ci95"]  # pevny seed
    lo, hi = a["C"]["brier_ci95"]
    assert lo < a["C"]["brier"] < hi and a["C"]["matches"] == 40


def test_calibration_bins_merge_small_bins_and_never_overclaim():
    rng = np.random.default_rng(1)
    p = list(rng.uniform(0, 1, 300))
    y = [float(rng.random() < x) for x in p]
    fid = [i // 3 for i in range(300)]  # 100 zapasu po 3 radcich
    bins = IG.calibration_bins(p, y, fid, min_matches=15)
    assert all(b["matches"] >= 15 for b in bins) and len(bins) <= 10
    assert sum(b["n"] for b in bins) == 300
    one = IG.calibration_bins([0.95], [1.0], [1], min_matches=15)  # jeden zapas = zadny zaver
    assert one[0]["enough"] is False and one[0]["ok"] is None


def test_calibration_status_needs_enough_matches_and_detects_bias():
    few = IG.common_sample(IG.clean_predictions(_sample(40))[0])
    assert IG.model_table(few, P4)["C"]["calibration"]["status"].startswith("málo dat")
    biased = IG.common_sample(IG.clean_predictions(_sample(160, shift=0.15))[0])
    t = IG.model_table(biased, P4)
    assert t["C"]["calibration"]["status"] == "průkazně špatně kalibrovaný (přeceňuje šance)"
    assert t["trh"]["calibration"]["status"] in ("průkazně dobře kalibrovaný", "neprůkazné")
    assert t["C"]["vs_market"]["status"] == "průkazně horší než trh"


def test_wilson_interval():
    lo, hi = IG.wilson(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3) and hi == pytest.approx(0.7634, abs=1e-3)


# --- data leakage a casova integrita -----------------------------------------------------------------------

def test_post_kickoff_and_repeated_predictions_are_not_independent_samples():
    rows = [_pred(1, "C", 0.6, 1.0, predicted=T0 - timedelta(hours=5)),
            _pred(1, "C", 0.7, 1.0, predicted=T0 - timedelta(hours=1)),  # opakovany sken – plati posledni
            _pred(2, "C", 0.9, 1.0, late=True)]  # po vykopu
    clean, dropped = IG.clean_predictions(rows)
    assert [r["p"] for r in clean] == [0.7] and dropped == {"po_výkopu": 1, "opakované": 1}


def test_leakage_audit_counts_violations():
    preds = [_pred(1, "C", 0.6, 1.0), _pred(2, "C", 0.6, 1.0, late=True)]
    tips = [{"fixture_id": 1, "market": "1X2", "selection": "1", "kickoff": iso(T0), "predicted_at": iso(T0 + timedelta(hours=1)),
             "odds_time": iso(T0 + timedelta(minutes=10))}]
    a = {c["name"]: c["count"] for c in IG.leakage_audit(preds, tips)["checks"]}
    assert a["předpovědi uložené až po výkopu"] == 1 and a["tipy uložené až po výkopu"] == 1
    assert a["kurz tipu z doby po výkopu"] == 1 and a["duplicitní předpovědi stejné verze"] == 0


def test_tip_saved_after_kickoff_is_excluded_from_results():
    """Regrese 22.11.2026: rozbor po vykopu ulozil tipy, ktere se zapocitaly do ROI."""
    ok = {"id": 1, "fixture_id": 1, "market": "1X2", "selection": "1", "result": "won", "decision": "APPROVED BET",
          "kickoff": iso(T0), "predicted_at": iso(T0 - timedelta(hours=2))}
    late = {**ok, "id": 2, "fixture_id": 2, "predicted_at": iso(T0 + timedelta(hours=6))}
    bets, dropped = history.unique_bets([ok, late])
    assert [b["id"] for b in bets] == [1] and dropped == 1


def _analysis():
    import test_profil4_flow as t
    from tipsbot.profil4 import hunter

    prov, store, _, _ = t.make()
    return hunter.analyze_fixture(prov, t.FID, t.NOW, P4, simulations=2_000), store


def test_analysis_after_kickoff_records_no_tips():
    an, store = _analysis()
    late = replace(an, created=an.fixture.kickoff + timedelta(hours=7))
    n = len(store.query("SELECT 1 FROM tips"))
    assert history.record(store, late, P4) == 0
    assert len(store.query("SELECT 1 FROM tips")) == n
    assert history.record_predictions(store, late, P4) == 0


def test_settled_predictions_are_immutable():
    an, store = _analysis()
    assert history.record_predictions(store, an, P4) > 0
    store.execute("UPDATE predictions SET result='won', outcome=1.0")
    before = [tuple(r) for r in store.query("SELECT id, p, odds, predicted_at FROM predictions ORDER BY id")]
    for c in an.candidates:
        c.p = {k: (v * 0.9 if v else v) for k, v in c.p.items()}
    later = replace(an, created=an.created + timedelta(minutes=30))
    history.record_predictions(store, later, P4)
    assert [tuple(r) for r in store.query("SELECT id, p, odds, predicted_at FROM predictions ORDER BY id")] == before


# --- per kandidat: kvalita dat, kurzy, EV, ranking ---------------------------------------------------------

def test_every_candidate_gets_intel_without_changing_decisions():
    an, store = _analysis()
    assert an.candidates
    for c in an.candidates:
        i = c.intel
        assert "error" not in i, i
        assert 0 <= i["quality"]["score"] <= 100 and 0 <= i["rank"]["score"] <= 100
        assert sum(p["max"] for p in i["quality"]["parts"]) == pytest.approx(100)
        assert sum(p["max"] for p in i["rank"]["parts"]) == pytest.approx(100)
        assert i["ev"]["models"]["C"] == c.ev_c and i["ev"]["conservative"] == c.ev_d and i["ev"]["min_odds"] == c.min_odds
        zero = dict(i["ev"]["sensitivity"])[0.0]
        assert zero == pytest.approx(c.ev_c, abs=1e-9)
        again = decision.decide(replace(c, reasons=[], risks=list(c.risks), decision=decision.NO_BET), P4.decision)
        assert again.decision == c.decision
    # ulozi se k tipum (kvalita, ranking a cely rozpad)
    history.record(store, an, P4)
    rows = store.query("SELECT quality, rank, intel FROM tips")
    assert rows and all(r["quality"] is not None and r["rank"] is not None and json.loads(r["intel"])["rank"] for r in rows)


def test_quality_of_data_explains_stale_odds_and_missing_counterside():
    from test_profil4_integrity import BAY, _cand, quotes, NOW
    from tipsbot.profil4.coverage import CoverageReport

    qs = quotes(BAY)
    c = _cand(qs, BAY, "1X2", "1")
    fresh = IG.quality_of_data(c, qs, CoverageReport(BAY), NOW, P4)
    stale = IG.quality_of_data(c, qs, CoverageReport(BAY), NOW + timedelta(hours=10), P4)
    assert stale["score"] < fresh["score"]
    alone = [q for q in qs if not (q.bookmaker == c.quote.bookmaker and q.market == "1X2" and q.selection != "1")]
    one = IG.quality_of_data(c, alone, CoverageReport(BAY), NOW, P4)
    assert "protistrana trhu neověřena" in one["uncertain"]
    assert "sestavy nepotvrzené" in fresh["uncertain"]  # chybejici data = nejistota, ne sportovni informace


def test_extreme_odds_are_flagged_and_never_approved():
    from test_profil4_integrity import BAY, _cand, quotes, NOW

    qs = quotes(BAY)
    c = _cand(qs, BAY, "TTA_1.0", "over", "1xbet")
    oi = IG.odds_intel(c, qs, NOW, P4)
    assert oi["suspicious"] and oi["fair_market"] < c.odds
    assert any("nad férovou cenou trhu" in f for f in oi["flags"])
    assert c.decision != decision.APPROVED


def test_value_not_backed_by_market_price_is_never_approved():
    from test_profil4_integrity import BAY, _cand, quotes

    c = _cand(quotes(BAY), BAY, "1X2", "1")
    c2 = decision.decide(replace(c, market_covered=False, p_market=None, ev_market=None, reasons=[], risks=[],
                                 validation=([], [])), P4.decision)
    assert c2.decision != decision.APPROVED
    assert IG.ev_breakdown(c2, None, P4)["market_backed"] is False


def test_opportunity_ranking_prefers_robust_conservative_value():
    from test_profil4_decision import cand

    good = cand(ev_c=0.06, ev_d=0.04, rob=85)
    weak = cand(ev_c=0.20, ev_d=-0.04, rob=40)
    for c in (good, weak):
        c.p_market, c.confirmed_by = 0.5, ["pinnacle 2.00"]
    qd = {"score": 80}
    oi = {"age_min": 10, "suspicious": False}
    rg, rw = IG.rank(good, qd, oi, P4), IG.rank(weak, qd, {**oi, "suspicious": True}, P4)
    assert rg["score"] > rw["score"]  # nominalne vyssi EV neznamena lepsi prilezitost
    assert "ne pravděpodobnost výhry" in rg["note"]


def test_best_available_stays_separate_from_approved():
    an, _ = _analysis()
    s = an.selection
    if s.best_available is not None:
        assert not s.approved and s.best_available.decision != decision.APPROVED


# --- souhrn, ladeni, sporty --------------------------------------------------------------------------------

def test_football_and_nhl_are_evaluated_separately():
    rows = _sample(12) + _sample(5, seed=9, league="NHL", base=9000)
    out = IG.build(rows, [], T0 + timedelta(days=400), P4)
    assert out["sports"]["fotbal"]["common_matches"] == 12 and out["sports"]["hokej"]["common_matches"] == 5
    assert set(out["sports"]["hokej"]["competitions"]) == {"NHL"}


def test_tuning_report_is_chronological_and_only_a_proposal():
    rows = _sample(120)
    common = IG.common_sample(IG.clean_predictions(rows)[0])
    tr = IG.tuning_report(common)
    assert tr["requires_confirmation"] and tr["train_matches"] + tr["test_matches"] == 120
    assert 0.0 <= tr["weight_c"] <= 1.0 and "ci95" in tr
    assert IG.tuning_report(common[:30])["status"] == "málo dat"


def test_error_analysis_does_not_invent_causes():
    t = {"result": "lost", "probs": json.dumps({"C": 0.55}), "p_market": 0.54, "odds_status": "AKTUÁLNÍ", "intel": None}
    d = IG.diagnose(t)
    assert d[0].startswith("prohra") and "není v datech doložena" in d[1]
    t2 = {**t, "odds_status": "ZASTARALÝ", "p_market": 0.30}
    d2 = IG.diagnose(t2)
    assert any("zastaralý kurz" in x for x in d2) and any("rozchod s trhem" in x for x in d2)


def test_store_intel_reads_only_saved_data():
    st = Store()
    for r in _sample(5):
        st.execute("INSERT INTO predictions(fixture_id, match, league, kickoff, market, selection, market_type, model, p, "
                   "p_market, market_covered, odds, bookmaker, odds_source, predicted_at, model_version, config_hash, "
                   "outcome, result) VALUES(?,?,?,?,?,?,?,?,?,?,1,2.0,'pinnacle','api',?,?,'x',?,'won')",
                   (r["fixture_id"], r["match"], r["league"], r["kickoff"], r["market"], r["selection"], r["market_type"],
                    r["model"], r["p"], r["p_market"], r["predicted_at"], r["model_version"], r["outcome"]))
    out = IG.store_intel(st, T0 + timedelta(days=30))
    assert json.loads(st.get(IG.INTEL_KEY))["sports"]["fotbal"]["common_matches"] == 5 == out["sports"]["fotbal"]["common_matches"]


# --- export a panel ----------------------------------------------------------------------------------------

def test_excel_has_new_analytic_sheets(tmp_path):
    from openpyxl import load_workbook

    from tipsbot import export
    from tipsbot.storage import make_session_factory

    st = Store(tmp_path / "p4.db")
    for r in _sample(5):
        st.execute("INSERT INTO predictions(fixture_id, match, league, kickoff, market, selection, market_type, model, p, "
                   "p_market, market_covered, odds, bookmaker, odds_source, predicted_at, model_version, config_hash, "
                   "outcome, result) VALUES(?,?,?,?,?,?,?,?,?,?,1,2.0,'pinnacle','api',?,?,'x',?,'won')",
                   (r["fixture_id"], r["match"], r["league"], r["kickoff"], r["market"], r["selection"], r["market_type"],
                    r["model"], r["p"], r["p_market"], r["predicted_at"], r["model_version"], r["outcome"]))
    st.close()
    with make_session_factory(f"sqlite:///{tmp_path / 'm.db'}")() as s:
        d = export.collect(s, tmp_path / "p4.db")
    wb = load_workbook(io.BytesIO(export.to_xlsx(d)))
    for name in ("Kalibrace modelů", "EV analýza", "Kvalita dat", "Analýza chyb", "Opportunity Ranking"):
        assert name in wb.sheetnames
    ws = wb["Kalibrace modelů"]
    assert ws.cell(7, 1).value == "fotbal" and ws.cell(7, 3).value == 5
    with make_session_factory(f"sqlite:///{tmp_path / 'm2.db'}")() as s:  # bez databaze Profilu 4
        wb2 = load_workbook(io.BytesIO(export.to_xlsx(export.collect(s, tmp_path / "chybi.db"))))
    assert wb2["Kalibrace modelů"].cell(6, 1).value == "Data Profilu 4 nejsou k dispozici."


def test_panel_shows_intelligence_section_as_mobile_cards(tmp_path, monkeypatch):
    import test_profil4_flow as t
    from tipsbot.web import create_app

    monkeypatch.setattr(type(P4), "db_path", property(lambda self: tmp_path / "p4.db"))
    st = Store(tmp_path / "p4.db")
    for r in _sample(5):
        st.execute("INSERT INTO predictions(fixture_id, match, league, kickoff, market, selection, market_type, model, p, "
                   "p_market, market_covered, odds, bookmaker, odds_source, predicted_at, model_version, config_hash, "
                   "outcome, result) VALUES(?,?,?,?,?,?,?,?,?,?,1,2.0,'pinnacle','api',?,?,'x',?,'won')",
                   (r["fixture_id"], r["match"], r["league"], r["kickoff"], r["market"], r["selection"], r["market_type"],
                    r["model"], r["p"], r["p_market"], r["predicted_at"], r["model_version"], r["outcome"]))
    st.close()
    c = create_app(f"sqlite:///{tmp_path / 'm.db'}").test_client()
    r = c.post("/profil4/intel", follow_redirects=False)
    assert r.status_code == 302
    assert "p4intel" in r.headers["Location"]
    html = c.get("/?tab=profil4").get_data(as_text=True)
    assert "Intelligence &amp; Calibration" in html and "p4mcard" in html and "Společný vzorek" in html
    assert "Kontrola úniku informací" in html and "@media(max-width:640px){.p4three" in html
    assert t  # flow fixtures importovatelne


def test_cli_text_and_intel_archive_only_on_change():
    st = Store()
    for r in _sample(3):
        st.execute("INSERT INTO predictions(fixture_id, match, league, kickoff, market, selection, market_type, model, p, "
                   "p_market, market_covered, odds, bookmaker, odds_source, predicted_at, model_version, config_hash, "
                   "outcome, result) VALUES(?,?,?,?,?,?,?,?,?,?,1,2.0,'pinnacle','api',?,?,'x',?,'won')",
                   (r["fixture_id"], r["match"], r["league"], r["kickoff"], r["market"], r["selection"], r["market_type"],
                    r["model"], r["p"], r["p_market"], r["predicted_at"], r["model_version"], r["outcome"]))
    out = IG.store_intel(st, T0 + timedelta(days=30))
    IG.store_intel(st, T0 + timedelta(days=30, hours=1))  # jiny cas, stejny obsah => zadny novy zaznam archivu
    assert st.archive_counts() == {"p4_intel": 1}
    text = IG.format_text(out)
    assert "FOTBAL: společný vzorek 3 zápasů" in text and "KONTROLA ÚNIKU INFORMACÍ" in text
