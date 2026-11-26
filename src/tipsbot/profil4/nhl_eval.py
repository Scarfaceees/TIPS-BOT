"""Phase 3.5.1 / M3 – vykaz vyhodnoceni NHL mereni (POUZE CTENI).

Cte jen tabulky nhl_snapshots, nhl_measurements, nhl_results a nhl_result_corrections (Phase 3.5.1 M1/M2).
Databaze se otevira `mode=ro` + `PRAGMA query_only` – zadny zapis, zadna migrace, zadna sit.

Vykazy (kazdy trh MONEYLINE / PUCKLINE / OU zvlast):
- A – cisty model (p_raw, nikdy ukotveny) proti ostre ferove linii ze stejneho snimku;
- B – hybrid 15 % / 85 %: smes hlavniho bota (logit) proti ostremu trhu a smes Profilu 4 (linearni) proti trhu B;
- C – samotny trh jako benchmark (kalibrace, stari trhu).

Metodika (plan vyhodnoceni v1, docs/phase-3-5-1-nhl-measurement-proposal.md kap. 5–7, predem stanoveno):
- jeden radek na zapas x trh: posledni SROVNATELNY snimek pred zacatkem (pravidla S1–S5 z mereni, S6 = vysledek);
- jedna strana trhu (home / home-1.5 / home+1.5 / over), aby se doplnkove pary nepocitaly dvakrat;
- bootstrap po ZAPASECH (cluster = zapas, vsechny linie a trhy zapasu spolu), 2 000 opakovani, seed 7;
- primarni testy: A-MONEYLINE (test vnoreni, koeficient cisteho modelu) a B-MONEYLINE (log loss smes − trh);
  ostatni jsou sekundarni (jen popisne);
- kontrolni body 300 / 600 / 950 srovnatelnych zapasu, hranice O'Brien–Fleming (Lan–DeMets) pro celkove
  alfa 0,05 oboustranne: |z| >= 3,488 / 2,471 / 2,006. Pred bodem 300 je vysledek vzdy NEPRŮKAZNÝ.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

PLAN_VERSION = "plan-v1 (26. 11. 2026)"
TABLES = ("nhl_snapshots", "nhl_measurements", "nhl_results", "nhl_result_corrections")
CHECKPOINTS = (300, 600, 950)
Z_BOUNDS = (3.4878, 2.4715, 2.0063)  # O'Brien–Fleming (Lan–DeMets), t = 300/950, 600/950, 1; alfa 0,05 oboustranne
BOOT_REPS, BOOT_SEED = 2000, 7
MIN_CALIB = 50  # pod tolik bodu se sklon kalibrace a test vnoreni nepocitaji
EPS = 1e-6
SIDE = {"MONEYLINE": "home", "PUCKLINE_1.5": "home-1.5", "PUCKLINE_1.5R": "home+1.5"}  # OU_* -> over
FAMILIES = ("MONEYLINE", "PUCKLINE", "OU")
AGE_BINS = ((0, 60), (60, 180), (180, 360), (360, 10 ** 9))


# ---------------------------------------------------------------------------------------------------------------
# Cteni (jen pro cteni)
# ---------------------------------------------------------------------------------------------------------------

def open_ro(path: str | Path) -> sqlite3.Connection:
    """Databaze jen pro cteni. Neexistujici soubor se NEVYTVORI (mode=ro selze)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"databáze {p} neexistuje")
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def side_of(market: str) -> str:
    return SIDE.get(market, "over")


def family(market: str) -> str:
    return market.split("_")[0]


def effective_results(conn) -> dict[int, tuple[dict | None, str]]:
    """{fixture_id: (vysledek pro nhl_settle.grade, stav)}. Stav: puvodni / opraveno / konflikt (nevyreseny)."""
    out: dict[int, tuple[dict | None, str]] = {}
    for r in conn.execute('SELECT fixture_id, final_h, final_a, reg_h, reg_a, "end" FROM nhl_results'):
        out[r["fixture_id"]] = ({"final": (r["final_h"], r["final_a"]), "reg": (r["reg_h"], r["reg_a"]), "end": r["end"]},
                                "původní")
    last: dict[int, dict[str, int]] = defaultdict(dict)
    fixes: dict[int, dict] = {}
    for r in conn.execute("SELECT id, fixture_id, kind, new FROM nhl_result_corrections ORDER BY id"):
        last[r["fixture_id"]][r["kind"]] = r["id"]
        if r["kind"] == "oprava":
            fixes[r["fixture_id"]] = json.loads(r["new"])
    for fid, kinds in last.items():
        if fid not in out:
            continue
        if kinds.get("konflikt", -1) > kinds.get("oprava", -1):
            out[fid] = (None, "konflikt")  # zdroj hlasi jiny vysledek a nikdo ho neopravil -> neznamy
        elif fid in fixes:
            f = fixes[fid]
            out[fid] = ({"final": tuple(f["final"]), "reg": tuple(f["reg"]), "end": f["end"]}, "opraveno")
    return out


def integrity(conn) -> dict:
    """Kontrola vazby snimek -> radek mereni -> vysledek."""
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "radky_bez_snimku": q("SELECT COUNT(*) FROM nhl_measurements m LEFT JOIN nhl_snapshots s ON s.id = m.snapshot_id "
                              "WHERE s.id IS NULL"),
        "radky_s_jinym_zapasem_nez_snimek": q("SELECT COUNT(*) FROM nhl_measurements m JOIN nhl_snapshots s "
                                              "ON s.id = m.snapshot_id WHERE s.fixture_id != m.fixture_id"),
        "vysledky_bez_snimku": q("SELECT COUNT(*) FROM nhl_results r WHERE NOT EXISTS "
                                 "(SELECT 1 FROM nhl_snapshots s WHERE s.fixture_id = r.fixture_id)"),
        "snimky_bez_radku": q("SELECT COUNT(*) FROM nhl_snapshots s WHERE NOT EXISTS "
                              "(SELECT 1 FROM nhl_measurements m WHERE m.snapshot_id = s.id)"),
        "duplicitni_sken_zapas": q("SELECT COUNT(*) FROM (SELECT scan_id, fixture_id FROM nhl_snapshots "
                                   "GROUP BY scan_id, fixture_id HAVING COUNT(*) > 1)"),
        "duplicitni_radky_snimku": q("SELECT COUNT(*) FROM (SELECT snapshot_id, market, selection FROM nhl_measurements "
                                     "GROUP BY snapshot_id, market, selection HAVING COUNT(*) > 1)"),
    }


# ---------------------------------------------------------------------------------------------------------------
# Vyber vzorku
# ---------------------------------------------------------------------------------------------------------------

def collect(conn, ref: str, now: datetime) -> dict:
    """Vzorek pro referenci `ref` ('sharp' = ostra linie hlavniho bota, 'p4' = trh B Profilu 4).

    Pro kazdy zapas x trh (jedna strana) se vezme POSLEDNI srovnatelny radek pred zacatkem. Vraci polozky po skupinach
    verzi (measure_version, code_rev) a presne duvody vyrazeni."""
    from ..nhl_settle import grade

    comp_col = "comparable_sharp" if ref == "sharp" else "comparable_p4"
    results = effective_results(conn)
    rows = conn.execute(
        "SELECT m.*, s.computed_at, s.kickoff, s.ref_age_min, s.anchored, s.code_rev, s.measure_version, s.scan_id "
        "FROM nhl_measurements m JOIN nhl_snapshots s ON s.id = m.snapshot_id ORDER BY s.computed_at, s.id").fetchall()
    by_key: dict[tuple[int, str], list] = defaultdict(list)
    for r in rows:
        if r["selection"] == side_of(r["market"]):
            by_key[(r["fixture_id"], r["market"])].append(r)
    groups: dict[str, list[dict]] = defaultdict(list)
    excluded: Counter = Counter()
    pending_no_result: set[int] = set()
    snaps_per_key: list[int] = []
    for (fid, mk), seq in by_key.items():
        snaps_per_key.append(len(seq))
        ko = _parse(seq[-1]["kickoff"])
        before = [r for r in seq if _parse(r["computed_at"]) < ko]
        comp = [r for r in before if r[comp_col]]
        if not comp:
            last = (before or seq)[-1]
            reasons = json.loads(last["exclusion"]).get(ref, []) or ["žádný snímek před začátkem zápasu"]
            for why in reasons:
                excluded[why] += 1
            continue
        r = comp[-1]
        res, status = results.get(fid, (None, "chybí"))
        if status == "konflikt":
            excluded["S6: nevyřešený konflikt výsledku"] += 1
            continue
        if res is None:
            excluded["S6: výsledek není uložen"] += 1
            if ko is not None and ko < now - timedelta(hours=12):
                pending_no_result.add(fid)
            continue
        out = grade(mk, r["selection"], res)
        if out not in ("won", "lost"):
            excluded[f"S6: trh vyhodnocen jako {out} (vrácení / neznámý trh)"] += 1
            continue
        first = comp[0]
        groups[f"{r['measure_version']} / kód {r['code_rev'] or 'unknown'}"].append({
            "fid": fid, "family": family(mk), "market": mk, "y": 1.0 if out == "won" else 0.0,
            "p_raw": r["p_raw"], "p_used": r["p_used"], "anchored": r["anchored"],
            "p_mkt_sharp": r["p_mkt_sharp"], "p_mkt_p4": r["p_mkt_p4"],
            "p_blend_main": r["p_blend_main"], "p_blend_p4": r["p_blend_p4"],
            "ref_age": r["ref_age_min"], "result_status": status,
            "first_mkt": first["p_mkt_sharp" if ref == "sharp" else "p_mkt_p4"], "first_raw": first["p_raw"],
            "n_snaps": len(comp),
        })
    return {"groups": dict(groups), "excluded": dict(excluded), "pending_no_result": sorted(pending_no_result),
            "keys": len(by_key), "snapshots_per_key_max": max(snaps_per_key, default=0)}


def market_age_items(conn) -> list[dict]:
    """Benchmark C podle stari trhu: posledni radek pred zacatkem i NESROVNATELNY (jen S1, trh a vysledek)."""
    from ..nhl_settle import grade

    results = effective_results(conn)
    rows = conn.execute("SELECT m.*, s.computed_at, s.kickoff, s.ref_age_min FROM nhl_measurements m "
                        "JOIN nhl_snapshots s ON s.id = m.snapshot_id ORDER BY s.computed_at, s.id").fetchall()
    last: dict[tuple[int, str], sqlite3.Row] = {}
    for r in rows:
        if r["selection"] == side_of(r["market"]) and _parse(r["computed_at"]) < _parse(r["kickoff"]) and r["p_mkt_sharp"] is not None:
            last[(r["fixture_id"], r["market"])] = r
    out = []
    for (fid, mk), r in last.items():
        res, status = results.get(fid, (None, "chybí"))
        g = grade(mk, r["selection"], res) if res else None
        if g in ("won", "lost") and r["ref_age_min"] is not None:
            out.append({"fid": fid, "family": family(mk), "y": 1.0 if g == "won" else 0.0, "p": r["p_mkt_sharp"],
                        "age": r["ref_age_min"]})
    return out


# ---------------------------------------------------------------------------------------------------------------
# Statistika
# ---------------------------------------------------------------------------------------------------------------

def ll(p: float, y: float) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def brier(p: float, y: float) -> float:
    return (p - y) ** 2


def _logit(p: float) -> float:
    p = min(max(p, EPS), 1 - EPS)
    return math.log(p / (1 - p))


def _clusters(items: list[dict]) -> dict[int, list[int]]:
    cl: dict[int, list[int]] = defaultdict(list)
    for i, it in enumerate(items):
        cl[it["fid"]].append(i)
    return cl


def cluster_boot(items: list[dict], stat, reps: int | None = None, seed: int = BOOT_SEED) -> tuple[float, float, float]:
    """(odhad, 2,5 %, 97,5 %) statistiky `stat(seznam polozek)` – prevzorkovani CELYCH zapasu."""
    est = stat(items)
    cl = list(_clusters(items).values())
    if len(cl) < 2 or est is None:
        return est, float("nan"), float("nan")
    reps = reps or BOOT_REPS
    rng = np.random.default_rng(seed)  # vlastni generator, globalni stav se nemeni
    vals = []
    for _ in range(reps):
        pick = rng.integers(0, len(cl), len(cl))
        v = stat([items[i] for k in pick for i in cl[k]])
        if v is not None and math.isfinite(v):
            vals.append(v)
    if len(vals) < reps * 0.9:
        return est, float("nan"), float("nan")
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return est, float(lo), float(hi)


def _boot_se(items, stat, reps=None, seed=BOOT_SEED) -> float:
    cl = list(_clusters(items).values())
    if len(cl) < 2:
        return float("nan")
    reps = reps or BOOT_REPS
    rng = np.random.default_rng(seed)
    vals = [stat([items[i] for k in rng.integers(0, len(cl), len(cl)) for i in cl[k]]) for _ in range(reps)]
    vals = [v for v in vals if v is not None and math.isfinite(v)]
    return float(np.std(vals, ddof=1)) if len(vals) > 1 else float("nan")


def logistic(X: np.ndarray, y: np.ndarray, iters: int = 50) -> np.ndarray | None:
    """Logisticka regrese (IRLS). None = nekonverguje / oddeleni dat."""
    b = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(X @ b)))
        w = p * (1 - p)
        if np.any(w < 1e-10):
            return None
        try:
            step = np.linalg.solve(X.T @ (X * w[:, None]), X.T @ (y - p))
        except np.linalg.LinAlgError:
            return None
        b = b + step
        if np.max(np.abs(step)) < 1e-9:
            return b if np.all(np.abs(b) < 50) else None
    return None


def calibration(items: list[dict], key: str) -> dict:
    bins: dict[int, list] = defaultdict(list)
    for it in items:
        bins[min(int(it[key] * 10), 9)].append((it[key], it["y"]))
    table = [{"koš": f"{b / 10:.1f}–{(b + 1) / 10:.1f}", "n": len(v), "průměr p": float(np.mean([p for p, _ in v])),
              "četnost": float(np.mean([y for _, y in v]))} for b, v in sorted(bins.items())]
    ece = sum(t["n"] * abs(t["průměr p"] - t["četnost"]) for t in table) / max(len(items), 1)
    slope = None
    if len(items) >= MIN_CALIB:
        X = np.array([[1.0, _logit(it[key])] for it in items])
        b = logistic(X, np.array([it["y"] for it in items]))
        slope = None if b is None else {"průsečík": float(b[0]), "sklon": float(b[1])}
    return {"koše": table, "ECE": float(ece), "logistická kalibrace": slope}


def verdict(n_fixtures: int, z: float | None, primary: bool, better: str = "hodnocená pravděpodobnost lepší než trh",
            worse: str = "trh lepší") -> str:
    """z < 0 = hodnocena pravdepodobnost lepsi (nizsi ztrata). Bez dosazeni kontrolniho bodu vzdy NEPRŮKAZNÝ."""
    if not primary:
        return "SEKUNDÁRNÍ – jen popisně, bez rozhodnutí"
    if n_fixtures < CHECKPOINTS[0]:
        return f"NEPRŮKAZNÝ – před 1. kontrolním bodem ({n_fixtures} < {CHECKPOINTS[0]} zápasů)"
    k = max(i for i, c in enumerate(CHECKPOINTS) if n_fixtures >= c)
    if z is None or not math.isfinite(z):
        return f"NEPRŮKAZNÝ – kontrolní bod {CHECKPOINTS[k]}, statistiku nelze spočítat"
    if abs(z) >= Z_BOUNDS[k]:
        return (f"PRŮKAZNÝ ROZDÍL v kontrolním bodě {CHECKPOINTS[k]} (|z| = {abs(z):.2f} ≥ {Z_BOUNDS[k]:.3f}); "
                f"směr: {better if z < 0 else worse}")
    return f"NEPRŮKAZNÝ – kontrolní bod {CHECKPOINTS[k]} (|z| = {abs(z):.2f} < {Z_BOUNDS[k]:.3f})"


def compare(items: list[dict], key: str, ref_key: str, primary: bool = False) -> dict:
    """Parove srovnani p[key] proti p[ref_key] na stejnych polozkach."""
    its = [it for it in items if it.get(key) is not None and it.get(ref_key) is not None]
    nf = len(_clusters(its))
    out = {"výběrů": len(its), "zápasů": nf}
    if not its:
        return {**out, "verdikt": "NEPRŮKAZNÝ – žádná srovnatelná data"}
    for name, f in (("log loss", ll), ("Brier", brier)):
        a = cluster_boot(its, lambda xs, f=f: float(np.mean([f(x[key], x["y"]) for x in xs])))
        b = cluster_boot(its, lambda xs, f=f: float(np.mean([f(x[ref_key], x["y"]) for x in xs])))
        dstat = lambda xs, f=f: float(np.mean([f(x[key], x["y"]) - f(x[ref_key], x["y"]) for x in xs]))  # noqa: E731
        d = cluster_boot(its, dstat)
        se = _boot_se(its, dstat)
        out[name] = {"hodnocená": a, "trh": b, "rozdíl": d, "z": d[0] / se if se and math.isfinite(se) and se > 0 else None}
    out["verdikt"] = verdict(nf, out["log loss"]["z"], primary)
    return out


def encompassing(items: list[dict], key: str, ref_key: str, primary: bool = False) -> dict:
    """Test vnoreni: y ~ 1 + logit(trh) + logit(model). Model pridava informaci, kdyz je koeficient modelu > 0."""
    its = [it for it in items if it.get(key) is not None and it.get(ref_key) is not None]
    nf = len(_clusters(its))
    if len(its) < MIN_CALIB:
        return {"výběrů": len(its), "zápasů": nf, "β_model": None,
                "verdikt": f"NEPRŮKAZNÝ – málo dat pro regresi ({len(its)} < {MIN_CALIB})" if primary else
                "SEKUNDÁRNÍ – málo dat pro regresi"}

    def beta(xs):
        X = np.array([[1.0, _logit(x[ref_key]), _logit(x[key])] for x in xs])
        b = logistic(X, np.array([x["y"] for x in xs]))
        return None if b is None else float(b[2])

    est = cluster_boot(its, beta)
    se = _boot_se(its, beta)
    z = est[0] / se if est[0] is not None and se and math.isfinite(se) and se > 0 else None
    return {"výběrů": len(its), "zápasů": nf, "β_model": est, "z": z,
            "verdikt": verdict(nf, -z if z is not None else None, primary, "čistý model přidává informaci navíc k trhu",
                               "koeficient modelu záporný (model informaci nepřidává)")}


def movement(items: list[dict]) -> dict:
    """Doplnkove: pohybuje se trh od prvniho k poslednimu srovnatelnemu snimku smerem k cistemu modelu?"""
    its = [it for it in items if it["n_snaps"] > 1 and None not in (it["first_raw"], it["first_mkt"], it["p_mkt_sharp"])
           and it["first_mkt"] != it["p_mkt_sharp"]]
    if len(its) < 10:
        return {"zápasů": len(_clusters(its)), "korelace": None, "poznámka": "málo zápasů s více snímky (< 10)"}

    def corr(xs):
        a = [x["first_raw"] - x["first_mkt"] for x in xs]
        b = [x["p_mkt_sharp"] - x["first_mkt"] for x in xs]
        return float(np.corrcoef(a, b)[0, 1]) if np.std(a) > 0 and np.std(b) > 0 else None

    return {"zápasů": len(_clusters(its)), "korelace": cluster_boot(its, corr),
            "poznámka": "poslední snímek není skutečně zavírací (cache ~6 h)"}


def _fam_corr(items: list[dict], key: str, ref_key: str) -> dict:
    """Korelace rozdilu log loss po zapasech mezi MONEYLINE a PUCKLINE (proc se trhy nesmi scitat)."""
    per: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for it in items:
        if it.get(key) is not None and it.get(ref_key) is not None:
            per[it["family"]][it["fid"]].append(ll(it[key], it["y"]) - ll(it[ref_key], it["y"]))
    a, b = per.get("MONEYLINE", {}), per.get("PUCKLINE", {})
    common = sorted(set(a) & set(b))
    if len(common) < 10:
        return {"zápasů": len(common), "korelace": None}
    x, y = [np.mean(a[f]) for f in common], [np.mean(b[f]) for f in common]
    return {"zápasů": len(common), "korelace": float(np.corrcoef(x, y)[0, 1]) if np.std(x) > 0 and np.std(y) > 0 else None}


# ---------------------------------------------------------------------------------------------------------------
# Vykaz
# ---------------------------------------------------------------------------------------------------------------

def report(path: str | Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    conn = open_ro(path)
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in TABLES if t not in have]
        base = {"plán": PLAN_VERSION, "databáze": str(path), "vytvořeno": now.isoformat(timespec="seconds"),
                "kontrolní body": dict(zip(CHECKPOINTS, Z_BOUNDS))}
        if missing:
            return {**base, "stav": f"měření v této databázi není (chybí tabulky {', '.join(missing)}) – M1/M2 nenasazeno"}
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        sharp, p4 = collect(conn, "sharp", now), collect(conn, "p4", now)
        p4_by_group = p4["groups"]
        out = {**base, "stav": "ok", "počty tabulek": counts, "integrita": integrity(conn),
               "vyřazeno (ostrá reference)": sharp["excluded"], "vyřazeno (reference Profilu 4)": p4["excluded"],
               "zápasy bez výsledku po 12 h (výběrové zkreslení)": len(set(sharp["pending_no_result"]) | set(p4["pending_no_result"])),
               "nejvíc srovnatelných snímků na zápas a trh": sharp["snapshots_per_key_max"], "skupiny": {}}
        ages = market_age_items(conn)
        for gname in sorted(set(sharp["groups"]) | set(p4_by_group)):
            its, its4 = sharp["groups"].get(gname, []), p4_by_group.get(gname, [])
            g: dict = {"A – čistý model vs. trh": {}, "B – hybrid 15/85 vs. trh": {}, "C – trh jako benchmark": {}}
            for fam in FAMILIES:
                f_its = [x for x in its if x["family"] == fam]
                f_its4 = [x for x in its4 if x["family"] == fam]
                prim = fam == "MONEYLINE"
                a = {"srovnání log loss / Brier": compare(f_its, "p_raw", "p_mkt_sharp"),
                     "test vnoření (primární)" if prim else "test vnoření": encompassing(f_its, "p_raw", "p_mkt_sharp", prim),
                     "kalibrace čistého modelu": calibration([x for x in f_its if x["p_raw"] is not None], "p_raw"),
                     "pohyb trhu k modelu (doplňkové)": movement(f_its)}
                if fam == "OU":
                    a["poznámka"] = ("čistý (neukotvený) model totalů – systém ho v rozhodování nepoužívá; ukotvené OU "
                                     "(p_used) v hodnocení modelu NENÍ")
                g["A – čistý model vs. trh"][fam] = a
                g["B – hybrid 15/85 vs. trh"][fam] = {
                    "hlavní bot (logit 15/85) vs. ostrý trh" + (" – primární" if prim else ""):
                        compare(f_its, "p_blend_main", "p_mkt_sharp", prim),
                    "Profil 4 (lineární 15/85) vs. trh B": compare(f_its4, "p_blend_p4", "p_mkt_p4"),
                    **({"poznámka": "u OU obsahuje směs ukotvený model (p_used) – hodnotí se výstup systému, ne model"}
                       if fam == "OU" else {})}
                base_rate = (float(np.mean([x["y"] for x in f_its])) if f_its else None)
                c = {"ostrý trh": {"výběrů": len(f_its), "zápasů": len(_clusters(f_its)),
                                   "log loss": cluster_boot(f_its, lambda xs: float(np.mean([ll(x["p_mkt_sharp"], x["y"]) for x in xs])))
                                   if f_its else None,
                                   "Brier": cluster_boot(f_its, lambda xs: float(np.mean([brier(x["p_mkt_sharp"], x["y"]) for x in xs])))
                                   if f_its else None,
                                   "log loss četnosti (bez informace)": ll(base_rate, 0) * (1 - base_rate) + ll(base_rate, 1) * base_rate
                                   if base_rate not in (None, 0.0, 1.0) else None,
                                   "kalibrace": calibration(f_its, "p_mkt_sharp")},
                     "trh B Profilu 4": {"výběrů": len(f_its4), "zápasů": len(_clusters(f_its4)),
                                         "kalibrace": calibration(f_its4, "p_mkt_p4")}}
                g["C – trh jako benchmark"][fam] = c
            g["korelace MONEYLINE × PUCKLINE (A, po zápasech)"] = _fam_corr(its, "p_raw", "p_mkt_sharp")
            out["skupiny"][gname] = g
        age = {}
        for lo, hi in AGE_BINS:
            sel = [x for x in ages if lo <= x["age"] < hi]
            age[f"{lo}–{hi if hi < 10 ** 9 else '∞'} min"] = {
                "výběrů": len(sel), "zápasů": len(_clusters(sel)),
                "log loss": cluster_boot(sel, lambda xs: float(np.mean([ll(x["p"], x["y"]) for x in xs]))) if sel else None}
        out["C – ostrý trh podle stáří (vč. nesrovnatelných)"] = age
        out["ROI / yield"] = ("nelze z měřicích tabulek: rozhodnutí (APPROVED / WATCH) se v nich neukládá. ROI a yield "
                              "sázkové strategie zůstávají v tabulce tips Profilu 4 (mimo rozsah M3) a z malého vzorku "
                              "nejsou důkazem dlouhodobé výkonnosti.")
        out["CLV"] = ("nelze z měřicích tabulek: CLV je vlastnost rozhodnutí (kurz tipu proti zavíracímu kurzu); "
                      "doplňkově je ve výkazu A pohyb trhu k modelu.")
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------------------------------------------

def _f(x, nd=4) -> str:
    if x is None:
        return "—"
    if isinstance(x, tuple):
        est, lo, hi = x
        if est is None:
            return "—"
        return f"{est:.{nd}f} (95% {lo:.{nd}f} až {hi:.{nd}f})" if math.isfinite(lo) else f"{est:.{nd}f} (interval nelze)"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def format_text(rep: dict) -> str:
    L = [f"NHL – výkaz měření ({rep['plán']})", f"databáze: {rep['databáze']} (jen pro čtení)", f"stav: {rep['stav']}"]
    L.append("kontrolní body (zápasy: hranice |z|): " + ", ".join(f"{k}: {v}" for k, v in rep["kontrolní body"].items()))
    if rep["stav"] != "ok":
        return "\n".join(L)
    L.append("tabulky: " + ", ".join(f"{k} {v}" for k, v in rep["počty tabulek"].items()))
    L.append("integrita: " + ", ".join(f"{k} {v}" for k, v in rep["integrita"].items()))
    for key in ("vyřazeno (ostrá reference)", "vyřazeno (reference Profilu 4)"):
        L.append(key + ":" + ("".join(f"\n  - {k}: {v}" for k, v in sorted(rep[key].items())) or " nic"))
    L.append(f"zápasy bez výsledku po 12 h (výběrové zkreslení): {rep['zápasy bez výsledku po 12 h (výběrové zkreslení)']}")
    for gname, g in rep["skupiny"].items():
        L.append(f"\n=== skupina verzí: {gname}" + ("  [VERZE KÓDU NEZNÁMÁ – vykazováno samostatně]" if "unknown" in gname else ""))
        for sec in ("A – čistý model vs. trh", "B – hybrid 15/85 vs. trh"):
            L.append(f"\n{sec}")
            for fam, d in g[sec].items():
                L.append(f"  {fam}")
                for name, v in d.items():
                    if name == "poznámka":
                        L.append(f"    pozn.: {v}")
                    elif "srovnání" in name or "vs." in name:
                        L.append(f"    {name}: {v['zápasů']} zápasů, {v['výběrů']} výběrů")
                        for m in ("log loss", "Brier"):
                            if m in v:
                                L.append(f"      {m}: hodnocená {_f(v[m]['hodnocená'])} | trh {_f(v[m]['trh'])} | "
                                         f"rozdíl {_f(v[m]['rozdíl'])} | z {_f(v[m]['z'], 2)}")
                        L.append(f"      → {v['verdikt']}")
                    elif "vnoření" in name:
                        L.append(f"    {name}: {v['zápasů']} zápasů; β modelu {_f(v.get('β_model'), 3)}; z {_f(v.get('z'), 2)}")
                        L.append(f"      → {v['verdikt']}")
                    elif "kalibrace" in name:
                        L.append(f"    {name}: ECE {_f(v['ECE'])}, logistická {v['logistická kalibrace'] or '— (málo dat)'}")
                        L.append("      koše: " + "; ".join(f"{t['koš']}: n {t['n']}, p {t['průměr p']:.3f}, četnost {t['četnost']:.3f}"
                                                         for t in v["koše"]))
                    elif "pohyb" in name:
                        L.append(f"    {name}: {v['zápasů']} zápasů, korelace {_f(v['korelace'], 3)} ({v['poznámka']})")
        L.append("\nC – trh jako benchmark")
        for fam, d in g["C – trh jako benchmark"].items():
            s = d["ostrý trh"]
            L.append(f"  {fam}: ostrý trh {s['zápasů']} zápasů / {s['výběrů']} výběrů, log loss {_f(s['log loss'])}, "
                     f"Brier {_f(s['Brier'])}, log loss četnosti {_f(s['log loss četnosti (bez informace)'])}, "
                     f"ECE {_f(s['kalibrace']['ECE'])}; trh B: {d['trh B Profilu 4']['zápasů']} zápasů, "
                     f"ECE {_f(d['trh B Profilu 4']['kalibrace']['ECE'])}")
        k = g["korelace MONEYLINE × PUCKLINE (A, po zápasech)"]
        L.append(f"  korelace rozdílu log loss MONEYLINE × PUCKLINE: {_f(k['korelace'], 2)} ({k['zápasů']} zápasů) – trhy se nesčítají")
    L.append("\nC – ostrý trh podle stáří trhu (poslední snímek před začátkem, i nesrovnatelné):")
    for b, v in rep["C – ostrý trh podle stáří (vč. nesrovnatelných)"].items():
        L.append(f"  {b}: {v['zápasů']} zápasů / {v['výběrů']} výběrů, log loss {_f(v['log loss'])}")
    L.append(f"\nROI / yield: {rep['ROI / yield']}")
    L.append(f"CLV: {rep['CLV']}")
    return "\n".join(L)
