"""Analyza tymu a zapasu: forma, domaci/venku a styl, H2H, jine souteze, zraneni, strelci, bulvar.

Vse z dat, ktera bot ma (vysledky lig a pohar, zraneni, API-Football, novinky). Co zdroj nedodal, se uvede
v sekci "Chybi" - nic se nedomysli. Zpravy se ukladaji pro kazdy tym (historie poslednich analyz).
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
import requests

from . import cups, ingestion
from .context import API_FOOTBALL, API_FOOTBALL_LEAGUES, Absence, fetch_news
from .names import match_team

log = logging.getLogger(__name__)

COMP_NAMES = {"ENG1": "Premier League", "ESP1": "La Liga", "GER1": "Bundesliga", "ITA1": "Serie A",
              "FRA1": "Ligue 1", "POR1": "Liga Portugal", "UCL": "Liga mistrů", "UEL": "Evropská liga",
              "UECL": "Konferenční liga"}


def load_all_results() -> pd.DataFrame:
    """Vsechny stazene vysledky (ligy + poháry); nazvy z poharu prevedeny na ligove, aby tym mel jeden nazev."""
    frames, known = [], set()
    for lg in cups.DOMESTIC:
        try:
            d = ingestion.load_football(lg)
        except FileNotFoundError:
            continue
        frames.append(d[["date", "home", "away", "home_goals", "away_goals", "league"]])
        known |= set(d["home"]) | set(d["away"])
    for cup in cups.CUP_API_FOOTBALL_IDS:
        try:
            d = ingestion.load_football(cup)
        except FileNotFoundError:
            continue
        d = d[["date", "home", "away", "home_goals", "away_goals", "league"]].copy()
        for col in ("home", "away"):
            d[col] = d[col].map(lambda n: match_team(n, known) or n)
        frames.append(d)
    if not frames:
        return pd.DataFrame(columns=["date", "home", "away", "home_goals", "away_goals", "league"])
    return pd.concat(frames, ignore_index=True).sort_values("date").reset_index(drop=True)


def _rows(df: pd.DataFrame, team: str, as_of: date, n: int, venue: str | None = None, league: str | None = None) -> list[dict]:
    d = df[(df["date"] < as_of) & ((df["home"] == team) | (df["away"] == team))]
    if venue == "home":
        d = d[d["home"] == team]
    elif venue == "away":
        d = d[d["away"] == team]
    if league:
        d = d[d["league"] == league]
    out = []
    for _, r in d.sort_values("date", ascending=False).head(n).iterrows():
        home = r["home"] == team
        gf, ga = (r["home_goals"], r["away_goals"]) if home else (r["away_goals"], r["home_goals"])
        out.append({"date": r["date"], "opp": r["away"] if home else r["home"], "home": home, "gf": int(gf), "ga": int(ga),
                    "res": "V" if gf > ga else "R" if gf == ga else "P", "league": r["league"]})
    return out


def _summary(rows: list[dict]) -> str:
    if not rows:
        return "bez dat"
    w, d, l = (sum(r["res"] == k for r in rows) for k in "VRP")
    gf, ga = sum(r["gf"] for r in rows), sum(r["ga"] for r in rows)
    return f"{w}V-{d}R-{l}P, skóre {gf}:{ga}"


def style_label(rows: list[dict]) -> str:
    """Styl z vysledku (ne z herni statistiky): kolik se dava a inkasuje, otevrenost, nuly."""
    if len(rows) < 4:
        return "málo zápasů"
    n = len(rows)
    gf, ga = sum(r["gf"] for r in rows) / n, sum(r["ga"] for r in rows) / n
    over = sum(r["gf"] + r["ga"] > 2.5 for r in rows) / n
    btts = sum(r["gf"] > 0 and r["ga"] > 0 for r in rows) / n
    zero_ga = sum(r["ga"] == 0 for r in rows) / n
    fts = sum(r["gf"] == 0 for r in rows) / n
    tags = []
    tags.append("útočný" if gf >= 1.8 else "málo gólový útok" if gf < 1.0 else "průměrný útok")
    tags.append("slabá obrana" if ga >= 1.6 else "pevná obrana" if ga <= 0.8 else "průměrná obrana")
    tags.append("otevřené zápasy" if over >= 0.6 else "uzavřené zápasy" if over <= 0.35 else "střední počet gólů")
    return (f"{', '.join(tags)} | gólů {gf:.1f}:{ga:.1f}, over 2,5 v {over:.0%}, oba skórují {btts:.0%}, "
            f"čisté konto {zero_ga:.0%}, bez gólu {fts:.0%}")


def _fmt(r: dict) -> str:
    return f"{r['date']:%d.%m.%Y} {'D' if r['home'] else 'V'} {r['opp']} {r['gf']}:{r['ga']} ({r['res']}, {COMP_NAMES.get(r['league'], r['league'])})"


def top_players(payload: dict, team_names: set[str]) -> dict[str, list[dict]]:
    """API-Football /players/topscorers: tym -> hraci (jmeno, goly, asistence, zapasy)."""
    out: dict[str, list[dict]] = {}
    for item in payload.get("response", []):
        p, stats = item.get("player", {}), (item.get("statistics") or [{}])[0]
        team = match_team(stats.get("team", {}).get("name", ""), team_names)
        if not team or not p.get("name"):
            continue
        out.setdefault(team, []).append({"name": p["name"], "goals": (stats.get("goals") or {}).get("total") or 0,
                                         "assists": (stats.get("goals") or {}).get("assists") or 0,
                                         "apps": (stats.get("games") or {}).get("appearences") or 0})
    return out


def fetch_top_players(league: str, season: int, api_key: str, team_names: set[str], timeout: int = 20) -> dict[str, list[dict]]:
    """Strelci a nahravaci ligy (2 pozadavky). Prazdne, kdyz plan/klic sezonu nepusti."""
    lid = API_FOOTBALL_LEAGUES.get(league)
    if not api_key or lid is None:
        return {}
    merged: dict[str, dict[str, dict]] = {}
    for ep in ("topscorers", "topassists"):
        try:
            r = requests.get(f"{API_FOOTBALL}/players/{ep}", params={"league": lid, "season": season},
                             headers={"x-apisports-key": api_key}, timeout=timeout)
            data = r.json()
            if data.get("errors"):
                log.warning("API-Football %s: %s", ep, data["errors"])
                continue
            for team, players in top_players(data, team_names).items():
                for pl in players:
                    merged.setdefault(team, {})[pl["name"]] = pl
        except Exception as e:  # noqa: BLE001
            log.warning("API-Football %s nedostupne: %s", ep, e)
    return {t: sorted(d.values(), key=lambda p: (-p["goals"], -p["assists"])) for t, d in merged.items()}


def build_team_report(team: str, display: str, df: pd.DataFrame, as_of: date, venue: str, opponent: str | None = None,
                      absences: list[Absence] | None = None, players: list[dict] | None = None,
                      news: list[str] | None = None, rumours: list[str] | None = None, absence_checked: bool = False) -> dict:
    """Analyza jednoho tymu. `venue` = kde hraje tento zapas ("home"/"away")."""
    sections: list[dict] = []
    missing: list[str] = []
    form = _rows(df, team, as_of, 6)
    if form:
        sections.append({"title": f"Forma (všechny soutěže v datech): {_summary(form)}", "lines": [_fmt(r) for r in form]})
    else:
        missing.append("forma (chybí výsledky týmu)")
    for v, label in (("home", "Doma"), ("away", "Venku")):
        rows = _rows(df, team, as_of, 10, venue=v)
        if rows:
            mark = " ← hraje tento zápas" if v == venue else ""
            sections.append({"title": f"{label}{mark}: {_summary(rows)}", "lines": [style_label(rows)]})
    others = []
    for lg in sorted(set(df["league"])):
        rows = _rows(df, team, as_of, 5, league=lg)
        if rows and lg in cups.CUP_API_FOOTBALL_IDS:
            others.append({"title": f"{COMP_NAMES.get(lg, lg)} (posledních {len(rows)}): {_summary(rows)}", "lines": [_fmt(r) for r in rows]})
    sections.extend(others)
    if opponent:
        h2h = _rows(df[((df["home"] == team) & (df["away"] == opponent)) | ((df["home"] == opponent) & (df["away"] == team))],
                    team, as_of, 7)
        if h2h:
            sections.append({"title": f"H2H proti {opponent} (v datech, max. 7): {_summary(h2h)}", "lines": [_fmt(r) for r in h2h]})
        else:
            missing.append("H2H (žádný společný zápas v datech)")
    if absence_checked:
        lines = [a.line() for a in (absences or [])] or ["žádné nahlášené absence"]
        sections.append({"title": "Zranění a absence", "lines": lines})
    else:
        missing.append("zranění (zdroj nedostupný nebo nenastavený)")
    if players:
        sections.append({"title": "Střelci a klíčoví hráči týmu", "lines": [
            f"{p['name']}: {p['goals']} gólů, {p['assists']} asistencí ({p['apps']} zápasů)" for p in players[:6]]})
    else:
        missing.append("střelci a hodnota hráčů (API-Football: aktuální sezona vyžaduje placený plán)")
    missing.append("sestavy (potvrzují se až těsně před výkopem)")
    if news:
        sections.append({"title": "Zprávy a sestavy z novin", "lines": news[:4]})
    if rumours:
        sections.append({"title": "Bulvár a spekulace (nepotvrzené)", "lines": rumours[:4]})
    if missing:
        sections.append({"title": "Chybí", "lines": missing})
    return {"team": display, "as_of": as_of.isoformat(), "venue": venue, "sections": sections}


def collect_news(team: str) -> tuple[list[str], list[str]]:
    """(zpravy o sestave/zraneni, bulvar a spekulace) z Google News RSS."""
    return fetch_news(team, "injury lineup")[:4], fetch_news(team, "rumours transfer gossip")[:4]
