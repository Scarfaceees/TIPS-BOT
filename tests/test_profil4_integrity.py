"""Audit Profilu 4: mapovani kurzu z API, nezavisly prepocet EV a kontrolni vrstva pred APPROVED BET.

Kurzy pochazeji ze ZAZNAMU skutecne odpovedi API-Football (tests/fixtures/apifootball_odds_20261120.json, zkraceno,
hodnoty beze zmeny). Testy nic nestahuji – nejde o zive overeni, ale o presne reprodukovana data.
"""

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from tipsbot import profile2
from tipsbot.models import football
from tipsbot.profil4 import analysis, integrity as IN, mathcore, models as M, odds as O
from tipsbot.profil4.config import P4
from tipsbot.profil4.decision import APPROVED, NO_BET, decide
from tipsbot.profil4.provider import Fixture
from tipsbot.profil4.store import Store

RAW = json.loads((Path(__file__).parent / "fixtures" / "apifootball_odds_20261120.json").read_text(encoding="utf-8"))
LEV, BAY = 1575222, 1575221
UPD = datetime(2026, 11, 20, 13, 17, 22, tzinfo=timezone.utc)
NOW = UPD + timedelta(hours=1)  # kurzy jsou v case testu aktualni


def quotes(fid: int) -> list[O.OddsQuote]:
    item = next(i for i in RAW["response"] if i["fixture"]["id"] == fid)
    return O.parse_api_odds([item], NOW, {fid: str(fid)})


def q_of(qs, book, market, sel):
    return next(q for q in qs if q.bookmaker == book and q.market == market and q.selection == sel)


def fixture(fid: int) -> Fixture:
    item = next(i for i in RAW["response"] if i["fixture"]["id"] == fid)
    ko = datetime.fromisoformat(item["fixture"]["date"].replace("Z", "+00:00"))
    return Fixture(fid, 78, "Bundesliga", 2026, ko, "NS", 1, "Domácí", 2, "Hosté")


def candidates(qs: list[O.OddsQuote], fid: int):
    """Kandidati presne jako v hloubkove analyze; model A = trh B (testuje se mapovani a kontroly, ne model)."""
    b = M.model_b([q for q in qs if q.status(NOW, P4.odds) == O.CURRENT], P4)
    c = M.model_c(b, b, 0.1, "test")
    ms = M.ModelSet(b, b, c, 25.0, b.matrix, b.matrix, 0.1)
    return analysis.build_candidates(fixture(fid), ms, qs, NOW, P4, 0.9, "confirmed")


# --- 1. mapovani trhu z API --------------------------------------------------

def test_api_asian_handicap_number_is_home_line_for_both_sides():
    qs = quotes(LEV)
    # Pinnacle: "Home -1.5" 1.70 / "Away -1.5" 2.22 je JEDEN trh (domaci -1.5 / hoste +1.5)
    home, away = q_of(qs, "pinnacle", "AH_-1.5", "home"), q_of(qs, "pinnacle", "AH_-1.5", "away")
    assert (home.odds, away.odds) == (1.70, 2.22)
    assert profile2.label("AH_-1.5", "away") == "hosté handicap +1,5"
    assert 1.0 < 1 / home.odds + 1 / away.odds < 1.06
    # Betfair "Away +1.5" = 13.00 je hoste -1.5 (Paderborn vyhraje o 2+), NE hoste +1.5
    assert q_of(qs, "betfair", "AH_1.5", "away").odds == 13.0
    assert q_of(qs, "betfair", "AH_-1.5", "away").odds == 2.10
    # "Away +0.5" = vyhra hostu => kurz odpovida 1X2 "Away"
    b365 = [q for q in qs if q.bookmaker == "bet365"]
    assert abs(q_of(b365, "bet365", "AH_0.5", "away").odds - q_of(b365, "bet365", "1X2", "2").odds) < 0.01


@pytest.mark.parametrize("line", [-1.75, -1.5, -0.75, 0.0, 1.5, 1.75])
def test_every_api_ah_pair_is_one_market(line):
    """Kazda dvojice Home/Away se stejnym cislem ma rozumnou marzi (0–12 %) – zadna zamena stran (ta da soucet < 100 %)."""
    qs = quotes(LEV)
    key = profile2._canon("AH", line)
    for book in {q.bookmaker for q in qs}:
        per = {q.selection: q.odds for q in qs if q.bookmaker == book and q.market == key}
        if len(per) == 2:
            assert 0.0 <= 1 / per["home"] + 1 / per["away"] - 1 < 0.12, (book, key, per)


def test_ah_quarter_lines_175_and_15_settle_correctly():
    # domaci -1.75 = pul na -1.5 a pul na -2.0
    assert mathcore.settle("AH_-1.75", "home", 2, 0) == mathcore.HALF_WON  # -1.5 vyhra, -2 vraceni
    assert mathcore.settle("AH_-1.75", "home", 3, 0) == mathcore.WON
    assert mathcore.settle("AH_-1.75", "home", 1, 0) == mathcore.LOST
    assert mathcore.settle("AH_-1.75", "away", 2, 0) == mathcore.HALF_LOST  # hoste +1.75
    assert mathcore.settle("AH_1.75", "away", 0, 2) == mathcore.HALF_WON  # hoste -1.75
    assert mathcore.settle("AH_-1.5", "away", 1, 0) == mathcore.WON  # hoste +1.5
    assert mathcore.settle("AH_1.5", "away", 0, 1) == mathcore.LOST  # hoste -1.5
    assert mathcore.settle("AH_1.5", "away", 0, 2) == mathcore.WON


def test_over_under_25_and_team_totals_map_and_settle():
    qs = quotes(BAY)
    assert q_of(qs, "pinnacle", "OU_3.5", "over").odds == 1.35 and q_of(qs, "pinnacle", "OU_3.5", "under").odds == 3.11
    assert q_of(qs, "pinnacle", "TTA_1.5", "over").odds == 4.30  # "Total - Away" = goly hostu, ne zapasu
    assert mathcore.settle("OU_2.5", "over", 2, 1) == mathcore.WON and mathcore.settle("OU_2.5", "under", 1, 1) == mathcore.WON
    assert mathcore.settle("TTA_1.0", "over", 5, 1) == mathcore.PUSH and mathcore.settle("TTA_1.0", "over", 0, 2) == mathcore.WON


def test_duplicate_selection_in_api_response():
    item = json.loads(json.dumps(next(i for i in RAW["response"] if i["fixture"]["id"] == LEV)))
    pin = next(b for b in item["bookmakers"] if b["name"] == "Pinnacle")
    mw = next(b for b in pin["bets"] if b["name"] == "Match Winner")
    mw["values"].append({"value": "Home", "odd": mw["values"][0]["odd"]})  # presny duplikat = jeden kurz
    qs = O.parse_api_odds([item], NOW)
    assert len([q for q in qs if q.bookmaker == "pinnacle" and q.key == "1X2:1"]) == 1
    mw["values"].append({"value": "Draw", "odd": "9.99"})  # stejny vyber s jinym kurzem = nejednoznacne
    qs = O.parse_api_odds([item], NOW)
    assert not [q for q in qs if q.bookmaker == "pinnacle" and q.key == "1X2:X"]


# --- 2. matematika: nezavisly prepocet --------------------------------------

def test_ev_and_fair_odds_formulas_use_probability_0_to_1():
    assert mathcore.expected_value(0.283, 4.32) == pytest.approx(0.283 * 4.32 - 1)
    assert mathcore.fair_odds(0.25) == 4.0
    assert math.isinf(mathcore.fair_odds(0.0)) and math.isinf(mathcore.fair_odds(28.3))  # 0–100 neni pravdepodobnost


@pytest.mark.parametrize("market,sels", [("1X2", ("1", "X", "2")), ("DC", ("1X", "12", "X2")), ("DNB", ("home", "away")),
                                         ("BTTS", ("yes", "no")), ("OU_2.5", ("over", "under")), ("OU_3.25", ("over", "under")),
                                         ("OU_2.0", ("over", "under")), ("TTA_1.0", ("over", "under")),
                                         ("TTH_1.75", ("over", "under")), ("AH_-1.75", ("home", "away")),
                                         ("AH_-1.5", ("home", "away")), ("AH_1.5", ("home", "away")),
                                         ("AH_1.75", ("home", "away")), ("AH_-0.25", ("home", "away")), ("AH_0.0", ("home", "away"))])
def test_independent_implementation_matches_engine(market, sels):
    mat = football.score_matrix(1.7, 1.1, -0.05, profile2.MAX_GOALS)
    for s in sels:
        e = mathcore.evaluate(market, s, mat, 2.37)
        ind = IN.recompute(market, s, mat, 2.37)
        assert ind["ev"] == pytest.approx(e.ev, abs=1e-12) and ind["fair"] == pytest.approx(e.fair_odds, rel=1e-12)
        assert ind["a"] == pytest.approx(e.p_win + 0.5 * e.p_half_win, abs=1e-12)
        for h in range(5):
            for a in range(5):  # settlement skore po skore
                parts = IN.outcome(market, s, h, a)
                won = sum(w for w, r in parts if r == "win")
                assert {1.0: mathcore.WON, 0.5: mathcore.HALF_WON}.get(won, None) in (mathcore.settle(market, s, h, a), None)


def _cand(qs, fid, market, sel, book=None):
    """Kandidat vznika z nejlepsiho kurzu na vyber; `book` overi, ze je to kurz te kancelare."""
    c = next(c for c in candidates(qs, fid) if c.market == market and c.selection == sel)
    assert book is None or c.quote.bookmaker == book, c.quote.bookmaker
    return c


def test_validation_flags_invalid_probabilities_and_mismatched_ev():
    qs = quotes(LEV)
    mat = M.model_b(qs, P4).matrix
    c = _cand(qs, LEV, "1X2", "X", "pinnacle")
    assert IN.validate(c, mat) == ([], [])
    for bad in (0.0, float("nan"), 1.5, 28.3):  # nula, NaN, nad 1 a procenta misto pravdepodobnosti
        hard, _ = IN.validate(replace(c, p={**c.p, "A": bad}), mat)
        assert any("není v intervalu" in h for h in hard), bad
    hard, _ = IN.validate(replace(c, ev_c=c.p["C"] * 100 * c.odds - 1), mat)  # EV z procent
    assert any("EV nesouhlasí" in h for h in hard)
    hard, _ = IN.validate(replace(c, fair_odds_c=c.odds), mat)  # ferovy kurz = kurz kancelare
    assert any("férový kurz nesouhlasí" in h for h in hard)


def test_validation_flags_wrong_market_or_fixture_identity():
    qs = quotes(LEV)
    mat = M.model_b(qs, P4).matrix
    c = _cand(qs, LEV, "OU_2.5", "over")
    other = q_of(qs, "pinnacle", "OU_3.5", "over")
    assert any("kurz je pro OU_3.5:over" in h for h in IN.validate(replace(c, quote=other), mat)[0])
    assert any("jinému zápasu" in h for h in IN.validate(replace(c, quote=replace(c.quote, fixture_id=1)), mat)[0])


# --- 3. kontrolni vrstva na skutecnych datech --------------------------------

def test_real_leverkusen_odds_give_no_absurd_ev():
    cands = candidates(quotes(LEV), LEV)
    assert cands and all(c.validation is not None and not c.validation[0] for c in cands)
    assert max(c.ev_c for c in cands) < 0.25  # drive AH +1.5 @ 13.00 s EV +495 %
    lone = _cand(quotes(LEV), LEV, "AH_1.5", "away", "betfair")  # skutecny vyznam kurzu 13.00: hoste -1.5
    assert lone.ev_c < 0 and lone.decision == NO_BET
    paderborn = _cand(quotes(LEV), LEV, "AH_-1.5", "away", "pinnacle")
    assert abs(paderborn.ev_c) < 0.05 and paderborn.label == "hosté handicap +1,5"


def test_old_wrong_ah_mapping_is_caught_by_pairing_check():
    """Kdyby se znamenko u 'Away' znovu otocilo, kurz 13.00 by se sparoval s 1.67 (soucet 67.6 %) => NO BET."""
    flipped = [replace(q, market=profile2._canon("AH", -float(q.market[3:])))
               if q.market.startswith("AH_") and q.selection == "away" else q for q in quotes(LEV)]
    c = _cand(flipped, LEV, "AH_-1.5", "away", "betfair")
    assert c.odds == 13.0 and c.decision == NO_BET
    assert any("nejsou jeden trh" in r for r in c.reasons)


def test_extreme_price_outlier_is_not_approved_without_confirmation():
    """1xBet 'Total - Away Over 1' = 4.15, ostatni trh ~3.1: o ~30 % nad ferovou cenou, nikdo jiny to nenabizi."""
    c = _cand(quotes(BAY), BAY, "TTA_1.0", "over", "1xbet")
    assert c.market_covered and c.ev_market > P4.decision.max_market_edge and not c.confirmed_by
    assert c.decision != APPROVED and any("nebyla nezávisle potvrzena" in r for r in c.reasons)


def test_stale_quotes_never_confirm_and_never_approve():
    qs = quotes(LEV)
    later = NOW + timedelta(hours=8)
    assert all(q.status(later, P4.odds) == O.STALE for q in qs)
    q = q_of(qs, "pinnacle", "1X2", "X")
    assert IN.confirmations(q, qs, later, P4.odds, 0.05) == []


def test_goal_market_without_current_goal_line_has_no_market_price():
    """Real–Celta: golove linie byly 10 h stare => model B jen dopocital goly z 1X2; to neni cena trhu."""
    b = M.model_b([q for q in quotes(LEV) if q.market == "1X2"], P4)
    assert "nejsou tržní" in b.note
    assert IN.market_price(b, "OU_2.5", "under", 3.2)[0] is None
    assert IN.market_price(b, "1X2", "X", 7.21)[0] is not None


def test_market_model_reproduces_its_1x2_input_and_ignores_model_a():
    """Schalke–Stuttgart (Pinnacle 4.15 / 4.17 / 1.81): trh B musi dat remizu ~23 %, ne 28 %."""
    mk = lambda s, o: O.OddsQuote(1575228, "S - S", "pinnacle", "1X2", s, o, NOW, "api", NOW + timedelta(hours=4), UPD)
    b = M.model_b([mk("1", 4.15), mk("X", 4.17), mk("2", 1.81)], P4)
    fair = b.extra["table"]["consensus"]
    for s in ("1", "X", "2"):
        assert abs(mathcore.evaluate("1X2", s, b.matrix).p_win - fair[f"1X2:{s}"]) < 0.005
    assert fair["1X2:X"] == pytest.approx(0.2324, abs=0.001)


# --- 4. migrace spatne namapovanych dat -------------------------------------

def test_store_migration_removes_wrongly_mapped_away_ah(tmp_path):
    path = tmp_path / "p4.db"
    st = Store(path)
    st.execute("DELETE FROM kv WHERE key='fix_ah_away_sign_v1'")
    st.execute("INSERT INTO odds_quotes(fixture_id, match, bookmaker, market, selection, odds, fetched_at, source) "
               "VALUES(1, 'A - B', 'betfair', 'AH_-1.5', 'away', 13.0, '2026-11-20T15:00:00+00:00', 'api')")
    st.execute("INSERT INTO odds_quotes(fixture_id, match, bookmaker, market, selection, odds, fetched_at, source) "
               "VALUES(1, 'A - B', 'tipsport', 'AH_-1.5', 'away', 2.1, '2026-11-20T15:00:00+00:00', 'manual')")
    st.execute("INSERT INTO tips(fixture_id, match, market, selection, probs, odds, bookmaker, odds_source, decision, "
               "predicted_at, model_version, config_hash) VALUES(1, 'A - B', 'AH_-1.5', 'away', '{}', 13.0, 'betfair', "
               "'api', 'BEST AVAILABLE (NO BET)', '2026-11-20T15:00:00+00:00', 'p4', 'x')")
    st.close()
    st = Store(path)
    rows = st.query("SELECT source FROM odds_quotes")
    assert [r["source"] for r in rows] == ["manual"]  # rucni kurz (zapis Profilu 2) zustava
    assert st.query("SELECT result, profit FROM tips")[0]["result"] == "void"
    st.close()


# --- 5. seznam tipu = detail tipu ---------------------------------------------

def test_scan_list_matches_match_detail():
    import test_profil4_flow as t
    from tipsbot.profil4 import hunter, view

    prov, _, _, _ = t.make()
    rep = hunter.run_scan(prov, "upcoming", t.NOW, t.P4, hours=24, simulations=2_000, record=False)
    detail = {(c.fixture_id, c.key, c.quote.bookmaker): c for an in rep.analyses for c in an.candidates}
    shown = rep.overall.approved + rep.overall.watch + [c for c in rep.overall.categories.values() if c]
    assert shown
    for c in shown:
        d = detail[(c.fixture_id, c.key, c.quote.bookmaker)]
        assert (c.match, c.odds, c.ev_c, c.decision, c.p, c.quote.stamp) == (d.match, d.odds, d.ev_c, d.decision, d.p, d.quote.stamp)
        assert view.candidate(c, t.NOW)["ev_c"] == view.candidate(d, t.NOW)["ev_c"]
