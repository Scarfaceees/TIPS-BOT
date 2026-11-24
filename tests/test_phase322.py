"""Faze 3.2.2: pojistka Modelu A, globalni tvrde odmitnuti kurzu, pul vyhra v Profilu 2, nekruhovy stin Profilu 3,
prisne overeni kombi, jednotny yield, body zaviracich kurzu, diagnostika rankingu, metodika rohu a karet."""

import io
import json
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from tipsbot import czechodds, export, kombi, marketcheck as MC, metrics, paper, profile2, rankdiag, strategy
from tipsbot.oddsapi import Event, MarketOdds
from tipsbot.profil4 import decision as D
from tipsbot.profil4 import guards, history, mathcore
from tipsbot.profil4 import performance as PF
from tipsbot.profil4.config import P4, DecisionConfig
from tipsbot.profil4.models import ModelResult, ModelSet
from tipsbot.storage import IntegrityRejection, Ledger, Ticket, make_session_factory

NOW = datetime(2026, 11, 24, 12, 0)
KO = NOW + timedelta(hours=6)
CFG = DecisionConfig()


@pytest.fixture
def led():
    return Ledger(make_session_factory("sqlite://")())


# --- 1. Model A – pojistka -------------------------------------------------------------------------------------
def _ms(a=(1.6, 1.1), b=(1.5, 1.15), market=True):
    from tipsbot.models.football import score_matrix

    ma = ModelResult("A", True, score_matrix(*a, -0.05, 10), a[0], a[1])
    mb = ModelResult("B", True, score_matrix(*b, 0.0, 10), b[0], b[1]) if market else ModelResult("B", False)
    return ModelSet(ma, mb, ma, 10.0, None, None, 0.5)


def test_model_a_guard_bodo_case_and_normal_match():
    bodo = guards.model_a_guard(_ms(a=(13.43, 0.18), b=(1.9, 0.9)), CFG)  # λ z auditu Bodo/Glimt – LASK
    assert bodo["triggered"] and bodo["max_logit_gap"] > 10 and "Model A" in bodo["reasons"][0]
    assert bodo["p_a"][0] > 0.999  # puvodni pravdepodobnost se zaznamena, nemeni se
    normal = guards.model_a_guard(_ms(), CFG)
    assert not normal["triggered"] and normal["max_logit_gap"] < 1
    # bez ceny trhu: extremni pravdepodobnost sama o sobe
    assert guards.model_a_guard(_ms(a=(9.0, 0.2), market=False), CFG)["triggered"]
    assert not guards.model_a_guard(_ms(a=(2.4, 0.6), market=False), CFG)["triggered"]  # Bayern – Koln ~0,9
    assert CFG.model_a_guard_logit == 3.0 and CFG.max_logit_gap == 0.5  # mekka hranice beze zmeny


def _cand(**kw):
    from datetime import timedelta as td

    from tipsbot.profil4.odds import CURRENT, OddsQuote

    q = OddsQuote(1, "A - B", "pinnacle", "1X2", "1", 2.0, NOW, "api", NOW + td(hours=3), NOW)
    c = D.Candidate(1, "A - B", "1X2", "1", "test", q, CURRENT, {"A": 0.55, "B": 0.52, "C": 0.53, "D": 0.51},
                    0.53, 0.0, 1.89, 1.96, 0.06, 0.02, 1.95, 75, 0.85, True, "confirmed", **kw)
    c.validation, c.market_covered, c.confirmed_by = ([], []), True, ["bet365 2.00"]
    c.p_market, c.ev_market, c.market_src = 0.52, 0.04, "test"
    return c


def test_guard_forces_no_bet_and_keeps_audit_trail():
    assert D.decide(_cand(), CFG).decision == D.APPROVED
    g = guards.model_a_guard(_ms(a=(13.43, 0.18), b=(1.9, 0.9)), CFG)
    c = D.decide(_cand(guard=g), CFG)
    assert c.decision == D.NO_BET and c.reasons[0].startswith(guards.GUARD)
    assert c.p == {"A": 0.55, "B": 0.52, "C": 0.53, "D": 0.51} and c.ev_c == 0.06  # p ani EV se nemeni


# --- 2. globalni tvrde odmitnuti ---------------------------------------------------------------------------------
def _ou_event():
    books = {b: {"over": 1.90, "under": 1.95} for b in ("pinnacle", "bet365", "williamhill", "marathonbet", "unibet_se")}
    return Event("x:1", "ENG1", "Arsenal", "Chelsea", KO, {"OU_2.5": MarketOdds("OU_2.5", ("over", "under"), books)})


def test_suspect_pick_kept_as_diagnostic_but_not_counted(led):
    entries, _ = czechodds.parse_text("tipsport | Arsenal - Chelsea | OU_2.5 | 1.70 2.60", NOW)
    model = {"OU_2.5:over": 0.40, "OU_2.5:under": 0.60}
    stamps = {"x:1": {b: {"totals": NOW} for b in ("pinnacle", "bet365", "williamhill", "marathonbet", "unibet_se")}}
    paper.track(led, "ENG1", [(_event := _ou_event(), model, entries)], NOW, stamps=stamps)
    (p,) = paper.load(led)
    assert (p.market, p.selection, p.odds) == ("OU_2.5", "under", 2.60)  # vyber zustal (diagnosticky zaznam)
    assert p.integrity["status"] == MC.SUSPECT and paper.hard_rejected(p)
    assert any(f.startswith(paper.HARD_REJECT) for f in p.flags)
    p.result, p.profit = "won", 1.6
    st = paper.stats([p])
    assert st["settled"] == 0 and st["rejected"] == 1 and st["roi"] is None  # nepocita se do vysledku
    (r,) = led.s.query(IntegrityRejection).all()
    assert (r.profile, r.odds, r.book, r.ref_n) == ("zaklad", 2.60, "tipsport", 5)
    assert r.ref_median is not None and json.loads(r.reasons)
    paper.save(led, [p])
    d = export.collect(led.s, None, NOW, light=True)
    assert any(x.group.endswith("bezpečnostně odmítnuto") for x in d.records)


def test_official_tip_with_suspect_odds_is_not_issued(led):
    from tipsbot import pipeline

    tip = type("T", (), {"market": "OU_2.5", "selection": "under", "raw_odds": 2.60, "bookmaker": "tipsport",
                         "match_id": "x:1", "match_name": "Arsenal - Chelsea", "kickoff": KO})()
    ev = _ou_event()
    assert pipeline._hard_reject_tip(led, tip, ev, NOW, {}) is True
    tip.raw_odds = 1.95
    assert pipeline._hard_reject_tip(led, tip, ev, NOW, {}) is False
    assert led.s.query(IntegrityRejection).filter_by(profile="tipy").count() == 1


def test_unverified_is_not_hard_reject():
    assert not MC.is_hard_reject({"status": MC.UNVERIFIED}) and not MC.is_hard_reject(None)
    assert MC.is_hard_reject({"status": MC.SUSPECT})


# --- 3. Profil 2: pul vyhra = 0,5 vyhry ------------------------------------------------------------------------
def _at(h, a):
    m = np.zeros((profile2.MAX_GOALS + 1,) * 2)
    m[h, a] = 1.0
    return m


@pytest.mark.parametrize("market,sel,score,expect,share", [
    ("AH_-0.25", "home", (1, 0), "win", 1.0),       # plna vyhra
    ("AH_0.25", "home", (0, 0), "half_win", 0.5),   # pul vyhra (domaci +0,25 pri remize)
    ("AH_0.0", "home", (0, 0), "push", 0.0),         # vraceni
    ("AH_-0.25", "home", (0, 0), "half_loss", 0.0),  # pul prohra
    ("AH_-0.25", "home", (0, 1), "loss", 0.0),       # plna prohra
])
def test_profile2_asian_outcomes_and_win_share(market, sel, score, expect, share):
    mat = _at(*score)
    split = profile2.outcome_split(market, sel, 2.0, mat)
    assert split[expect] == pytest.approx(1.0)
    o = profile2._eval_offer(market, sel, "b", 2.0, [mat], np.array([list(score)] * 10), profile2.P2)
    assert o.worst_win_share == pytest.approx(share) and profile2.win_share(o) == pytest.approx(share)
    if expect == "half_win":
        assert o.worst_win == pytest.approx(1.0)  # p_win (a p_model) beze zmeny – jen podminka 60 % pouziva podil


def test_old_saved_offer_without_share_still_works():
    o = profile2._eval_offer("1X2", "1", "b", 2.0, [_at(1, 0)], np.array([[1, 0]]), profile2.P2)
    o.worst_win_share = None
    assert profile2.win_share(o) == o.worst_win
    assert profile2.P2.high_prob_min_win == 0.60 and profile2.P2.safe_max_loss == 0.25  # prahy beze zmeny


# --- 4. Profil 3 – nekruhovy stin -------------------------------------------------------------------------------
def test_profile3_shadow_separate_and_production_unchanged(led):
    from test_marketsim import NOW as WP_NOW
    from test_marketsim import _entries

    from tipsbot import marketsim

    ev = Event("x:wp", "UNL", "Wales", "Portugal", WP_NOW + timedelta(hours=20))
    paper.track(led, "UNL", [(ev, None, _entries())], WP_NOW)
    (p3,) = [p for p in paper.load(led) if p.profile == "profil3"]
    (x,) = [p for p in paper.load(led) if p.profile == "profil3x"]
    prod = marketsim.best_pick("x:wp", None, _entries())
    assert (p3.market, p3.selection, p3.odds) == (prod.market, prod.selection, prod.odds)  # produkce beze zmeny
    assert p3.meta["p3"]["lobo"] is False and "p3_lobo" in p3.meta  # paralelni mereni u produkce zustava
    assert x.meta["p3"]["lobo"] is True and x.detail.startswith("Nekruhově") and "p3_lobo" not in x.meta
    # cela linie (OU_2.0) se do fitu golu nebere nikdy -> u ni kruhovost nevznika a stin dava totez λ jako produkce
    assert x.market == "OU_2.0" and x.meta["p3"]["fit_markets"] == p3.meta["p3"]["fit_markets"]
    assert x.meta["p3"]["lam"] == p3.meta["p3"]["lam"]
    an = paper.load_analyses(led)
    assert list(an) == ["x:wp"] and "lobo_unverified" not in an["x:wp"]  # stin neprepisuje rozbory Profilu 3
    pr = strategy.paired(paper.load(led), "profil3", "profil3x")
    assert pr["common_matches"] == 1 and pr["same_bet"] == 1 and paper.profile("profil3x").method == "sim_lobo"


def test_lobo_analyse_skips_market_without_independent_fit():
    from tipsbot import marketsim

    one = [profile2.OddsEntry("m", "OU_2.5", (1.40, 2.90)), profile2.OddsEntry("m", "1X2", (1.5, 4.0, 6.0))]
    pick, info = marketsim.analyse_lobo("m", None, one, marketsim.SimConfig(min_ev=-1, worst_ev=-1, max_loss=1))
    assert pick is None and info["lobo_unverified"] > 0  # bez trhu zbyva 1 trh -> NEOVĚŘENO, nic se nevybere


# --- 5. kombi ----------------------------------------------------------------------------------------------------
def _k(legs, result=None, profit=None):
    return paper.Pick(id="k" + str(len(legs)) + str(legs[0]["odds"]), league="KOMBI", match_id="k", name="t",
                      day="2026-11-24", kickoff=KO.isoformat(), market="KOMBI", selection="2", odds=3.0, book="kombinace",
                      p_model=0.4, source="model", created=NOW.isoformat(), profile="kombi", legs=legs,
                      result=result, profit=profit)


def _leg(name, **kw):
    return {"name": name, "day": "2026-11-24", "market": "1X2", "selection": "1", "odds": 1.8, "target": False,
            "result": None, **kw}


def test_kombi_only_fully_documented_tickets_are_verified(led):
    target_ok = _k([_leg("A", target=True, verified_odds=2.0), _leg("B", verified_odds=1.8, available=True)])
    assert kombi.price_class(target_ok) == kombi.HYPO  # cilovy kurz neni nikdy overeny, ani kdyz nabidka je vyssi
    old = _k([_leg("C"), _leg("D")])
    assert kombi.price_class(old) == kombi.UNDOCUMENTED and not kombi.is_verified(old)
    good = _k([_leg("E", verified_odds=1.85, available=True), _leg("F", verified_odds=1.8, available=True)], "won", 2.0)
    assert kombi.is_verified(good)
    v = kombi.verification([target_ok, old, good])
    assert v["fully_verified"] == 1 and v["verified"]["roi"] == pytest.approx(2.0) and v["hypothetical"]["settled"] == 0
    led.s.add(Ticket(match_id="a", league="", match_name="AKU", match_day=NOW.date(), market="AKU", selection="2",
                     bookmaker="tipsport", odds=3.0, stake=100.0, status="won", profit_loss=200.0))
    led.s.flush()
    pl = kombi.placed(led.s)
    assert pl["tickets"] == 1 and pl["roi"] == pytest.approx(2.0)


# --- 6. jednotny yield ------------------------------------------------------------------------------------------
RESULTS = [("won", 1.0), ("half_won", 0.5), ("lost", -1.0), ("half_lost", -0.5), ("void", 0.0)]


def test_one_yield_definition_everywhere():
    ref = metrics.unit_yield(RESULTS)
    assert ref["staked"] == 4 and ref["profit"] == pytest.approx(0.0) and ref["yield"] == pytest.approx(0.0)
    picks = [paper.Pick(id=str(i), league="ENG1", match_id=str(i), name=f"A{i} - B{i}", day="2026-11-20",
                        kickoff="2026-11-20T15:00:00", market="1X2", selection="1", odds=2.0, book="x", p_model=0.5,
                        source="model", created=NOW.isoformat(), result=r, profit=p) for i, (r, p) in enumerate(RESULTS)]
    st = paper.stats(picks)
    recs = [export.Rec("g", "k", "fotbal", "", f"A{i} - B{i}", "2026-11-20", "1X2", "1", 2.0, "", 0.5, r, profit_units=p)
            for i, (r, p) in enumerate(RESULTS)]
    ex = export.stats(recs)
    p4 = [{"fixture_id": i, "result": {"void": mathcore.PUSH}.get(r, r), "profit": p, "odds": 2.0}
          for i, (r, p) in enumerate(RESULTS)]
    hb, pb = history.bet_stats(p4), PF._bet_stats(p4)
    assert st["roi"] == ex["roi"] == hb["yield"] == hb["roi"] == pytest.approx(ref["yield"])
    assert pb["yield"] == pytest.approx(ref["yield"]) and pb["turnover"] == 4 and hb["n"] == 4
    assert st["push"] == ex["push"] == hb["push"] == 1  # vraceni zvlast
    assert hb["capital_return"] == pytest.approx(0.0)


def test_panel_and_excel_show_same_roi(tmp_path):
    from tipsbot.web import create_app

    url = f"sqlite:///{tmp_path / 'y.db'}"
    with make_session_factory(url)() as s:
        led = Ledger(s)
        paper.save(led, [paper.Pick(id=str(i), league="ENG1", match_id=str(i), name=f"A{i} - B{i}", day="2026-11-20",
                                    kickoff="2026-11-20T15:00:00", market="1X2", selection="1", odds=2.5, book="x",
                                    p_model=0.5, source="model", created=NOW.isoformat(), result=r,
                                    profit=paper.unit_profit(r, 2.5)) for i, r in enumerate(["won", "lost", "void", "lost"])])
        s.commit()
        d = export.collect(s, None, NOW, light=True)
        row = next(r for r in export._summary_rows(d) if r[0] == "Základ")
    roi_excel = row[11]
    html = create_app(url).test_client().get("/?tab=sledovani&profile=zaklad").get_data(as_text=True)
    assert roi_excel == pytest.approx(-0.5 / 3) and f"{roi_excel * 100:+.1f} %" in html


# --- 7. CLV ------------------------------------------------------------------------------------------------------
def test_closing_slots_unchanged_and_stale_is_not_clv():
    assert P4.odds.closing_slots_min == (75, 30, 10)  # posun bodu jen doporucen (meni logiku posledniho bodu)
    assert P4.odds.closing_observed_within_min == 20 and P4.odds.closing_max_source_age_min == 60  # pravidla 2.1
    rows = [{"fixture_id": 1, "result": "won", "profit": 1.0, "clv": None, "clv_last": 0.03,
             "closing_kind": history.CK_STALE}]
    b = history.bet_stats(rows)
    assert b["clv_n"] == 0 and math.isnan(b["avg_clv"])  # zastaraly kurz neni CLV 0 ani overene CLV


# --- 8. ranking --------------------------------------------------------------------------------------------------
def _rrows(n_matches):
    rng = np.random.default_rng(1)
    out = []
    for f in range(n_matches):
        for k in range(3):
            q = float(rng.uniform(40, 95))
            r = q * 0.8 + float(rng.normal(0, 5))
            res = "won" if rng.random() < 0.5 else "lost"
            out.append({"fixture_id": f, "rank": r, "quality": q, "ev": float(rng.normal(0.03, 0.05)),
                        "ev_conservative": 0.0, "band": "60–79", "result": res,
                        "profit": 1.0 if res == "won" else -1.0, "sport": "fotbal"})
    return out


def test_ranking_diagnostics_requires_100_matches():
    small = rankdiag.diagnose(_rrows(40))
    assert small["verdict"].startswith("MÁLO DAT") and small["matches"] == 40
    assert len(small["residual"]) == 3 and small["r2_quality"] > 0.5
    big = rankdiag.diagnose(_rrows(120))
    assert not big["verdict"].startswith("MÁLO DAT")
    assert rankdiag.MIN_MATCHES == 100


# --- 9. rohy a karty ----------------------------------------------------------------------------------------------
def test_new_props_method_not_judged_on_one_pick(tmp_path):
    from tipsbot.web import create_app

    url = f"sqlite:///{tmp_path / 'p.db'}"
    with make_session_factory(url)() as s:
        p = paper.Pick(id="c1", league="ENG1", match_id="m", name="A - B", day="2026-11-22", kickoff="2026-11-22T17:00:00",
                       market="KARTY_4.5", selection="under", odds=1.67, book="x", p_model=0.63, source="model",
                       created="2026-11-22T16:00:00", profile="karty", meta={"method": "25/75"}, result="lost", profit=-1.0)
        paper.save(Ledger(s), [p])
        s.commit()
    html = create_app(url).test_client().get("/?tab=sledovani&profile=karty").get_data(as_text=True)
    assert "nevyhodnocuje se (1 záp., min. 30)" in html


# --- 10. export ---------------------------------------------------------------------------------------------------
def test_excel_has_ranking_diagnostics_and_rejections(led):
    import openpyxl

    MC.log_rejection(led.s, "zaklad", "x:1", "Arsenal - Chelsea", KO,
                     {"market": "OU_2.5", "selection": "under", "book": "tipsport", "odds": 2.6, "ref_fair": 1.93,
                      "ref_median_odds": 1.95, "age_min": 12, "ref_n": 4, "reasons": ["kurz o +33 % nad mediánem"]})
    led.s.commit()
    d = export.collect(led.s, None, NOW)
    wb = openpyxl.load_workbook(io.BytesIO(export.to_xlsx(d)))
    assert "Ranking Diagnostics" in wb.sheetnames and len(wb.sheetnames) == 30  # + Odds Freshness
    vals = [c for row in wb["Market Integrity – All Profiles"].iter_rows(values_only=True) for c in row if c is not None]
    assert "kurz o +33 % nad mediánem" in vals and 2.6 in vals


def test_lobo_uses_fewer_markets_for_half_line_pick():
    from test_marketsim import _entries

    from tipsbot import marketsim

    half = [e for e in _entries() if e.market in ("1X2", "OU_2.5", "OU_1.5", "OU_3.5", "BTTS")]
    cfg = marketsim.SimConfig(min_odds=1.01, max_odds=10, min_ev=-1, worst_ev=-1, max_loss=1)
    prod, _ = marketsim.analyse("m", None, half, cfg)
    lobo, _ = marketsim.analyse_lobo("m", None, half, cfg)
    rest = [e for e in half if e.market != lobo.market]  # vsechny radky (i jinych kancelari) trhu sazky pryc
    assert lobo.scen["lobo"] and lobo.scen["fit_markets"] == marketsim.fit_lambdas(rest)[3] < prod.scen["fit_markets"]
