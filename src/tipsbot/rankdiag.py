"""Faze 3.2.2 – diagnostika Opportunity Rankingu Profilu 4 (jen cteni, vahy se NEMENI).

Oddeluje ctyri veci, ktere se v rankingu michaji: kvalitu dat, samotny ranking, EV a skutecny vysledek.
Audit 3.2.1: ranking koreluje s kvalitou dat 0,70, s EV jen 0,10 – je proto potreba zjistit, jestli ranking pridava
neco NAD kvalitu dat. K tomu slouzi „zbytek rankingu“: ranking minus to, co z nej vysvetli samotna kvalita dat
(linearni regrese). Kdyz ma vysoky zbytek lepsi vysledek nez nizky, ranking pridava informaci nad kvalitu dat.

Zaver o pridane hodnote se dela az od 100 NEZAVISLYCH zapasu (MIN_MATCHES); do te doby MÁLO DAT bez ohledu na
cisla. Intervaly = bootstrap po zapasech. Vstupni radky jsou stejne jako v Ranking Performance (posledni
predzapasovy ranking aktualni verze + vysledek).
"""

from __future__ import annotations

import math
import sqlite3
from pathlib import Path

import numpy as np

MIN_MATCHES = 100
QUALITY_BANDS = ((0, 50, "kvalita 0–49"), (50, 70, "kvalita 50–69"), (70, 101, "kvalita 70–100"))
EV_BANDS = ((-9, 0, "EV < 0"), (0, 0.05, "EV 0–5 %"), (0.05, 0.15, "EV 5–15 %"), (0.15, 99, "EV > 15 %"))


def load(p4_path: Path | None) -> list[dict]:
    """Radky rankingu s vysledkem z databaze Profilu 4 (jen pro cteni)."""
    if p4_path is None or not Path(p4_path).exists():
        return []
    from .export import _ReadOnly
    from .profil4 import intelligence as IG
    from .profil4 import performance as PF

    con = sqlite3.connect(f"file:{Path(p4_path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        ro = _ReadOnly(con)
        preds, tips = IG.load_rows(ro.query)
        return PF.ranking_rows(IG.load_rankings(ro.query), preds, tips)
    except sqlite3.DatabaseError:
        return []
    finally:
        con.close()


def _band(rows: list[dict], name: str) -> dict:
    from .metrics import is_staked
    from .profil4.history import cluster_ci

    st = [r for r in rows if is_staked(r.get("result"))]
    cl: dict = {}
    for r in st:
        c = cl.setdefault(r["fixture_id"], [0.0, 0.0])
        c[0] += r["profit"]
        c[1] += 1.0
    y, lo, hi = cluster_ci({k: tuple(v) for k, v in cl.items()}) if cl else (math.nan,) * 3
    m = len(cl)
    return {"band": name, "selections": len(rows), "staked": len(st), "matches": m,
            "profit": sum(r["profit"] for r in st), "yield": None if math.isnan(y) else y,
            "yield_ci": None if m < 30 or math.isnan(lo) else (lo, hi),
            "avg_rank": _avg(rows, "rank"), "avg_quality": _avg(rows, "quality"), "avg_ev": _avg(rows, "ev")}


def _avg(rows, k):
    xs = [r[k] for r in rows if r.get(k) is not None]
    return sum(xs) / len(xs) if xs else None


def _corr(rows, a, b):
    from .profil4.performance import _spearman

    pairs = [(r[a], r[b]) for r in rows if r.get(a) is not None and r.get(b) is not None]
    return _spearman([x for x, _ in pairs], [y for _, y in pairs]) if pairs else None


def diagnose(rows: list[dict]) -> dict:
    """Diagnostika pro jeden vzorek (sport). Nic nemeni, jen popisuje."""
    from .profil4 import intelligence as IG

    matches = len({r["fixture_id"] for r in rows})
    out = {"selections": len(rows), "matches": matches, "min_matches": MIN_MATCHES,
           "verdict": (f"MÁLO DAT – {matches} nezávislých zápasů z potřebných {MIN_MATCHES}; o přidané hodnotě rankingu "
                       "se zatím nerozhoduje" if matches < MIN_MATCHES else
                       "dost dat pro závěr – rozhoduje interval rozdílu vysokého a nízkého zbytku rankingu"),
           "corr": {"ranking × kvalita dat": _corr(rows, "rank", "quality"), "ranking × EV": _corr(rows, "rank", "ev"),
                    "ranking × konz. EV": _corr(rows, "rank", "ev_conservative"),
                    "ranking × zisk": _corr(rows, "rank", "profit"), "kvalita dat × zisk": _corr(rows, "quality", "profit"),
                    "EV × zisk": _corr(rows, "ev", "profit")},
           "by_quality": [_band([r for r in rows if r.get("quality") is not None and lo <= r["quality"] < hi], n)
                          for lo, hi, n in QUALITY_BANDS],
           "by_rank": [_band([r for r in rows if r.get("band") == n], n) for _, _, n in IG.RANK_BANDS],
           "by_ev": [_band([r for r in rows if r.get("ev") is not None and lo <= r["ev"] < hi], n) for lo, hi, n in EV_BANDS],
           "residual": [], "r2_quality": None}
    xy = [r for r in rows if r.get("quality") is not None and r.get("rank") is not None]
    if len(xy) >= 10:
        q = np.array([r["quality"] for r in xy], float)
        k = np.array([r["rank"] for r in xy], float)
        X = np.c_[np.ones(len(q)), q]
        beta = np.linalg.lstsq(X, k, rcond=None)[0]
        res = k - X @ beta
        ss = float(((k - k.mean()) ** 2).sum())
        out["r2_quality"] = 1 - float((res ** 2).sum()) / ss if ss else None
        t1, t2 = np.quantile(res, [1 / 3, 2 / 3])
        for r, e in zip(xy, res):
            r["_res"] = float(e)
        out["residual"] = [_band([r for r in xy if r["_res"] < t1], "zbytek rankingu – nízký (pod kvalitu dat)"),
                           _band([r for r in xy if t1 <= r["_res"] < t2], "zbytek rankingu – střední"),
                           _band([r for r in xy if r["_res"] >= t2], "zbytek rankingu – vysoký (nad kvalitu dat)")]
    return out


def report(p4_path: Path | None) -> dict:
    rows = load(p4_path)
    out = {"all": diagnose(rows), "sports": {}}
    for sp in sorted({r.get("sport") for r in rows if r.get("sport")}):
        out["sports"][sp] = diagnose([r for r in rows if r.get("sport") == sp])
    return out
