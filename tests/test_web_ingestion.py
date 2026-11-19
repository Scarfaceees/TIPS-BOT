from datetime import date, datetime, timedelta

import pytest

from tipsbot import backtest, ingestion
from tipsbot.storage import Ledger, Tip, make_session_factory
from tipsbot.tips import Candidate, Offer, evaluate
from tipsbot.web import create_app

CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,PSH,PSD,PSA,PSCH,PSCD,PSCA,AvgH,AvgD,AvgA,MaxH,MaxD,MaxA
E0,16/08/2024,20:00,Man United,Fulham,1,0,H,1.60,4.20,5.50,1.62,4.10,5.60,1.58,4.15,5.40,1.65,4.33,5.80
E0,17/08/2024,12:30,Ipswich,Liverpool,0,2,A,8.50,5.50,1.36,8.90,5.60,1.35,8.20,5.40,1.35,9.00,5.75,1.38
E0,,,,,,,,,,,,,,,,,,,
"""


def test_parse_football_csv(tmp_path):
    f = tmp_path / "ENG1_2425.csv"
    f.write_text(CSV, encoding="latin-1")
    df = ingestion.parse_football_csv(f, "ENG1")
    assert len(df) == 2
    assert df.loc[0, "date"] == date(2024, 8, 16) and df.loc[0, "home"] == "Man United"
    assert df.loc[0, "sharp_1"] == 1.62  # zaviraci Pinnacle ma prednost
    assert df.loc[1, "odds_2"] == 1.35 and df.loc[1, "away_goals"] == 2
    with pytest.raises(FileNotFoundError):
        ingestion.load_football("ENG1", data_dir=tmp_path)


def test_season_codes():
    assert ingestion.season_code(2025) == "2526"
    assert ingestion.season_code(1999) == "9900"
    assert ingestion.current_season_start_year(date(2026, 11, 11)) == 2026
    assert ingestion.current_season_start_year(date(2026, 3, 1)) == 2025
    with pytest.raises(ValueError):
        ingestion.download_football_season("CZE1", 2025)


def test_backtest_with_sharp_and_offered_odds(league):
    df, _ = league
    days = sorted(df["date"].unique())
    preds = backtest.walk_forward(df, days[-6], refit_days=30)
    for c, v in (("sharp_1", 2.2), ("sharp_x", 3.5), ("sharp_2", 3.4), ("odds_1", 2.45), ("odds_x", 3.3), ("odds_2", 3.2)):
        preds[c] = v
    rep = backtest.evaluate(preds)
    assert rep.bets > 0 and 0 <= rep.win_rate <= 1


@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{tmp_path / 'w.db'}"
    app = create_app(url)
    app.testing = True
    return app.test_client(), make_session_factory(url)


def test_panel_flow(client):
    c, factory = client
    assert "Nastav bankroll" in c.get("/").get_data(as_text=True)
    r = c.post("/deposit", data={"amount": "10000", "test": "1"})
    assert "TESTOVACÍ REŽIM" in r.get_data(as_text=True)

    now = datetime(2026, 11, 14, 12, 0)
    with factory() as s:
        led = Ledger(s)
        tip = evaluate(
            Candidate("m1", "ENG1", "Arsenal - Chelsea", now + timedelta(minutes=90), "1X2", "1", 0.52, 0.50,
                      (Offer("tipsport", 2.14),)),
            led, now=now,
        )
        assert isinstance(tip, Tip)
        led.add_tip(tip)
        s.commit()
        tip_id = tip.id

    assert "Arsenal - Chelsea" in c.get("/").get_data(as_text=True)
    r = c.post("/tickets", data={"tip_id": tip_id, "bookmaker": "tipsport", "odds": "2.12", "stake": "50"})
    assert "Tiket #1 zapsán" in r.get_data(as_text=True)
    r = c.post("/tickets/1/settle", data={"result": "won", "closing": "2.02"})
    body = r.get_data(as_text=True)
    assert "+56" in body and "10056" in body
    assert c.post("/tickets/1/settle", data={"result": "won"}).status_code == 400

    r = c.post("/tickets", data={"match_name": "Sparta - Slavia", "match_day": "2026-11-14", "market": "1X2",
                                 "selection": "X", "bookmaker": "fortuna", "odds": "3.4", "stake": "100"})
    assert "Tiket #2 zapsán" in r.get_data(as_text=True)


def test_calculator(client):
    c, _ = client
    c.post("/deposit", data={"amount": "10000", "test": "1"})
    good = c.get("/calc", query_string={"sharp": "2.10 3.40 3.60", "pick": "1", "odds": "2.30", "league": "ENG1"})
    assert "SÁZET" in good.get_data(as_text=True) and "NESÁZET" not in good.get_data(as_text=True)
    bad = c.get("/calc", query_string={"sharp": "2.10 3.40 3.60", "pick": "1", "odds": "2.05", "league": "ENG1"})
    assert "NESÁZET" in bad.get_data(as_text=True)
    assert c.get("/calc", query_string={"sharp": "abc", "pick": "1", "odds": "2", "league": "ENG1"}).status_code == 400


def test_czech_odds_form(client):
    c, _ = client
    c.post("/deposit", data={"amount": "10000", "test": "1"})
    r = c.post("/czech-odds", data={"text": "tipsport | Německo - Itálie | 1X2 | 2.10 3.30 3.40\nspatny radek", "tab": "nastaveni"})
    html = r.get_data(as_text=True)
    assert "Uloženo kurzů: 1" in html and "Německo - Itálie" in html
    assert "Smazat všechny" in c.get("/?tab=nastaveni").get_data(as_text=True)
    assert "Smazat všechny" not in c.post("/czech-odds/clear", data={"tab": "nastaveni"}).get_data(as_text=True)
    assert c.post("/czech-odds", data={"text": "x"}).status_code == 400


def test_paper_tab_flow(client):
    c, _ = client
    c.post("/deposit", data={"amount": "10000", "test": "1"})
    r = c.post("/paper", data={"name": "Sparta - Slavia", "day": "2026-11-20", "market": "OU_2.5", "selection": "under",
                               "book": "tipsport", "odds": "2.1", "tab": "sledovani"})
    assert "Sparta - Slavia" in r.get_data(as_text=True)
    pid = __import__("re").search(r"/paper/(\w+)/result", r.get_data(as_text=True)).group(1)
    html = c.post(f"/paper/{pid}/result", data={"result": "won", "tab": "sledovani"}).get_data(as_text=True)
    assert "+1.10" in html
    assert c.post("/paper", data={"name": "x", "day": "2026-11-20", "market": "OU_2.5", "selection": "1", "book": "t", "odds": "2"}).status_code == 400
    assert c.get("/?tab=sledovani").status_code == 200


def test_quick_value_calculator_rendered(client):
    c, _ = client
    c.post("/deposit", data={"amount": "10000", "test": "1"})
    html = c.get("/?tab=kalkulacka").get_data(as_text=True)
    assert "Value kalkulačka" in html and "K=0.125" in html and "CAP=0.02" in html


def test_history_shows_track_record(client):
    c, _ = client
    c.post("/deposit", data={"amount": "10000", "test": "1"})
    for odds, res in (("2.0", "won"), ("2.0", "lost"), ("2.0", "lost")):
        r = c.post("/tickets", data={"match_name": "A - B", "match_day": "2026-11-14", "market": "1X2", "selection": "1",
                                     "bookmaker": "tipsport", "odds": odds, "stake": "100"})
        tid = __import__("re").findall(r"/tickets/(\d+)/settle", r.get_data(as_text=True))[-1]
        c.post(f"/tickets/{tid}/settle", data={"result": res})
    html = c.get("/?tab=historie").get_data(as_text=True)
    assert "3 sázek" in html and "nejdelší série proher 2" in html and "největší propad 200" in html


def test_score_entry_settles_ticket(client):
    c, _ = client
    c.post("/deposit", data={"amount": "10000", "test": "1"})
    today = date.today().isoformat()
    r = c.post("/tickets", data={"match_name": "Kypr - Černá Hora", "match_day": today, "market": "OU", "line": "3,5",
                                 "selection": "pod", "bookmaker": "tipsport", "odds": "1.49", "stake": "500", "tab": "tikety"})
    html = r.get_data(as_text=True)
    assert "OU_3.5 under" in html and "Zadat výsledek zápasu" in html
    r = c.post("/score", data={"match": f"{today}|Kypr - Černá Hora", "home": "1", "away": "4", "tab": "tikety"})
    assert "lost" in r.get_data(as_text=True) and "9500" in r.get_data(as_text=True)
    assert c.post("/score", data={"match": f"{today}|Kypr - Černá Hora", "score": "x"}).status_code == 400
    assert c.post("/tickets", data={"match_name": "A - B", "match_day": today, "market": "OU", "line": "",
                                    "selection": "over", "bookmaker": "tipsport", "odds": "2", "stake": "10"}).status_code == 400
    for t in ("prehled", "tikety", "sledovani", "historie", "kalkulacka", "nastaveni", "sazka"):
        assert c.get(f"/?tab={t}").status_code == 200
    assert "Profil 2" in c.get("/?tab=sledovani&profile=profil2").get_data(as_text=True)


def test_overview_lists_bets_on_czech_odds(client):
    from types import SimpleNamespace

    from tipsbot import web

    row = SimpleNamespace(market="1X2", selection="2", label="Výhra B", p_final=0.3, status="none", cz=(3.9, "tipsport"),
                          cz_ev=0.17, cz_ok=False, cz_suspect=True)
    ok = SimpleNamespace(market="OU_2.5", selection="over", label="Over 2.5", p_final=0.55, status="model", cz=(2.07, "tipsport"),
                         cz_ev=0.14, cz_ok=True, cz_suspect=False)
    v = SimpleNamespace(kickoff="2026-11-16T19:45:00", rows=[row, ok], name="A - B")
    led = SimpleNamespace(paused=lambda: False)
    bets, check = web._to_bet([v], led, 10000.0, 0.0)
    assert [b["row"] for b in bets] == [ok] and [c["row"] for c in check] == [row]
    assert bets[0]["day"] == "2026-11-16" and 0 < bets[0]["stake"] <= 100


def test_post_redirects_and_mobile_ui(client):
    c, _ = client
    r = c.post("/deposit", data={"amount": "10000", "test": "1"}, follow_redirects=False)
    assert r.status_code == 302 and "/?tab=prehled" in r.headers["Location"]  # obnoveni stranky nic neodesle znovu
    html = c.get(r.headers["Location"]).get_data(as_text=True)
    assert "Bankroll uložen." in html and 'class="bottomnav"' in html and "Ruční rozbor" in html
    assert "Bankroll uložen." not in c.get("/").get_data(as_text=True)  # zprava jen jednou
    st = c.get("/auto/state").get_json()
    assert st == {"requested": False, "running": "", "t": ""}
    today = date.today().isoformat()
    for market, sel in (("DNB", "home"), ("BTTS", "yes"), ("DC", "X2"), ("JINY", "rohy nad 9.5")):
        r = c.post("/tickets", data={"match_name": "A - B", "match_day": today, "market": market, "line": "2.5", "selection": sel,
                                     "bookmaker": "tipsport", "odds": "2", "stake": "10", "tab": "tikety"})
        assert r.status_code == 200 and "zapsán" in r.get_data(as_text=True), market
    html = c.get("/?tab=tikety").get_data(as_text=True)
    assert 'data-for="market"' in html and 'list="matchlist"' in html and 'list="stakes"' in html


def test_every_tab_renders_and_every_post_route_has_a_form(client):
    """Redesign nesmi ztratit zadny formular: kazda POST adresa panelu je v nejake zalozce (bez zapasu a bankrollu)."""
    import re

    from tipsbot.web import MAIN_TABS, PAGE, TABS

    c, _ = client
    app = c.application
    html = ""
    for key, label, _icon, _short in TABS:
        r = c.get(f"/?tab={key}")
        page = r.get_data(as_text=True)
        assert r.status_code == 200 and label in page, key
        assert 'class="bottomnav"' in page and 'class="page-head"' in page or key == "prehled", key
        html += page
    assert len(TABS) > MAIN_TABS  # zbytek je v mobilni nabidce Vice
    actions = set(re.findall(r'action="([^"?]+)', html))
    for rule in app.url_map.iter_rules():
        if "POST" not in rule.methods or rule.arguments:  # adresy s id (konkretni tiket/vyber) jsou u radku tabulek
            continue
        if rule.rule in actions:
            continue
        # formulare, ktere se ukazi az s daty (zapasy, vysledky, rozbory, stop-loss): aspon musi byt v sablone
        assert f"url_for('{rule.endpoint}'" in PAGE, f"chybi formular pro {rule.rule}"
