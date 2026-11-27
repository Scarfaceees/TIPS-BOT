"""Faze 3.2.1: spolecna Market Integrity, Profily 2 a 3, rohy a karty zvlast, kombi kurzy, Profil 4 podle sportu,
Profil 5 – Market Value (shadow), jednotne metriky a export."""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from tipsbot import czechodds, kombi, marketcheck as MC, marketsim, paper, profile2, profil5
from tipsbot.devig import devig_power
from tipsbot.oddsapi import Event, MarketOdds
from tipsbot.storage import BankrollEntry, Ledger, P5Candidate, Tip, make_session_factory, utcnow

NOW = datetime(2026, 11, 24, 12, 0)
KO = NOW + timedelta(hours=6)


@pytest.fixture
def led():
    return Ledger(make_session_factory("sqlite://")())


def _books(**over):
    """Trh 1X2 u 6 kancelari (2 z jedne skupiny), ferova cena kolem 2.00 / 3.40 / 3.90."""
    b = {"pinnacle": {"1": 1.97, "X": 3.45, "2": 3.95}, "bet365": {"1": 1.95, "X": 3.40, "2": 3.90},
         "williamhill": {"1": 1.93, "X": 3.40, "2": 3.85}, "unibet_se": {"1": 1.96, "X": 3.35, "2": 3.80},
         "unibet_nl": {"1": 1.96, "X": 3.35, "2": 3.80}, "marathonbet": {"1": 1.98, "X": 3.40, "2": 3.90}}
    b.update(over)
    return b


def _stamps(books, age=30):
    return {k: NOW - timedelta(minutes=age) for k in books}


def _event(books=None, market="1X2", sels=("1", "X", "2"), mid="oddsapi:e1", ko=KO):
    return Event(mid, "ENG1", "Arsenal", "Chelsea", ko, {market: MarketOdds(market, sels, books or _books())})


# --- spolecna Market Integrity ----------------------------------------------------------------------------------
def test_same_offer_same_verdict_in_every_profile(led):
    books = _books(marathonbet={"1": 2.30, "X": 3.40, "2": 3.90})  # extremni kurz jedne kancelare
    a = MC.check("1X2", "1", 2.30, "marathonbet", books, ("1", "X", "2"), NOW, _stamps(books))
    b = MC.check("1X2", "1", 2.30, "marathonbet", books, ("1", "X", "2"), NOW, _stamps(books))
    assert a == b and a["status"] == MC.SUSPECT and any("nad mediánem" in r for r in a["reasons"])
    picks = [paper.Pick(id=str(i), league="ENG1", match_id="e1", name="A - B", day="2026-11-24", kickoff=KO.isoformat(),
                        market="1X2", selection="1", odds=2.30, book="marathonbet", p_model=0.5, source="model",
                        created=NOW.isoformat(), profile=prof, integrity=a) for i, prof in enumerate(("zaklad", "profil2"))]
    from tipsbot import strategy

    assert strategy.integrity_consistency(picks) == []
    picks[1].integrity = {**a, "status": MC.OK}
    assert len(strategy.integrity_consistency(picks)) == 1  # neshoda by se ukazala


def test_reference_excludes_evaluated_book_and_its_family():
    books = _books()
    ref = MC.consensus(books, ("1", "X", "2"), "1", NOW, _stamps(books), exclude="unibet_se")
    assert "unibet_se" not in ref["included"] and "unibet_nl" not in ref["included"]
    assert ref["excluded"]["unibet_nl"].startswith("stejná skupina") and ref["n"] == 4
    # de-vig: kazda kancelar mocninnou metodou, skupina = median, reference = median skupin
    fair = [devig_power([books[b][s] for s in ("1", "X", "2")])[0] for b in ("bet365", "marathonbet", "pinnacle", "williamhill")]
    assert ref["p"] == pytest.approx(float(np.median(fair)))
    assert sum(devig_power([1.97, 3.45, 3.95])) == pytest.approx(1.0)


def test_stale_missing_counterside_and_too_few_books():
    books = _books(bet365={"1": 1.95, "X": 3.40})  # bet365 bez protistrany
    st = _stamps(books) | {"williamhill": NOW - timedelta(hours=8)}  # zastaraly
    ref = MC.consensus(books, ("1", "X", "2"), "1", NOW, st, exclude="pinnacle")
    assert ref["excluded"]["bet365"].startswith("neúplný trh") and ref["excluded"]["williamhill"].startswith("zastaralý")
    chk = MC.check("1X2", "1", 1.95, "bet365", books, ("1", "X", "2"), NOW, st)
    assert chk["counterside"] == "CHYBÍ" and chk["status"] in (MC.PARTIAL, MC.UNVERIFIED)
    old = MC.check("1X2", "1", 1.93, "williamhill", books, ("1", "X", "2"), NOW, st)
    assert old["freshness"] == "ZASTARALÝ"
    few = {"pinnacle": books["pinnacle"], "marathonbet": books["marathonbet"]}
    chk = MC.check("1X2", "1", 1.97, "pinnacle", few, ("1", "X", "2"), NOW, _stamps(few))
    assert chk["reference"] == MC.UNVERIFIED and chk["status"] == MC.UNVERIFIED  # malo dat != v poradku


def test_same_market_and_line_only():
    ou25 = {b: {"over": 1.90, "under": 1.95} for b in ("a", "b", "c", "d")}
    ev = Event("oddsapi:x", "ENG1", "A", "B", KO, {"OU_2.5": MarketOdds("OU_2.5", ("over", "under"), ou25)})
    assert MC.for_event(ev, "OU_2.75", "over", 1.9, "a", NOW)["status"] == MC.UNVERIFIED  # jina linie = jiny trh
    assert MC.check("OU_2.5", "home", 1.9, "a", ou25, ("over", "under"), NOW)["validity"] == "NESHODA"
    assert MC.check("1X2", "1", 2.0, "x", None, None, NOW)["status"] == MC.UNVERIFIED  # bez dat = NEOVĚŘENO


def test_median_price_availability():
    books = _books()
    chk = MC.check("1X2", "1", 1.96, "zahraničí (medián)", books, ("1", "X", "2"), NOW, _stamps(books))
    assert chk["availability"] == MC.OK and "pinnacle" in chk["available_at"]
    assert chk["ref_n"] == 5  # u medianu se nic nevynechava (neni to nabidka jedne kancelare)


def test_track_records_integrity_without_changing_pick(led):
    entries, _ = czechodds.parse_text("tipsport | Německo - Itálie | OU_2.5 | 1.76 2.10", NOW)
    ev = Event("x:1", "UNL", "Germany", "Italy", KO)
    model = {"OU_2.5:over": 0.40, "OU_2.5:under": 0.60}
    paper.track(led, "UNL", [(ev, model, entries)], NOW)
    (p,) = paper.load(led)
    assert (p.market, p.selection, p.odds) == ("OU_2.5", "under", 2.10)  # stejny vyber jako drive
    assert p.integrity["status"] == MC.UNVERIFIED  # trh neni v kurzech kancelari -> NEOVĚŘENO, ne v poradku


# --- Profil 2 a 3 ----------------------------------------------------------------------------------------------
def test_profile2_scenarios_split_and_ev():
    entries = [profile2.OddsEntry("t", "1X2", (2.10, 3.40, 3.60)), profile2.OddsEntry("t", "OU_2.5", (1.95, 1.90)),
               profile2.OddsEntry("t", "AH_-0.25", (2.05, 1.80))]
    a = profile2.analyse("A - B", entries, (1.9, 1.0), "model")
    o = next(x for x in a.offers if x.market == "AH_-0.25" and x.selection == "home")
    pk = profile2.Pick("value", "test", o)
    d = profile2.scenario_data(a, pk)
    assert d["sims"] == 1000 and d["seed"] == profile2._seed("A - B")
    for s, ev in zip(d["scenarios"], o.ev):
        sp = s["split"]
        assert sum(sp.values()) == pytest.approx(1.0, abs=1e-3)
        assert sp["half_loss"] > 0  # ctvrtinova linie: pul prohra pri remize
        ev2 = sp["win"] * (o.odds - 1) + sp["half_win"] * (o.odds - 1) / 2 - sp["half_loss"] * 0.5 - sp["loss"]
        assert ev2 == pytest.approx(ev, abs=2e-3) and s["ev"] == pytest.approx(ev, abs=1e-4)
        lo, hi = s["p_range"]
        assert lo < hi


def test_profile3_lobo_excludes_evaluated_market_and_seed_does_not_change_pick():
    entries = [profile2.OddsEntry("m", "1X2", (1.80, 3.70, 4.60)), profile2.OddsEntry("m", "OU_2.5", (1.85, 2.00)),
               profile2.OddsEntry("m", "BTTS", (1.80, 2.00)), profile2.OddsEntry("m", "OU_1.5", (1.25, 3.90))]
    cfg = marketsim.SimConfig(min_ev=-1, worst_ev=-1, max_loss=1, max_odds=4)
    pick, _ = marketsim.analyse("m1", None, entries, cfg)
    pick2, _ = marketsim.analyse("jine-id-jiny-seed", None, entries, cfg)
    assert (pick.market, pick.selection, pick.ev) == (pick2.market, pick2.selection, pick2.ev)  # seed jen zobrazeni
    lb = marketsim.lobo_market("m1", None, entries, pick, cfg)
    assert pick.market not in lb["markets_used"] and lb["status"] == "OK" and lb["ev_lobo"] is not None
    only = [e for e in entries if e.market == pick.market]
    assert marketsim.lobo_market("m1", None, only, pick, cfg)["status"] == "NEOVĚŘENO"
    st = marketsim.sim_stability(1.4, 1.1, ns=(1000, 10000), seeds=10)
    assert st[1]["sd"] < st[0]["sd"]  # vic simulaci = mensi rozptyl, EV se z nich nepocita


# --- rohy a karty ----------------------------------------------------------------------------------------------
def _pp(profile, market, created, detail=""):
    return paper.Pick(id=market + created, league="ENG1", match_id="m", name="A - B", day="2026-11-21",
                      kickoff="2026-11-21T15:00:00", market=market, selection="over", odds=2.0, book="x", p_model=0.5,
                      source="model", created=created, profile=profile, detail=detail, result="lost", profit=-1.0)


def test_corners_and_cards_separate_and_methods():
    old_cards = _pp("rohy", "KARTY_4.5", "2026-11-21T12:00:00")
    old_corners = _pp("rohy", "ROHY_9.5", "2026-11-21T12:00:00")
    new_cards = _pp("karty", "KARTY_3.5", "2026-11-22T12:00:00", "Šance 60 % = 25% model (65 %) + 75% trh Pinnacle (58 %).")
    picks = [old_cards, old_corners, new_cards]
    assert paper.of_profile(picks, "karty") == [old_cards, new_cards] and paper.of_profile(picks, "rohy") == [old_corners]
    assert paper.props_method(old_cards) == "jen model" and paper.props_method(new_cards) == "25/75"
    assert paper.props_method(_pp("rohy", "KARTY_4.5", "2026-11-21T22:30:00")) == "25/75"  # po zmene metodiky
    assert old_cards.profile == "rohy"  # historicky zaznam se neprepisuje
    assert paper.profile("rohy").markets == ("ROHY_",) and paper.profile("karty").markets == ("KARTY_",)


def test_props_profiles_pick_independently(led):
    from tipsbot.czechodds import Entry

    ev = Event("x:2", "ENG1", "Arsenal", "Chelsea", KO, {
        "ROHY_9.5": MarketOdds("ROHY_9.5", ("over", "under"), {"pinnacle": {"over": 1.90, "under": 1.95}}),
        "KARTY_4.5": MarketOdds("KARTY_4.5", ("over", "under"), {"pinnacle": {"over": 2.00, "under": 1.85}})})
    entries = [Entry("zahraničí (medián)", "Arsenal", "Chelsea", "ROHY_9.5", (2.05, 1.80), NOW),
               Entry("zahraničí (medián)", "Arsenal", "Chelsea", "KARTY_4.5", (2.20, 1.70), NOW)]
    model = {"ROHY_9.5:over": 0.70, "ROHY_9.5:under": 0.30, "KARTY_4.5:over": 0.80, "KARTY_4.5:under": 0.20}
    paper.track(led, "ENG1", [(ev, model, entries)], NOW)
    got = {(p.profile, p.market) for p in paper.load(led)}
    assert ("rohy", "ROHY_9.5") in got and ("karty", "KARTY_4.5") in got
    p = next(x for x in paper.load(led) if x.profile == "karty")
    assert p.meta["method"] == "25/75" and p.meta["p_market"] > 0


# --- kombi -----------------------------------------------------------------------------------------------------
def _combo(target=True, offers=None):
    from tipsbot.analysis import Row
    from tipsbot.combo import Combo, Leg

    def row(m, odds):
        return Row("1X2", "1", f"Výhra {m}", 0.6, 0.6, 0.6, 1.66, odds, "cíl – ověř u kanceláře" if target else "pinnacle",
                   0.0, "value", offers=offers or [])
    legs = (Leg("A - B", KO.isoformat(), row("A", 1.8)), Leg("C - D", (KO + timedelta(hours=1)).isoformat(), row("C", 1.7)))
    return Combo(legs, 1.8 * 1.7, 0.36, 10.0, 0.0, 2.9)


def test_kombi_target_legs_are_hypothetical_and_not_mixed(led):
    kombi.record(led, [_combo(target=True)], NOW)
    (hyp,) = paper.load(led)
    assert kombi.price_class(hyp) == kombi.HYPO and hyp.legs[0]["price_source"] == kombi.SRC_TARGET
    assert hyp.legs[0]["target_odds"] == 1.8 and hyp.legs[0]["verified_odds"] is None
    hyp.result, hyp.profit = "won", 2.06
    v = kombi.verification([hyp])
    assert v["fully_verified"] == 0 and v["with_unverified_leg"] == 1 and v["unique_legs"] == 2
    assert v["verified"]["roi"] is None and v["hypothetical"]["roi"] == pytest.approx(2.06)  # nesmichano
    # starsi noha bez overeni: cilova = hypoteticka, necilova = kurz kancelare z API pri navrhu
    assert kombi.leg_price({"target": True, "odds": 1.5}) == kombi.HYPO
    # Faze 3.2.2 (prisne): noha bez dolozeneho kurzu neni overena, i kdyz neni cilova
    assert kombi.leg_price({"target": False, "odds": 1.5}) == kombi.UNDOCUMENTED


def test_kombi_verified_prices_and_change(led):
    from tipsbot.analysis import Row  # noqa: F401

    kombi.record(led, [_combo(target=False, offers=[["pinnacle", 1.85]])], NOW)
    (p,) = paper.load(led)
    x = p.legs[0]
    assert x["verified_odds"] == 1.85 and x["verified_book"] == "pinnacle" and x["available"] is True
    assert kombi.price_class(p) == kombi.VERIFIED and x["first_odds"] == x["odds"]
    x["first_odds"] = 1.9  # kurz se pred vykopem zmenil
    assert kombi.price_class(p) == kombi.CHANGED
    p.result, p.profit = "won", 1.0
    v = kombi.verification([p])
    assert v["verified"]["roi"] == pytest.approx(1.0) and v["hypothetical"]["settled"] == 0


# --- jednotne metriky: PUSH != INVALID, vracene mimo jmenovatel ---------------------------------------------------
def test_push_not_in_roi_denominator_and_not_invalid():
    ps = [_pp("zaklad", "OU_2.5", "2026-11-22T10:00:00"), _pp("zaklad", "OU_3.0", "2026-11-22T10:00:00")]
    ps[0].result, ps[0].profit = "won", 1.0
    ps[1].result, ps[1].profit = "void", 0.0
    st = paper.stats(ps)
    assert st["roi"] == pytest.approx(1.0) and st["push"] == 1 and st["staked"] == 1
    from tipsbot.export import INVALID, PUSH, Rec

    assert Rec("g", "k", "fotbal", "", "", "", "", "", 2, "", None, "void").outcome == PUSH
    assert Rec("g", "k", "fotbal", "", "", "", "", "", 2, "", None, "invalid").outcome == INVALID


# --- Profil 4 podle sportu ---------------------------------------------------------------------------------------
def test_profil4_groups_by_sport():
    from tipsbot.profil4 import history
    from tipsbot.profil4.store import Store, iso

    st = Store()
    for fid, lg, res, profit in ((1, "Premier League", "won", 1.0), (2, "NHL", "lost", -1.0)):
        st.execute("INSERT INTO tips(fixture_id, match, league, kickoff, market, selection, probs, odds, bookmaker, "
                   "odds_source, decision, predicted_at, model_version, config_hash, result, profit) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (fid, "A - B", lg, iso(KO), "1X2", "1", "{}", 2.0, "p", "api", "APPROVED BET", iso(NOW), "v", "h", res, profit))
    ev = history.evaluate(st)
    assert set(ev["groups"]["sport"]) == {"fotbal", "NHL"}
    assert ev["groups"]["sport · rozhodnutí"]["NHL · APPROVED BET"]["profit"] == -1.0


# --- Profil 5 ----------------------------------------------------------------------------------------------------
def _p5_books():
    b = _books()
    b["betvictor"] = {"1": 2.10, "X": 3.30, "2": 3.70}  # o ~6 % nad konsenzem, pod hranici extremu
    return b


def test_p5_candidate_lobo_and_shadow_mode(led):
    books = _p5_books()
    ev = _event(books)
    stamps = {"oddsapi:e1": {b: {"h2h": NOW - timedelta(minutes=20)} for b in books}}
    led.deposit(1000)
    before = (led.bankroll(), len(paper.load(led)), led.s.query(Tip).count())
    out = profil5.scan(led, "ENG1", [ev], NOW, stamps)
    (c,) = profil5.rows(led.s)
    assert out["nový kandidát"] == 1 and (c.book, c.selection, c.odds) == ("betvictor", "1", 2.10)
    import json

    d = json.loads(c.data)
    assert "betvictor" not in d["included"] and d["excluded"]["betvictor"] == "hodnocená kancelář"
    assert c.n_ref >= 3 and c.edge == pytest.approx(c.odds * c.p_ref - 1) and c.fair_odds == pytest.approx(1 / c.p_ref)
    assert c.dev_median == pytest.approx(2.10 / np.median([books[b]["1"] for b in d["included"]]) - 1)
    assert c.best_odds == 2.10 and c.median_odds is not None  # line shopping zvlast
    assert (led.bankroll(), len(paper.load(led)), led.s.query(Tip).count()) == before  # stinovy rezim nic nemeni
    assert profil5.validate(led.s) == []
    # opakovany sken: zadny duplikat, vstupy kandidata se nemeni, dopise se jen zaviraci cena
    books2 = {**books, "betvictor": {"1": 1.99, "X": 3.40, "2": 3.80}}
    profil5.scan(led, "ENG1", [_event(books2)], NOW + timedelta(hours=1), stamps)
    (c2,) = profil5.rows(led.s)
    assert c2.odds == 2.10 and c2.closing_p is not None and c2.clv == pytest.approx(2.10 * c2.closing_p - 1)
    assert c2.closing_kind == "poslední pozorovaný kurz"  # ne 20 min pred vykopem -> neni overene CLV


def test_p5_requires_independent_books_fresh_odds_and_rejects_extremes(led):
    few = {k: v for k, v in _p5_books().items() if k in ("pinnacle", "unibet_se", "unibet_nl", "betvictor")}
    stamps = {"oddsapi:e1": {b: {"h2h": NOW} for b in few}}
    profil5.scan(led, "ENG1", [_event(few)], NOW, stamps)
    assert profil5.rows(led.s, None) == []  # jen 2 nezavisle skupiny mimo hodnocenou -> zadny kandidat
    books = _books(betvictor={"1": 2.60, "X": 3.30, "2": 3.70})  # extrem
    out = profil5.scan(led, "ENG1", [_event(books, mid="oddsapi:e2")], NOW, {})
    rej = profil5.rows(led.s, profil5.REJECTED)
    assert rej and not profil5.rows(led.s) and any("odmítnuto" in k for k in out)
    reasons = " ".join(json.loads(r.reasons)[0] for r in rej for json in [__import__("json")])
    assert "nad mediánem" in reasons or "stáří" in reasons


def test_p5_settle_and_report(led):
    books = _p5_books()
    stamps = {"oddsapi:e1": {b: {"h2h": NOW} for b in books}}
    profil5.scan(led, "ENG1", [_event(books)], NOW, stamps)
    res = pd.DataFrame([{"date": KO.date(), "home": "Arsenal", "away": "Chelsea", "home_goals": 2, "away_goals": 0}])
    profil5.settle(led, res)
    (c,) = profil5.rows(led.s)
    assert c.result == "won" and c.profit == pytest.approx(1.10)
    rep = profil5.report(led.s)
    assert rep["candidates"] == 1 and rep["all"]["yield"] == pytest.approx(1.10)
    ls = rep["line_shopping"]
    assert ls["at_book"]["yield"] == pytest.approx(1.10) and ls["at_median"]["yield"] < 1.10  # zvlast, nescita se


# --- export ------------------------------------------------------------------------------------------------------
def test_export_has_new_sheets_and_keeps_old(tmp_path, led):
    import io

    import openpyxl

    from tipsbot import export

    books = _p5_books()
    profil5.scan(led, "ENG1", [_event(books)], NOW, {"oddsapi:e1": {b: {"h2h": NOW} for b in books}})
    kombi.record(led, [_combo(target=True)], NOW)
    led.s.commit()
    d = export.collect(led.s, None, NOW)
    wb = openpyxl.load_workbook(io.BytesIO(export.to_xlsx(d)))
    names = wb.sheetnames
    for old in ("Souhrn", "Všechny tipy", "Kombi tikety", "Market Integrity", "Settlement Integrity", "Metodika"):
        assert old in names
    for new in ("Strategy Comparison", "Market Integrity – All Profiles", "Profile 5 Market Value",
                "Profile 5 Line Shopping", "Corners Performance", "Cards Performance", "Combo Odds Verification"):
        assert new in names
    ws = wb["Profile 5 Market Value"]
    vals = [c.value for row in ws.iter_rows() for c in row]
    assert "betvictor" in vals
    # prazdna hodnota zustava prazdna, ne 0 (kandidat bez vysledku)
    head = next(r for r in ws.iter_rows(values_only=True) if r and r[0] == "ID")
    row = next(r for r in ws.iter_rows(values_only=True) if r and str(r[0]).startswith("p5"))
    assert row[head.index("Zisk (j.)")] is None and row[head.index("CLV")] is None
    assert any(str(r[0]).startswith("Kombi tikety – hypotetické") for r in wb["Souhrn"].iter_rows(values_only=True) if r[0])


def test_panel_pages_render(tmp_path):
    from tipsbot.web import create_app

    url = f"sqlite:///{tmp_path / 'w.db'}"
    with make_session_factory(url)() as s:
        led = Ledger(s)
        books = _p5_books()
        profil5.scan(led, "ENG1", [_event(books, ko=utcnow() + timedelta(hours=6))], utcnow(),
                     {"oddsapi:e1": {b: {"h2h": utcnow()} for b in books}})
        s.commit()
    c = create_app(url).test_client()
    html = c.get("/?tab=sledovani&profile=profil5").get_data(as_text=True)
    assert "SHADOW MODE" in html and "betvictor" in html and "Line Shopping" in html
    html = c.get("/?tab=sledovani&cmp=1").get_data(as_text=True)
    assert "Srovnání strategií" in html and "Profil 5 – Market Value" in html
    for prof in ("rohy", "karty", "kombi", "zaklad"):
        assert c.get(f"/?tab=sledovani&profile={prof}").status_code == 200

