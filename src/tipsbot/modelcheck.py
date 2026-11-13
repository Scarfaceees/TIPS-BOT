"""Srovnani modelu s trhem: pri kazdem skenu se u zapasu ulozi pravdepodobnosti 1X2 modelu a trhu,
po zapasech se porovna, kdo mel blize k vysledku (log-loss) a jaka vaha modelu by byla nejlepsi."""

from __future__ import annotations

import json
import math
from datetime import datetime

import pandas as pd

from .settlement import find_result

KEEP = 3000
WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)


def _key(league: str) -> str:
    return f"model_check:{league}"


def load(ledger, league: str) -> dict[str, dict]:
    raw = ledger._get(_key(league), "")
    return json.loads(raw) if raw else {}


def log_predictions(ledger, league: str, cands_by_match: dict, now: datetime) -> int:
    """Ulozi 1X2 modelu a trhu; u zapasu se prepisuje posledni sken pred vykopem. Vraci pocet zapasu."""
    data = load(ledger, league)
    n = 0
    for mid, cands in cands_by_match.items():
        c1 = {c.selection: c for c in cands if c.market == "1X2"}
        if set(c1) != {"1", "X", "2"} or c1["1"].kickoff <= now:
            continue
        first = c1["1"]
        data[mid] = {"name": first.match_name, "day": first.kickoff.date().isoformat(), "t": now.isoformat(),
                     "p": {k: [round(c1[k].p_model, 4), round(c1[k].p_market, 4)] for k in ("1", "X", "2")}}
        n += 1
    if n:
        keep = dict(sorted(data.items(), key=lambda kv: kv[1]["day"])[-KEEP:])
        ledger._set(_key(league), json.dumps(keep, ensure_ascii=False))
    return n


def evaluate(data: dict[str, dict], results: pd.DataFrame) -> tuple[int, dict[float, float]]:
    """(pocet vyhodnocenych zapasu, log-loss pro jednotlive vahy modelu)."""
    losses: dict[float, list[float]] = {w: [] for w in WEIGHTS}
    for rec in data.values():
        res = find_result(results, rec["name"], datetime.fromisoformat(rec["day"]).date())
        if res is None:
            continue
        actual = "1" if res[0] > res[1] else "X" if res[0] == res[1] else "2"
        pm, pk = rec["p"][actual]
        for w in WEIGHTS:
            losses[w].append(-math.log(max(w * pm + (1 - w) * pk, 1e-9)))
    n = len(losses[WEIGHTS[0]])
    return n, {w: sum(v) / len(v) for w, v in losses.items() if v}


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
