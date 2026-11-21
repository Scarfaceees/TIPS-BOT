"""Srovnani modelu s trhem v ostrem provozu: pri kazdem skenu se u zapasu ulozi pravdepodobnosti hlavniho trhu
(fotbal 1X2, NHL vitez vc. prodlouzeni) podle modelu a podle trhu; po zapasech se porovna, kdo mel blize k vysledku
(log-loss) a jaka vaha modelu by byla nejlepsi.

Prukaznost: rozdil log-lossu vahy proti samotnemu trhu s 95% intervalem z bootstrapu po zapasech (kazdy zapas je
jedno pozorovani). Pod MIN_MATCHES se o prukaznosti nerozhoduje. Vaha modelu v konfiguraci se NIKDY nemeni
automaticky – vystup je podklad pro rozhodnuti (zmena jen pri prukaznem a dlouhodobem rozdilu).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .settlement import find_result

KEEP = 3000
WEIGHTS = (0.0, 0.1, 0.15, 0.25, 0.5, 0.75, 1.0)
MIN_MATCHES = 30
BOOT_REPS, BOOT_SEED = 2000, 7
STATE_KEY = "model_quality"
MAIN = {"1X2": ("1", "X", "2"), "MONEYLINE": ("home", "away")}


def _key(league: str) -> str:
    return f"model_check:{league}"


def load(ledger, league: str) -> dict[str, dict]:
    raw = ledger._get(_key(league), "")
    return json.loads(raw) if raw else {}


def save(ledger, league: str, data: dict) -> None:
    keep = dict(sorted(data.items(), key=lambda kv: kv[1]["day"])[-KEEP:])
    ledger._set(_key(league), json.dumps(keep, ensure_ascii=False))


def log_predictions(ledger, league: str, cands_by_match: dict, now: datetime) -> int:
    """Ulozi hlavni trh modelu a trhu; u zapasu se prepisuje posledni sken pred vykopem. Vraci pocet zapasu."""
    data = load(ledger, league)
    n = 0
    for mid, cands in cands_by_match.items():
        for market, sels in MAIN.items():
            c1 = {c.selection: c for c in cands if c.market == market}
            if set(c1) == set(sels):
                break
        else:
            continue
        first = c1[sels[0]]
        if first.kickoff <= now or data.get(mid, {}).get("res"):
            continue
        data[mid] = {"name": first.match_name, "day": first.kickoff.date().isoformat(), "t": now.isoformat(),
                     "ko": first.kickoff.isoformat(), "m": market,
                     "p": {k: [round(c1[k].p_model, 4), round(c1[k].p_market, 4)] for k in sels}}
        n += 1
    if n:
        save(ledger, league, data)
    return n


def outcome(rec: dict, hg: int, ag: int) -> str:
    if rec.get("m", "1X2") == "MONEYLINE":
        return "home" if hg > ag else "away"
    return "1" if hg > ag else "X" if hg == ag else "2"


def resolve(data: dict[str, dict], results: pd.DataFrame) -> int:
    """Doplni k zaznamum vysledek (jednou provzdy). Vraci pocet nove vyresenych."""
    n = 0
    for rec in data.values():
        if rec.get("res"):
            continue
        res = find_result(results, rec["name"], datetime.fromisoformat(rec["day"]).date())
        if res is not None:
            rec["res"] = outcome(rec, int(res[0]), int(res[1]))
            n += 1
    return n


def _losses(data: dict[str, dict], weights) -> dict[float, np.ndarray]:
    done = [r for r in data.values() if r.get("res") in r["p"]]
    out = {}
    for w in weights:
        out[w] = np.array([-math.log(max(w * r["p"][r["res"]][0] + (1 - w) * r["p"][r["res"]][1], 1e-9)) for r in done])
    return out


def evaluate(data: dict[str, dict], results: pd.DataFrame | None = None) -> tuple[int, dict[float, float]]:
    """(pocet vyhodnocenych zapasu, log-loss pro jednotlive vahy modelu)."""
    if results is not None:
        resolve(data, results)
    ls = _losses(data, WEIGHTS)
    n = len(ls[WEIGHTS[0]])
    return n, {w: float(v.mean()) for w, v in ls.items() if len(v)}


def compare(data: dict[str, dict], current_weight: float | None = None) -> dict:
    """Log-loss vah modelu a rozdil proti samotnemu trhu s 95% intervalem (bootstrap po zapasech)."""
    weights = sorted(set(WEIGHTS) | ({current_weight} if current_weight is not None else set()))
    ls = _losses(data, weights)
    n = len(ls[0.0])
    out = {"n": n, "stored": len(data), "current": current_weight, "rows": [], "verdict": "", "best": None}
    if not n:
        out["verdict"] = "zatím žádný zápas s výsledkem"
        return out
    rng = np.random.default_rng(BOOT_SEED)
    idx = rng.integers(0, n, size=(BOOT_REPS, n))
    for w in weights:
        d = ls[w] - ls[0.0]
        lo, hi = (np.percentile(d[idx].mean(axis=1), [2.5, 97.5]) if w and n >= MIN_MATCHES else (math.nan, math.nan))
        out["rows"].append({"w": w, "ll": float(ls[w].mean()), "diff": float(d.mean()), "lo": float(lo), "hi": float(hi),
                            "current": w == current_weight})
    best = min(out["rows"], key=lambda r: r["ll"])
    out["best"] = best["w"]
    cur = next((r for r in out["rows"] if r["current"]), None)
    if n < MIN_MATCHES:
        out["verdict"] = f"málo dat ({n} zápasů, průkaznost od {MIN_MATCHES}) – jen orientační"
    elif cur is not None and cur["w"] and cur["hi"] < 0:
        out["verdict"] = "nastavená váha modelu je průkazně lepší než samotný trh"
    elif cur is not None and cur["w"] and cur["lo"] > 0:
        out["verdict"] = "nastavená váha modelu je průkazně HORŠÍ než samotný trh – zvážit nižší váhu"
    else:
        out["verdict"] = "rozdíl proti trhu zatím není průkazný"
    return out


def report(ledger, league: str, results: pd.DataFrame) -> list[str]:
    data = load(ledger, league)
    n, ll = evaluate(data, results)
    if n == 0:
        return [f"{league}: uloženo {len(data)} zápasů, zatím žádný nemá výsledek."]
    lines = [f"{league}: vyhodnoceno {n} z {len(data)} uložených zápasů (log-loss, nižší je lepší)"]
    best = min(ll, key=ll.get)
    for w, v in sorted(ll.items()):
        name = "jen trh" if w == 0 else "jen model" if w == 1 else f"model {w:.0%}"
        lines.append(f"  {name}: {v:.4f}" + ("  ← nejlepší" if w == best else ""))
    if n < 100:
        lines.append(f"  Pozor: {n} zápasů je málo na závěry, orientační údaj.")
    return lines


def nhl_results(days, fetch=None) -> pd.DataFrame:
    """Vysledky NHL (konecne skore vc. prodlouzeni a najezdu) z NHL API ve tvaru fotbalovych vysledku."""
    from . import nhl_settle
    from .names import NHL_ABBR

    fetch = fetch or nhl_settle.fetch_scores
    inv = {v: k for k, v in NHL_ABBR.items()}
    rows = []
    for d in sorted(set(days)):
        try:
            games = nhl_settle.parse_scores(fetch(d))
        except Exception:  # noqa: BLE001 - chybejici den se doplni priste
            continue
        for g in games:
            if g["home"] in inv and g["away"] in inv and g["start"]:
                rows.append({"date": g["start"].date(), "home": inv[g["home"]], "away": inv[g["away"]],
                             "home_goals": g["final"][0], "away_goals": g["final"][1]})
    return pd.DataFrame(rows, columns=["date", "home", "away", "home_goals", "away_goals"])


def update_all(ledger, now: datetime, football_results: pd.DataFrame | None, nhl_fetch=None, settings=None) -> dict:
    """Doplni vysledky ke vsem ulozenym predikcim a ulozi srovnani model vs. trh pro panel (Overeni modelu)."""
    from .config import SETTINGS

    settings = settings or SETTINGS
    out = {"t": now.isoformat(), "leagues": {}}
    for league in settings.odds_api.sport_keys:
        data = load(ledger, league)
        if not data:
            continue
        if league == "NHL":
            open_days = {datetime.fromisoformat(r["day"]).date() + timedelta(days=k) for r in data.values()
                         if not r.get("res") and datetime.fromisoformat(r.get("ko", r["day"])) < now - timedelta(hours=4)
                         for k in (-1, 0)}
            res = nhl_results(open_days, nhl_fetch) if open_days else None
        else:
            res = football_results
        if res is not None and len(res) and resolve(data, res):
            save(ledger, league, data)
        out["leagues"][league] = compare(data, settings.value.league_model_weight.get(league, settings.value.model_weight))
    ledger._set(STATE_KEY, json.dumps(out, ensure_ascii=False))
    return out
