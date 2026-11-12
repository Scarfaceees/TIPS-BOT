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
    good = c.post("/calc", data={"sharp": "2.10 3.40 3.60", "pick": "1", "odds": "2.30", "league": "ENG1"})
    assert "SÁZET" in good.get_data(as_text=True) and "NESÁZET" not in good.get_data(as_text=True)
    bad = c.post("/calc", data={"sharp": "2.10 3.40 3.60", "pick": "1", "odds": "2.05", "league": "ENG1"})
    assert "NESÁZET" in bad.get_data(as_text=True)
    assert c.post("/calc", data={"sharp": "abc", "pick": "1", "odds": "2", "league": "ENG1"}).status_code == 400
