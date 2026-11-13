from datetime import date

import pandas as pd

from tipsbot import teamreport
from tipsbot.context import Absence


def _df():
    rows = []
    for i in range(12):
        rows.append((date(2026, 1, 1 + i), "A", "B" if i % 2 else "C", 2, 1, "ENG1"))
        rows.append((date(2026, 2, 1 + i), "D", "A", 0, 1, "ENG1"))
    rows.append((date(2026, 3, 1), "A", "B", 3, 3, "UCL"))
    return pd.DataFrame(rows, columns=["date", "home", "away", "home_goals", "away_goals", "league"])


def test_team_report_has_all_available_sections_and_lists_missing():
    rep = teamreport.build_team_report("A", "Arsenal", _df(), date(2026, 6, 1), "home", "B",
                                       absences=[Absence("Arsenal", "X", "Out")], absence_checked=True)
    titles = " | ".join(s["title"] for s in rep["sections"])
    assert "Forma" in titles and "Doma" in titles and "Venku" in titles and "H2H proti B" in titles
    assert "Liga mistrů" in titles and "Zranění" in titles
    miss = next(s for s in rep["sections"] if s["title"] == "Chybí")["lines"]
    assert any("střelci" in m for m in miss) and any("sestava" in m for m in miss)


def test_report_never_uses_future_matches():
    rep = teamreport.build_team_report("A", "Arsenal", _df(), date(2026, 1, 3), "home", "B")
    form = rep["sections"][0]["lines"]
    assert all("2026" in l for l in form) and len(form) == 2


def test_top_players_parsed():
    payload = {"response": [{"player": {"name": "Saka"}, "statistics": [
        {"team": {"name": "Arsenal"}, "goals": {"total": 7, "assists": 4}, "games": {"appearences": 10}}]}]}
    got = teamreport.top_players(payload, {"Arsenal", "Chelsea"})
    assert got["Arsenal"][0]["goals"] == 7


def test_lineup_section_replaces_missing_note():
    from tipsbot.context import parse_lineups
    payload = {"response": [{"team": {"name": "Arsenal"}, "formation": "4-3-3", "coach": {"name": "Arteta"},
                             "startXI": [{"player": {"name": "Raya", "pos": "G"}}, {"player": {"name": "Saka", "pos": "F"}}]}]}
    lu = parse_lineups(payload)["Arsenal"]
    rep = teamreport.build_team_report("Arsenal", "Arsenal", teamreport.load_all_results().iloc[0:0], date(2026, 11, 14), "home",
                                       lineup=lu)
    titles = [s["title"] for s in rep["sections"]]
    assert any(t.startswith("Základní sestava (4-3-3, trenér Arteta)") for t in titles)
    assert not any("sestava (zveřejňuje" in line for s in rep["sections"] for line in s["lines"])
    assert parse_lineups({"response": []}) == {}
