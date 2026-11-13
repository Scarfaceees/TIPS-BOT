"""NHL analyza tymu z oficialniho verejneho NHL API (api-web.nhle.com, zdarma, bez klice).

Forma, domaci/venku, unava (back-to-back), H2H, strelci a brankari tymu. Co zdroj nedodal (zakladni brankar
v den zapasu), se uvede v sekci "Chybi" - nic se nedomysli.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import requests

from .context import Absence

log = logging.getLogger(__name__)

NHL_API = "https://api-web.nhle.com/v1"
_PERIOD = {"REG": "", "OT": " po prodl.", "SO": " po nájezdech"}


def _get(path: str, timeout: int = 20) -> dict:
    r = requests.get(f"{NHL_API}/{path}", headers={"User-Agent": "tipsbot"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def parse_games(payload: dict, abbr: str) -> list[dict]:
    """Odehrane zapasy tymu (zakladni cast a playoff) z rozpisu: datum, souper, domaci/venku, skore, jak skoncil."""
    out = []
    for g in payload.get("games", []):
        if g.get("gameType") not in (2, 3) or g.get("gameState") not in ("OFF", "FINAL"):
            continue
        h, a = g["homeTeam"], g["awayTeam"]
        if "score" not in h or "score" not in a:
            continue
        home = h["abbrev"] == abbr
        gf, ga = (h["score"], a["score"]) if home else (a["score"], h["score"])
        out.append({"id": g.get("id"), "date": date.fromisoformat(g["gameDate"]), "opp": a["abbrev"] if home else h["abbrev"], "home": home,
                    "gf": int(gf), "ga": int(ga), "win": gf > ga, "end": (g.get("gameOutcome") or {}).get("lastPeriodType", "REG")})
    return sorted(out, key=lambda r: r["date"])


def fetch_club(abbr: str, season: int) -> dict:
    """Vsechno o tymu: odehrane zapasy (tato a minula sezona), strelci a brankari. Chyba zdroje = prazdne casti."""
    club: dict = {"games": [], "skaters": [], "goalies": []}
    for ep in (f"club-schedule-season/{abbr}/{season}{season + 1}", f"club-schedule-season/{abbr}/{season - 1}{season}"):
        try:
            club["games"] += parse_games(_get(ep), abbr)
        except Exception as e:  # noqa: BLE001
            log.warning("NHL API %s nedostupne: %s", ep, e)
    club["games"].sort(key=lambda r: r["date"])
    club["last_starter"] = _last_starter(club["games"], abbr)
    try:
        stats = _get(f"club-stats/{abbr}/now")
        club["skaters"], club["goalies"] = stats.get("skaters", []), stats.get("goalies", [])
    except Exception as e:  # noqa: BLE001
        log.warning("NHL API club-stats %s nedostupne: %s", abbr, e)
    return club


def _last_starter(games: list[dict], abbr: str) -> tuple[str, date] | None:
    """Kdo chytal v poslednim odehranem zapase tymu (z boxscore); None, kdyz zdroj nedodal."""
    if not games or not games[-1].get("id"):
        return None
    try:
        box = _get(f"gamecenter/{games[-1]['id']}/boxscore")
        side = "homeTeam" if box["homeTeam"]["abbrev"] == abbr else "awayTeam"
        for gk in box["playerByGameStats"][side]["goalies"]:
            if gk.get("starter"):
                return gk["name"]["default"], games[-1]["date"]
    except Exception as e:  # noqa: BLE001
        log.warning("NHL API boxscore %s nedostupny: %s", abbr, e)
    return None


def _name(p: dict) -> str:
    return f"{p['firstName']['default']} {p['lastName']['default']}"


def _res(r: dict) -> str:
    return ("V" if r["win"] else "P") + _PERIOD.get(r["end"], "")


def _fmt(r: dict) -> str:
    return f"{r['date']:%d.%m.%Y} {'D' if r['home'] else 'V'} {r['opp']} {r['gf']}:{r['ga']} ({_res(r)})"


def _summary(rows: list[dict]) -> str:
    if not rows:
        return "bez dat"
    w = sum(r["win"] for r in rows)
    ot = sum((not r["win"]) and r["end"] != "REG" for r in rows)
    gf, ga = sum(r["gf"] for r in rows), sum(r["ga"] for r in rows)
    return f"{w}V-{len(rows) - w}P (z toho {ot}× po prodl./nájezdech), skóre {gf}:{ga}"


def _style(rows: list[dict]) -> str:
    n = len(rows)
    if n < 4:
        return "málo zápasů"
    gf, ga = sum(r["gf"] for r in rows) / n, sum(r["ga"] for r in rows) / n
    over = sum(r["gf"] + r["ga"] > 5.5 for r in rows) / n
    return f"góly {gf:.1f}:{ga:.1f} na zápas, over 5,5 v {over:.0%}"


def rest_info(rows: list[dict], as_of: date) -> list[str]:
    """Odpocinek: dny od posledniho zapasu, back-to-back, zatez za poslednich 7 dni."""
    if not rows:
        return []
    last = rows[-1]["date"]
    gap = (as_of - last).days
    week = sum((as_of - r["date"]).days <= 7 for r in rows)
    lines = [f"poslední zápas {last:%d.%m.}, odpočinek {max(gap - 1, 0)} dní" + (" – BACK-TO-BACK" if gap <= 1 else "")]
    lines.append(f"zápasů za posledních 7 dní: {week}")
    return lines


def build_nhl_report(abbr: str, display: str, club: dict, as_of: date, venue: str, opponent: str | None = None,
                     absences: list[Absence] | None = None, news: list[str] | None = None,
                     rumours: list[str] | None = None, absence_checked: bool = False) -> dict:
    """Analyza jednoho NHL tymu ve stejnem tvaru jako u fotbalu (viz teamreport.build_team_report)."""
    games = [r for r in club.get("games", []) if r["date"] < as_of]
    sections: list[dict] = []
    missing: list[str] = []
    if games:
        form = list(reversed(games[-6:]))
        sections.append({"title": f"Forma (posledních {len(form)}): {_summary(form)}", "lines": [_fmt(r) for r in form]})
        sections.append({"title": "Únava a rozpis", "lines": rest_info(games, as_of)})
    else:
        missing.append("forma (NHL API nevrátilo odehrané zápasy)")
    for v, label in ((True, "Doma"), (False, "Venku")):
        rows = [r for r in games if r["home"] is v][-10:]
        if rows:
            mark = " ← hraje tento zápas" if (venue == "home") == v else ""
            sections.append({"title": f"{label}{mark}: {_summary(rows)}", "lines": [_style(rows)]})
    if opponent:
        h2h = [r for r in games if r["opp"] == opponent][-7:]
        if h2h:
            sections.append({"title": f"H2H proti {opponent} (2 sezóny, max. 7): {_summary(list(reversed(h2h)))}",
                             "lines": [_fmt(r) for r in reversed(h2h)]})
        else:
            missing.append("H2H (žádný společný zápas za 2 sezóny)")
    skaters = sorted(club.get("skaters", []), key=lambda p: (-p.get("points", 0), -p.get("goals", 0)))[:6]
    if skaters:
        sections.append({"title": "Klíčoví hráči (body v sezóně)", "lines": [
            f"{_name(p)} ({p.get('positionCode', '?')}): {p['goals']} G + {p['assists']} A = {p['points']} b. "
            f"({p['gamesPlayed']} zápasů, {p.get('powerPlayGoals', 0)} G v přesilovce)" for p in skaters]})
    else:
        missing.append("střelci a klíčoví hráči")
    goalies = sorted(club.get("goalies", []), key=lambda p: -p.get("gamesStarted", 0))[:2]
    if goalies:
        sections.append({"title": "Brankáři (kdo chytá, se potvrdí až v den zápasu)", "lines": [
            f"{_name(p)}: {p.get('gamesStarted', 0)} startů, úspěšnost {p.get('savePercentage', 0):.3f}, "
            f"průměr {p.get('goalsAgainstAverage', 0):.2f} gólu" for p in goalies]})
    last = club.get("last_starter")
    if last and last[1] < as_of:
        gap = (as_of - last[1]).days
        hint = " – včera, back-to-back: dnes často chytá záložník (nepotvrzeno)" if gap <= 1 else ""
        sections.append({"title": "Kdo chytal naposledy", "lines": [f"{last[0]} ({last[1]:%d.%m.})" + hint]})
    missing.append("základní brankář a sestava na dnešní zápas (potvrdí se těsně před výkopem)")
    if absence_checked:
        lines = [a.line() for a in (absences or [])] or ["žádné nahlášené absence"]
        sections.append({"title": "Zranění a absence", "lines": lines})
    else:
        missing.append("zranění (zdroj nedostupný)")
    if news:
        sections.append({"title": "Zprávy z novin", "lines": news[:4]})
    if rumours:
        sections.append({"title": "Bulvár a spekulace (nepotvrzené)", "lines": rumours[:4]})
    sections.append({"title": "Chybí", "lines": missing})
    return {"team": display, "as_of": as_of.isoformat(), "venue": venue, "sections": sections}
