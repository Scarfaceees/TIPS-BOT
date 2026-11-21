"""Sledovani vsech tipu bota (vc. zrusenych a NHL) a srovnani modelu s trhem v ostrem provozu s prukaznosti."""

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from test_automation import SCORES
from tipsbot import modelcheck, nhl_settle, paper
from tipsbot.storage import TIP_CANCELLED, Ledger, Tip, make_session_factory
from tipsbot.tips import Candidate, Offer

NOW = datetime(2026, 11, 19, 12, 0)
KO = datetime(2026, 11, 20, 0, 0)  # TOR - LAK 3:4 po najezdech (SCORES)


def _tip(led, status="open", odds=2.4, market="MONEYLINE", sel="away", league="NHL",
         name="Toronto Maple Leafs - Los Angeles Kings", ko=KO):
    t = Tip(match_id="oddsapi:x1", sport="nhl", league=league, match_name=name, kickoff=ko, market=market, selection=sel,
            bookmaker="pinnacle", timestamp=NOW, valid_until=ko, raw_odds=odds, min_odds=2.2, devigged_sharp_odds=2.3,
            p_model=0.46, p_market=0.43, model_probability=0.45, ev_percentage=8.0, ev_threshold=3.0, kelly_stake=50,
            status=status)
    return led.add_tip(t)


def test_every_bot_tip_is_tracked_even_when_cancelled_and_then_settled():
    led = Ledger(make_session_factory("sqlite://")())
    t = _tip(led)
    assert paper.mirror_tips(led, [t], NOW) == 1
    t.raw_odds = 2.5
    paper.mirror_tips(led, [t], NOW + timedelta(hours=1))  # obnoveny tip pred zapasem: kurz se aktualizuje
    t.status, t.cancel_reason = TIP_CANCELLED, "kurz klesl pod minimum"
    paper.mirror_tips(led, [t], NOW + timedelta(hours=2))
    p = next(p for p in paper.load(led) if p.profile == "tipy")
    assert p.odds == 2.5 and p.p_model == 0.45 and "cancelled" in p.flags[0]
    assert paper.mirror_tips(led, [t], NOW + timedelta(hours=3)) == 0  # zadna duplicita
    notes = nhl_settle.settle_paper(led, KO + timedelta(hours=8), fetch=lambda d: SCORES)
    p = next(p for p in paper.load(led) if p.profile == "tipy")
    assert p.result == "won" and p.profit == pytest.approx(1.5) and notes  # zruseny tip se vyhodnotil taky


def test_tips_profile_is_listed_but_never_picks_itself():
    pr = paper.profile("tipy")
    assert pr.key == "tipy" and pr.method == "tips"
    assert paper.selections("MONEYLINE") == ("home", "away") and paper.selections("PUCKLINE_1.5R") == ("home+1.5", "away-1.5")


def test_nhl_is_tracked_by_model_profile_only():
    from tipsbot.oddsapi import Event, MarketOdds
    from tipsbot.czechodds import Entry

    led = Ledger(make_session_factory("sqlite://")())
    ev = Event("oddsapi:n1", "NHL", "Toronto Maple Leafs", "Los Angeles Kings", KO,
               {"MONEYLINE": MarketOdds("MONEYLINE", ("home", "away"), {})})
    entries = [Entry("zahraničí (medián)", ev.home, ev.away, "MONEYLINE", (1.80, 2.10), NOW)]
    model = {"MONEYLINE:home": 0.62, "MONEYLINE:away": 0.38, "lambda_home": 3.2, "lambda_away": 2.8}
    assert paper.track(led, "NHL", [(ev, model, entries)], NOW) == 1
    picks = paper.load(led)
    assert [(p.profile, p.market, p.selection) for p in picks] == [("zaklad", "MONEYLINE", "home")]


def _nhl_cands(p_model_home, p_market_home, mid="n1", ko=KO, name="Toronto Maple Leafs - Los Angeles Kings"):
    return {mid: [Candidate(match_id=mid, league="NHL", match_name=name, kickoff=ko, market="MONEYLINE", selection=s,
                            p_model=pm, p_market=pk, offers=(Offer("pinnacle", 2.0),))
                  for s, pm, pk in (("home", p_model_home, p_market_home), ("away", 1 - p_model_home, 1 - p_market_home))]}


def test_nhl_moneyline_is_logged_and_resolved_from_nhl_api():
    led = Ledger(make_session_factory("sqlite://")())
    assert modelcheck.log_predictions(led, "NHL", _nhl_cands(0.40, 0.55), NOW) == 1
    out = modelcheck.update_all(led, KO + timedelta(hours=6), None, nhl_fetch=lambda d: SCORES)
    q = out["leagues"]["NHL"]
    assert q["n"] == 1 and modelcheck.load(led, "NHL")["n1"]["res"] == "away"  # LAK vyhral v najezdech
    assert "málo dat" in q["verdict"]
    model_only = next(r for r in q["rows"] if r["w"] == 1.0)
    assert model_only["diff"] < 0  # model (60 % hoste) byl blize nez trh (45 %)
    assert next(r for r in q["rows"] if r["current"])["w"] == 0.15  # vaha NHL ze zpetneho testu


def test_compare_gives_confidence_interval_and_verdict_only_from_30_matches():
    data = {}
    for i in range(40):  # model systematicky lepsi nez trh
        res = "1" if i % 2 else "2"
        p = {"1": [0.6 if res == "1" else 0.2, 0.45], "X": [0.2, 0.25], "2": [0.2 if res == "1" else 0.6, 0.30]}
        data[f"m{i}"] = {"name": f"A{i} - B{i}", "day": "2026-11-01", "m": "1X2", "p": p, "res": res}
    q = modelcheck.compare(data, current_weight=0.25)
    cur = next(r for r in q["rows"] if r["current"])
    assert q["n"] == 40 and cur["hi"] < 0 and "průkazně lepší" in q["verdict"]
    small = modelcheck.compare(dict(list(data.items())[:10]), 0.25)
    assert "málo dat" in small["verdict"] and all(r["lo"] != r["lo"] for r in small["rows"])  # bez intervalu


def test_resolved_match_is_not_overwritten_and_old_records_default_to_1x2():
    rec = {"name": "Spain - England", "day": "2026-11-15", "p": {"1": [0.6, 0.5], "X": [0.2, 0.25], "2": [0.2, 0.25]}}
    res = pd.DataFrame([(date(2026, 11, 15), "Spain", "England", 1, 1)], columns=["date", "home", "away", "home_goals", "away_goals"])
    data = {"m1": rec}
    assert modelcheck.resolve(data, res) == 1 and rec["res"] == "X"
    assert modelcheck.resolve(data, res) == 0


def test_panel_shows_model_quality(tmp_path):
    from tipsbot.web import create_app

    url = f"sqlite:///{tmp_path / 'p.db'}"
    with make_session_factory(url)() as s:
        led = Ledger(s)
        modelcheck.log_predictions(led, "NHL", _nhl_cands(0.40, 0.55), NOW)
        modelcheck.update_all(led, KO + timedelta(hours=6), None, nhl_fetch=lambda d: SCORES)
        s.commit()
    html = create_app(url).test_client().get("/?tab=overeni").get_data(as_text=True)
    assert "Model vs. trh v ostrém provozu" in html and "vyhodnoceno 1 z 1" in html
