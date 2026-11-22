"""Profil 4 – Faze 3.2: Settlement Integrity Monitor.

Ke kazdemu vyhodnoceni tipu se uklada auditni zaznam (odkud vysledek je, jak se normalizoval, jakou metodou se
vyhodnotil) a vysledek se nezavisle prepocita VLASTNI implementaci, ktera nevola produkcni funkce
(mathcore.settle, mathcore.unit_profit, nhl_settle.grade):

- fotbal: integrity.outcome (rozklad vkladu po polovinach u ctvrtinovych linii, Faze 3.1) + vlastni prevod na
  vysledek a vlastni vypocet zisku;
- NHL: nhl_check nize – vlastni pravidla (vitez z konecneho skore, 60 minut ze skore po 3. tretine, prodlouzeni
  podle zpusobu konce zapasu, handicapy a souctove linie vcetne ctvrtin).

Pri neshode se zaznam oznaci INTEGRITY MISMATCH s duvodem. Produkcni vysledek se NEPREPISUJE (settlement se
nemeni), jen se nevydava za overeny. Historicky audit (audit_history) prepocita vsechna ulozena vyhodnoceni a
zapise jen souhrn behu do settlement_audit_runs – tipy ani predikce nikdy nemeni.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from . import integrity as IN
from .store import Store, iso, utcnow

MATCH, MISMATCH, UNVERIFIABLE = "SHODA", "INTEGRITY MISMATCH", "NEOVĚŘITELNÉ"
CHECK_VERSION = "audit-1 (Fáze 3.2, 22.11.2026)"
PROD_METHOD = {"fotbal": ("mathcore.settle – skóre po 90 minutách (bez prodloužení)", "p4-settle-1"),
               "hokej": ("nhl_settle.grade – konečné skóre / 60 minut podle trhu", "nhl-grade-1")}
SOURCE = {"fotbal": "API-Football /fixtures (score.fulltime)", "hokej": "NHL API api-web.nhle.com/v1/score"}
UNITS = "j. (virtuální jednotka, 1 j. vklad)"
_EPS = 1e-9
# vysledky Profilu 4: void = neplatna sazka (zruseny zapas, opravene mapovani), push = vraceni vkladu
WON, HALF_WON, PUSH, HALF_LOST, LOST, VOID = "won", "half_won", "push", "half_lost", "lost", "void"


# ---------------------------------------------------------------------------------------------------------------
# Nezavisly vypocet
# ---------------------------------------------------------------------------------------------------------------

def _from_parts(parts: list[tuple[float, str]], odds: float) -> tuple[str, float]:
    """[(podil vkladu, win|push|lose)] -> (vysledek, zisk na 1 j.). Vlastni prevod, ne mathcore."""
    profit = 0.0
    win = lose = 0.0
    for share, r in parts:
        if r == "win":
            profit += share * (odds - 1.0)
            win += share
        elif r == "lose":
            profit -= share
            lose += share
    if win >= 1 - _EPS:
        res = WON
    elif lose >= 1 - _EPS:
        res = LOST
    elif win > _EPS and lose > _EPS:  # pul vyhra / pul prohra zaroven neexistuje u linii po 0,25
        raise ValueError("neočekávaný rozklad vkladu")
    elif win > _EPS:
        res = HALF_WON
    elif lose > _EPS:
        res = HALF_LOST
    else:
        res = PUSH
    return res, profit


def football_check(market: str, selection: str, home: int, away: int, odds: float) -> tuple[str, float]:
    """Nezavisly vysledek a zisk fotbaloveho tipu ze skore po 90 minutach. ValueError = trh nelze overit."""
    return _from_parts(IN.outcome(market, selection, home, away), odds)


def _line_parts(line: float) -> list[float]:
    q = line * 4
    if abs(q - round(q)) > 1e-6:
        raise ValueError(f"linie {line} není násobkem 0,25")
    return [line - 0.25, line + 0.25] if round(q) % 2 else [line]


def nhl_check(market: str, selection: str, reg: tuple[int, int] | None, final: tuple[int, int], end: str | None,
              odds: float) -> tuple[str, float]:
    """Nezavisly vysledek NHL. final = konecne skore (prodlouzeni / najezdy vcetne vitezneho golu najezdu),
    reg = skore po 60 minutach (None = nezname), end = REG | OT | SO. ValueError = nelze overit."""
    fh, fa = final
    end = (end or "REG").upper()
    sel = selection.strip()
    kind, _, raw = market.partition("_")

    def need_reg():
        if reg is not None:
            return reg
        if end == "REG":
            return final
        raise ValueError("chybí skóre po 60 minutách")

    if market == "MONEYLINE":
        if sel not in ("home", "away") or fh == fa:
            raise ValueError("vítěz zápasu nelze určit")
        return _from_parts([(1.0, "win" if (fh > fa) == (sel == "home") else "lose")], odds)
    if market == "REG":
        rh, ra = need_reg()
        res = "1" if rh > ra else "2" if ra > rh else "X"
        if sel.upper() not in ("1", "X", "2"):
            raise ValueError(sel)
        return _from_parts([(1.0, "win" if sel.upper() == res else "lose")], odds)
    if market == "OT":  # zapas pokracoval po 60 minutach – podle zpusobu konce, ne podle skore
        if sel not in ("yes", "no"):
            raise ValueError(sel)
        return _from_parts([(1.0, "win" if (end in ("OT", "SO")) == (sel == "yes") else "lose")], odds)
    if kind == "PUCKLINE":
        m = re.fullmatch(r"(home|away)([+-]\d+(?:\.\d+)?)", sel)
        if not m:
            raise ValueError(sel)
        side, hcp = m.group(1), float(m.group(2))
        margin = (fh - fa) if side == "home" else (fa - fh)
        return _from_parts([(1 / len(p), "win" if margin + l > _EPS else "lose" if margin + l < -_EPS else "push")
                            for p in [_line_parts(hcp)] for l in p], odds)
    if kind in ("OU", "OU60", "TTH", "TTA"):
        if sel not in ("over", "under"):
            raise ValueError(sel)
        line = float(raw)
        if kind == "OU60":
            rh, ra = need_reg()
            total = rh + ra
        else:
            total = {"OU": fh + fa, "TTH": fh, "TTA": fa}[kind]
        parts = _line_parts(line)
        out = []
        for l in parts:
            d = (total - l) if sel == "over" else (l - total)
            out.append((1 / len(parts), "win" if d > _EPS else "lose" if d < -_EPS else "push"))
        return _from_parts(out, odds)
    raise ValueError(f"trh {market} nelze nezávisle ověřit")


def line_of(market: str) -> float | None:
    _, _, raw = market.partition("_")
    try:
        return float(raw.rstrip("R")) if raw else None
    except ValueError:
        return None


def compare(result: str, profit: float | None, check: tuple[str, float] | None, why: str = "") -> tuple[str, str]:
    """(stav, duvod) – shoda vysledku i zisku na 1e-6."""
    if check is None:
        return UNVERIFIABLE, why or "nezávislý výpočet nelze provést"
    cres, cprofit = check
    reasons = []
    if result != cres:
        if result == VOID and cres == PUSH:
            reasons.append("produkce: void (neplatná sázka), nezávisle: push (vrácení vkladu) – zisk stejný, "
                           "liší se jen třída výsledku")
        else:
            reasons.append(f"výsledek produkce {result}, nezávisle {cres}")
    if profit is None or abs(profit - cprofit) > 1e-6:
        reasons.append(f"zisk produkce {profit}, nezávisle {cprofit:+.4f}")
    return (MISMATCH, "; ".join(reasons)) if reasons else (MATCH, "")


def sport_of(league: str | None) -> str:
    return "hokej" if (league or "").upper() == "NHL" else "fotbal"


# ---------------------------------------------------------------------------------------------------------------
# Zaznam pri vyhodnoceni
# ---------------------------------------------------------------------------------------------------------------

def independent(t, raw: dict | None, sport: str) -> tuple[tuple[str, float] | None, str]:
    """Nezavisly vysledek tipu z puvodnich dat vysledku (ne z produkcniho vysledku)."""
    try:
        if raw is None:
            return None, "chybí původní data výsledku"
        if raw.get("void_status"):
            return (VOID, 0.0), ""
        if sport == "hokej":
            reg = tuple(raw["reg"]) if raw.get("reg") is not None else None
            return nhl_check(t["market"], t["selection"], reg, tuple(raw["final"]), raw.get("end"), t["odds"]), ""
        h, a = raw["ft"]
        return football_check(t["market"], t["selection"], int(h), int(a), t["odds"]), ""
    except (ValueError, KeyError, TypeError) as e:
        return None, f"nelze nezávisle ověřit: {e}"


def record(store: Store, t, result: str, profit: float, normalized: str | None, evidence: dict | None,
           now: datetime) -> str:
    """Auditni zaznam vyhodnoceni tipu (INSERT OR IGNORE – existujici zaznam se nikdy neprepise)."""
    sport = sport_of(t["league"])
    ev = evidence or {}
    raw = ev.get("raw")
    check, why = independent(t, raw, sport)
    status, reason = compare(result, profit, check, why)
    method, version = ev.get("method") or PROD_METHOD[sport][0], ev.get("version") or PROD_METHOD[sport][1]
    store.execute(
        "INSERT OR IGNORE INTO settlement_audit (tip_id, fixture_id, sport, market, selection, line, odds, raw_result, "
        "normalized, result_fetched_at, source, method, algo_version, result, profit, units, settled_at, check_result, "
        "check_profit, check_version, status, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["id"], t["fixture_id"], sport, t["market"], t["selection"], line_of(t["market"]), t["odds"],
         json.dumps(raw, ensure_ascii=False, default=str) if raw is not None else None, normalized,
         ev.get("fetched_at") or iso(now), ev.get("source") or SOURCE[sport], method, version, result, profit, UNITS,
         iso(now), check[0] if check else None, check[1] if check else None, CHECK_VERSION, status, reason))
    return status


def safe_record(store: Store, *args, **kw) -> str | None:
    """Audit nesmi zastavit vyhodnoceni (napr. starsi databaze) – chyba se jen vrati jako None."""
    try:
        return record(store, *args, **kw)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------------------------
# Historicky audit (jen cteni + souhrn behu)
# ---------------------------------------------------------------------------------------------------------------

_SCORE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)(?:\s*\((REG|OT|SO)\b)?", re.I)


def parse_score(score: str | None) -> tuple[int, int, str | None] | None:
    m = _SCORE.match(score or "")
    return (int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper() or None) if m else None


def _check_stored(r: dict, sport: str, raw: dict | None) -> tuple[tuple[str, float] | None, str]:
    if r["result"] == VOID and not r.get("score"):
        return None, "neplatná sázka bez skóre (zrušený zápas / opravené mapování) – ze skóre nelze ověřit"
    if raw is not None:  # puvodni data z doby vyhodnoceni (od Faze 3.2)
        return independent(r, raw, sport)
    sc = parse_score(r.get("score"))
    if sc is None:
        return None, f"chybí nebo neznámé skóre ({r.get('score')!r})"
    h, a, end = sc
    try:
        if sport == "hokej":
            return nhl_check(r["market"], r["selection"], None, (h, a), end or "REG", r.get("odds") or 1.0), ""
        return football_check(r["market"], r["selection"], h, a, r.get("odds") or 1.0), ""
    except ValueError as e:
        return None, f"nelze nezávisle ověřit: {e}"


OUTCOME_OF = {WON: 1.0, HALF_WON: 1.0, LOST: 0.0, HALF_LOST: 0.0, PUSH: None, VOID: None}


def audit_history(store: Store, now: datetime | None = None, persist: bool = True, limit_items: int = 300) -> dict:
    """Znovu overi vsechna ulozena vyhodnoceni tipu a predikci. Nic neopravuje ani neprepisuje."""
    now = now or utcnow()
    try:  # puvodni data vysledku z doby vyhodnoceni (starsi databaze tabulku nemaji)
        raws = {r["tip_id"]: json.loads(r["raw_result"]) for r in store.query(
            "SELECT tip_id, raw_result FROM settlement_audit WHERE raw_result IS NOT NULL")}
    except Exception:  # noqa: BLE001
        raws = {}
    out = {"run_at": iso(now), "check_version": CHECK_VERSION, "groups": {}, "items": [], "unverifiable_reasons": {}}

    def add(kind, sport, status, item=None, why=""):
        g = out["groups"].setdefault(f"{kind} – {sport}", {"checked": 0, MATCH: 0, MISMATCH: 0, UNVERIFIABLE: 0})
        g["checked"] += 1
        g[status] += 1
        if status == UNVERIFIABLE:
            out["unverifiable_reasons"][why] = out["unverifiable_reasons"].get(why, 0) + 1
        if item is not None and status == MISMATCH and len(out["items"]) < limit_items:
            out["items"].append(item)

    for t in store.query("SELECT * FROM tips WHERE result IS NOT NULL ORDER BY id"):
        t = dict(t)
        sport = sport_of(t.get("league"))
        check, why = _check_stored(t, sport, raws.get(t["id"]))
        st, reason = compare(t["result"], t.get("profit"), check, why)
        add("tipy", sport, st, {"kind": "tip", "id": t["id"], "match": t.get("match"), "market": t["market"],
                                "selection": t["selection"], "odds": t.get("odds"), "score": t.get("score"),
                                "result": t["result"], "profit": t.get("profit"), "check": check, "reason": reason}, why)
    for p in store.query("SELECT id, fixture_id, match, league, market, selection, result, outcome, score "
                         "FROM predictions WHERE result IS NOT NULL"):
        p = dict(p)
        sport = sport_of(p.get("league"))
        check, why = _check_stored({**p, "odds": 2.0}, sport, None)  # u predikce se overuje vysledek, ne zisk
        if check is None:
            add("předpovědi", sport, UNVERIFIABLE, why=why)
            continue
        reasons = []
        if p["result"] != check[0] and not (p["result"] == VOID and check[0] == PUSH):
            reasons.append(f"výsledek {p['result']}, nezávisle {check[0]}")
        exp = OUTCOME_OF.get(p["result"], "?")
        if exp == "?" or (exp is None) != (p.get("outcome") is None) or (exp is not None and abs(exp - p["outcome"]) > _EPS):
            reasons.append(f"hodnota pro kalibraci {p.get('outcome')} neodpovídá výsledku {p['result']}")
        add("předpovědi", sport, MISMATCH if reasons else MATCH,
            {"kind": "předpověď", "id": p["id"], "match": p.get("match"), "market": p["market"], "selection": p["selection"],
             "score": p.get("score"), "result": p["result"], "check": check, "reason": "; ".join(reasons)})
    tot = {"checked": 0, MATCH: 0, MISMATCH: 0, UNVERIFIABLE: 0}
    for g in out["groups"].values():
        for k in tot:
            tot[k] += g[k]
    out["total"] = tot
    if persist:
        store.execute("INSERT INTO settlement_audit_runs (run_at, check_version, checked, matched, mismatched, "
                      "unverifiable, detail) VALUES (?,?,?,?,?,?,?)",
                      (out["run_at"], CHECK_VERSION, tot["checked"], tot[MATCH], tot[MISMATCH], tot[UNVERIFIABLE],
                       json.dumps(out, ensure_ascii=False, default=str)))
    return out


def summary(store: Store) -> dict:
    """Stav pro panel: zaznamy pri vyhodnoceni + posledni historicky audit."""
    try:
        live = {r["status"]: r["n"] for r in store.query("SELECT status, COUNT(*) AS n FROM settlement_audit GROUP BY status")}
        mism = [dict(r) for r in store.query("SELECT * FROM settlement_audit WHERE status=? ORDER BY id DESC LIMIT 50",
                                             (MISMATCH,))]
        last = store.query("SELECT * FROM settlement_audit_runs ORDER BY id DESC LIMIT 1")
        no_audit = store.query("SELECT COUNT(*) AS n FROM tips WHERE result IS NOT NULL AND id NOT IN "
                               "(SELECT tip_id FROM settlement_audit WHERE tip_id IS NOT NULL)")[0]["n"]
    except Exception as e:  # noqa: BLE001 - starsi databaze bez tabulek
        return {"error": str(e)}
    run = dict(last[0]) if last else None
    detail = json.loads(run.pop("detail")) if run and run.get("detail") else None
    return {"live": {"checked": sum(live.values()), MATCH: live.get(MATCH, 0), MISMATCH: live.get(MISMATCH, 0),
                     UNVERIFIABLE: live.get(UNVERIFIABLE, 0)},
            "live_mismatches": mism, "settled_without_record": no_audit, "last_run": run,
            "last_run_groups": (detail or {}).get("groups"), "last_run_items": (detail or {}).get("items", [])[:50],
            "last_run_unverifiable": (detail or {}).get("unverifiable_reasons"),
            "note": "Nezávislý přepočet vlastní implementací (ne produkční funkcí). Neshoda se jen označí – výsledek "
                    "se nepřepisuje ani neopravuje. Tipy vyhodnocené před Fází 3.2 nemají záznam z doby vyhodnocení; "
                    "ověřují se jen historickým auditem z uloženého skóre."}
