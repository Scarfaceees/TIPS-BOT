from datetime import date, datetime, timedelta

import numpy as np
import pytest

from tipsbot import backtest, calibration, risk
from tipsbot.cli import main as cli_main
from tipsbot.config import SETTINGS, Tier
from tipsbot.storage import TIP_CANCELLED, TIP_PLACED, Tip
from tipsbot.tips import Candidate, Offer, Rejection, best_per_match, evaluate, find_arbitrage, format_tip, recheck, remainder

NOW = datetime(2026, 11, 14, 12, 0)


# ---------- risk ----------
def test_kelly_and_caps():
    assert risk.kelly_fraction(0.5, 2.1) == pytest.approx(0.05 / 1.1)
    assert risk.kelly_fraction(0.4, 2.0) < 0
    # 1/8 Kelly: 10000 * 0.125 * 0.04545 = 56.8 -> zaokrouhleno dolu na 50
    assert risk.size_stake(0.5, 2.1, 10_000).stake == 50
    # obrovska vyhoda narazi na strop 2 %
    d = risk.size_stake(0.7, 2.5, 10_000)
    assert d.stake == 200 and d.reason == "strop na sazku"


def test_exposure_match_limit_and_pause():
    assert risk.size_stake(0.7, 2.5, 10_000, open_exposure_today=1400).stake == 100
    assert not risk.size_stake(0.7, 2.5, 10_000, open_exposure_today=1500).allowed
    assert not risk.size_stake(0.7, 2.5, 10_000, bets_on_match=1).allowed
    assert not risk.size_stake(0.7, 2.5, 10_000, paused=True).allowed
    assert not risk.size_stake(0.4, 2.0, 10_000).allowed
    assert not risk.size_stake(0.5, 2.1, 1_000).allowed  # pod minimalnim vkladem


def test_stop_loss_and_arb_stakes():
    assert not risk.stop_loss_hit(8_600, 10_000)
    assert risk.stop_loss_hit(8_500, 10_000)
    stakes = risk.arbitrage_stakes([2.1, 2.1], 1000)
    assert stakes == pytest.approx([500, 500])
    assert stakes[0] * 2.1 == pytest.approx(1050)


# ---------- kalibrace ----------
def test_platt_fixes_overconfidence():
    rng = np.random.default_rng(0)
    true_p = rng.uniform(0.2, 0.8, 5000)
    y = (rng.random(5000) < true_p).astype(int)
    logit = np.log(true_p / (1 - true_p)) * 1.8  # model je prilis sebejisty
    over = 1 / (1 + np.exp(-logit))
    scaler = calibration.fit_platt(over, y)
    assert scaler.a < 0.8
    assert calibration.brier_score(scaler.apply(over), y) < calibration.brier_score(over, y)
    assert calibration.fit_platt([0.5] * 10, [0, 1] * 5) == calibration.PlattScaler()


def test_metrics():
    assert calibration.brier_score([1, 0], [1, 0]) == 0
    assert calibration.brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
    assert calibration.max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    assert calibration.sharpe_ratio([0.01, 0.01, 0.01]) == 0.0
    assert calibration.sharpe_ratio([0.02, -0.01, 0.03, 0.0]) > 0


# ---------- evidence ----------
def test_ledger_flow_and_clv(ledger):
    t = ledger.add_ticket(bookmaker="tipsport", odds=2.10, stake=200, match_id="m1", market="1X2",
                          selection="1", match_day=date(2026, 11, 14))
    assert ledger.open_exposure(date(2026, 11, 14)) == 200
    assert ledger.bets_on_match("m1") == 1
    ledger.settle_ticket(t.id, "won", fair_closing_odds=2.00)
    assert ledger.bankroll() == pytest.approx(10_220)
    assert t.clv_pct == pytest.approx(5.0)
    assert ledger.open_exposure(date(2026, 11, 14)) == 0
    with pytest.raises(ValueError):
        ledger.settle_ticket(t.id, "lost")
    s = ledger.summary()
    assert s["win_rate_pct"] == 100 and s["yield_pct"] == pytest.approx(110.0)


def test_stop_loss_pauses_and_resume(ledger):
    for i in range(8):
        t = ledger.add_ticket(bookmaker="fortuna", odds=2.0, stake=200, match_id=f"m{i}", market="1X2",
                              selection="1", match_day=date(2026, 11, 14))
        ledger.settle_ticket(t.id, "lost")
    assert ledger.bankroll() == 8_400
    assert ledger.paused()
    ledger.resume()
    assert not ledger.paused() and ledger.drawdown() == 0


def test_deposit_does_not_hide_drawdown(ledger):
    t = ledger.add_ticket(bookmaker="fortuna", odds=2.0, stake=1000, match_id="m", market="1X2",
                          selection="1", match_day=date(2026, 11, 14))
    ledger.settle_ticket(t.id, "lost")
    before = ledger.peak() - ledger.bankroll()
    ledger.deposit(5000)
    assert ledger.peak() - ledger.bankroll() == pytest.approx(before)


# ---------- tipy ----------
def _cand(**kw):
    base = dict(
        match_id="m1", league="ENG1", match_name="Arsenal - Chelsea", kickoff=NOW + timedelta(minutes=90),
        market="1X2", selection="1", p_model=0.52, p_market=0.50,
        offers=(Offer("tipsport", 2.10), Offer("fortuna", 2.14), Offer("pinnacle", 2.30)),
    )
    base.update(kw)
    return Candidate(**base)


def test_evaluate_issues_tip_from_best_czech_book(ledger):
    tip = evaluate(_cand(), ledger, now=NOW)
    assert isinstance(tip, Tip)
    assert tip.bookmaker == "fortuna" and tip.raw_odds == 2.14  # pinnacle neni kancelar pro sazeni
    assert tip.min_odds <= 2.14 and tip.kelly_stake > 0 and not tip.early
    assert tip.valid_until == tip.kickoff
    ledger.add_tip(tip)
    assert "nesazet pod" in format_tip(tip)
    # druhy tip na stejne utkani uz ne
    assert isinstance(evaluate(_cand(selection="2"), ledger, now=NOW), Rejection)


def test_evaluate_windows_and_thresholds(ledger):
    outside = evaluate(_cand(kickoff=NOW + timedelta(hours=30)), ledger, now=NOW)
    assert isinstance(outside, Rejection) and "mimo sazkove okno" in outside.reasons
    # casny tip potrebuje dvojnasobny prah: EV ~3.5 % nestaci, ~7 % ano
    early_low = evaluate(_cand(kickoff=NOW + timedelta(hours=18), offers=(Offer("tipsport", 2.03),)), ledger, now=NOW)
    assert isinstance(early_low, Rejection)
    early_ok = evaluate(_cand(kickoff=NOW + timedelta(hours=18), offers=(Offer("tipsport", 2.10),)), ledger, now=NOW)
    assert isinstance(early_ok, Tip) and early_ok.early
    # mene likvidni liga ma prah 4 %
    minor = evaluate(_cand(league="CZE1", offers=(Offer("tipsport", 2.03),)), ledger, now=NOW)
    assert isinstance(minor, Rejection)
    assert isinstance(evaluate(_cand(market="CORRECT_SCORE"), ledger, now=NOW), Rejection)
    assert isinstance(evaluate(_cand(offers=(Offer("pinnacle", 2.4),)), ledger, now=NOW), Rejection)


def test_best_per_match(ledger):
    a = evaluate(_cand(), ledger, now=NOW)
    b = evaluate(_cand(market="OU_2.5", selection="over", offers=(Offer("betano", 2.20),)), ledger, now=NOW)
    picked = best_per_match([a, b, Rejection(_cand(), ("x",))])
    assert len(picked) == 1 and picked[0].selection == "over"


def test_recheck_cancels_refreshes_and_expires(ledger):
    tip = ledger.add_tip(evaluate(_cand(), ledger, now=NOW))
    assert recheck(tip, ledger, 2.14, now=NOW + timedelta(minutes=2)).action == "keep"
    r = recheck(tip, ledger, 2.20, now=NOW + timedelta(minutes=4))
    assert r.action == "refresh" and tip.raw_odds == 2.20
    assert recheck(tip, ledger, 2.20, now=NOW + timedelta(minutes=20)).action == "keep"
    r = recheck(tip, ledger, 1.95, now=NOW + timedelta(minutes=22))
    assert r.action == "cancel" and tip.status == TIP_CANCELLED

    tip2 = ledger.add_tip(evaluate(_cand(match_id="m2"), ledger, now=NOW))
    # trh se pohnul proti nam -> hodnota zmizela i pri stejnem kurzu
    assert recheck(tip2, ledger, 2.14, p_market=0.46, p_model=0.50, now=NOW + timedelta(minutes=2)).action == "cancel"
    tip3 = ledger.add_tip(evaluate(_cand(match_id="m3"), ledger, now=NOW))
    assert recheck(tip3, ledger, None, now=NOW + timedelta(minutes=2)).action == "cancel"


def test_remainder_after_bookmaker_limit(ledger):
    tip = ledger.add_tip(evaluate(_cand(p_model=0.56, p_market=0.52), ledger, now=NOW))
    full = tip.kelly_stake
    ledger.add_ticket(tip_id=tip.id, bookmaker="fortuna", odds=2.14, stake=full - 60)
    assert tip.status == TIP_PLACED
    offers = (Offer("fortuna", 2.14), Offer("tipsport", 2.10), Offer("chance", 1.80))
    sug = remainder(tip, ledger, offers)
    assert sug and sug.bookmaker == "tipsport" and sug.stake == 60
    ledger.add_ticket(tip_id=tip.id, bookmaker="tipsport", odds=2.10, stake=60)
    assert remainder(tip, ledger, offers) is None


def test_arbitrage_only_above_three_percent():
    good = find_arbitrage({"over": [Offer("tipsport", 2.12)], "under": [Offer("fortuna", 2.10)]}, 1000)
    assert good and good.yield_pct > 3 and sum(l[3] for l in good.legs) == pytest.approx(1000, abs=0.02)
    assert find_arbitrage({"over": [Offer("tipsport", 2.04)], "under": [Offer("fortuna", 2.02)]}, 1000) is None
    assert find_arbitrage({"over": [Offer("tipsport", 2.2)], "under": [Offer("pinnacle", 2.2)]}, 1000) is None


# ---------- CLI ----------
def test_cli_roundtrip(tmp_path, capsys):
    db = f"sqlite:///{tmp_path / 't.db'}"
    assert cli_main(["--db", db, "deposit", "5000"]) == 0
    assert cli_main(["--db", db, "ticket-add", "--book", "betano", "--odds", "1.9", "--stake", "100",
                     "--match-id", "x1", "--market", "1X2", "--selection", "1", "--day", "2026-11-14"]) == 0
    assert cli_main(["--db", db, "ticket-settle", "1", "won", "--closing", "1.85"]) == 0
    assert cli_main(["--db", db, "status"]) == 0
    out = capsys.readouterr().out
    assert "5090.00" in out and "Prumerne CLV" in out
    assert cli_main(["--db", db, "ticket-settle", "99", "won"]) == 1


# ---------- backtest ----------
def test_walk_forward_and_report(league):
    df, _ = league
    days = sorted(df["date"].unique())
    preds = backtest.walk_forward(df, days[-6], refit_days=30)
    assert len(preds) >= 50
    assert (preds[["p1", "px", "p2"]].sum(axis=1) - 1).abs().max() < 1e-9
    rep = backtest.evaluate(preds)
    assert 0.15 < rep.brier < 0.25 and rep.bets == 0
    # s kurzy se simuluje sazeni
    preds["odds_1"], preds["odds_x"], preds["odds_2"] = 2.3, 3.4, 3.3
    rep2 = backtest.evaluate(preds, tier=Tier.MAJOR)
    assert rep2.matches == rep.matches and "Brier" in rep2.summary()


def test_combos_pairs_from_different_matches():
    from tipsbot import combo
    from tipsbot.analysis import MatchView, Row

    def view(name, odds, p, status="tip"):
        r = Row("1X2", "1", "Vyhra " + name, p, p, p, 1 / p, odds, "x", (p * odds - 1) * 100, status)
        return MatchView(name, "ENG1", name, "2026-11-15T15:00:00", True, [r], 0, "TIP")

    views = [view("A", 1.6, 0.70), view("B", 1.7, 0.66), view("C", 1.65, 0.68), view("D", 2.0, 0.40, "none")]
    legs = combo.candidate_legs(views)
    assert [lg.match for lg in legs] == ["A", "B", "C"]  # D nema hodnotu
    top = combo.best_combos(views, 10000)
    assert top and all(len({lg.match for lg in c.legs}) == len(c.legs) for c in top)
    assert all(len(c.legs) <= 3 for c in top) and top[0].growth == max(c.growth for c in top)
    p = combo.partner_for("A", views, 10000)
    assert p and any(lg.match == "A" for lg in p.legs) and len(p.legs) == 2


def test_target_tip_and_combos_without_verified_value():
    from tipsbot import analysis, combo

    probs = {"OU_5.5:over": 0.63, "OU_7.5:under": 0.665, "BTTS:yes": 0.55, "1X2:1": 0.30}
    rows = analysis.model_rows(probs, set(), "A", "B", 0.025)
    view = analysis.MatchView("m1", "NHL", "A - B", "2026-11-15T15:00:00", True, [analysis.Row("MONEYLINE", "home", "x", .5, .5, .5, 2, 2.0, "b", -5, "none")] + rows, 0, "")
    view.target = max((i for i in range(1, len(view.rows)) if .58 <= view.rows[i].p_model <= .72), key=lambda i: view.rows[i].p_model)
    assert view.rows[view.target].selection == "under" and view.rows[view.target].market == "OU_7.5"
    v2 = analysis.MatchView("m2", "NHL", "C - D", "2026-11-15T16:00:00", True, list(view.rows), 0, "")
    v2.target = view.target
    legs = combo.candidate_legs([view, v2])
    assert len(legs) == 2 and all("cíl" in lg.row.best_book for lg in legs)
    assert combo.best_combos([view, v2], 10000)
