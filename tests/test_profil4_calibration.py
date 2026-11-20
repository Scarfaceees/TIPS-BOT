"""Profil 4 – faze 2: kontrola protistran, stari kurzu, ukladani predikci A–D, kalibrace, CLV a data leakage.

Fake API (p4_fakes) a zaznam skutecne odpovedi API – zadne zive overeni.
"""

from datetime import timedelta
from dataclasses import replace

import pytest

from p4_fakes import Clock
from test_profil4_flow import FID, KO, NOW, P4, make, routes
from test_profil4_integrity import LEV, NOW as RNOW, candidates, quotes
from tipsbot import profile2
from tipsbot.profil4 import closing, history, hunter, integrity as IN, models as M, odds as O, report, view
from tipsbot.profil4.config import DecisionConfig
from tipsbot.profil4.provider import Fixture

DC = DecisionConfig()


def q(book, market, sel, odds, stamp=NOW, source="api"):
    return O.OddsQuote(1, "A - B", book, market, sel, odds, stamp, source, NOW + timedelta(hours=5), stamp)


# --- 1. kontrola protistran ----------------------------------------------------

def test_every_real_complete_market_passes_pairing():
    """Vsechny kompletni trhy ze zaznamu skutecne odpovedi (vc. AH, 1X2, O/U, golu tymu) projdou bez tvrde chyby."""
    qs = quotes(LEV)
    checked = 0
    for x in qs:
        sels = profile2.selections(x.market)
        if sels and all(any(y.bookmaker == x.bookmaker and y.market == x.market and y.selection == s for y in qs)
                        for s in sels):
            hard, _ = IN.pairing(x, qs, DC)
            assert hard == [], (x.bookmaker, x.key, hard)
            checked += 1
    assert checked > 100


@pytest.mark.parametrize("quotes_, hard, soft", [
    ([q("p", "OU_2.5", "over", 1.95), q("p", "OU_2.5", "under", 1.95)], False, False),  # marze 2.6 %
    ([q("x", "OU_2.5", "over", 2.01), q("x", "OU_2.5", "under", 2.0)], False, False),  # burza ~100 % (−0.2 %)
    ([q("p", "1X2", "1", 2.5), q("p", "1X2", "X", 3.4), q("p", "1X2", "2", 3.1)], False, False),  # trojcestny 1.7 %
    ([q("p", "DC", "1X", 1.25), q("p", "DC", "12", 1.30), q("p", "DC", "X2", 1.55)], False, False),  # DC: soucet ~2.15
    ([q("p", "AH_-0.25", "home", 1.90), q("p", "AH_-0.25", "away", 1.98)], False, False),  # ctvrtinova linie
    ([q("p", "AH_-1.5", "home", 1.67), q("p", "AH_-1.5", "away", 13.0)], True, False),  # zamena strany: 67.6 %
    ([q("p", "OU_2.5", "over", 2.2), q("p", "OU_2.5", "under", 2.2)], True, False),  # 90.9 % = nemozny trh
    ([q("p", "OU_2.5", "over", 1.40), q("p", "OU_2.5", "under", 1.80)], False, True),  # marze 27 % = podezrele
    ([q("p", "OU_2.5", "over", 1.95)], False, True),  # neuplny trh z API
    ([q("tipsport", "OU_2.5", "over", 1.95, source="manual")], False, False),  # rucni jediny kurz je v poradku
])
def test_pairing_rule_on_valid_and_invalid_markets(quotes_, hard, soft):
    h, s = IN.pairing(quotes_[0], quotes_, DC)
    assert bool(h) == hard and bool(s) == soft, (h, s)


def test_pairing_never_mixes_two_updates():
    """Protistrana z jine aktualizace kurzu netvori jeden trh (soucet by mohl byt cokoli)."""
    old = NOW - timedelta(hours=3)
    h, s = IN.pairing(q("p", "OU_2.5", "over", 2.2), [q("p", "OU_2.5", "over", 2.2), q("p", "OU_2.5", "under", 2.2, old)], DC)
    assert h == [] and any("jiné aktualizace" in x for x in s)


# --- 2. stari kurzu ------------------------------------------------------------

def test_quote_card_shows_source_time_fetch_time_age_book_source_and_market():
    c = candidates(quotes(LEV), LEV)[0]
    v = view.candidate(c, RNOW)
    assert v["quote_time"] and v["quote_age"] and v["book"] and v["source"] and v["key"] == c.key
    # kurz u zdroje 14:17 SEC, stazen o hodinu pozdeji: zobrazi se oba casy
    assert v["quote_status"] == O.CURRENT and v["quote_fetched"] and v["quote_fetched"] != v["quote_time"]
    later = RNOW + timedelta(hours=8)
    stale = view.candidate(replace(c, quote=replace(c.quote, fetched_at=later)), later)
    assert stale["quote_status"] == O.STALE and "h" in stale["quote_age"]
    same = view.candidate(replace(c, quote=replace(c.quote, fetched_at=c.quote.stamp)), RNOW)
    assert same["quote_fetched"] == ""


def test_stale_quotes_are_never_a_market_price():
    """Kdyz jsou vsechny kurzy zastarale, trzni model B nevznikne a zadny tip nema 'cenu trhu' ani APPROVED."""
    later = RNOW + timedelta(hours=8)
    qs = quotes(LEV)
    b = M.model_b([x for x in qs if x.status(later, P4.odds) == O.CURRENT], P4)
    assert not b.available and IN.market_price(b, "1X2", "X", 7.0)[0] is None


# --- 3. predikce A–D, kalibrace, CLV -------------------------------------------

def _settled_store(close_delta=-0.1, close_at=KO - timedelta(minutes=10)):
    prov, store, _, _ = make()
    hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000)
    if close_delta is not None:
        later = [O.OddsQuote(FID, "x", t["bookmaker"], t["market"], t["selection"], round(t["odds"] + close_delta, 2),
                             close_at, "api", KO, close_at) for t in store.query("SELECT * FROM tips")]
        O.save(store, later)
    prov2, _, _, _ = make(routes(finished=(2, 1)), clock=Clock(KO + timedelta(hours=3)))
    prov2.client.store = store
    history.settle(store, prov2, KO + timedelta(hours=3))
    return store


def test_predictions_store_every_model_market_and_metadata():
    prov, store, _, _ = make()
    an = hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000)
    rows = [dict(r) for r in store.query("SELECT * FROM predictions")]
    assert {r["model"] for r in rows} == {"A", "B", "C", "D"}
    assert {r["market_type"] for r in rows} >= {"1X2", "OU"}
    r = rows[0]
    for k in ("p", "market", "selection", "league", "market_type", "predicted_at", "model_version", "odds", "bookmaker"):
        assert r[k] is not None, k
    assert all(0 < x["p"] < 1 for x in rows) and all(x["predicted_at"] < x["kickoff"] for x in rows)
    n = len(rows)
    hunter.analyze_fixture(prov, FID, NOW + timedelta(minutes=30), P4, simulations=1_000)  # opakovany sken = prepis
    assert len(store.query("SELECT * FROM predictions")) == n
    assert len({(c.market, c.selection) for c in an.candidates}) * 4 >= n


def test_no_prediction_recorded_after_kickoff():
    prov, store, _, _ = make()
    an = hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000, record=False)
    an.created = KO + timedelta(minutes=1)
    assert history.record_predictions(store, an, P4) == 0


def test_calibration_report_after_settlement():
    store = _settled_store()
    rows = store.query("SELECT * FROM predictions WHERE result IS NOT NULL")
    assert rows and all(r["outcome"] in (0.0, 1.0, None) for r in rows)
    o = {r["market"] + ":" + r["selection"]: r["outcome"] for r in rows if r["model"] == "C"}
    assert o.get("1X2:1") == 1.0 and o.get("OU_2.5:over") == 1.0 and o.get("1X2:X") == 0.0  # skore 2:1
    cal = history.calibration(store)
    assert set(cal["models"]) >= {"A", "B", "C", "D"} and cal["matches"] == 1
    assert not cal["models"]["C"]["significant"]
    assert {"liga", "typ trhu", "kurzové pásmo"} <= set(cal["by"])
    txt = report.format_evaluation(history.evaluate(store))
    assert "KALIBRACE MODELŮ" in txt and "KVALITA DAT" in txt and "kurzové pásmo" in txt and "CLV" in txt


def test_brier_and_log_loss_values():
    s = history.prob_scores([0.8, 0.3], [1, 0])
    assert s["brier"] == pytest.approx((0.04 + 0.09) / 2)
    assert s["log_loss"] == pytest.approx(-(pytest.importorskip("math").log(0.8) + pytest.importorskip("math").log(0.7)) / 2)


def _observe(store, at, latest_update, status="AVAILABLE", n=50, slot=10, note=""):
    from tipsbot.profil4.store import iso

    store.execute("INSERT INTO odds_observations(fixture_id, kickoff, slot_min, observed_at, status, n_quotes, "
                  "latest_update, note) VALUES(?,?,?,?,?,?,?,?)",
                  (FID, iso(KO), slot, iso(at), status, n, iso(latest_update), note))


def _settle_with(close_delta, close_at, obs=None):
    prov, store, _, _ = make()
    hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000)
    if close_delta is not None:
        O.save(store, [O.OddsQuote(FID, "x", t["bookmaker"], t["market"], t["selection"], round(t["odds"] + close_delta, 2),
                                   close_at, "api", KO, close_at) for t in store.query("SELECT * FROM tips")])
    for o in obs or []:
        _observe(store, *o)
    prov2, _, _, _ = make(routes(finished=(2, 1)), clock=Clock(KO + timedelta(hours=3)))
    prov2.client.store = store
    history.settle(store, prov2, KO + timedelta(hours=3))
    return store, [dict(r) for r in store.query("SELECT * FROM tips")]


@pytest.mark.parametrize("delta,status", [(-0.1, history.CLV_POS), (0.3, history.CLV_NEG)])
def test_true_closing_line_gives_clv(delta, status):
    """Zaviraci kurz: pozorovan 8 min pred vykopem a poskytovatel ho aktualizoval 15 min pred vykopem."""
    at = KO - timedelta(minutes=15)
    _, rows = _settle_with(delta, at, [(KO - timedelta(minutes=8), at)])
    assert rows and all(r["closing_kind"] == history.CK_CLOSING and r["clv_status"] == status for r in rows)
    assert all((r["clv"] > 0) == (status == history.CLV_POS) and r["clv_last"] is None for r in rows)


def test_last_observed_quote_is_not_called_closing():
    """Cerstvy kurz (40 min pred vykopem), ale posledni stazeni 35 min pred vykopem: pozdejsi stav neznamy => zadne CLV."""
    at = KO - timedelta(minutes=40)
    _, rows = _settle_with(-0.1, at, [(KO - timedelta(minutes=35), at, "AVAILABLE", 50, 30)])
    assert all(r["closing_kind"] == history.CK_LAST and r["clv"] is None and r["clv_last"] > 0 for r in rows)


def test_provider_not_updating_gives_stale_not_closing():
    """Stazeno 10 min pred vykopem, ale poskytovatel kurz naposledy aktualizoval 5 h pred vykopem."""
    at = KO - timedelta(hours=5)
    _, rows = _settle_with(-0.1, at, [(KO - timedelta(minutes=10), at)])
    assert all(r["closing_kind"] == history.CK_STALE and r["clv"] is None and "5.0 h" in r["closing_reason"] for r in rows)
    # zadny novejsi kurz od doporuceni a posledni pozorovani ma stale stejnou aktualizaci
    _, rows = _settle_with(None, None, [(KO - timedelta(minutes=10), NOW)])
    assert all(r["closing_kind"] == history.CK_STALE and "neaktualizoval" in r["closing_reason"] for r in rows)


def test_missing_closing_has_reason_and_is_not_zero_clv():
    store, rows = _settle_with(None, None)
    assert all(r["closing_kind"] == history.CK_MISSING and r["clv"] is None and "nestáhly" in r["closing_reason"]
               for r in rows)
    _, rows = _settle_with(None, None, [(KO - timedelta(minutes=10), None, "SKIPPED_BUDGET", 0, 10, "kvóta šetřena")])
    assert all(r["closing_kind"] == history.CK_MISSING and "kvóta" in r["closing_reason"] for r in rows)
    s = history.bet_stats(rows)
    assert s["clv_n"] == 0 and s["avg_clv"] != s["avg_clv"]  # NaN, ne 0


def test_clv_detects_line_change():
    prov, store, _, _ = make()
    hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000)
    store.execute("UPDATE tips SET market='OU_3.5', selection='over'")  # tip na linii, kterou kancelar uz nenabizi
    later = [O.OddsQuote(FID, "x", t["bookmaker"], "OU_2.75", "over", 2.0, KO - timedelta(minutes=10), "api", KO,
                         KO - timedelta(minutes=10)) for t in store.query("SELECT * FROM tips")]
    O.save(store, later)
    prov2, _, _, _ = make(routes(finished=(2, 1)), clock=Clock(KO + timedelta(hours=3)))
    prov2.client.store = store
    history.settle(store, prov2, KO + timedelta(hours=3))
    assert all(r["clv_status"] == history.CLV_LINE for r in store.query("SELECT * FROM tips"))


def test_clv_compares_same_book_market_selection_and_line():
    """Novejsi kurz jine kancelare, jineho vyberu nebo jine linie se jako zaviraci nepouzije."""
    prov, store, _, _ = make()
    hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000)
    t = dict(store.query("SELECT * FROM tips")[0])
    at = KO - timedelta(minutes=12)
    other_sel = {"over": "under", "under": "over", "home": "away", "away": "home"}.get(t["selection"], "X")
    O.save(store, [O.OddsQuote(FID, "x", "jina", t["market"], t["selection"], 9.9, at, "api", KO, at),
                   O.OddsQuote(FID, "x", t["bookmaker"], t["market"], other_sel, 9.8, at, "api", KO, at)])
    _observe(store, KO - timedelta(minutes=8), at)
    cl = history._closing(store, FID, t["bookmaker"], t["market"], t["selection"], KO, history.parse_dt(t["odds_time"]))
    assert cl["odds"] not in (9.9, 9.8)


def _tick_env(prov, store):
    import tipsbot.profil4.closing as cl

    store.close = lambda: None
    orig = cl.open_store
    cl.open_store = lambda cfg=None: store
    return cl, orig


def test_closing_slots_75_30_10_each_once_and_recorded():
    prov, store, _, _ = make()
    hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000)
    calls = []
    real = prov.odds_fixture

    def spy(fx, priority="HIGH", force=False):
        calls.append((fx.id, force, priority))
        return real(fx, priority, force)

    prov.odds_fixture = spy
    cl, orig = _tick_env(prov, store)
    build = lambda: (store, prov)  # noqa: E731
    try:
        assert closing.tick(KO - timedelta(hours=5), P4, build) == []  # mimo okno nic, zadny pozadavek
        for m in (74, 60, 29, 20, 9, 3):
            closing.tick(KO - timedelta(minutes=m), P4, build)
        assert len(calls) == 3 and all(c[1] for c in calls)  # 75, 30 a 10 min – kazdy bod jednou, mimo cache
        obs = store.query("SELECT slot_min, status, latest_update FROM odds_observations ORDER BY id")
        assert [o["slot_min"] for o in obs] == [75, 30, 10] and all(o["latest_update"] for o in obs)
        assert closing.tick(KO + timedelta(minutes=1), P4, build) == [] or len(calls) == 3  # po vykopu uz ne
        assert len(calls) == 3
    finally:
        cl.open_store = orig


def test_missed_slots_caught_up_with_one_request_and_budget_skip_is_recorded():
    prov, store, _, _ = make()
    hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000)
    cl, orig = _tick_env(prov, store)
    try:
        prov.client.budget.check = lambda priority="MEDIUM", fixture_id=None, cost=1: type("D", (), {"allowed": False,
                                                                                            "reason": "rezerva"})()
        closing.tick(KO - timedelta(minutes=12), P4, lambda: (store, prov))  # zmeskane 75 a 30 => jedno pozorovani
        obs = store.query("SELECT slot_min, status, note FROM odds_observations")
        assert {o["slot_min"] for o in obs} == {75, 30} and all(o["status"] == "SKIPPED_BUDGET" for o in obs)
        assert "kvóta" in obs[0]["note"]
    finally:
        cl.open_store = orig


def test_approved_tips_fetched_first():
    from tipsbot.profil4.store import Store, iso

    st = Store()
    now = KO - timedelta(minutes=70)
    for fid, dec in ((1, "WATCH"), (2, "APPROVED BET")):
        st.execute("INSERT INTO tips(fixture_id, match, kickoff, market, selection, probs, odds, bookmaker, odds_source, "
                   "decision, predicted_at, model_version, config_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (fid, "A - B", iso(KO), "1X2", "1", "{}", 2.0, "p", "api", dec, iso(NOW), "v", "h"))
    fixtures, _ = closing.due(st, now, P4)
    assert [f["fixture_id"] for f in fixtures] == [2, 1] and fixtures[0]["approved"]


def test_repeated_scans_do_not_duplicate_tips_and_settled_predictions_stay():
    prov, store, _, _ = make()
    hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000)
    n = store.query("SELECT COUNT(*) n FROM tips")[0]["n"]
    hunter.analyze_fixture(prov, FID, NOW + timedelta(minutes=20), P4, simulations=1_000)
    assert store.query("SELECT COUNT(*) n FROM tips")[0]["n"] == n  # vc. BEST AVAILABLE (WATCH)
    prov2, _, _, _ = make(routes(finished=(2, 1)), clock=Clock(KO + timedelta(hours=3)))
    prov2.client.store = store
    history.settle(store, prov2, KO + timedelta(hours=3))
    before = [tuple(r) for r in store.query("SELECT model, market, selection, p, outcome FROM predictions ORDER BY id")]
    an = hunter.analyze_fixture(prov, FID, NOW + timedelta(minutes=40), P4, simulations=1_000, record=False)
    history.record_predictions(store, an, P4)
    after = [tuple(r) for r in store.query("SELECT model, market, selection, p, outcome FROM predictions ORDER BY id")]
    assert before == after  # vyhodnocene predikce se uz nemeni


def test_duplicate_tip_rows_count_as_one_bet():
    rows = [{"id": 1, "fixture_id": 7, "market": "OU_2.5", "selection": "under", "result": "won", "profit": 2.2,
             "decision": "WATCH"},
            {"id": 2, "fixture_id": 7, "market": "OU_2.5", "selection": "under", "result": "won", "profit": 2.2,
             "decision": "WATCH"}]
    bets, dups = history.unique_bets(rows)
    assert len(bets) == 1 and dups == 1


def test_cluster_bootstrap_widens_interval_for_correlated_tips():
    """10 zapasu po 5 tipech se stejnym vysledkem: interval po zapasech musi byt sirsi nez pri nezavislosti."""
    rows = [{"fixture_id": f, "profit": (1.0 if f % 2 else -1.0)} for f in range(10) for _ in range(5)]
    _, lo, hi = history.cluster_ci(history._clusters(rows, lambda r: r["profit"]))
    _, lo2, hi2 = history.cluster_ci({i: (r["profit"], 1.0) for i, r in enumerate(rows)})
    assert (hi - lo) > (hi2 - lo2) * 1.5


def test_significance_needs_interval_not_just_100_matches():
    assert history.verdict(-0.05, 0.08, 150).startswith("neprůkazné")  # 150 zapasu, interval pres nulu
    assert history.verdict(0.01, 0.08, 150) == "průkazně kladné"
    assert history.verdict(0.01, 0.08, 12).startswith("málo dat")


# --- 4. data leakage ----------------------------------------------------------

def test_model_a_ignores_matches_after_kickoff():
    """Vysledky (i zacatek sezony) po vykopu predikovaneho zapasu nesmi zmenit predikci."""
    import test_profil4_flow as t
    from p4_fakes import season_items
    from tipsbot.profil4.provider import parse_fixture

    hist = [parse_fixture(x) for x in season_items(t.TEAMS, t.SEASON_START, rounds=4, seed=5)]
    target = hist[len(hist) // 2]
    fx = replace(target, status="NS", ft_home=None, ft_away=None)
    before = M.model_a(fx, [h for h in hist if h.kickoff < fx.kickoff], P4)
    future = [replace(h, ft_home=9, ft_away=0) if h.kickoff >= fx.kickoff else h for h in hist]
    after = M.model_a(fx, future, P4)
    assert before.available and (before.lam_home, before.lam_away) == (after.lam_home, after.lam_away)


def test_backtest_goal_anchor_uses_only_prematch_odds():
    from tipsbot import backtest

    row = {"pre_o25": 1.90, "pre_u25": 1.95, "sharp_o25": 1.50, "sharp_u25": 2.60}  # zaviraci se hodne pohnul
    key, p = backtest._ou25_anchor(row)
    assert key == "OU_2.5:over" and p == pytest.approx((1 / 1.9) / (1 / 1.9 + 1 / 1.95))
    assert backtest._ou25_anchor({"sharp_o25": 1.5, "sharp_u25": 2.6}) is None  # jen zaviraci kurz = bez ukotveni


def test_ingestion_keeps_prematch_and_closing_goal_odds_apart(tmp_path):
    from tipsbot import ingestion

    csv = ("Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,P>2.5,P<2.5,PC>2.5,PC<2.5\n"
           "E0,10/08/2025,A,B,1,0,1.90,1.95,1.50,2.60\n")
    p = tmp_path / "x.csv"
    p.write_text(csv)
    df = ingestion.parse_football_csv(p, "ENG1")
    assert df["pre_o25"].iloc[0] == 1.90 and df["sharp_o25"].iloc[0] == 1.50
