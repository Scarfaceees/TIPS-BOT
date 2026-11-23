"""Tikety AKU (vic zapasu na jednom tiketu, jeden vklad): zapis v panelu, vyhodnoceni, mazani omylu.

Pozadavek z ostreho provozu 22.11.2026: 4 tikety Tipsportu po 2 zapasech nesly v panelu zapsat jako jeden tiket."""

import re
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from tipsbot import nhl_settle
from tipsbot.settlement import settle_open_tickets, settle_with_score
from tipsbot.storage import COMBO, Ledger, make_session_factory
from tipsbot.web import create_app

D = date(2026, 11, 22)


def _leg(name, market, sel, odds, day=D):
    return {"match_name": name, "match_day": day, "market": market, "selection": sel, "odds": odds}


@pytest.fixture
def led():
    s = make_session_factory("sqlite://")()
    lg = Ledger(s)
    lg.deposit(1000)
    return lg


def _df(rows):
    return pd.DataFrame([{"date": D, "home": h, "away": a, "home_goals": hg, "away_goals": ag} for h, a, hg, ag in rows])


def test_combo_won_after_last_leg(led):
    t = led.add_combo_ticket(bookmaker="tipsport", odds=1.89, stake=25, legs=[
        _leg("Hull City - Brighton", "OU_3.5", "under", 1.50), _leg("Liverpool - Manchester United", "OU_4.5", "under", 1.26)])
    assert t.market == COMBO and t.match_day == D and len(led.ticket_legs(t.id)) == 2
    settle_open_tickets(led, _df([("Hull City", "Brighton", 1, 1)]))
    assert t.status == "open" and led.ticket_legs(t.id)[0].status == "won"  # druhy zapas jeste ceka
    notes = settle_open_tickets(led, _df([("Liverpool", "Manchester United", 2, 2)]))
    assert t.status == "won" and t.profit_loss == pytest.approx(25 * 0.89)
    assert led.bankroll() == pytest.approx(1000 + 25 * 0.89)
    assert any("celý tiket won" in n for n in notes)


def test_combo_lost_as_soon_as_one_leg_loses(led):
    t = led.add_combo_ticket(bookmaker="tipsport", odds=2.54, stake=25, legs=[
        _leg("Paris FC - Angers", "OU_2.5", "under", 2.00), _leg("Lorient - Rennes", "OU_3.5", "under", 1.27)])
    settle_open_tickets(led, _df([("Paris FC", "Angers", 2, 1)]))
    assert t.status == "lost" and t.profit_loss == -25 and led.bankroll() == 975
    # 23.11.2026: druha noha se vyhodnoti i u prohraneho tiketu, tiket ani bankroll se tim nemeni
    assert [x.match_name for x in led.open_legs()] == ["Lorient - Rennes"]
    settle_open_tickets(led, _df([("Lorient", "Rennes", 1, 1)]))
    assert [x.status for x in led.ticket_legs(t.id)] == ["lost", "won"] and led.ticket_legs(t.id)[1].score == "1:1"
    assert t.status == "lost" and t.profit_loss == -25 and led.bankroll() == 975 and led.open_legs() == []


def test_combo_void_and_push_leg(led):
    """Brighton 'méně než 2.0' pri presne 2 golech = vraceno; tiket se prepocita bez te nohy (kurz 1.29)."""
    t = led.add_combo_ticket(bookmaker="tipsport", odds=2.39, stake=25, legs=[
        _leg("Hull City - Brighton", "TTA_2", "under", 1.85), _leg("Bologna - Udinese", "OU_1.5", "over", 1.29)])
    settle_open_tickets(led, _df([("Hull City", "Brighton", 1, 2), ("Bologna", "Udinese", 2, 0)]))
    assert [x.status for x in led.ticket_legs(t.id)] == ["void", "won"]
    assert t.status == "won" and t.profit_loss == pytest.approx(25 * (2.39 / 1.85 - 1))


def test_manual_score_settles_leg(led):
    t = led.add_combo_ticket(bookmaker="tipsport", odds=1.84, stake=25, legs=[
        _leg("Augsburg - FSV Mainz 05", "OU_4.5", "under", 1.33),
        _leg("Real Racing Club de Santander - Real Sociedad", "OU_4.5", "under", 1.38)])
    settle_with_score(led, "Augsburg - FSV Mainz 05", D, 3, 2)
    assert t.status == "lost"


def test_nhl_leg(led):
    t = led.add_combo_ticket(bookmaker="tipsport", odds=2.0, stake=10, legs=[
        _leg("Toronto Maple Leafs - Los Angeles Kings", "MONEYLINE", "away", 1.4, date(2026, 11, 20)),
        _leg("Sparta - Slavia", "1X2", "1", 1.43, date(2026, 11, 20))])
    from test_automation import SCORES

    notes = nhl_settle.settle_tickets(led, datetime(2026, 11, 21, 12), fetch=lambda d: SCORES)
    assert led.ticket_legs(t.id)[0].status == "won" and "SO" in led.ticket_legs(t.id)[0].score
    assert t.status == "open" and notes


def test_combo_validation(led):
    with pytest.raises(ValueError):
        led.add_combo_ticket(bookmaker="tipsport", odds=1.5, stake=25, legs=[_leg("A - B", "OU_2.5", "under", 1.5)])
    with pytest.raises(ValueError):
        led.add_combo_ticket(bookmaker="tipsport", odds=1.5, stake=25,
                             legs=[_leg("A - B", "OU_2.5", "under", 1.0), _leg("C - D", "OU_2.5", "under", 1.5)])


def test_delete_only_open_ticket(led):
    t = led.add_combo_ticket(bookmaker="tipsport", odds=1.89, stake=25, legs=[
        _leg("A - B", "OU_3.5", "under", 1.5), _leg("C - D", "OU_4.5", "under", 1.26)])
    single = led.add_ticket(bookmaker="tipsport", odds=2.0, stake=10, match_id="m", market="1X2", selection="1",
                            match_day=D, match_name="E - F")
    led.settle_ticket(single.id, "won")
    with pytest.raises(ValueError):
        led.delete_open_ticket(single.id)  # vyhodnoceny tiket je v bankrollu
    led.delete_open_ticket(t.id)
    assert led.ticket_legs(t.id) == [] and led.open_legs() == []


# --- panel ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{tmp_path / 'w.db'}"
    app = create_app(url)
    app.testing = True
    c = app.test_client()
    c.post("/deposit", data={"amount": "1000", "test": "1"})
    return c


def test_panel_aku_form_settle_and_delete(client):
    c = client
    day = date.today().isoformat()
    html = c.get("/?tab=tikety").get_data(as_text=True)
    assert "+ další zápas na tiket (AKU)" in html and 'name="leg_odds"' in html and "Celkový kurz tiketu" in html
    r = c.post("/tickets", data={"tab": "tikety", "bookmaker": "tipsport", "odds": "2.39", "stake": "25",
                                 "match_name": ["Hull City - Brighton", "Bologna - Udinese"], "match_day": [day, day],
                                 "market": ["TTA", "OU"], "line": ["2.0", "1.5"], "selection": ["under", "over"],
                                 "leg_odds": ["1.85", "1.29"]})
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Tiket AKU #1 zapsán: 2 zápasy, kurz 2.39" in html
    assert "AKU (2 zápasy)" in html and "Bologna - Udinese" in html and "@ 1.85" in html
    assert "Zadat výsledek zápasu" in html  # oba zapasy lze vyhodnotit zadanim skore
    c.post("/score", data={"match": f"{day}|Bologna - Udinese", "home": "0", "away": "1", "tab": "tikety"})
    html = c.get("/?tab=tikety").get_data(as_text=True)
    assert "❌" in html and "-25" in html  # prohrana noha = prohrany tiket
    # jednoduchy tiket pres novy formular (jen kurz zapasu) a smazani omylu
    r = c.post("/tickets", data={"tab": "tikety", "bookmaker": "tipsport", "odds": "1.5", "stake": "25",
                                 "match_name": "Hull City - Brighton", "match_day": day, "market": "OU", "line": "3.5",
                                 "selection": "under", "leg_odds": "1.5"})
    tid = re.findall(r"/tickets/(\d+)/delete", r.get_data(as_text=True))[-1]
    r = c.post(f"/tickets/{tid}/delete", data={"tab": "tikety"})
    assert f"Tiket #{tid} smazán" in r.get_data(as_text=True)
    assert c.post("/tickets/1/delete").status_code == 400  # vyhodnoceny AKU nejde smazat
    bad = c.post("/tickets", data={"bookmaker": "tipsport", "odds": "2", "stake": "25",
                                   "match_name": ["A - B", ""], "match_day": [day, day], "market": ["OU", "OU"],
                                   "line": ["2.5", "2.5"], "selection": ["under", "under"], "leg_odds": ["1.5", ""]})
    assert bad.status_code == 400


# --- handicap a kombinace vysledek + goly (pozadavek 22.11.2026: tikety Elche +1.5, Marseille a vic nez 1.5,
# Tampa a vic nez 5.5 se nedaly vyhodnotit automaticky) -------------------------------------------------------

def test_grade_handicap_and_result_goals_combo():
    from tipsbot.settlement import grade

    assert grade("AH_+1.5", "away", 1, 0) == "won"  # Elche +1.5: prohra o 1 = vyhra sazky
    assert grade("AH_+1.5", "away", 2, 0) == "lost"
    assert grade("AH_-1", "home", 2, 1) == "void" and grade("AH_-0.75", "home", 2, 1) == "half_won"
    assert grade("1X2&OU_1.5", "1&over", 2, 0) == "won"  # Marseille a vic nez 1,5 golu
    assert grade("1X2&OU_1.5", "1&over", 1, 0) == "lost" and grade("1X2&OU_1.5", "1&over", 1, 2) == "lost"
    assert grade("1X2&OU_2", "1&over", 2, 0) is None  # vracena cast: rucne podle pravidel kancelare


def test_grade_nhl_result_goals_60min_and_ot():
    g_reg = {"reg": (4, 2), "final": (4, 2), "end": "REG"}
    g_ot = {"reg": (3, 3), "final": (4, 3), "end": "OT"}
    assert nhl_settle.grade("REG&OU60_5.5", "1&over", g_reg) == "won"
    assert nhl_settle.grade("REG&OU60_5.5", "1&over", g_ot) == "lost"  # po 60 min remiza
    assert nhl_settle.grade("OT", "no", g_reg) == "won" and nhl_settle.grade("OT", "no", g_ot) == "lost"


def test_combo_with_handicap_and_combo_legs_settles(led):
    t = led.add_combo_ticket(bookmaker="tipsport", odds=1.65, stake=25, legs=[
        _leg("Levante - Elche", "AH_+1.5", "away", 1.24), _leg("Marseille - Le Mans", "1X2&OU_1.5", "1&over", 1.33)])
    settle_open_tickets(led, _df([("Levante", "Elche", 1, 0), ("Marseille", "Le Mans", 3, 1)]))
    assert [x.status for x in led.ticket_legs(t.id)] == ["won", "won"] and t.status == "won"


def test_panel_handicap_and_combo_markets(client):
    c = client
    day = date.today().isoformat()
    html = c.get("/?tab=tikety").get_data(as_text=True)
    assert 'value="AH"' in html and 'value="1X2_OU"' in html and 'value="REG_OU60"' in html and 'value="OT"' in html
    r = c.post("/tickets", data={"tab": "tikety", "bookmaker": "tipsport", "odds": "3.25", "stake": "25",
                                 "match_name": ["Levante - Elche", "Marseille - Le Mans",
                                                "Tampa Bay Lightning - Buffalo Sabres", "Dallas Stars - Pittsburgh Penguins"],
                                 "match_day": [day] * 4, "market": ["AH", "1X2_OU", "REG_OU60", "OT"],
                                 "line": ["2.5", "1.5", "5.5", "2.5"], "hcap": ["+1.5", "+1.5", "+1.5", "+1.5"],
                                 "selection": ["away", "1&over", "1&over", "no"], "leg_odds": ["1.24", "1.64", "2.59", "1.11"]})
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "Tiket AKU #1 zapsán: 4 zápasy" in html
    assert "Handicap Elche +1.5" in html and "Výhra Marseille a Over 1.5 gólů" in html
    assert "Zápas jde do prodloužení: ne" in html


def test_other_market_text_is_never_auto_graded(led):
    """22.11.2026: noha 'JINY' s popisem 'Marseille a více než 1.5 gólu' se vyhodnotila jako over 1.5 (2:3 -> výhra)."""
    from tipsbot.settlement import grade

    assert grade("JINY", "Marseille a více než 1.5 gólu", 2, 3) is None
    t = led.add_combo_ticket(bookmaker="tipsport", odds=2.07, stake=25, legs=[
        _leg("Getafe - Atletico Madrid", "OU_3.5", "under", 1.26),
        _leg("Marseille - Le Mans", "JINY", "Marseille a více než 1.5 gólu", 1.64)])
    notes = settle_open_tickets(led, _df([("Getafe", "Atletico Madrid", 0, 3), ("Marseille", "Le Mans", 2, 3)]))
    assert [x.status for x in led.ticket_legs(t.id)] == ["won", "open"] and t.status == "open"
    assert any("neumím vyhodnotit" in n for n in notes)
