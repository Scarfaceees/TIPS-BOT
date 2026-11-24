"""Profil 4 – Faze 3.2: Predictive Validation & Settlement Integrity (jen mereni, nic se nemeni)."""

import argparse
import io
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from tipsbot.profil4 import history, intelligence as IG, mathcore, performance as PF, settleaudit as SA
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store, iso

UTC = timezone.utc
T0 = datetime(2026, 12, 1, 15, tzinfo=UTC)
W, HW, P, HL, L, V = mathcore.WON, mathcore.HALF_WON, mathcore.PUSH, mathcore.HALF_LOST, mathcore.LOST, mathcore.VOID


# ---------------------------------------------------------------------------------------------------------------
# Synteticka data
# ---------------------------------------------------------------------------------------------------------------

def _preds(n_matches, league="Premier League", seed=3, skip_model=None, t0=T0):
    """Predikce A–D + trh (bez marze) pro 2 vybery na zapas; skip_model = model chybi u prvniho zapasu."""
    rng = np.random.default_rng(seed)
    rows = []
    for f in range(n_matches):
        ko = t0 + timedelta(days=f)
        for sel in ("over", "under"):
            pm = float(rng.uniform(0.3, 0.7))
            y = float(rng.random() < pm)
            for m in IG.MODELS:
                if skip_model == m and f == 0:
                    continue
                rows.append({"fixture_id": f + (1000 if league == "NHL" else 0), "match": f"H{f} - A{f}", "league": league,
                             "kickoff": iso(ko), "market": "OU_2.5", "selection": sel, "market_type": "OU", "model": m,
                             "p": min(0.95, max(0.05, pm + float(rng.normal(0, 0.03)))), "p_market": pm, "odds": 1.9,
                             "odds_time": iso(ko - timedelta(hours=5)), "predicted_at": iso(ko - timedelta(hours=4)),
                             "settled_at": iso(ko + timedelta(hours=3)), "model_version": "p4", "outcome": y,
                             "result": W if y else L})
    return rows


def _ranks(n_matches, good=True, seed=5, league="Premier League", rank_from_ev=False):
    rng = np.random.default_rng(seed)
    ranks, preds = [], []
    for f in range(n_matches):
        ko = T0 + timedelta(days=f)
        for sel, rk in (("1", float(rng.uniform(65, 95))), ("2", float(rng.uniform(5, 35)))):
            ev = float(rng.uniform(-0.05, 0.1))
            won = rng.random() < ((0.62 if rk >= 60 else 0.38) if good else 0.5)
            ranks.append({"id": len(ranks) + 1, "fixture_id": f, "match": f"H{f} - A{f}", "league": league,
                          "kickoff": iso(ko), "market": "1X2", "selection": sel, "odds": 2.0,
                          "rank": ev * 500 + 30 if rank_from_ev else rk, "computed_at": iso(ko - timedelta(hours=3)),
                          "version": IG.RANK_VERSION, "valid": 1, "decision": "WATCH", "ev": ev,
                          "ev_conservative": ev - 0.01, "robustness": 60.0, "quality": 80.0,
                          "detail": json.dumps({"p": {"C": 0.5}})})
            preds.append({"fixture_id": f, "market": "1X2", "selection": sel, "model": "C", "p_market": 0.48,
                          "result": W if won else L, "outcome": 1.0 if won else 0.0})
    return ranks, preds


def _tip(fid, ko=T0, **kw):
    base = {"fixture_id": fid, "match": f"H{fid} - A{fid}", "league_id": 39, "league": "Premier League",
            "kickoff": iso(ko), "market": "1X2", "selection": "1", "label": "domácí",
            "probs": json.dumps({"A": 0.55, "B": 0.5, "C": 0.52, "D": 0.45}), "fair_odds": 2.0, "odds": 2.1,
            "bookmaker": "pinnacle", "odds_source": "api", "odds_time": iso(ko - timedelta(hours=5)), "ev": 0.05,
            "ev_conservative": 0.02, "robustness": 70, "decision": "WATCH", "category": "",
            "predicted_at": iso(ko - timedelta(hours=4)), "model_version": "p4", "config_hash": "x", "p_market": 0.48,
            "result": None}
    base.update(kw)
    return base


def _insert(st, row):
    st.execute(f"INSERT INTO tips({', '.join(row)}) VALUES({', '.join('?' * len(row))})", list(row.values()))
    return st.query("SELECT * FROM tips ORDER BY id DESC LIMIT 1")[0]


# ---------------------------------------------------------------------------------------------------------------
# 3. Modely A–D proti trhu
# ---------------------------------------------------------------------------------------------------------------

def test_models_compared_on_identical_rows_and_sports_separate():
    rows = _preds(40, skip_model="B") + _preds(10, league="NHL")
    mc = PF.model_comparison(rows, P4)
    fb, nhl = mc["sports"]["fotbal"], mc["sports"]["hokej"]
    assert fb["rows"] == 39 * 2 and fb["matches"] == 39  # zapas bez modelu B vypadl pro VSECHNY modely i trh
    assert {m["matches"] for m in fb["models"].values()} == {39}
    assert nhl["matches"] == 10 and nhl["sufficiency"]["level"] == "MÁLO DAT"


def test_market_probability_is_the_devigged_one_not_raw_odds():
    rows = _preds(35)
    common = IG.common_sample(IG.clean_predictions(rows)[0])
    assert all(r["p_market"] != pytest.approx(1 / 1.9) for r in common)  # ne 1/kurz (s marzi)
    mc = PF.model_comparison(rows, P4)["sports"]["fotbal"]["models"]["trh"]
    exp = np.mean([(r["p_market"] - r["outcome"]) ** 2 for r in common])
    assert mc["brier"] == pytest.approx(exp)
    fair, probs = mathcore.devig([1.9, 1.9])
    assert sum(probs) == pytest.approx(1.0) and probs[0] == pytest.approx(0.5)


def test_chronological_split_has_no_random_mixing():
    common = IG.common_sample(IG.clean_predictions(_preds(50))[0])
    parts = PF.chrono_split(common)
    assert [p["part"] for p in parts] == ["historická / trénovací", "validační", "out-of-sample test"]
    assert [p["matches"] for p in parts] == [30, 10, 10]
    assert parts[0]["to"] < parts[1]["from"] and parts[1]["to"] < parts[2]["from"]  # test je vzdy nejnovejsi
    ids = [{r["fixture_id"] for r in common if p["from"] <= r["kickoff"] <= p["to"]} for p in parts]
    assert not (ids[0] & ids[1]) and not (ids[1] & ids[2])  # zapas nikdy ve dvou castech


def test_leakage_predictions_after_kickoff_dropped_and_late_odds_reported():
    rows = _preds(32)
    late = dict(rows[0], predicted_at=iso(T0 + timedelta(minutes=5)), fixture_id=999)  # predikce po vykopu
    odd = dict(rows[1], odds_time=iso(T0 + timedelta(minutes=1)))  # kurz vstupu po vykopu
    mc = PF.model_comparison([late, odd] + rows[2:], P4)
    assert mc["dropped"]["po_výkopu"] == 1
    tl = mc["sports"]["fotbal"]["timeline"]
    assert tl["odds_after_kickoff"] == 1 and tl["ok"] is False
    assert "nikdy" in tl["note"] and "NEUKLÁDÁ" in mc["reconstruction"]


def test_small_samples_have_no_false_precision():
    mc = PF.model_comparison(_preds(12), P4)["sports"]["fotbal"]["models"]
    assert all(m["brier_ci95"] is None and m["ece"] is None for m in mc.values())
    assert "ECE až od 30" in mc["C"]["ece_note"]


# ---------------------------------------------------------------------------------------------------------------
# 2. Opportunity Ranking
# ---------------------------------------------------------------------------------------------------------------

def test_ranking_bands_small_sample_and_band_metrics():
    ranks, preds = _ranks(20)
    rp = PF.ranking_performance(ranks, preds, [])
    sd = rp["sports"]["fotbal"]
    assert [b["band"] for b in sd["bands"]] == ["0–39", "40–59", "60–79", "80–100"]
    assert sd["matches"] == 20 and sd["selections"] == 40 and sd["sufficiency"]["level"] == "MÁLO DAT"
    b = next(b for b in sd["bands"] if b["selections"])
    for k in ("won", "half_won", "lost", "half_lost", "push", "win_rate", "avg_odds", "avg_p", "avg_p_market", "avg_ev",
              "avg_ev_conservative", "roi", "yield", "clv", "brier", "log_loss", "matches", "selections"):
        assert k in b
    assert all(x["status"].startswith("MÁLO DAT") for x in sd["bands"])
    assert rp["sports"]["hokej"]["matches"] == 0  # sporty se nesmichaji


def test_ranking_levels_and_bonferroni():
    assert PF.sufficiency(99)["level"] == "MÁLO DAT" and PF.sufficiency(100)["level"] == "PŘEDBĚŽNÁ VALIDACE"
    assert PF.sufficiency(200)["level"] == "ROZŠÍŘENÁ VALIDACE" and PF.sufficiency(500)["level"] == "SILNĚJŠÍ DATOVÝ ZÁKLAD"
    assert "nezaručuje" in PF.sufficiency(600)["reason"]
    ranks, preds = _ranks(150)
    sd = PF.ranking_performance(ranks, preds, [])["sports"]["fotbal"]
    assert sd["sufficiency"]["level"] == "PŘEDBĚŽNÁ VALIDACE"
    hi = next(b for b in sd["bands"] if b["band"] == "80–100")
    lo, hi_ = hi["yield_ci95"]
    blo, bhi = hi["yield_ci_bonferroni"]
    assert blo <= lo and bhi >= hi_  # korekce na 4 pasma = sirsi interval
    assert sd["hi_minus_lo"]["status"] == "ranking ≥ 60 průkazně lepší"


def test_added_value_detects_ranking_copying_ev():
    ranks, preds = _ranks(40, rank_from_ev=True)
    av = PF.ranking_performance(ranks, preds, [])["sports"]["fotbal"]["added_value"]
    assert av["correlations"]["EV"] == pytest.approx(1.0) and "EV" in av["copies"]
    assert av["stratified_status"].startswith("MÁLO DAT")


def test_cluster_bootstrap_resamples_matches_not_rows():
    rows = [{"fixture_id": f, "profit": float(v)} for f, v in enumerate(np.random.default_rng(1).normal(0, 1, 40))]
    dup = [dict(r) for r in rows for _ in range(10)]  # 10 korelovanych vyberu kazdeho zapasu

    def mean(rs):
        return float(np.mean([r["profit"] for r in rs]))

    _, ci1 = PF._boot(rows, mean)
    _, ci2 = PF._boot(dup, mean)
    assert ci1 == pytest.approx(ci2)  # vic radku z tehoz zapasu interval NEZUZI


# ---------------------------------------------------------------------------------------------------------------
# 4. CLV, 5. ROI a yield
# ---------------------------------------------------------------------------------------------------------------

def test_clv_counts_only_real_closing_odds():
    tips = [_tip(1, result=W, profit=1.1, clv=0.05, closing_kind=history.CK_CLOSING),
            _tip(2, result=L, profit=-1, clv=-0.02, closing_kind=history.CK_FOREIGN),
            _tip(3, result=W, profit=1.1, clv=None, clv_last=0.3, closing_kind=history.CK_STALE),
            _tip(4, result=L, profit=-1, clv=None, clv_last=0.2, closing_kind=history.CK_LAST),
            _tip(5, result=L, profit=-1, clv=None, closing_kind=history.CK_MISSING),
            _tip(6)]
    for i, t in enumerate(tips):
        t["id"] = i + 1
    c = PF.clv_performance(tips, [], T0 + timedelta(days=1))
    assert c["valid"] == 2 and c["settled"] == 5
    assert c["overall"]["clv"] == pytest.approx(0.015) and c["overall"]["clv_pos"] == 0.5
    assert c["classes"]["zastaralý kurz"] == 1 and c["classes"]["poslední pozorovaný kurz (není závěrečný)"] == 1
    assert c["classes"]["zatím nevyhodnoceno"] == 1
    assert set(c["by_sport"]["fotbal"]["by_source"]) == {PF.CLV_CLASS[history.CK_CLOSING], PF.CLV_CLASS[history.CK_FOREIGN]}


def test_roi_yield_push_half_and_duplicates():
    tips = [_tip(1, result=W, profit=1.0, odds=2.0), _tip(2, result=L, profit=-1.0), _tip(3, result=P, profit=0.0),
            _tip(4, result=HW, profit=1.0, odds=3.0), _tip(5, result=V, profit=0.0),
            _tip(1, result=W, profit=1.0, odds=2.0),  # opakovany zaznam stejneho vyberu
            _tip(7, result=W, profit=1.0, predicted_at=iso(T0 + timedelta(minutes=1)))]  # ulozeny po vykopu
    for i, t in enumerate(tips):
        t["id"] = i + 1
    f = PF.finance(tips, [])
    a = f["by_sport"]["fotbal"]["all"]
    # Faze 3.2.2 (jednotna definice, metrics.py): push neni v obratu -> obrat 3, yield 1/3; ROI na 1 j. = yield,
    # zisk / 100 j. je zvlast jako zhodnoceni kapitalu (driv obrat 4 a yield 25 %)
    assert a["n"] == 4 and a["turnover"] == 3.0 and a["profit"] == pytest.approx(1.0) and a["push"] == 1
    assert a["yield"] == pytest.approx(1 / 3) and a["roi"] == pytest.approx(1 / 3)
    assert a["capital_return"] == pytest.approx(1.0 / PF.BANKROLL_UNITS)
    assert a["win_rate"] == pytest.approx(2 / 3)  # push se do win rate nepocita
    assert f["excluded"] == {"opakované záznamy stejného výběru": 1, "uloženo po výkopu": 1, "neplatné (void)": 1}


def test_virtual_units_and_real_money_never_mixed(tmp_path):
    from datetime import date

    from tipsbot import export
    from tipsbot.storage import Ledger, make_session_factory

    s = make_session_factory(f"sqlite:///{tmp_path / 'm.db'}")()
    led = Ledger(s)
    led.deposit(1000)
    t = led.add_ticket(bookmaker="tipsport", odds=2.0, stake=50, match_id="m", market="1X2", selection="1",
                       match_day=date(2026, 11, 20), match_name="A - B")
    led.settle_ticket(t.id, "won")
    led.add_combo_ticket(bookmaker="tipsport", odds=1.9, stake=25, legs=[
        {"match_name": "C - D", "match_day": date(2026, 11, 20), "market": "OU_2.5", "selection": "under", "odds": 1.4},
        {"match_name": "E - F", "match_day": date(2026, 11, 20), "market": "OU_2.5", "selection": "under", "odds": 1.36}])
    s.commit()
    st = Store(tmp_path / "p4.db")
    _insert(st, _tip(1, result=W, profit=1.1))
    st.close()
    d = export.collect(s, tmp_path / "p4.db", datetime(2026, 12, 3))
    rows = export.finance_rows(d)
    real = [r for r in rows if r[2] == "Kč"]
    virt = [r for r in rows if r[2] == "j."]
    assert real and virt
    assert all(r[9] is None and r[10] is None for r in real)  # Kc nikdy ve sloupcich jednotek
    assert all(r[12] is None and r[13] is None for r in virt)  # jednotky nikdy ve sloupcich Kc
    assert {r[0] for r in real} == {"Moje tikety (vsazeno) – ručně zadaný", "Moje tikety (vsazeno) – kombinovaný (AKU)"}
    assert next(r for r in real if "ručně" in r[0])[13] == pytest.approx(50.0)


# ---------------------------------------------------------------------------------------------------------------
# 6. Settlement Integrity – nezavisly vypocet (vlastni pripady)
# ---------------------------------------------------------------------------------------------------------------

FOOTBALL_CASES = [  # (trh, vyber, skore, kurz, vysledek, zisk) – rucne podle pravidel kancelari
    ("AH_-0.25", "home", (1, 1), 2.0, HL, -0.5), ("AH_-0.25", "home", (2, 1), 2.0, W, 1.0),
    ("AH_-0.75", "home", (1, 0), 2.0, HW, 0.5), ("AH_-0.75", "home", (2, 0), 2.0, W, 1.0),
    ("AH_-1.0", "home", (1, 0), 1.8, P, 0.0), ("AH_0.25", "away", (1, 1), 1.9, HL, -0.5),
    ("AH_1.75", "home", (0, 2), 2.2, HL, -0.5), ("AH_-1.5", "away", (2, 0), 2.5, L, -1.0),
    ("OU_2.25", "over", (1, 1), 2.0, HL, -0.5), ("OU_2.75", "over", (2, 1), 2.0, HW, 0.5),
    ("OU_3.0", "under", (2, 1), 1.9, P, 0.0), ("TTA_1.0", "under", (0, 1), 1.85, P, 0.0),
    ("TTH_0.5", "over", (1, 0), 1.5, W, 0.5), ("DNB", "away", (1, 1), 2.4, P, 0.0), ("1X2", "X", (0, 0), 3.4, W, 2.4),
]


@pytest.mark.parametrize("market,sel,score,odds,res,profit", FOOTBALL_CASES)
def test_independent_football_settlement(market, sel, score, odds, res, profit):
    assert SA.football_check(market, sel, *score, odds) == (res, pytest.approx(profit))
    # produkce na stejnych pripadech souhlasi (kontrola, ze se obe implementace shoduji)
    assert mathcore.settle(market, sel, *score) == res
    assert mathcore.unit_profit(res, odds) == pytest.approx(profit)


NHL_CASES = [  # (trh, vyber, reg, final, konec, kurz, vysledek, zisk)
    ("MONEYLINE", "home", (2, 2), (3, 2), "OT", 1.8, W, 0.8),  # vitez v prodlouzeni
    ("MONEYLINE", "away", (3, 3), (4, 3), "SO", 2.1, L, -1.0),  # najezdy: vitezny gol najezdu v konecnem skore
    ("REG", "X", (2, 2), (3, 2), "OT", 4.0, W, 3.0), ("REG", "1", (2, 2), (3, 2), "OT", 2.2, L, -1.0),
    ("OT", "yes", (1, 1), (1, 2), "SO", 4.2, W, 3.2), ("OT", "no", (3, 1), (3, 1), "REG", 1.3, W, 0.3),
    ("PUCKLINE_1.5", "home-1.5", (4, 2), (4, 2), "REG", 2.5, W, 1.5),
    ("PUCKLINE_1.5", "away+1.5", (3, 3), (4, 3), "SO", 1.6, W, 0.6),
    ("PUCKLINE_1.5R", "away-1.5", (3, 3), (4, 3), "SO", 1.87, L, -1.0),  # Vancouver – Minnesota 22.11.2026
    ("OU_5.5", "over", (3, 2), (3, 3), "OT", 1.9, W, 0.9), ("OU_6.0", "under", (3, 3), (3, 3), "REG", 1.9, P, 0.0),
    ("OU60_5.5", "over", (3, 2), (4, 2), "OT", 2.0, L, -1.0), ("OU_5.25", "under", (3, 2), (3, 2), "REG", 2.0, HW, 0.5),
    ("TTH_2.5", "over", (2, 2), (3, 2), "OT", 2.0, W, 1.0),
]


@pytest.mark.parametrize("market,sel,reg,final,end,odds,res,profit", NHL_CASES)
def test_independent_nhl_settlement(market, sel, reg, final, end, odds, res, profit):
    assert SA.nhl_check(market, sel, reg, final, end, odds) == (res, pytest.approx(profit))


def test_nhl_needs_regulation_score_or_is_unverifiable():
    with pytest.raises(ValueError):
        SA.nhl_check("REG", "1", None, (3, 2), "OT", 2.0)
    assert SA.nhl_check("REG", "1", None, (3, 2), "REG", 2.0)[0] == W  # konec v 60 min: reg = konecne skore


def test_known_finding_nhl_push_stored_as_void_is_flagged():
    """Nalez Faze 3.2: nhl_settle.grade (hlavni bot) vraci u souctu presne na linii 'void', ktery Profil 4 chape
    jako neplatnou sazku. Hlavni bot zustava beze zmeny; Profil 4 prevadi na push (profil4.nhl.p4_result)."""
    from tipsbot import nhl_settle
    from tipsbot.profil4 import nhl

    g = {"reg": (3, 3), "final": (3, 3), "end": "REG"}
    assert nhl_settle.grade("OU_6.0", "under", g) == "void"  # konvence hlavniho bota (Ticket.status void = vraceno)
    assert nhl.p4_result("void") == P and nhl.p4_result(W) == W and nhl.p4_result(None) is None
    st, why = SA.compare("void", 0.0, SA.nhl_check("OU_6.0", "under", (3, 3), (3, 3), "REG", 1.9))
    assert st == SA.MISMATCH and "void (neplatná sázka)" in why and "známá chyba" in why


def _nhl_game(fid, home=3, away=3, ko=T0):
    goals = [{"period": 1, "homeScore": h, "awayScore": a} for h, a in ((1, 0), (2, 1), (3, 2), (3, 3))]
    return {"games": [{"id": fid, "gameState": "FINAL", "startTimeUTC": ko.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "homeTeam": {"abbrev": "TOR", "score": home}, "awayTeam": {"abbrev": "LAK", "score": away},
                       "goals": goals, "gameOutcome": {"lastPeriodType": "REG"}}]}


def test_nhl_push_saved_as_push_and_counted_in_statistics():
    from tipsbot.profil4 import nhl

    st = Store()
    nhl_tip = dict(league="NHL", market="OU_6.0", selection="under", odds=1.9, closing_kind=None)
    t = _insert(st, _tip(7001, **nhl_tip))
    _insert(st, _tip(7002, league="NHL", market="MONEYLINE", selection="home", odds=2.0, ko=T0))
    st.execute("INSERT INTO predictions (fixture_id, match, league, kickoff, market, selection, market_type, model, p, "
               "predicted_at, model_version, config_hash) VALUES (7001, 'H - A', 'NHL', ?, 'OU_6.0', 'under', 'OU', 'C', "
               "0.5, ?, 'p4', 'x')", (iso(T0), iso(T0 - timedelta(hours=4))))
    games = {"games": _nhl_game(7001)["games"] + [dict(_nhl_game(7002, 4, 2)["games"][0], id=7002)]}
    nhl.settle(st, T0 + timedelta(hours=5), fetch=lambda d: games)
    row = st.query("SELECT * FROM tips WHERE id=?", (t["id"],))[0]
    assert row["result"] == P and row["profit"] == 0.0 and row["score"] == "3:3"
    assert st.query("SELECT result, outcome FROM predictions")[0][:] == (P, None)
    a = st.query("SELECT * FROM settlement_audit WHERE tip_id=?", (t["id"],))[0]
    assert a["status"] == SA.MATCH and a["algo_version"] == nhl.NHL_GRADE_VERSION and "push" in a["method"]
    assert json.loads(a["raw_result"])["reg"] == [3, 3]
    # agregace: vraceni (push) je zvlast evidovane vypořádání se ziskem 0, ne neplatna sazka; od Faze 3.2.2 neni v obratu
    tips = [dict(r) for r in st.query("SELECT * FROM tips ORDER BY id")]
    bs = history.bet_stats(tips)
    # Faze 3.2.2 jednotna definice: push neni v obratu ani v poctu sazek (driv n 2, yield 0,5), eviduje se zvlast
    assert bs["n"] == 1 and bs["push"] == 1 and bs["profit"] == pytest.approx(1.0) and bs["yield"] == pytest.approx(1.0)
    fin = PF.finance(tips, [])["by_sport"]["hokej"]["all"]
    assert fin["push"] == 1 and fin["turnover"] == 1.0 and fin["win_rate"] == pytest.approx(1.0)  # driv obrat 2
    assert PF.finance(tips, [])["excluded"]["neplatné (void)"] == 0


def test_legacy_nhl_void_push_stays_and_is_reported():
    """Starsi zaznam (pred opravou) se NEPREPISUJE – audit ho jen oznaci se srozumitelnym duvodem."""
    st = Store()
    _insert(st, _tip(7100, league="NHL", market="OU_6.0", selection="under", odds=1.9, result=V, profit=0.0,
                     score="3:3"))
    before = [dict(r) for r in st.query("SELECT * FROM tips")]
    out = SA.audit_history(st, T0 + timedelta(days=1))
    assert [dict(r) for r in st.query("SELECT * FROM tips")] == before
    assert out["total"][SA.MISMATCH] == 1 and "známá chyba NHL" in out["items"][0]["reason"]
    assert any(k["id"] == "nhl-push-as-void" for k in IG.KNOWN_ISSUES)


def test_audit_record_at_settlement_and_no_silent_overwrite():
    st = Store()
    t = _insert(st, _tip(10, market="AH_-0.25", selection="home", odds=2.0))
    history.settle_tip(st, t, HL, "1:1", T0, T0 + timedelta(hours=3),
                       {"raw": {"fixture_id": 10, "status": "FT", "ft": [1, 1]}})
    a = st.query("SELECT * FROM settlement_audit")[0]
    assert a["status"] == SA.MATCH and a["result"] == HL and a["profit"] == -0.5 and a["check_result"] == HL
    assert a["line"] == -0.25 and a["units"].startswith("j.") and json.loads(a["raw_result"])["ft"] == [1, 1]
    assert a["source"].startswith("API-Football") and a["algo_version"] and a["check_version"] == SA.CHECK_VERSION
    # druhy zapis stejneho tipu (napr. opakovany beh) auditni zaznam neprepise
    SA.record(st, t, W, 1.0, "1:1", {"raw": {"ft": [1, 1]}}, T0)
    assert st.query("SELECT COUNT(*) AS n, MAX(result) AS r FROM settlement_audit")[0]["r"] == HL


def test_mismatch_is_flagged_not_overwritten():
    st = Store()
    t = _insert(st, _tip(11, market="OU_2.5", selection="over"))
    assert SA.record(st, t, W, 1.1, "1:1", {"raw": {"ft": [1, 1]}}, T0) == SA.MISMATCH  # produkce by se mylila
    a = st.query("SELECT * FROM settlement_audit")[0]
    assert a["check_result"] == L and "výsledek produkce won, nezávisle lost" in a["reason"]
    assert st.query("SELECT result FROM tips WHERE id=?", (t["id"],))[0]["result"] is None  # nic neprepsano


def test_settlement_audit_failure_never_stops_settlement():
    st = Store()
    st.execute("DROP TABLE settlement_audit")
    t = _insert(st, _tip(12))
    history.settle_tip(st, t, W, "2:0", T0, T0 + timedelta(hours=3), {"raw": {"ft": [2, 0]}})
    assert st.query("SELECT result FROM tips")[0]["result"] == W


def test_historical_audit_reads_only_and_reports_counts():
    st = Store()
    _insert(st, _tip(20, result=W, profit=1.1, score="2:0"))
    _insert(st, _tip(21, market="OU_2.5", selection="over", result=W, profit=1.1, score="1:0"))  # spatne ulozeno
    _insert(st, _tip(22, result=V, profit=0.0, score=None))  # zruseny zapas bez skore
    _insert(st, _tip(23, league="NHL", market="REG", selection="1", result=L, profit=-1.0, score="3:2 (OT)"))
    st.execute("INSERT INTO predictions (fixture_id, match, league, kickoff, market, selection, market_type, model, p, "
               "predicted_at, model_version, config_hash, outcome, result, score) VALUES "
               "(20, 'H20 - A20', 'Premier League', ?, '1X2', '1', '1X2', 'C', 0.5, ?, 'p4', 'x', 1.0, 'won', '2:0')",
               (iso(T0), iso(T0 - timedelta(hours=4))))
    before = [dict(r) for r in st.query("SELECT * FROM tips ORDER BY id")]
    out = SA.audit_history(st, T0 + timedelta(days=1))
    assert [dict(r) for r in st.query("SELECT * FROM tips ORDER BY id")] == before  # nic se nezmenilo
    assert out["total"] == {"checked": 5, SA.MATCH: 2, SA.MISMATCH: 1, SA.UNVERIFIABLE: 2}
    assert out["items"][0]["id"] == 2 and "nezávisle lost" in out["items"][0]["reason"]
    assert any("60 minutách" in k for k in out["unverifiable_reasons"])
    run = st.query("SELECT * FROM settlement_audit_runs")[0]
    assert (run["checked"], run["matched"], run["mismatched"], run["unverifiable"]) == (5, 2, 1, 2)
    summ = SA.summary(st)
    assert summ["last_run"]["mismatched"] == 1 and summ["settled_without_record"] == 4


def test_old_database_without_audit_tables_still_audits(tmp_path):
    import sqlite3

    st = Store(tmp_path / "x.db")
    _insert(st, _tip(30, result=W, profit=1.1, score="2:0"))
    st.close()
    con = sqlite3.connect(tmp_path / "x.db")
    con.execute("DROP TABLE settlement_audit")
    con.commit()
    con.row_factory = sqlite3.Row

    class RO:
        def query(self, q, p=()):
            return con.execute(q, tuple(p)).fetchall()

    out = SA.audit_history(RO(), T0, persist=False)
    assert out["total"][SA.MATCH] == 1 and "error" in SA.summary(RO())
    con.close()
    st = Store(tmp_path / "x.db")  # migrace je opakovatelna a nedestruktivni
    assert st.query("SELECT COUNT(*) AS n FROM tips")[0]["n"] == 1
    st.close()
    st = Store(tmp_path / "x.db")
    assert st.query("SELECT COUNT(*) AS n FROM settlement_audit")[0]["n"] == 0
    st.close()


# ---------------------------------------------------------------------------------------------------------------
# 11. Monitoring, panel, CLI, Excel
# ---------------------------------------------------------------------------------------------------------------

def _filled_store(path=None):
    st = Store(path) if path else Store()
    for i in range(3):
        _insert(st, _tip(40 + i, result=W if i else L, profit=1.1 if i else -1.0, score="2:0" if i else "0:1",
                         clv=0.02, closing_kind=history.CK_CLOSING))
    return st


def test_alerts_mismatch_and_store_performance_is_read_only_for_history():
    st = _filled_store()
    _insert(st, _tip(50, market="OU_2.5", selection="over", result=W, profit=1.1, score="1:0"))  # neshoda
    before = [dict(r) for r in st.query("SELECT * FROM tips ORDER BY id")]
    out = PF.store_performance(st, T0 + timedelta(days=1))
    assert [dict(r) for r in st.query("SELECT * FROM tips ORDER BY id")] == before
    assert out["alerts"][0]["level"] == "chyba" and "Settlement Integrity" in out["alerts"][0]["text"]
    assert json.loads(st.get(PF.PERF_KEY))["t"] == out["t"]
    assert st.archived("p4_perf_week")  # tydenni snimek
    PF.store_performance(st, T0 + timedelta(days=1, hours=2))  # historicky audit jen jednou za den
    assert len(st.query("SELECT * FROM settlement_audit_runs")) == 1


def test_clv_sharp_change_alert():
    now = T0 + timedelta(days=40)
    tips = [_tip(i, ko=now - timedelta(days=20 + i % 10), result=W, profit=1.1, clv=0.06, closing_kind=history.CK_CLOSING)
            for i in range(12)]
    tips += [_tip(100 + i, ko=now - timedelta(days=1 + i % 5), result=L, profit=-1, clv=-0.03, closing_kind=history.CK_CLOSING)
             for i in range(12)]
    for i, t in enumerate(tips):
        t["id"] = i + 1
    perf = {"clv": PF.clv_performance(tips, [], now)}
    al = PF.alerts(perf, None, None)
    assert any("Prudká změna CLV" in a["text"] for a in al)


def test_daily_monitoring_runs_once_a_day(tmp_path, monkeypatch):
    from tipsbot.profil4 import closing

    monkeypatch.setattr(type(P4), "db_path", property(lambda self: tmp_path / "p4.db"))
    _filled_store(tmp_path / "p4.db").close()
    calls = []
    monkeypatch.setattr(PF, "store_performance", lambda st, now, **kw: calls.append(now) or {"alerts": []})
    closing.perf_daily(T0, P4)
    closing.perf_daily(T0 + timedelta(hours=5), P4)
    closing.perf_daily(T0 + timedelta(hours=25), P4)
    assert calls == [T0, T0 + timedelta(hours=25)]


def test_panel_predictive_performance_section_mobile_cards(tmp_path, monkeypatch):
    from tipsbot.web import create_app

    monkeypatch.setattr(type(P4), "db_path", property(lambda self: tmp_path / "p4.db"))
    st = _filled_store(tmp_path / "p4.db")
    PF.store_performance(st, T0 + timedelta(days=1))
    st.close()
    c = create_app(f"sqlite:///{tmp_path / 'm.db'}").test_client()
    html = c.get("/?tab=profil4").get_data(as_text=True)
    sec = html[html.index('id="p4perf"'):html.index('id="p4nhl"')]
    for text in ("A. Model Comparison", "B. Opportunity Ranking Performance", "C. CLV Monitor", "D. Settlement Integrity",
                 "E. Data Sufficiency", "MÁLO DAT", "Metodika výpočtu", "Audit settlementu"):
        assert text in sec, text
    assert "<table" not in sec  # na mobilu karty, ne siroke tabulky
    r = c.post("/profil4/settlement-audit", follow_redirects=True)
    assert "Audit settlementu: zkontrolováno 3, shoda 3" in r.get_data(as_text=True)


def test_cli_performance_sections_and_audit_history(tmp_path, monkeypatch, capsys):
    from tipsbot.profil4 import cli

    _filled_store(tmp_path / "c.db").close()
    monkeypatch.setattr(cli, "build", lambda db=None, cfg=P4: (Store(tmp_path / "c.db"), None))  # CLI store zavira
    p = argparse.ArgumentParser()
    cli.add_parser(p.add_subparsers(dest="cmd"))
    out = tmp_path / "report.json"
    for sec in ("performance", "ranking-performance", "model-comparison", "clv", "settlement-integrity"):
        args = p.parse_args(["profil4", "intel", sec] + (["--out", str(out)] if sec == "performance" else []))
        args.db = None
        assert cli.run(args) == 0
    text = capsys.readouterr().out
    for t in ("Model Comparison", "Opportunity Ranking Performance", "== CLV ==", "Settlement Integrity", "Upozornění"):
        assert t in text
    assert json.loads(out.read_text(encoding="utf-8"))["ranking"]["version"] == IG.RANK_VERSION
    args = p.parse_args(["profil4", "intel", "settlement-integrity", "--audit-history"])
    args.db = None
    assert cli.run(args) == 0
    st = Store(tmp_path / "c.db")
    assert len(st.query("SELECT * FROM settlement_audit_runs")) == 2  # denni + vyzadany beh, tipy beze zmeny
    st.close()


def test_excel_new_sheets_filled_and_missing_data_is_not_zero(tmp_path):
    from openpyxl import load_workbook

    from tipsbot import export
    from tipsbot.storage import make_session_factory

    s = make_session_factory(f"sqlite:///{tmp_path / 'm.db'}")()
    _filled_store(tmp_path / "p4.db").close()
    d = export.collect(s, tmp_path / "p4.db", datetime(2026, 12, 3))
    wb = load_workbook(io.BytesIO(export.to_xlsx(d)))
    assert all(n in wb.sheetnames for n in export.PERF_SHEETS) and len(wb.sheetnames) == 30  # 20 puvodnich + 8 Faze 3.2.1 + 1 Faze 3.2.2 + Odds Freshness
    si = [c for row in wb["Settlement Integrity"].iter_rows(values_only=True) for c in row if c is not None]
    assert "přepočet při exportu" in si
    d2 = export.collect(s, tmp_path / "neni.db", datetime(2026, 12, 3))
    wb2 = load_workbook(io.BytesIO(export.to_xlsx(d2)))
    for n in export.PERF_SHEETS:
        assert "nejsou k dispozici" in str(wb2[n].cell(5, 1).value)
    rp = wb["Ranking Performance"]
    vals = [r for r in rp.iter_rows(min_row=8, max_row=15, values_only=True) if r[0]]
    assert all(v[9] is None for v in vals if v[3] == 0)  # pasmo bez vyberu: win rate prazdny, ne 0


def test_measurement_never_changes_decisions_or_weights():
    import inspect

    src = inspect.getsource(PF) + inspect.getsource(SA)
    for forbidden in ("UPDATE tips", "UPDATE predictions", "DELETE FROM", "UPDATE rankings"):
        assert forbidden not in src
