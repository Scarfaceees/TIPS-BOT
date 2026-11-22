"""Profil 4 – Faze 3: Intelligence & Calibration.

Nadstavba nad existujicimi modely A–D. NEMENI pravdepodobnosti, EV, settlement ani rozhodnuti APPROVED/WATCH/NO BET –
jen meri, kontroluje a vysvetluje:

- per kandidat (pri analyze): kvalita dat 0–100, kurzova inteligence (podezrele kurzy), rozpad EV po modelech
  a citlivost, Opportunity Ranking 0–100;
- z ulozene historie (bez API pozadavku): kalibrace A–D na SPOLECNEM vzorku (stejne zapasy, trhy a vybery pro
  vsechny modely i trh), kalibracni pasma se slucovanim malych pasem, intervaly ze shlukoveho bootstrapu po zapasech,
  profily soutezi, trhu a kurzovych pasem, vyvoj v case, analyza chyb, audit data leakage a diagnosticky navrh
  ladeni (jen navrh – nasazeni vyzaduje potvrzeni uzivatele).

Fotbal a NHL se vyhodnocuji vzdy zvlast (sport = "hokej" pro soutez NHL).
Kazdy vystup je reprodukovatelny: bootstrap ma pevny seed, vstupem jsou jen ulozena data.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime

import numpy as np

from . import mathcore
from .history import (MIN_MATCHES_CI, _clusters, _ll, cluster_ci, market_type, odds_band, outcome_value, prob_scores,
                      unique_bets, verdict)
from .store import iso, parse_dt

INTEL_KEY = "web_p4_intel"
FOOTBALL, HOCKEY = "fotbal", "hokej"
MODELS = ("A", "B", "C", "D")
CONF_OK, CONF_LIMITED, CONF_LOW = "dostatečný vzorek", "omezený vzorek", "nízká důvěra"


def sport_of(league: str | None) -> str:
    return HOCKEY if (league or "").upper() == "NHL" else FOOTBALL


# ===========================================================================================================
# 1. Per kandidat: kvalita dat, kurzova inteligence, EV, ranking
# ===========================================================================================================

def _cur(quotes, now, odds_cfg):
    from . import odds as O

    return [q for q in quotes if q.status(now, odds_cfg) == O.CURRENT]


def quality_of_data(c, quotes, cov, now, cfg) -> dict:
    """Kvalita vstupnich dat 0–100 s vysvetlenim kazde slozky. Neni to sance na vyhru ani robustnost.

    Chybejici data se nepovazuji za negativni sportovni informaci – snizuji jen jistotu (oznaceno „nejistota“)."""
    from .provider import AVAILABLE, NOT_COVERED, NOT_YET, PARTIAL

    w, q = cfg.intel, c.quote
    parts, uncertain = [], []

    def add(name, weight, frac, text):
        frac = max(0.0, min(1.0, frac))
        parts.append({"name": name, "points": round(weight * frac, 1), "max": weight, "text": text})

    limit = cfg.odds.max_age_api_min if q.source in ("api", "oddsapi") else cfg.odds.max_age_manual_min
    age = q.age_minutes(now)
    add("aktuálnost kurzu", w.q_odds_fresh, 1.0 if age <= limit / 2 else 1.0 - (age - limit / 2) / (1.5 * limit),
        f"kurz {age:.0f} min od aktualizace (limit {limit} min)")
    lag = (q.fetched_at - q.stamp).total_seconds() / 60 if q.source_time else 0.0
    add("zpoždění stažení", w.q_provider_lag, 1.0 if lag <= 60 else 1.0 - (lag - 60) / 300,
        f"staženo {lag:.0f} min po aktualizaci u poskytovatele" if q.source_time else "čas aktualizace u zdroje = čas stažení")
    from .. import profile2

    sels = profile2.selections(c.market) if c.market != "EXPR" else None
    if sels:
        per = {x.selection for x in quotes if x.bookmaker == q.bookmaker and x.market == c.market and x.stamp == q.stamp}
        per.add(c.selection)
        ok = all(s in per for s in sels)
        add("protistrana trhu", w.q_counterside, 1.0 if ok else 0.0,
            "protistrana ověřena ze stejné aktualizace" if ok else "protistrana chybí nebo je z jiné aktualizace")
        if not ok:
            uncertain.append("protistrana trhu neověřena")
    else:
        add("protistrana trhu", w.q_counterside, 0.5, "trh nelze spárovat s protistranou")
    cur = _cur(quotes, now, cfg.odds)
    books = sorted({x.bookmaker for x in cur if x.market == c.market and x.selection == c.selection})
    nb = len(books)
    add("počet kanceláří", w.q_books, min(1.0, nb / 5.0), f"{nb} kanceláří s aktuálním kurzem na tento výběr")
    prices = [x.odds for x in cur if x.market == c.market and x.selection == c.selection]
    if len(prices) >= 3:
        spread = (max(prices) / float(np.median(prices))) - 1.0
        add("konzistence zdrojů", w.q_consistency, 1.0 - max(0.0, spread - 0.03) / 0.12,
            f"nejlepší kurz {spread * 100:+.1f} % proti mediánu {len(prices)} kanceláří")
    else:
        add("konzistence zdrojů", w.q_consistency, 0.5, "málo kanceláří pro porovnání (nejistota)")
        uncertain.append("kurz nelze porovnat s dalšími kancelářemi")
    score_map = {AVAILABLE: 1.0, PARTIAL: 0.5, NOT_YET: 0.6, NOT_COVERED: 0.3}

    def status(name):
        return cov.status(name) if cov is not None else "UNAVAILABLE"

    st = min(score_map.get(status("model_data"), 0.0), score_map.get(status("team_stats"), 0.0)) \
        if cov is not None and "team_stats" in cov.items else score_map.get(status("model_data"), 0.0)
    add("statistiky", w.q_stats, st, f"výsledky pro model: {status('model_data').lower()}")
    lu = status("lineups")
    add("sestavy", w.q_lineups, score_map.get(lu, 0.0), "sestavy potvrzené" if lu == AVAILABLE else "sestavy nepotvrzené")
    if lu != AVAILABLE:
        uncertain.append("sestavy nepotvrzené")
    inj = status("injuries")
    add("absence", w.q_injuries, score_map.get(inj, 0.0), f"absence: {inj.lower()}")
    if inj != AVAILABLE:
        uncertain.append("absence neověřené")
    rest = [status(n) for n in ("fixture", "standings")]
    add("úplnost", w.q_completeness, sum(score_map.get(s, 0.0) for s in rest) / len(rest), "zápas a tabulka")
    total = sum(p["max"] for p in parts)
    score = round(100.0 * sum(p["points"] for p in parts) / total, 0) if total else 0.0
    return {"score": score, "parts": parts, "uncertain": uncertain, "books": books}


def odds_intel(c, quotes, now, cfg) -> dict:
    """Kurzova inteligence: nejlepsi cena, median trhu, ferovy trzni kurz, odchylka a duvody podezreni.

    Jen informace – rozhodnuti dela decision engine (extremni kurz bez potvrzeni APPROVED nedostane uz tam)."""
    from . import odds as O

    w, q = cfg.intel, c.quote
    cur = _cur(quotes, now, cfg.odds)
    same = [x for x in cur if x.market == c.market and x.selection == c.selection]
    prices = [x.odds for x in same]
    med = float(np.median(prices)) if prices else None
    fair = 1.0 / c.p_market if c.p_market else None
    dev = (q.odds / med - 1.0) if med else None
    flags = []
    if dev is not None and len(prices) >= 3 and dev > w.outlier_pct:
        flags.append(f"extrémně odlišný kurz: {dev * 100:+.0f} % nad mediánem {len(prices)} kanceláří")
    st = q.status(now, cfg.odds)
    if st != O.CURRENT:
        flags.append(f"kurz není aktuální ({st.lower()})")
    if c.validation is not None:
        for msg in c.validation[0]:
            flags.append(f"chyba mapování / výpočtu: {msg}")
        for msg in c.validation[1]:
            if "protistran" in msg or "aktualizace" in msg:
                flags.append(f"neúplný trh: {msg}")
            elif "marže" in msg:
                flags.append(msg)
    rules = {x.rules for x in same}
    if len(rules) > 1:
        flags.append("kanceláře uvádějí rozdílná pravidla vyhodnocení: " + "; ".join(sorted(rules)))
    if c.ev_market is not None and c.ev_market > cfg.decision.max_market_edge:
        flags.append(f"kurz o {c.ev_market * 100:+.0f} % nad férovou cenou trhu" + ("" if c.confirmed_by else " a nepotvrzen"))
    if c.ev_c > cfg.decision.suspicious_ev:
        flags.append(f"podezřele vysoké EV {c.ev_c * 100:+.0f} %")
    if c.p_market is not None and c.p.get("C") is not None and abs(c.p["C"] - c.p_market) > w.model_market_gap:
        flags.append(f"nesoulad modelu ({c.p['C'] * 100:.0f} %) a trhu ({c.p_market * 100:.0f} %)")
    if not c.confirmed_by:
        flags.append("kurz nepotvrzen jinou kanceláří")
    return {"best": q.odds, "book": q.bookmaker, "median": med, "fair_market": fair, "dev": dev, "books": len(prices),
            "age_min": round(q.age_minutes(now)), "fetched": iso(q.fetched_at), "updated": iso(q.stamp),
            "flags": flags, "suspicious": any(not f.startswith("kurz nepotvrzen") for f in flags)}


def ev_breakdown(c, ms, cfg) -> dict:
    """EV kazdeho modelu pri dostupnem kurzu (stejny engine mathcore.evaluate), konzervativni EV, EV proti trhu,
    minimalni kurz, rozpad hodnoty (model vs cena) a citlivost EV na zmenu pravdepodobnosti."""
    odds = c.odds
    evs = {"C": c.ev_c, "D": c.ev_d}
    for name, part in (("A", ms.a if ms else None), ("B", ms.b if ms else None)):
        try:
            evs[name] = mathcore.evaluate(c.market, c.selection, part.matrix, odds).ev if part and part.available else None
        except ValueError:
            evs[name] = None
    base = mathcore.evaluate(c.market, c.selection, ms.c.matrix, odds) if ms and ms.c.available else None
    sens = []
    if base is not None:
        for d in (-0.05, -0.03, -0.01, 0.0, 0.01, 0.03, 0.05):
            a = min(max(base.a + d, 0.0), 1.0 - base.b)
            sens.append((d, mathcore.ev_from_ab(a, base.b, odds)))
    return {"models": {k: evs.get(k) for k in MODELS}, "conservative": c.ev_d, "market": c.ev_market,
            "available": c.ev_c, "min_odds": c.min_odds,
            # hodnota z ceny = o kolik je kurz nad ferovou cenou trhu; z modelu = co model pridava navic nad trh
            "value_price": c.ev_market, "value_model": (c.ev_c - c.ev_market) if c.ev_market is not None else None,
            "market_backed": bool(c.market_covered), "sensitivity": sens}


def rank(c, qd: dict, oi: dict, cfg, calib: dict | None = None) -> dict:
    """Opportunity Ranking 0–100: jak zajimava je prilezitost proti ostatnim. NENI pravdepodobnost vyhry.

    Slozky a vahy (config.IntelligenceConfig, experimentalni): konzervativni EV, kvalita dat, robustnost, shoda
    modelu s trhem, aktualnost kurzu, potvrzeni kurzu, rizika a historicka kalibrace typu trhu."""
    w = cfg.intel
    parts = []

    def add(name, weight, frac, text):
        frac = max(0.0, min(1.0, frac))
        parts.append({"name": name, "points": round(weight * frac, 1), "max": weight, "text": text})

    evd = c.ev_d if c.ev_d is not None else w.r_ev_floor
    add("konzervativní EV", w.r_conservative_ev, (evd - w.r_ev_floor) / (w.r_ev_cap - w.r_ev_floor),
        f"konzervativní EV {evd * 100:+.1f} %" if c.ev_d is not None else "konzervativní EV nelze spočítat")
    add("kvalita dat", w.r_data_quality, qd["score"] / 100.0, f"kvalita dat {qd['score']:.0f}/100")
    add("robustnost", w.r_robustness, c.robustness / 100.0, f"robustnost {c.robustness:.0f}/100")
    if c.p_market is not None and c.p.get("C") is not None:
        gap = abs(c.p["C"] - c.p_market)
        add("shoda modelu a trhu", w.r_model_market, 1.0 - gap / 0.15, f"model vs trh {gap * 100:.1f} p.b.")
    else:
        add("shoda modelu a trhu", w.r_model_market, 0.0, "cena trhu neověřena – hodnota stojí jen na modelu")
    limit = cfg.odds.max_age_api_min if c.quote.source in ("api", "oddsapi") else cfg.odds.max_age_manual_min
    add("aktuálnost kurzu", w.r_freshness, 1.0 - oi["age_min"] / max(limit, 1), f"kurz starý {oi['age_min']} min")
    add("potvrzení kurzu", w.r_confirmation, 1.0 if c.confirmed_by else 0.0,
        ("potvrzuje " + ", ".join(c.confirmed_by[:3])) if c.confirmed_by else "jiná kancelář kurz nepotvrzuje")
    nrisk = len(set(c.risks)) + (2 if oi["suspicious"] else 0)
    add("rizika", w.r_risks, 1.0 - nrisk / 4.0, f"{nrisk} rizik/podezření")
    st = (calib or {}).get(market_type(c.market))
    frac = {"průkazně lepší než trh": 1.0, "průkazně horší než trh": 0.0}.get(st, 0.5)
    add("kalibrace typu trhu", w.r_calibration, frac, f"historie trhu {market_type(c.market)}: {st or 'bez dat'}")
    total = sum(p["max"] for p in parts)
    return {"score": round(100.0 * sum(p["points"] for p in parts) / total, 0) if total else 0.0, "parts": parts,
            "note": "pořadí zajímavosti, ne pravděpodobnost výhry; váhy experimentální"}


def annotate(cands, ms, quotes, cov, now, cfg, calib: dict | None = None) -> None:
    """Doplni kandidatum `intel` (po rozhodnuti – na rozhodnuti nema vliv)."""
    for c in cands:
        try:
            qd = quality_of_data(c, quotes, cov, now, cfg)
            oi = odds_intel(c, quotes, now, cfg)
            c.intel = {"quality": qd, "odds": oi, "ev": ev_breakdown(c, ms, cfg), "rank": rank(c, qd, oi, cfg, calib)}
        except Exception as e:  # noqa: BLE001 - diagnostika nesmi shodit analyzu
            c.intel = {"error": str(e)}


def market_calibration(store) -> dict:
    """Stav prukaznosti typu trhu proti trhu (z posledniho prepoctu, bez API)."""
    try:
        raw = json.loads(store.get(INTEL_KEY, "") or "null") or {}
        return {k: v.get("status") for k, v in ((raw.get("sports") or {}).get(FOOTBALL, {}).get("markets") or {}).items()}
    except Exception:  # noqa: BLE001
        return {}


# ===========================================================================================================
# 2. Z ulozene historie: kalibrace na spolecnem vzorku a diagnostika
# ===========================================================================================================

def clean_predictions(rows: list[dict]) -> tuple[list[dict], dict]:
    """Vyhodnocene predikce bez data leakage: jen predzapasove (predicted_at < kickoff), posledni pred zapasem
    pro kazdy (zapas, trh, vyber, model) – opakovane skeny nejsou nezavisle vzorky."""
    latest: dict[tuple, dict] = {}
    dropped = {"po_výkopu": 0, "opakované": 0}
    for r in sorted(rows, key=lambda r: r["predicted_at"] or ""):
        if r.get("outcome") is None:
            continue
        ko, pt = parse_dt(r.get("kickoff")), parse_dt(r.get("predicted_at"))
        if ko is None or pt is None or pt >= ko:
            dropped["po_výkopu"] += 1
            continue
        k = (r["fixture_id"], r["market"], r["selection"], r["model"])
        if k in latest:
            dropped["opakované"] += 1
        latest[k] = r
    return list(latest.values()), dropped


def common_sample(rows: list[dict]) -> list[dict]:
    """Radky (zapas, trh, vyber), kde maji predikci VSECHNY modely A–D i trh bez marze.

    Vraci [{fixture_id, league, kickoff, market, market_type, odds, outcome, p: {A,B,C,D}, p_market}]."""
    by: dict[tuple, dict] = {}
    for r in rows:
        k = (r["fixture_id"], r["market"], r["selection"])
        e = by.setdefault(k, {"fixture_id": r["fixture_id"], "league": r.get("league"), "kickoff": r.get("kickoff"),
                              "match": r.get("match"), "market": r["market"], "selection": r["selection"],
                              "market_type": r.get("market_type") or market_type(r["market"]), "odds": r.get("odds"),
                              "outcome": r["outcome"], "p": {}, "p_market": None})
        e["p"][r["model"]] = r["p"]
        if r.get("p_market") is not None and 0 < r["p_market"] < 1 and r["model"] == "C":
            e["p_market"] = r["p_market"]
    return [e for e in by.values() if all(e["p"].get(m) is not None for m in MODELS) and e["p_market"] is not None]


def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return math.nan, math.nan
    ph = k / n
    den = 1 + z * z / n
    mid = (ph + z * z / (2 * n)) / den
    half = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return max(0.0, mid - half), min(1.0, mid + half)


def calibration_bins(p: list[float], y: list[float], fid: list, min_matches: int, bins: int = 10) -> list[dict]:
    """Pasma 0–10 %, …, 90–100 %; pasmo s mene nez `min_matches` zapasy se slouci se sousednim (mensim) pasmem.
    Interval skutecnosti je Wilsonuv z poctu ZAPASU (konzervativni – radky jednoho zapasu nejsou nezavisle)."""
    edges = list(np.linspace(0, 1, bins + 1))
    groups = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, x in enumerate(p) if lo <= x < hi or (hi == 1.0 and x == 1.0)]
        groups.append({"lo": lo, "hi": hi, "idx": idx})
    groups = [g for g in groups if g["idx"]]

    def nm(g):
        return len({fid[i] for i in g["idx"]})

    while len(groups) > 1:
        small = [i for i, g in enumerate(groups) if nm(g) < min_matches]
        if not small:
            break
        i = min(small, key=lambda i: nm(groups[i]))
        j = i - 1 if i == len(groups) - 1 else i + 1 if i == 0 else (i - 1 if nm(groups[i - 1]) <= nm(groups[i + 1]) else i + 1)
        a, b = sorted((i, j))
        groups[a] = {"lo": groups[a]["lo"], "hi": groups[b]["hi"], "idx": groups[a]["idx"] + groups[b]["idx"]}
        del groups[b]
    out = []
    for g in groups:
        idx, m = g["idx"], nm(g)
        pm, ya = float(np.mean([p[i] for i in idx])), float(np.mean([y[i] for i in idx]))
        lo, hi = wilson(ya * m, m)
        out.append({"bin": f"{g['lo'] * 100:.0f}–{g['hi'] * 100:.0f} %", "n": len(idx), "matches": m, "predicted": pm,
                    "actual": ya, "ci95": (lo, hi), "enough": m >= min_matches,
                    "ok": (lo <= pm <= hi) if m >= min_matches else None})
    return out


def _verdict(lo, hi, m, cfg, **kw) -> str:
    if m < cfg.intel.min_matches_verdict:
        return f"málo dat ({m} záp., rozhoduje se od {cfg.intel.min_matches_verdict})"
    return verdict(lo, hi, m, **kw)


def calibration_status(rs: list[dict], key: str, tol: float, min_matches: int = MIN_MATCHES_CI) -> dict:
    """Stav kalibrace z rozdilu skutecnost − odhad (shlukovy bootstrap po zapasech)."""
    m = len({r["fixture_id"] for r in rs})
    d, lo, hi = cluster_ci(_clusters(rs, lambda r: r["outcome"] - r["p"][key] if key in r["p"] else r["outcome"] - r[key]))
    if m < max(min_matches, MIN_MATCHES_CI) or math.isnan(lo):
        st = f"málo dat ({m} záp.)"
    elif hi < 0:
        st = "průkazně špatně kalibrovaný (přeceňuje šance)"
    elif lo > 0:
        st = "průkazně špatně kalibrovaný (podceňuje šance)"
    elif -tol <= lo and hi <= tol:
        st = "průkazně dobře kalibrovaný"
    else:
        st = "neprůkazné"
    return {"bias": d, "bias_ci95": None if math.isnan(lo) else (lo, hi), "status": st}


def _getp(r, key):
    return r["p_market"] if key == "trh" else r["p"][key]


def model_table(common: list[dict], cfg) -> dict:
    """Metriky A–D a trhu na stejnych radcich + rozdil log loss proti trhu (zaporny = model lepsi)."""
    out = {}
    for key in MODELS + ("trh",):
        ps, ys = [_getp(r, key) for r in common], [r["outcome"] for r in common]
        sc = prob_scores(ps, ys)
        sc["matches"] = len({r["fixture_id"] for r in common})
        _, blo, bhi = cluster_ci(_clusters(common, lambda r: (_getp(r, key) - r["outcome"]) ** 2))
        _, llo, lhi = cluster_ci(_clusters(common, lambda r: _ll(_getp(r, key), r["outcome"])))
        sc["brier_ci95"] = None if math.isnan(blo) else (blo, bhi)
        sc["log_loss_ci95"] = None if math.isnan(llo) else (llo, lhi)
        rows = [{"fixture_id": r["fixture_id"], "outcome": r["outcome"], "p": {"x": _getp(r, key)}} for r in common]
        sc["calibration"] = calibration_status(rows, "x", cfg.intel.well_calibrated_tol, cfg.intel.min_matches_verdict)
        sc["bins"] = calibration_bins(ps, ys, [r["fixture_id"] for r in common], cfg.intel.min_bin_matches)
        if key != "trh":
            d, dlo, dhi = cluster_ci(_clusters(common, lambda r: _ll(r["p"][key], r["outcome"]) - _ll(r["p_market"], r["outcome"])))
            sc["vs_market"] = {"diff": d, "ci95": None if math.isnan(dlo) else (dlo, dhi),
                               "status": _verdict(dlo, dhi, sc["matches"], cfg, pos="průkazně horší než trh",
                                                  neg="průkazně lepší než trh")}
        out[key] = sc
    return out


def _group_profile(common: list[dict], fn, tips: list[dict], tip_fn, cfg) -> dict:
    """Profil skupiny (soutez, typ trhu, kurzove pasmo): metriky modelu C a trhu na spolecnem vzorku + ROI tipu."""
    g: dict[str, list] = defaultdict(list)
    for r in common:
        g[fn(r)].append(r)
    tg: dict[str, list] = defaultdict(list)
    for t in tips:
        tg[tip_fn(t)].append(t)
    out = {}
    for k in sorted(set(g) | set(tg)):
        rs = g.get(k, [])
        m = len({r["fixture_id"] for r in rs})
        e = {"matches": m, "predictions": len(rs), "confidence": CONF_OK if m >= 100 else CONF_LIMITED if m >= 30 else CONF_LOW}
        if rs:
            c = prob_scores([r["p"]["C"] for r in rs], [r["outcome"] for r in rs])
            mk = prob_scores([r["p_market"] for r in rs], [r["outcome"] for r in rs])
            d, dlo, dhi = cluster_ci(_clusters(rs, lambda r: _ll(r["p"]["C"], r["outcome"]) - _ll(r["p_market"], r["outcome"])))
            e.update(brier=c["brier"], log_loss=c["log_loss"], market_brier=mk["brier"], market_log_loss=mk["log_loss"],
                     mean_p=c["mean_p"], rate=c["rate"], vs_market=d, vs_market_ci95=None if math.isnan(dlo) else (dlo, dhi),
                     status=_verdict(dlo, dhi, m, cfg, pos="průkazně horší než trh", neg="průkazně lepší než trh"),
                     calibration=calibration_status(rs, "C", cfg.intel.well_calibrated_tol, cfg.intel.min_matches_verdict)["status"])
            srt = sorted(rs, key=lambda r: r["kickoff"] or "")
            if m >= 20:  # stabilita: prvni vs druha polovina (chronologicky)
                half = len(srt) // 2
                a, b = srt[:half], srt[half:]
                e["stability"] = {"first": prob_scores([r["p"]["C"] for r in a], [r["outcome"] for r in a])["log_loss"],
                                  "second": prob_scores([r["p"]["C"] for r in b], [r["outcome"] for r in b])["log_loss"]}
        ts = [t for t in tg.get(k, []) if t["result"] and t["result"] != mathcore.VOID]
        if ts:
            profit = sum(t["profit"] for t in ts)
            clv = [t["clv"] for t in ts if t.get("clv") is not None]
            e.update(tips=len(ts), profit=profit, yield_=profit / len(ts),
                     clv=float(np.mean(clv)) if clv else None, clv_n=len(clv))
        out[k] = e
    return out


def series(common: list[dict]) -> list[dict]:
    """Vyvoj Brier / log loss po mesicich pro A–D a trh na spolecnem vzorku."""
    g: dict[str, list] = defaultdict(list)
    for r in common:
        g[(r["kickoff"] or "")[:7]].append(r)
    out = []
    for month in sorted(g):
        rs = g[month]
        e = {"month": month, "matches": len({r["fixture_id"] for r in rs}), "n": len(rs)}
        for key in MODELS + ("trh",):
            sc = prob_scores([_getp(r, key) for r in rs], [r["outcome"] for r in rs])
            e[key] = {"brier": sc["brier"], "log_loss": sc["log_loss"]}
        out.append(e)
    return out


def tuning_report(common: list[dict], train_share: float = 0.6) -> dict:
    """Diagnosticky navrh (NE automaticka zmena): chronologicky out-of-sample test smichani modelu C s trhem.

    Trenink = prvnich `train_share` zapasu podle vykopu, test = zbytek (zadne michani v case). Na treninku se najde
    vaha w v p = w·C + (1−w)·trh s nejnizsim log loss; na testu se porovna s C (shlukovy bootstrap po zapasech)."""
    fx = sorted({(r["kickoff"] or "", r["fixture_id"]) for r in common})
    n = len(fx)
    base = {"proposal": "smíchání modelu C s trhem (kalibrace k trhu)", "matches": n,
            "risks": "posun k trhu snižuje počet value příležitostí; malý vzorek může vést k přeučení",
            "requires_confirmation": True}
    if n < 60:
        return {**base, "status": "málo dat", "recommendation": f"nenavrhovat – jen {n} společných zápasů (potřeba ≥ 60)"}
    cut = fx[int(n * train_share)][0]
    train = [r for r in common if (r["kickoff"] or "") < cut]
    test = [r for r in common if (r["kickoff"] or "") >= cut]

    def ll(rs, w):
        return float(np.mean([_ll(w * r["p"]["C"] + (1 - w) * r["p_market"], r["outcome"]) for r in rs]))

    grid = [i / 20 for i in range(21)]
    w = min(grid, key=lambda x: ll(train, x))
    d, lo, hi = cluster_ci(_clusters(test, lambda r: _ll(w * r["p"]["C"] + (1 - w) * r["p_market"], r["outcome"])
                                     - _ll(r["p"]["C"], r["outcome"])))
    m = len({r["fixture_id"] for r in test})
    st = verdict(lo, hi, m, pos="průkazně horší", neg="průkazně lepší")
    rec = (f"kandidát k ručnímu schválení: w = {w:.2f} zlepšil log loss mimo trénink" if st == "průkazně lepší"
           else "neměnit – zlepšení mimo trénink není průkazné")
    return {**base, "status": st, "weight_c": w, "train_matches": n - m, "test_matches": m,
            "before": ll(test, 1.0), "after": ll(test, w), "diff": d, "ci95": None if math.isnan(lo) else (lo, hi),
            "reason": "porovnání log loss modelu C a směsi s trhem na zápasech, které při hledání váhy nebyly použity",
            "recommendation": rec}


def leakage_audit(pred_rows: list[dict], tip_rows: list[dict]) -> dict:
    """Kontroly casove integrity nad ulozenymi daty (bez API)."""
    late_p = sum(1 for r in pred_rows if (parse_dt(r.get("predicted_at")) or datetime.max) >= (parse_dt(r.get("kickoff")) or datetime.min))
    late_t = sum(1 for r in tip_rows if parse_dt(r.get("predicted_at")) and parse_dt(r.get("kickoff"))
                 and parse_dt(r["predicted_at"]) >= parse_dt(r["kickoff"]))
    closing_input = sum(1 for r in tip_rows if parse_dt(r.get("odds_time")) and parse_dt(r.get("kickoff"))
                        and parse_dt(r["odds_time"]) >= parse_dt(r["kickoff"]))
    settled_after = sum(1 for r in pred_rows if r.get("settled_at") and r.get("predicted_at")
                        and parse_dt(r["settled_at"]) and parse_dt(r["settled_at"]) < parse_dt(r["predicted_at"]))
    keys = defaultdict(int)
    for r in pred_rows:
        keys[(r["fixture_id"], r["market"], r["selection"], r["model"], r.get("model_version"))] += 1
    dup = sum(v - 1 for v in keys.values() if v > 1)
    checks = [
        ("předpovědi uložené až po výkopu", late_p, "do kalibrace se nepočítají"),
        ("tipy uložené až po výkopu", late_t, "od 22.11.2026 se neukládají; starší se do výsledků nepočítají"),
        ("kurz tipu z doby po výkopu", closing_input, "závěrečné a živé kurzy slouží jen k CLV, ne jako vstup"),
        ("předpověď změněná po vyhodnocení", settled_after, "vyhodnocená předpověď je neměnná"),
        ("duplicitní předpovědi stejné verze", dup, "opakovaný sken přepisuje jednu předpověď"),
    ]
    return {"checks": [{"name": n, "count": c, "note": t, "ok": c == 0 or n.startswith(("předpovědi uložené", "tipy uložené"))}
                       for n, c, t in checks]}


def diagnose(t: dict, gap: float = 0.10) -> list[str]:
    """Analyza chyb jednoho vyhodnoceneho tipu – jen z ulozenych dat. Pricinu prohry netvrdi, pokud neni dolozena."""
    out = []
    res = t.get("result")
    probs = json.loads(t["probs"]) if t.get("probs") else {}
    p = probs.get("C")
    intel = json.loads(t["intel"]) if t.get("intel") else {}
    if res == mathcore.VOID:
        return ["neplatná sázka (zrušený zápas nebo opravené mapování kurzu) – do výsledků se nepočítá"]
    if (t.get("odds_status") or "") not in ("", "AKTUÁLNÍ"):
        out.append(f"zastaralý kurz při doporučení ({t['odds_status'].lower()})")
    q = (intel.get("quality") or {}).get("score")
    if q is not None and q < 60:
        out.append(f"nedostatečná data (kvalita dat {q:.0f}/100)")
    oi = intel.get("odds") or {}
    if any(f.startswith(("extrémně", "chyba mapování")) for f in oi.get("flags", [])):
        out.append("možná chyba kurzu: " + "; ".join(f for f in oi["flags"] if f.startswith(("extrémně", "chyba mapování"))))
    if p is not None and t.get("p_market") is not None and abs(p - t["p_market"]) > gap:
        out.append(f"rozchod s trhem (model {p * 100:.0f} %, trh {t['p_market'] * 100:.0f} %)")
    if t.get("clv") is not None and t["clv"] < 0:
        out.append(f"trh se po doporučení posunul proti tipu (CLV {t['clv'] * 100:+.1f} %)")
    if res in (mathcore.WON, mathcore.HALF_WON):
        out.insert(0, "výhra – odhad potvrzen výsledkem" + (f" (šance {p * 100:.0f} %)" if p else ""))
    elif res in (mathcore.LOST, mathcore.HALF_LOST):
        out.insert(0, "prohra" + (f" – při odhadu {p * 100:.0f} % je prohra očekávaná v {(1 - p) * 100:.0f} % případů" if p else ""))
        if len(out) == 1:
            out.append("příčina není v datech doložena (jednotlivý výsledek nedokazuje chybu modelu)")
    elif res == mathcore.PUSH:
        out.insert(0, "vrácení vkladu")
    return out


def tip_rows_unique(tip_rows: list[dict]) -> list[dict]:
    return unique_bets(tip_rows)[0]


def opportunities(tip_rows: list[dict], now: datetime, limit: int = 20) -> list[dict]:
    """Aktualni prilezitosti (zapas jeste nezacal) serazene podle Opportunity Rankingu z posledni analyzy."""
    latest: dict[tuple, dict] = {}
    for r in sorted(tip_rows, key=lambda r: (r.get("predicted_at") or "", r.get("id", 0))):
        ko = parse_dt(r.get("kickoff"))
        if ko is None or ko <= now or not r.get("intel"):
            continue
        latest[(r["fixture_id"], r["market"], r["selection"])] = r
    out = []
    for r in latest.values():
        intel = json.loads(r["intel"])
        rk = intel.get("rank") or {}
        out.append({"match": r["match"], "league": r.get("league"), "kickoff": r.get("kickoff"), "label": r.get("label"),
                    "market": r["market"], "odds": r["odds"], "book": r.get("bookmaker"), "ev": r.get("ev"),
                    "ev_conservative": r.get("ev_conservative"), "robustness": r.get("robustness"),
                    "quality": (intel.get("quality") or {}).get("score"), "rank": rk.get("score"),
                    "min_odds": (intel.get("ev") or {}).get("min_odds"), "decision": r.get("decision"),
                    "category": r.get("category"),
                    "reasons": [f"{p['name']}: {p['text']} ({p['points']:.0f}/{p['max']:.0f})" for p in rk.get("parts", [])],
                    "flags": (intel.get("odds") or {}).get("flags", [])})
    out.sort(key=lambda o: -(o["rank"] or 0))
    return out[:limit]


def build(pred_rows: list[dict], tip_rows: list[dict], now: datetime, cfg) -> dict:
    """Kompletni prehled Intelligence & Calibration (fotbal a NHL zvlast). Jen ulozena data, zadne API."""
    if now.tzinfo is None:  # hlavni databaze pocita v UTC bez pasma, Profil 4 s pasmem
        from datetime import timezone

        now = now.replace(tzinfo=timezone.utc)
    clean, dropped = clean_predictions(pred_rows)
    tips = tip_rows_unique(tip_rows)
    sports = {}
    for sport in (FOOTBALL, HOCKEY):
        mine = [r for r in clean if sport_of(r.get("league")) == sport]
        common = common_sample(mine)
        st_tips = [t for t in tips if sport_of(t.get("league")) == sport]
        per_model = {m: len({r["fixture_id"] for r in mine if r["model"] == m}) for m in MODELS}
        sports[sport] = {
            "predictions": len(mine), "matches_any": len({r["fixture_id"] for r in mine}), "per_model_matches": per_model,
            "common_rows": len(common), "common_matches": len({r["fixture_id"] for r in common}),
            "models": model_table(common, cfg) if common else {},
            "competitions": _group_profile(common, lambda r: r["league"] or "?", st_tips, lambda t: t.get("league") or "?", cfg),
            "markets": _group_profile(common, lambda r: r["market_type"], st_tips, lambda t: market_type(t["market"]), cfg),
            "bands": _group_profile(common, lambda r: odds_band(r["odds"]), st_tips, lambda t: odds_band(t["odds"]), cfg),
            "series": series(common), "tuning": tuning_report(common),
        }
    settled = [t for t in tips if t.get("result")]
    errors = [{"match": t["match"], "label": t.get("label") or t["selection"], "odds": t["odds"],
               "p": (json.loads(t["probs"]) if t.get("probs") else {}).get("C"), "p_market": t.get("p_market"),
               "result": t["result"], "decision": t.get("decision"), "sport": sport_of(t.get("league")),
               "kickoff": t.get("kickoff"), "diagnosis": diagnose(t, cfg.intel.model_market_gap)} for t in settled]
    return {"t": iso(now), "sports": sports, "dropped": dropped, "leakage": leakage_audit(pred_rows, tip_rows),
            "opportunities": opportunities(tip_rows, now), "errors": errors[-200:],
            "note": "Jen uložená data (žádné API). Modely i trh se srovnávají na stejných řádcích; intervaly 95 % "
                    "z bootstrapu po zápasech; fotbal a NHL zvlášť."}


def load_rows(query) -> tuple[list[dict], list[dict]]:
    """`query(sql)` -> radky (Store.query nebo sqlite3 jen pro cteni)."""
    preds = [dict(r) for r in query("SELECT * FROM predictions WHERE outcome IS NOT NULL")]
    tips = [dict(r) for r in query("SELECT * FROM tips ORDER BY id")]
    return preds, tips


def store_intel(st, now: datetime, cfg=None) -> dict:
    """Prepocet pro panel (automat po vyhodnoceni / tlacitko). Zapise jen kv `web_p4_intel`, historii nemeni."""
    from .config import P4

    preds, tips = load_rows(st.query)
    out = build(preds, tips, now, cfg or P4)
    st.set(INTEL_KEY, json.dumps(out, ensure_ascii=False, default=str))
    if hasattr(st, "archive"):  # archiv jen pri zmene obsahu (bez casu prepoctu)
        st.archive("p4_intel", "intel", json.dumps({k: v for k, v in out.items() if k != "t"}, ensure_ascii=False, default=str))
    return out


def format_text(out: dict) -> str:
    """Kratky textovy souhrn pro CLI."""
    lines = [f"INTELLIGENCE & CALIBRATION – {out['t']} (jen uložená data, bez API)"]
    for sport, sd in out["sports"].items():
        lines.append("")
        lines.append(f"{sport.upper()}: společný vzorek {sd['common_matches']} zápasů / {sd['common_rows']} výběrů "
                     f"(předpovědí celkem {sd['predictions']})")
        for k, m in sd["models"].items():
            vm = (m.get("vs_market") or {}).get("status", "")
            lines.append(f"  {k:>3}: Brier {m['brier']:.4f}  log loss {m['log_loss']:.4f}  kalibrace: "
                         f"{m['calibration']['status']}" + (f"  proti trhu: {vm}" if vm else ""))
        lines.append(f"  ladění: {sd['tuning']['recommendation']}")
    lines += ["", "KONTROLA ÚNIKU INFORMACÍ:"]
    lines += [f"  {'OK ' if c['ok'] else 'POZOR'} {c['name']}: {c['count']}" for c in out["leakage"]["checks"]]
    lines += ["", f"OPPORTUNITY RANKING (nadcházející): {len(out['opportunities'])}"]
    lines += [f"  {o['rank']:.0f}  {o['match']} – {o['label']} @ {o['odds']:.2f} ({o['decision']})" for o in out["opportunities"][:10]]
    return chr(10).join(lines)
