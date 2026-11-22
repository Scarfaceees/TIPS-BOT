"""Automaticke vyhodnoceni tiketu z vysledku (fotbal): 1X2, dvojita sance, DNB, over/under, oba skoruji.

Trhy, kterym bot nerozumi (handicap, hraci, poločasy...), zustanou k rucnimu vyhodnoceni.
Zaviraci kurz (CLV) se uklada pri skenech pred vykopem (viz pipeline.update_closing).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date, timedelta

import pandas as pd

from .names import match_team

log = logging.getLogger(__name__)


def _ou_single(total: int, line: float, over: bool) -> str:
    diff = total - line
    if abs(diff) < 1e-9:
        return "void"
    return "won" if (diff > 0) == over else "lost"


_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_SCORE = re.compile(r"^\s*(\d{1,2})\s*[:\-–]\s*(\d{1,2})\s*$")
_BTTS = {"btts", "gg", "oba daji gol", "oba tymy daji gol", "oba skoruji", "both teams to score"}
_DC = {"dc", "dvojita sance", "double chance", "neprohra"}
_1X2 = {"1x2", "vysledek", "zapas", "vitez", "vysledek zapasu"}


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9.,/]+", " ", text).split())


def normalize_bet(market: str, selection: str) -> tuple[str, str]:
    """Rucne zapsany trh/vyber -> tvar, kterému rozumi `grade` (napr. 'under' + '1,5' -> 'OU_1.5', 'under')."""
    if market.strip().upper().startswith(COUNT_MARKETS):  # rohy a karty: linie je v nazvu trhu
        return market.strip().upper(), {"nad": "over", "vic": "over", "pod": "under", "min": "under"}.get(_plain(selection), _plain(selection))
    m, s = _plain(market), _plain(selection)
    words = set(f"{m} {s}".split())
    side = "over" if words & {"over", "nad", "vice"} else "under" if words & {"under", "pod", "mene"} else None
    num = _NUM.search(f"{market} {selection}")
    if side and num and not market.strip().upper().startswith(("TTH_", "TTA_", "AH_")):  # goly jen jednoho tymu
        return f"OU_{float(num.group().replace(',', '.')):g}", side
    if m in _BTTS:
        return "BTTS", {"ano": "yes", "a": "yes", "ne": "no", "n": "no"}.get(s, s)
    if m in _DC:
        return "DC", {"10": "1X", "02": "X2", "1x": "1X", "x2": "X2"}.get(s, s.upper())
    if m in _1X2:
        return "1X2", {"0": "X", "remiza": "X", "domaci": "1", "hoste": "2"}.get(s, s.upper())
    return market.strip(), selection.strip()


def parse_score(text: str) -> tuple[int, int]:
    """'2:1', '2-1' -> (2, 1)."""
    m = _SCORE.match(text or "")
    if not m:
        raise ValueError("Skóre zapiš jako 2:1.")
    return int(m.group(1)), int(m.group(2))


def grade(market: str, selection: str, hg: int, ag: int) -> str | None:
    """Vysledek sazky po 90 minutach; None = trh neumime."""
    market, selection = normalize_bet(market, selection)
    sel = selection.strip()
    low = sel.lower()
    if market == "1X2":
        actual = "1" if hg > ag else "X" if hg == ag else "2"
        return "won" if sel.upper() == actual else "lost" if sel.upper() in ("1", "X", "2") else None
    if market == "DC":
        actual = "1" if hg > ag else "X" if hg == ag else "2"
        covers = {"1X": ("1", "X"), "X2": ("X", "2"), "12": ("1", "2")}.get(sel.upper())
        return None if covers is None else ("won" if actual in covers else "lost")
    if market == "DNB":
        if hg == ag:
            return "void"
        home_wins = hg > ag
        return "won" if (low in ("home", "1") and home_wins) or (low in ("away", "2") and not home_wins) else (
            "lost" if low in ("home", "1", "away", "2") else None)
    if market == "BTTS":
        both = hg > 0 and ag > 0
        return "won" if (low in ("yes", "ano") and both) or (low in ("no", "ne") and not both) else (
            "lost" if low in ("yes", "ano", "no", "ne") else None)
    if market.startswith(("TTH_", "TTA_")) and low in ("over", "under"):  # goly jednoho tymu
        try:
            line = float(market[4:])
        except ValueError:
            return None
        return _ou_single(hg if market.startswith("TTH_") else ag, line, low == "over")
    if market.startswith("OU_") and low in ("over", "under"):
        try:
            line = float(market[3:])
        except ValueError:
            return None
        over, total = low == "over", hg + ag
        quarter = abs(line * 4 - round(line * 4)) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9
        if not quarter:
            return _ou_single(total, line, over)
        a, b = _ou_single(total, line - 0.25, over), _ou_single(total, line + 0.25, over)
        pair = {a, b}
        if pair == {"won"}:
            return "won"
        if pair == {"lost"}:
            return "lost"
        if pair == {"won", "void"}:
            return "half_won"
        if pair == {"lost", "void"}:
            return "half_lost"
        return None
    return None


COUNT_MARKETS = ("ROHY_", "KARTY_")
_COUNT_COLUMNS = {"ROHY": ("home_corners", "away_corners"), "KARTY": ("home_cards", "away_cards")}


def grade_count(market: str, selection: str, total: float) -> str | None:
    """Rohy / karty: 'ROHY_9.5' + over/under proti skutecnemu poctu."""
    try:
        line = float(market.split("_", 1)[1])
    except (IndexError, ValueError):
        return None
    return _ou_single(int(total), line, selection == "over") if selection in ("over", "under") else None


def settle_bet(df: pd.DataFrame, match_name: str, match_day, market: str, selection: str) -> tuple[str, str] | None:
    """(skore nebo pocet, vysledek) jedne sazky z vysledku v `df`; None, kdyz zapas (nebo pocet rohu/karet) v datech neni."""
    row = _find_row(df, match_name, match_day)
    if row is None:
        return None
    hg, ag = int(row["home_goals"]), int(row["away_goals"])
    m, s = normalize_bet(market, selection)
    if m.startswith(COUNT_MARKETS):
        hc, ac = _COUNT_COLUMNS[m.split("_", 1)[0]]
        if hc not in row or pd.isna(row[hc]) or pd.isna(row[ac]):
            return None  # zdroj vysledku pocty nema (doplni football-data.co.uk)
        total = row[hc] + row[ac]
        return f"{hg}:{ag}, {int(total)} {'rohů' if m.startswith('ROHY') else 'karet'}", grade_count(m, s, total)
    return f"{hg}:{ag}", grade(market, selection, hg, ag)


def find_result(df: pd.DataFrame, match_name: str, match_day) -> tuple[int, int] | None:
    row = _find_row(df, match_name, match_day)
    return None if row is None else (int(row["home_goals"]), int(row["away_goals"]))


def _find_row(df: pd.DataFrame, match_name: str, match_day):
    """Vysledek zapasu 'Domaci - Hoste' v okoli data (+-1 den), nazvy se paruji tolerantne."""
    if " - " not in match_name:
        return None
    home_n, away_n = (p.strip() for p in match_name.split(" - ", 1))
    known = set(df["home"]) | set(df["away"])
    h, a = match_team(home_n, known), match_team(away_n, known)
    if not h or not a:
        return None
    lo, hi = match_day - timedelta(days=1), match_day + timedelta(days=1)
    m = df[(df["home"] == h) & (df["away"] == a) & (df["date"] >= lo) & (df["date"] <= hi)]
    return None if m.empty else m.iloc[-1]


def settle_open_tickets(ledger, df: pd.DataFrame) -> list[str]:
    """Vyhodnoti otevrene tikety, ktere umi. Vraci zpravy o tom, co se stalo."""
    from sqlalchemy import select

    from .storage import COMBO, TICKET_OPEN, Ticket

    notes = []
    for leg in ledger.open_legs():  # zapasy tiketu AKU
        got = settle_bet(df, leg.match_name, leg.match_day, leg.market, leg.selection)
        if got is None:
            continue
        score, outcome = got
        if outcome is None:
            notes.append(f"Tiket {leg.ticket_id} – {leg.match_name} ({leg.market}:{leg.selection}): trh neumím vyhodnotit, "
                         "udělej to ručně")
            continue
        t = ledger.settle_leg(leg.id, outcome, score)
        notes.append(f"Tiket AKU {leg.ticket_id}: {leg.match_name} {score} -> {outcome}"
                     + (f"; celý tiket {t.status} ({t.profit_loss:+.0f})" if t is not None else ""))
    for t in list(ledger.s.scalars(select(Ticket).where(Ticket.status == TICKET_OPEN, Ticket.market != COMBO))):
        got = settle_bet(df, t.match_name, t.match_day, t.market, t.selection)
        if got is None:
            continue
        score, outcome = got
        if outcome is None:
            notes.append(f"Tiket {t.id} ({t.match_name}, {t.market}:{t.selection}): trh neumím vyhodnotit, udělej to ručně")
            continue
        ledger.settle_ticket(t.id, outcome, fair_closing_odds=t.closing_odds)
        notes.append(f"Tiket {t.id}: {t.match_name} {score} -> {outcome}")
    return notes


API_FOOTBALL = "https://v3.football.api-sports.io"
FRESH_DAYS = 3  # cerstve vysledky z API-Football jen pro zapasy z poslednich dni (starsi doplni football-data.co.uk)
FRESH_AFTER = timedelta(hours=2)  # od vykopu: 90 minut + nastaveni + prestavka
TICKETS_FROM_HOUR = 12  # tiket zna jen den zapasu; drive (UTC) nemuze byt zadny evropsky zapas dohrany


def _pending(ledger, now) -> list[tuple[str, date, str]]:
    """(zapas, den, trh) vsech otevrenych tiketu a sledovanych vyberu, ktere uz mely skoncit (poslednich FRESH_DAYS)."""
    from datetime import datetime

    from sqlalchemy import select

    from . import paper
    from .storage import COMBO, TICKET_OPEN, Ticket

    lo = now - timedelta(days=FRESH_DAYS)
    last = now.date() if now.hour >= TICKETS_FROM_HOUR else now.date() - timedelta(days=1)
    from . import kombi

    out = [(p.name, date.fromisoformat(p.day), p.market) for p in paper.load(ledger)
           if p.result is None and p.league != "NHL" and not p.legs and lo <= datetime.fromisoformat(p.kickoff) <= now - FRESH_AFTER]
    out += [(x["name"], date.fromisoformat(x["day"]), x["market"]) for x in kombi.open_legs(ledger)
            if x["league"] != "NHL" and lo <= datetime.fromisoformat(x["kickoff"]) <= now - FRESH_AFTER]
    out += [(t.match_name, t.match_day, t.market) for t in ledger.s.scalars(select(Ticket).where(Ticket.status == TICKET_OPEN))
            if t.league != "NHL" and t.market != COMBO and t.match_day and lo.date() <= t.match_day <= last]
    out += [(x.match_name, x.match_day, x.market) for x in ledger.open_legs()
            if x.league != "NHL" and lo.date() <= x.match_day <= last]
    return out


def _api_get(key: str, endpoint: str, params: dict, timeout: int = 30) -> list[dict]:
    import requests

    r = requests.get(f"{API_FOOTBALL}/{endpoint}", headers={"x-apisports-key": key}, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"API-Football: {data['errors']}")
    return data.get("response") or []


def _count_stats(items: list[dict]) -> dict[str, int]:
    """Rohy a karty (zlute + cervene, kazda za 1 – jako u football-data.co.uk) z /fixtures/statistics."""
    teams = [{s.get("type"): s.get("value") for s in t.get("statistics") or []} for t in items[:2]]
    # API uvadi nulovy pocet jako null; statistiky plati, jen kdyz jsou u obou tymu vyplnene (strely, drzeni mice)
    if len(teams) < 2 or not all(any(v is not None for v in st.values()) for st in teams):
        return {}
    out = {}
    for side, st in zip(("home", "away"), teams):
        out[f"{side}_corners"] = int(st.get("Corner Kicks") or 0)
        out[f"{side}_cards"] = int(st.get("Yellow Cards") or 0) + int(st.get("Red Cards") or 0)
    return out


def fresh_results(ledger, now, get=None) -> tuple[pd.DataFrame, list[str]]:
    """Vysledky prave odehranych zapasu z API-Football (football-data.co.uk je dopisuje az za nekolik dni).

    1 pozadavek na den s otevrenymi sazkami (vsechny souteze najednou, jen sledovane ligy a pohary) a u sazek na
    rohy / karty 1 pozadavek na statistiky zapasu. Skore je po 90 minutach (bez prodlouzeni a penalt)."""
    from .cups import FINISHED
    from .profil4.config import P4

    cols = ["date", "home", "away", "home_goals", "away_goals", "home_corners", "away_corners", "home_cards", "away_cards"]
    empty = pd.DataFrame(columns=cols)
    key = ledger.api_football_key()
    pending = _pending(ledger, now)
    if not key or not pending:
        return empty, []
    get = get or (lambda ep, params: _api_get(key, ep, params))
    leagues = {c.league_id for c in P4.competitions}
    rows, ids, notes = [], [], []
    for day in sorted({d for _, d, _ in pending}):
        try:
            items = get("fixtures", {"date": day.isoformat(), "timezone": "UTC"})
        except Exception as e:  # noqa: BLE001 - zbytek doplni football-data.co.uk
            notes.append(f"výsledky z API-Football {day:%d.%m.}: chyba ({type(e).__name__})")
            continue
        for f in items:
            ft = (f.get("score") or {}).get("fulltime") or {}
            if (f.get("league") or {}).get("id") not in leagues or (f.get("fixture") or {}).get("status", {}).get("short") not in FINISHED \
                    or ft.get("home") is None or ft.get("away") is None:
                continue
            rows.append({"date": pd.Timestamp(f["fixture"]["date"]).date(), "home": f["teams"]["home"]["name"],
                         "away": f["teams"]["away"]["name"], "home_goals": int(ft["home"]), "away_goals": int(ft["away"])})
            ids.append(int(f["fixture"]["id"]))
    if not rows:
        return empty, notes
    df = pd.DataFrame(rows).reindex(columns=cols)
    df["fixture_id"] = ids
    for name, day, market in pending:  # rohy a karty jen u zapasu, kde na ne neco ceka
        if not normalize_bet(market, "")[0].startswith(COUNT_MARKETS):
            continue
        row = _find_row(df, name, day)
        if row is None or not pd.isna(row["home_corners"]) or not pd.isna(row["home_cards"]):
            continue
        try:
            stats = _count_stats(get("fixtures/statistics", {"fixture": int(row["fixture_id"])}))
        except Exception as e:  # noqa: BLE001
            notes.append(f"statistiky {name} z API-Football: chyba ({type(e).__name__})")
            continue
        for col, v in stats.items():
            df.loc[row.name, col] = v
    return df, notes


def settle_fresh(ledger, now) -> list[str]:
    """Jen cerstve vysledky z API-Football (bez stahovani CSV): automat to vola i kazdych 15 min mezi pruchody.
    Dotaz jde jen, kdyz neco ceka na vysledek (1 pozadavek na den zapasu)."""
    from . import kombi, paper

    fresh, notes = fresh_results(ledger, now)
    if len(fresh):
        notes += settle_open_tickets(ledger, fresh) + paper.settle(ledger, fresh) + kombi.settle(ledger, fresh)
    return notes


def settle_everything(ledger) -> list[str]:
    """Stahne aktualni vysledky (ligy, poharty, reprezentace) a vyhodnoti tikety i sledovane vybery."""
    import sys

    from . import kombi, paper
    from .cups import DOMESTIC, fetch_cups
    from .ingestion import fetch_football
    from .storage import utcnow
    from .teamreport import load_all_results

    # nejdriv cerstve vysledky z API-Football (zapasy odehrane dnes), samostatne – jine nazvy tymu se nemichaji
    notes = settle_fresh(ledger, utcnow())
    for lg in DOMESTIC:  # aktualni sezona (football-data.co.uk se dopln jednou denne)
        fetch_football(lg, seasons=1)
    fetch_cups(ledger.api_football_key(), ledger.football_data_key(), seasons=1)
    results = load_all_results()
    intl_last = None
    try:  # reprezentace (Liga narodu), vcetne neutralnich zapasu
        from .nations import download_results, load_international

        download_results()
        intl = load_international(neutral=True)
        intl_last = max(intl["date"]) if len(intl) else None
        results = pd.concat([results, intl.reindex(columns=results.columns)], ignore_index=True)
    except Exception as e:  # noqa: BLE001 - bez nich se vyhodnoti aspon kluby
        print(f"Vysledky reprezentaci nejsou k dispozici: {e}", file=sys.stderr)
    done = notes + settle_open_tickets(ledger, results) + paper.settle(ledger, results) + kombi.settle(ledger, results)
    return done if any("->" in n for n in done) else done + [nothing_to_settle(ledger, intl_last)]


def nothing_to_settle(ledger, intl_last=None) -> str:
    """Proc se nic nevyhodnotilo: kolik veci ceka a do kdy jsou vysledky v datech."""
    from sqlalchemy import func, select

    from . import paper
    from .storage import TICKET_OPEN, Ticket

    n_t = ledger.s.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == TICKET_OPEN)) or 0
    n_p = sum(p.result is None for p in paper.load(ledger))
    if not n_t and not n_p:
        return "Nic k vyhodnocení: žádné otevřené tikety ani sledované výběry (už jsou vyhodnocené)."
    msg = f"Nic se nevyhodnotilo: čeká {n_t} tiketů a {n_p} sledovaných výběrů, jejich výsledky zatím nejsou ve stažených datech."
    if intl_last is not None:
        msg += f" Výsledky reprezentací jsou v datech jen do {intl_last:%d.%m.%Y}."
    return msg + " Skóre můžeš zadat ručně (Zadat výsledek zápasu)."


def settle_with_score(ledger, match_name: str, day: date, hg: int, ag: int) -> list[str]:
    """Rucne zadane skore zapasu vyhodnoti vsechny otevrene tikety i sledovane vybery toho zapasu."""
    from . import paper
    from .czechodds import _en

    if " - " not in match_name:
        raise ValueError("Zápas zapiš jako 'Domácí - Hosté'.")
    home, away = (p.strip() for p in match_name.split(" - ", 1))
    pairs = {(home, away), (_en(home), _en(away))}  # tiket muze byt cesky, sledovany vyber anglicky
    df = pd.DataFrame([{"date": day, "home": h, "away": a, "home_goals": hg, "away_goals": ag} for h, a in pairs])
    notes = settle_open_tickets(ledger, df) + paper.settle(ledger, df)
    if not notes:
        raise ValueError(f"U zápasu {match_name} ({day:%d.%m.%Y}) není nic otevřeného.")
    return notes
