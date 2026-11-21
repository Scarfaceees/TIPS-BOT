from datetime import timedelta

from p4_fakes import NOW
from tipsbot.profil4 import decision as D
from tipsbot.profil4.config import DecisionConfig
from tipsbot.profil4.odds import CURRENT, STALE, OddsQuote

CFG = DecisionConfig()


def cand(odds=2.0, ev_c=0.06, ev_d=0.02, rob=75, dq=0.85, pa=0.55, pb=0.52, status=CURRENT, complete=True,
         lineups="confirmed", key=("1X2", "1"), validation=([], []), covered=True, confirmed=("bet365 2.00",)):
    q = OddsQuote(1, "A - B", "pinnacle", key[0], key[1], odds, NOW, "api", NOW + timedelta(hours=3), NOW)
    c = D.Candidate(1, "A - B", key[0], key[1], "test", q, status, {"A": pa, "B": pb, "C": 0.53, "D": 0.51},
                    0.53, 0.0, 1.89, 1.96, ev_c, ev_d, 1.95, rob, dq, complete, lineups)
    c.validation, c.market_covered, c.confirmed_by = validation, covered, list(confirmed)
    if covered:
        c.p_market, c.ev_market, c.market_src = pb, pb * odds - 1, "test"
    return c


def test_safety_layer_blocks_unverified_or_inconsistent_tips():
    assert D.decide(cand(validation=None), CFG).decision == D.WATCH  # bez nezavisleho overeni nikdy APPROVED
    bad = D.decide(cand(validation=(["EV nesouhlasí s nezávislým výpočtem"], [])), CFG)
    assert bad.decision == D.NO_BET and "EV nesouhlasí" in " ".join(bad.reasons)
    assert D.decide(cand(validation=([], ["nenabízí protistranu"])), CFG).decision == D.WATCH
    assert D.decide(cand(covered=False), CFG).decision == D.WATCH  # cena trhu neznama
    # outsider: model A 30 % vs trh 18 % (rozdil jen 12 p.b., ale pomer sanci 1.95x)
    assert "rozcházejí" in " ".join(D.decide(cand(odds=6.0, pa=0.30, pb=0.18, ev_c=0.08), CFG).reasons)


def test_extreme_edge_needs_independent_confirmation():
    # kurz 2.60 na vyber s ferovou cenou trhu 52 % = o 35 % nad trhem
    lone = D.decide(cand(odds=2.60, ev_c=0.20, pb=0.52, pa=0.53, confirmed=()), CFG)
    assert lone.decision != D.APPROVED and "nebyla nezávisle potvrzena" in " ".join(lone.reasons)
    # stejna situace, ale jina kancelar nabizi podobny kurz: hodnota je potvrzena (EV 20 % stale nad 'podezrele' ne)
    ok = D.decide(cand(odds=2.60, ev_c=0.20, pb=0.52, pa=0.53, confirmed=("bet365 2.55",)), CFG)
    assert "nebyla nezávisle potvrzena" not in " ".join(ok.reasons)
    # normalni hodnota (kurz 4 % nad trhem) potvrzeni nepotrebuje
    assert D.decide(cand(confirmed=()), CFG).decision == D.APPROVED


def test_approved_when_all_conditions_met():
    c = D.decide(cand(), CFG)
    assert c.decision == D.APPROVED and c.reasons


def test_watch_for_soft_failures():
    assert D.decide(cand(status=STALE), CFG).decision == D.WATCH
    assert D.decide(cand(complete=False), CFG).decision == D.WATCH
    assert D.decide(cand(ev_c=0.01), CFG).decision == D.WATCH  # kladne, ale pod prahem
    assert D.decide(cand(dq=0.4), CFG).decision == D.WATCH
    assert D.decide(cand(ev_c=0.40), CFG).decision == D.WATCH  # podezrele vysoke EV
    assert D.decide(cand(rob=50), CFG).decision == D.WATCH


def test_no_bet_for_hard_failures():
    assert D.decide(cand(ev_c=-0.02), CFG).decision == D.NO_BET
    assert D.decide(cand(odds=7.5), CFG).decision == D.NO_BET
    assert D.decide(cand(ev_d=-0.12), CFG).decision == D.NO_BET
    assert D.decide(cand(pa=0.70, pb=0.40), CFG).decision == D.NO_BET
    assert D.decide(cand(rob=20), CFG).decision == D.NO_BET


def test_unconfirmed_lineups_is_a_risk_not_a_block():
    c = D.decide(cand(lineups="pending"), CFG)
    assert c.decision == D.APPROVED and "sestavy nejsou potvrzené" in c.risks


def test_categories_and_best_available():
    a = D.decide(cand(odds=1.40, ev_c=0.035, ev_d=0.01, rob=85, key=("DC", "1X")), CFG)
    b = D.decide(cand(odds=2.10, ev_c=0.07, ev_d=0.02, rob=70, key=("1X2", "1")), CFG)
    c = D.decide(cand(odds=3.80, ev_c=0.12, ev_d=-0.02, rob=55, key=("1X2", "2")), CFG)
    sel = D.select([a, b, c], CFG)
    assert sel.verdict == D.APPROVED and sel.best_available is None
    assert sel.categories[D.CAT_B] is b and sel.categories[D.CAT_C] is c
    assert sel.categories[D.CAT_A] in (a, b)


def test_best_available_never_approved():
    w = D.decide(cand(ev_c=0.015), CFG)
    n = D.decide(cand(ev_c=-0.05, key=("1X2", "X")), CFG)
    sel = D.select([w, n], CFG)
    assert sel.verdict == D.WATCH and sel.best_available is w and w.decision != D.APPROVED
    assert sel.categories[D.CAT_B] is None and "NENÍ schválená" in sel.explanation
    assert D.select([], CFG).verdict == D.NO_BET


def test_category_a_needs_real_chance_and_c_never_shows_no_bet():
    # schvaleny handicap @ 4.96 s 17 % vyhry (+24 % vraceni) nesmi byt "nejrozumnejsi tip"
    long = D.decide(cand(odds=4.96, ev_c=0.058, ev_d=0.035, rob=67, pa=0.21, pb=0.21, key=("AH_0.0", "home")), CFG)
    long.p_win_c, long.p_push_c = 0.166, 0.237
    # kurz 13 mimo pasmo s vysokym EV = NO BET; nesmi se ukazat jako C – maximum value
    outsider = D.decide(cand(odds=13.22, ev_c=0.13, ev_d=0.14, rob=79, pa=0.08, pb=0.086, key=("1X2", "2")), CFG)
    assert long.decision == D.APPROVED and outsider.decision == D.NO_BET
    sel = D.select([long, outsider], CFG)
    assert sel.categories[D.CAT_A] is None and sel.categories[D.CAT_C] is long and sel.categories[D.CAT_B] is long
    fav = D.decide(cand(odds=1.80, ev_c=0.04, ev_d=0.01, rob=80), CFG)  # p_win 53 %
    assert D.select([long, outsider, fav], CFG).categories[D.CAT_A] is fav


def test_zero_handicap_label_has_no_minus_zero():
    from tipsbot.profile2 import label

    assert label("AH_0.0", "away") == "hosté handicap ±0,0" and label("AH_-1.5", "away") == "hosté handicap +1,5"
