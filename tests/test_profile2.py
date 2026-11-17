"""Profil 2: vypocet sazek na skorove matici, scenare, doporuceni a sure bety."""

from datetime import date, datetime
from pathlib import Path

import numpy as np
import pytest

from tipsbot import paper, profile2 as p2
from tipsbot.storage import Ledger, make_session_factory

ODDS = (Path(__file__).parent / "fixtures" / "profil2_wales_portugal.txt").read_text(encoding="utf-8")


def test_score_matrix_sums_to_one_and_has_fatter_tails():
    pure = p2.score_matrix(0.8, 1.95, k=0)
    mixed = p2.score_matrix(0.8, 1.95)
    assert pure.sum() == pytest.approx(1.0) and mixed.sum() == pytest.approx(1.0)
    goals = p2.H + p2.A
    assert mixed[goals >= 5].sum() > pure[goals >= 5].sum()  # nejistota => vic vysokych vysledku


@pytest.mark.parametrize("market,sel,score,expected", [
    ("OU_4.0", "under", (1, 3), 1.0),  # presne 4 goly = vraceni
    ("OU_4.0", "under", (0, 3), 1.33),
    ("OU_4.0", "under", (2, 3), 0.0),
    ("OU_2.25", "over", (1, 1), 0.5),  # ctvrtinova linie: pul vraceno, pul prohra
    ("OU_2.25", "over", (2, 1), 1.33),
    ("AH_1.0", "away", (0, 1), 1.0),  # hoste -1 a vyhra o gol = vraceni
    ("AH_1.0", "away", (0, 2), 1.33),
    ("AH_1.0", "home", (0, 0), 1.33),  # domaci +1 pri remize vyhrava
    ("TTA_1.5", "under", (3, 1), 1.33),
    ("DNB", "away", (1, 1), 1.0),
    ("BTTS", "no", (0, 2), 1.33),
    ("EXPR", "2&U3.5", (1, 2), 1.33),
    ("EXPR", "2&U3.5", (1, 3), 0.0),
    ("EXPR", "G1-3", (0, 0), 0.0),
    ("EXPR", "2&H_U0.5", (0, 1), 1.33),
])
def test_returns(market, sel, score, expected):
    r = p2.returns(market, sel, 1.33)
    assert r[score] == pytest.approx(expected)


def test_parse_odds_formats_and_errors():
    text = """tipsport | OU_4 | 3.30 1.33
    U4.5 | 1.25
    fortuna | Wales - Portugalsko | BTTS | 1.65 2.11
    AH_1.0:away | 1.65
    2&U3.5 | 2.41
    tipsport | 1X2 | 2.0 3.0
    tipsport | XYZ | 1.5
    """
    entries, errors = p2.parse_odds(text)
    got = {(e.market, e.selection, e.book) for e in entries}
    assert ("OU_4.0", "", "tipsport") in got  # jednotny zapis linie
    assert ("OU_4.5", "under", "tipsport") in got
    assert ("BTTS", "", "fortuna") in got
    assert ("AH_1.0", "away", "tipsport") in got
    assert ("EXPR", "2&U3.5", "tipsport") in got
    assert len(errors) == 2  # 1X2 se dvema kurzy, neznamy trh


def test_market_lambdas_recover_known_lambdas():
    mat = p2.score_matrix(1.1, 1.6)
    goals = p2.H + p2.A
    def two_way(p, margin=1.05):
        return f"{1 / (p * margin):.3f} {1 / ((1 - p) * margin):.3f}"
    p1, px = mat[p2.H > p2.A].sum(), mat[p2.H == p2.A].sum()
    p2_ = 1 - p1 - px
    text = (f"1X2 | {1 / (p1 * 1.05):.3f} {1 / (px * 1.05):.3f} {1 / (p2_ * 1.05):.3f}\n"
            f"OU_2.5 | {two_way(mat[goals > 2.5].sum())}\nOU_3.5 | {two_way(mat[goals > 3.5].sum())}")
    entries, _ = p2.parse_odds(text)
    lh, la = p2.market_lambdas(entries)
    assert lh == pytest.approx(1.1, abs=0.08) and la == pytest.approx(1.6, abs=0.08)


def test_wales_portugal_reproduces_conversation():
    """Rozbor z 16.11.2026: s odhadem 0,825 : 1,95 a kurzy Tipsportu vychazi Under 4,0, Under 3,5 a Under 2,5."""
    entries, errors = p2.parse_odds(ODDS)
    assert not errors
    a = p2.analyse("Wales - Portugalsko", entries, (0.825, 1.95), day="2026-11-17", bankroll=10000)
    assert [s.name[0] for s in a.scenarios] == ["A", "B", "C"]
    total_market = a.scenarios[2].lam_h + a.scenarios[2].lam_a
    assert 3.0 < total_market < 3.6  # trh ceka zhruba 3,3 golu
    picks = {p.role: p.offer for p in a.picks}
    assert picks["safe"].key == "OU_4.0:under" and picks["safe"].worst_loss < 0.25
    assert picks["high_prob"].key == "OU_3.5:under" and picks["high_prob"].odds >= 1.5
    assert picks["value"].key == "OU_2.5:under"
    for o in picks.values():
        assert o.ev[0] > 0 and o.ev[1] > 0  # robustni v A i B
        assert o.ev[2] < 0  # proti ferove linii tychz kurzu je EV zaporne (marze)
        assert o.min_odds <= o.odds
    assert any("AKO" in n for n in a.notes)  # vsechny tri stoji na malo golech
    avoid = {o.key for o in a.avoid}
    assert "OU_5.5:over" in avoid
    over25 = next(o for o in a.offers if o.key == "OU_2.5:over")
    assert over25.verdict == "avoid"
    assert not any(s["sure"] for s in a.surebets)
    assert a.sim["n"] == 1000 and a.sim["top"][0]["score"] in {"0:1", "0:2", "1:1"}


def test_surebet_found_across_books():
    entries, _ = p2.parse_odds("tipsport | OU_2.5 | 1.52 2.55\nfortuna | OU_2.5 | 1.75 2.20")
    sb = p2.surebets(entries)[0]
    assert sb["sure"] and sb["books"] == ["fortuna", "tipsport"]
    assert sb["yield"] == pytest.approx(1 / (1 / 1.75 + 1 / 2.55) - 1)
    assert sum(sb["split"]) == pytest.approx(100, abs=0.2)


def test_market_only_has_no_robust_value():
    entries, _ = p2.parse_odds(ODDS)
    a = p2.analyse("Wales - Portugalsko", entries, None)
    assert [s.name[0] for s in a.scenarios] == ["C"]
    assert not a.picks  # proti ferove linii tychz kurzu nic nevyjde


def test_save_and_track(tmp_path):
    led = Ledger(make_session_factory(f"sqlite:///{tmp_path / 'db.sqlite'}")())
    entries, _ = p2.parse_odds(ODDS)
    a = p2.analyse("Wales - Portugalsko", entries, (0.825, 1.95), league="UNL", day="2026-11-17")
    p2.save(led, a)
    p2.save(led, a)  # stejny zapas se prepise
    loaded = p2.load(led)
    assert len(loaded) == 1 and loaded[0].picks[0].offer.key == a.picks[0].offer.key
    assert p2.track_picks(led, loaded[0], datetime(2026, 11, 17)) == 3
    assert p2.track_picks(led, loaded[0], datetime(2026, 11, 17)) == 0  # uz sledovano
    picks = [p for p in paper.load(led) if p.profile == "profil2"]
    assert {p.market for p in picks} == {"OU_4.0", "OU_3.5", "OU_2.5"}
    assert all(0 < p.p_model < 1 for p in picks)
    p2.remove(led, "Wales - Portugalsko", "2026-11-17")
    assert p2.load(led) == []


def test_text_output_contains_picks():
    entries, _ = p2.parse_odds(ODDS)
    txt = p2.format_text(p2.analyse("Wales - Portugalsko", entries, (0.825, 1.95)))
    assert "Nejrozumnější sázka: méně než 4,0 gólu @ 1.33" in txt
    assert "Simulace 1000 zápasů" in txt


def test_web_tab(tmp_path):
    from tipsbot.web import create_app

    client = create_app(f"sqlite:///{tmp_path / 'web.db'}").test_client()
    form = {"match": "Wales - Portugalsko", "day": "2026-11-17", "league": "", "book": "tipsport",
            "lam_h": "0.825", "lam_a": "1.95", "ctx_h": "1", "ctx_a": "1", "odds": ODDS, "tab": "profil2"}
    r = client.post("/profil2", data=form)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Nejrozumnější sázka" in html and "méně než 4,0 gólu" in html
    r = client.post("/profil2/track", data={"match": "Wales - Portugalsko", "day": "2026-11-17", "tab": "profil2"})
    assert "Do sledování přidáno: 3" in r.get_data(as_text=True)
    assert client.post("/profil2", data={**form, "odds": "", "lam_h": ""}).status_code == 400


def test_auto_pick_in_tracking(tmp_path):
    """Pri skenu vybere Profil 2 ve Sledovani robustni sazku z modelu bota a ceskych kurzu."""
    from types import SimpleNamespace

    from tipsbot.czechodds import Entry

    led = Ledger(make_session_factory(f"sqlite:///{tmp_path / 'db.sqlite'}")())
    now = datetime(2026, 11, 17, 10, 0)
    czech = [Entry("tipsport", "Wales", "Portugalsko", m, o, now) for m, o in (
        ("OU_2.5", (1.52, 2.55)), ("OU_3.5", (2.34, 1.62)), ("OU_4.0", (3.30, 1.33)), ("OU_4.5", (3.95, 1.25)),
        ("BTTS", (1.65, 2.11)), ("TTA_1.5", (1.38, 2.78)))]
    model = {"lambda_home": 0.825, "lambda_away": 1.95}
    pk, a = p2.auto_pick("Wales - Portugal", model, czech)
    assert pk.role == "safe" and pk.offer.key == "OU_4.0:under"
    assert p2.auto_pick("Wales - Portugal", {}, czech) is None  # bez modelu nic
    ev = SimpleNamespace(match_id="m1", name="Wales - Portugal", kickoff=datetime(2026, 11, 17, 19, 45))
    paper.track(led, "UNL", [(ev, model, czech)], now)
    mine = [p for p in paper.load(led) if p.profile == "profil2"]
    assert len(mine) == 1 and mine[0].market == "OU_4.0" and mine[0].selection == "under" and mine[0].detail
