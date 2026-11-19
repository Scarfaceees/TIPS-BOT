from datetime import timedelta

from p4_fakes import NOW
from tipsbot.profil4 import decision as D
from tipsbot.profil4.config import DecisionConfig
from tipsbot.profil4.odds import CURRENT, STALE, OddsQuote

CFG = DecisionConfig()


def cand(odds=2.0, ev_c=0.06, ev_d=0.02, rob=75, dq=0.85, pa=0.55, pb=0.52, status=CURRENT, complete=True,
         lineups="confirmed", key=("1X2", "1")):
    q = OddsQuote(1, "A - B", "pinnacle", key[0], key[1], odds, NOW, "api", NOW + timedelta(hours=3), NOW)
    return D.Candidate(1, "A - B", key[0], key[1], "test", q, status, {"A": pa, "B": pb, "C": 0.53, "D": 0.51},
                       0.53, 0.0, 1.89, 1.96, ev_c, ev_d, 1.95, rob, dq, complete, lineups)


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
