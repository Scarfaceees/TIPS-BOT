from datetime import date

from tipsbot import nhl_api


def _g(day, home, away, hs, as_, end="REG", gtype=2, state="OFF"):
    return {"gameType": gtype, "gameState": state, "gameDate": day, "gameOutcome": {"lastPeriodType": end},
            "homeTeam": {"abbrev": home, "score": hs}, "awayTeam": {"abbrev": away, "score": as_}}


PAYLOAD = {"games": [
    _g("2026-11-01", "TOR", "MTL", 4, 2), _g("2026-11-03", "BOS", "TOR", 3, 2, "OT"),
    _g("2026-11-05", "TOR", "MTL", 1, 3), _g("2026-11-07", "TOR", "BOS", 5, 1),
    _g("2026-09-20", "TOR", "MTL", 9, 0, gtype=1),  # pripravne zapasy se nepocitaji
    {"gameType": 2, "gameState": "FUT", "gameDate": "2026-11-20", "homeTeam": {"abbrev": "TOR"}, "awayTeam": {"abbrev": "NYR"}},
]}


def test_parse_games_only_played_regular_season():
    rows = nhl_api.parse_games(PAYLOAD, "TOR")
    assert [r["opp"] for r in rows] == ["MTL", "BOS", "MTL", "BOS"]
    assert rows[1]["gf"] == 2 and rows[1]["ga"] == 3 and not rows[1]["win"] and rows[1]["end"] == "OT"


def test_report_form_rest_h2h_and_missing():
    club = {"games": nhl_api.parse_games(PAYLOAD, "TOR"),
            "skaters": [{"firstName": {"default": "John"}, "lastName": {"default": "Tavares"}, "positionCode": "C",
                         "gamesPlayed": 11, "goals": 2, "assists": 5, "points": 7, "powerPlayGoals": 1}],
            "goalies": [{"firstName": {"default": "Anthony"}, "lastName": {"default": "Stolarz"}, "gamesStarted": 9,
                         "savePercentage": 0.915, "goalsAgainstAverage": 2.4}]}
    rep = nhl_api.build_nhl_report("TOR", "Toronto Maple Leafs", club, date(2026, 11, 8), "home", "MTL")
    titles = " | ".join(s["title"] for s in rep["sections"])
    assert "Forma (posledních 4): 2V-2P" in titles and "po prodl." in " ".join(rep["sections"][0]["lines"])
    rest = next(s for s in rep["sections"] if s["title"] == "Únava a rozpis")["lines"]
    assert "BACK-TO-BACK" in rest[0]  # posledni zapas byl den predtim
    h2h = next(s for s in rep["sections"] if s["title"].startswith("H2H proti MTL"))
    assert len(h2h["lines"]) == 2
    assert "Tavares" in " ".join(next(s for s in rep["sections"] if s["title"].startswith("Klíčoví"))["lines"])
    assert any("základní brankář" in line for line in rep["sections"][-1]["lines"])


def test_report_without_data_lists_missing():
    rep = nhl_api.build_nhl_report("TOR", "Toronto", {}, date(2026, 11, 8), "away", "MTL")
    lines = " ".join(rep["sections"][-1]["lines"])
    assert "forma" in lines and "střelci" in lines


def test_last_starter_hint_on_back_to_back():
    club = {"games": nhl_api.parse_games(PAYLOAD, "TOR"), "last_starter": ("Anthony Stolarz", date(2026, 11, 7))}
    rep = nhl_api.build_nhl_report("TOR", "Toronto", club, date(2026, 11, 8), "home", "MTL")
    line = next(s for s in rep["sections"] if s["title"] == "Kdo chytal naposledy")["lines"][0]
    assert "Stolarz" in line and "záložník" in line and "nepotvrzeno" in line
    later = nhl_api.build_nhl_report("TOR", "Toronto", club, date(2026, 11, 10), "home", "MTL")
    assert "záložník" not in next(s for s in later["sections"] if s["title"] == "Kdo chytal naposledy")["lines"][0]
