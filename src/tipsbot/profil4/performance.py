"""Profil 4 – Faze 3.2: Predictive Validation (mereni, ne optimalizace).

Jen z ulozenych dat (predictions, tips, rankings, settlement_audit), zadne API. NIC nemeni: modely A–D, vahy,
ranking, EV, settlement ani rozhodovaci prahy. Vystup slouzi k rucnimu posouzeni, zda modely a Opportunity Ranking
maji vyhodu proti trhu – muze ji potvrdit i vyvratit.

Zasady:
- nezavisla jednotka = ZAPAS: vybery jednoho zapasu jsou korelovane; intervaly z bootstrapu po zapasech;
- fotbal a NHL se nikdy nesmichaji;
- jen predzapasove zaznamy (predikce, tip, ranking ulozeny pred vykopem); zaviraci kurz nikdy neni vstup;
- CLV jen proti skutecnemu zaviracimu kurzu (history.CLV_KINDS); posledni pozorovany / zastaraly kurz ne;
- virtualni jednotky Profilu 4 se nikdy nesecitaji se skutecnymi korunami.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from . import intelligence as IG
from . import mathcore
from .history import (APPROVED, CK_CLOSING, CK_FOREIGN, CK_LAST, CK_LINE, CK_MISSING, CK_STALE, CLV_KINDS, _clusters,
                      _ll, cluster_ci, market_type, odds_band, prob_scores, unique_bets)
from .store import iso, parse_dt

PERF_KEY = "web_p4_perf"
BANKROLL_UNITS = 100.0  # ROI = zisk / vychozi virtualni bankroll 100 j. (stejne jako history.bet_stats)
REPS = 1000
LEVELS = ((500, "SILNĚJŠÍ DATOVÝ ZÁKLAD"), (200, "ROZŠÍŘENÁ VALIDACE"), (100, "PŘEDBĚŽNÁ VALIDACE"), (0, "MÁLO DAT"))
FOOTBALL, HOCKEY = IG.FOOTBALL, IG.HOCKEY
SPORTS = (FOOTBALL, HOCKEY)
METHOD = {
    "jednotka": "1 tip = 1 j. virtuálního vkladu (Profil 4 nesází skutečné peníze).",
    "obrat": "součet vkladů vyhodnocených sázek (výhra, půl výhra, vrácení, půl prohra, prohra); neplatné sázky "
             "(zrušený zápas, zneplatněné mapování) se nepočítají vůbec.",
    "čistý zisk": "výhra = kurz − 1; půl výhra = (kurz − 1) / 2; vrácení = 0 (vklad se vrací); půl prohra = −0,5; "
                  "prohra = −1 (čtvrtinová linie = půl vkladu na každou sousední linii).",
    "yield": "čistý zisk / obrat.",
    "ROI": f"čistý zisk / výchozí virtuální bankroll {BANKROLL_UNITS:.0f} j. (stejně jako Úspěšnost modelů).",
    "win rate": "(výhry + půl výhry) / sázky bez vrácení.",
    "kombinované tikety": "Kč, kurz tiketu = součin kurzů; vrácená noha se z kurzu vyjme, prohraná noha = prohraný tiket "
                          "(hlavní databáze, ne Profil 4).",
    "duplicity": "jeden výběr jednoho zápasu = jedna sázka (první schválený, jinak první platný záznam); tipy uložené "
                 "po výkopu se nepočítají; opakované skeny nevytvářejí nové sázky.",
}


def _f(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def sufficiency(matches: int, n: int | None = None, first: str | None = None, last: str | None = None,
                limits: list[str] | None = None) -> dict:
    """Uroven datoveho zakladu podle poctu NEZAVISLYCH zapasu – sama o sobe neni dukaz vyhody."""
    level = next(name for lim, name in LEVELS if matches >= lim)
    nxt = next((lim for lim, _ in reversed(LEVELS) if lim > matches), None)
    days = None
    if first and last and parse_dt(first) and parse_dt(last):
        days = (parse_dt(last) - parse_dt(first)).days + 1
    why = (f"{matches} nezávislých zápasů" + (f" ({n} výběrů)" if n is not None else "")
           + (f" za {days} dní" if days else "")
           + (f"; další úroveň od {nxt} zápasů" if nxt else "")
           + ". Počet zápasů sám nezaručuje statistickou významnost – rozhoduje interval spolehlivosti.")
    return {"level": level, "matches": matches, "n": n, "first": first, "last": last, "days": days,
            "reason": why, "limits": limits or []}


def _boot(rows: list[dict], stat, reps: int = REPS, seed: int = 11, level: float = 0.95):
    """Bodovy odhad a interval statistiky `stat(rows)` z bootstrapu po ZAPASECH (cele zapasy, korelace zustava)."""
    by: dict = defaultdict(list)
    for r in rows:
        by[r["fixture_id"]].append(r)
    keys = list(by)
    point = stat(rows) if rows else None
    if len(keys) < 2 or point is None:
        return _f(point), None
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(reps):
        samp = [x for i in rng.integers(0, len(keys), len(keys)) for x in by[keys[i]]]
        v = stat(samp)
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            vals.append(v)
    if not vals:
        return _f(point), None
    tail = (1 - level) / 2 * 100
    return _f(point), (float(np.percentile(vals, tail)), float(np.percentile(vals, 100 - tail)))


def _spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 5:
        return None
    rx, ry = np.argsort(np.argsort(x)).astype(float), np.argsort(np.argsort(y)).astype(float)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else None


def _small(m: int) -> bool:
    """Pod 30 zapasy se interval nezobrazuje (percentilovy bootstrap z par zapasu by byl falesne presny)."""
    return m < IG.MIN_MATCHES_CI


def _bet_stats(rows: list[dict], key_profit: str = "profit") -> dict:
    """Spolecne metriky vyhodnocenych sazek (1 j.). Radky: result, odds, profit, fixture_id (+ volitelne p, y, ...)."""
    rs = [r for r in rows if r.get("result") and r["result"] != mathcore.VOID]
    cnt = defaultdict(int)
    for r in rs:
        cnt[r["result"]] += 1
    non_push = len(rs) - cnt[mathcore.PUSH]
    profit = sum(r[key_profit] for r in rs)
    m = len({r["fixture_id"] for r in rs})
    y, ylo, yhi = cluster_ci(_clusters(rs, lambda r: r[key_profit]), reps=REPS) if rs else (math.nan,) * 3
    w, wlo, whi = (cluster_ci(_clusters([r for r in rs if r["result"] != mathcore.PUSH],
                                        lambda r: 1.0 if r["result"] in (mathcore.WON, mathcore.HALF_WON) else 0.0), reps=REPS)
                   if non_push else (math.nan,) * 3)
    if _small(m):
        ylo = wlo = math.nan
    return {"matches": m, "n": len(rs), "won": cnt[mathcore.WON], "half_won": cnt[mathcore.HALF_WON],
            "push": cnt[mathcore.PUSH], "half_lost": cnt[mathcore.HALF_LOST], "lost": cnt[mathcore.LOST],
            "win_rate": _f(w), "win_rate_ci95": None if math.isnan(wlo) else (wlo, whi),
            "avg_odds": _mean([r.get("odds") for r in rs]), "turnover": float(len(rs)), "profit": float(profit),
            "yield": _f(y) if rs else None, "yield_ci95": None if math.isnan(ylo) else (ylo, yhi),
            "roi": float(profit) / BANKROLL_UNITS if rs else None,
            "yield_status": IG.verdict(ylo, yhi, m) if rs else "bez vyhodnocených sázek"}


def _prob_stats(rows: list[dict], pk: str = "p") -> dict:
    rs = [r for r in rows if r.get(pk) is not None and r.get("y") is not None]
    if not rs:
        return {"brier": None, "log_loss": None, "brier_ci95": None, "prob_n": 0}
    sc = prob_scores([r[pk] for r in rs], [r["y"] for r in rs])
    _, blo, bhi = cluster_ci(_clusters(rs, lambda r: (r[pk] - r["y"]) ** 2), reps=REPS)
    _, llo, lhi = cluster_ci(_clusters(rs, lambda r: _ll(r[pk], r["y"])), reps=REPS)
    if _small(len({r["fixture_id"] for r in rs})):
        blo = llo = math.nan
    return {"brier": _f(sc["brier"]), "log_loss": _f(sc["log_loss"]), "prob_n": len(rs),
            "brier_ci95": None if math.isnan(blo) else (blo, bhi), "log_loss_ci95": None if math.isnan(llo) else (llo, lhi)}


def _clv_stats(rows: list[dict]) -> dict:
    rs = [r for r in rows if r.get("clv") is not None]
    if not rs:
        return {"clv_n": 0, "clv": None, "clv_median": None, "clv_pos": None, "clv_ci95": None, "clv_matches": 0}
    c, lo, hi = cluster_ci(_clusters(rs, lambda r: r["clv"]), reps=REPS)
    vals = [r["clv"] for r in rs]
    m = len({r["fixture_id"] for r in rs})
    if _small(m):
        lo = math.nan
    return {"clv_n": len(rs), "clv_matches": m, "clv": _f(c), "clv_median": float(np.median(vals)),
            "clv_pos": sum(v > 1e-9 for v in vals) / len(vals), "clv_neg": sum(v < -1e-9 for v in vals) / len(vals),
            "clv_ci95": None if math.isnan(lo) else (lo, hi), "clv_status": IG.verdict(lo, hi, m)}


# ===========================================================================================================
# 2. Opportunity Ranking – predikcni validace
# ===========================================================================================================

def ranking_rows(rank_rows: list[dict], pred_rows: list[dict], tip_rows: list[dict]) -> list[dict]:
    """Posledni PREDZAPASOVY platny ranking aktualni verze kazdeho vyberu + vysledek (predikce modelu C), trzni
    pravdepodobnost bez marze z doby predikce a CLV jen z overeneho zaviraciho kurzu stejneho vyberu."""
    res = {}
    for r in pred_rows:
        if r.get("model") == "C" and r.get("result"):
            res[(r["fixture_id"], r["market"], r["selection"])] = r
    clv = {}
    for t in unique_bets(tip_rows)[0]:
        if t.get("clv") is not None and (t.get("closing_kind") or "") in CLV_KINDS:
            clv[(t["fixture_id"], t["market"], t["selection"])] = t["clv"]
    out = []
    for key, r in IG._latest_rank(rank_rows).items():
        if r.get("version") != IG.RANK_VERSION or key not in res or r.get("odds") is None:
            continue
        p = res[key]
        if p["result"] == mathcore.VOID:
            continue
        d = json.loads(r["detail"]) if r.get("detail") else {}
        out.append({"fixture_id": r["fixture_id"], "kickoff": r.get("kickoff") or "", "computed_at": r.get("computed_at"),
                    "sport": IG.sport_of(r.get("league")), "league": r.get("league"), "market": r["market"],
                    "market_type": market_type(r["market"]), "selection": r["selection"], "rank": r["rank"],
                    "band": IG.rank_band(r["rank"]), "odds": r["odds"], "book": r.get("bookmaker"),
                    "result": p["result"], "profit": mathcore.unit_profit(p["result"], r["odds"]),
                    "p": (d.get("p") or {}).get("C"), "p_market": p.get("p_market"), "y": p.get("outcome"),
                    "ev": r.get("ev"), "ev_conservative": r.get("ev_conservative"), "robustness": r.get("robustness"),
                    "quality": r.get("quality"), "decision": r.get("decision"), "clv": clv.get(key)})
    return out


def _band_table(rows: list[dict]) -> list[dict]:
    k = len(IG.RANK_BANDS)
    out = []
    for _, _, name in IG.RANK_BANDS:
        mine = [x for x in rows if x["band"] == name]
        e = {"band": name, "selections": len(mine), **_bet_stats(mine), **_prob_stats(mine), **_clv_stats(mine),
             "avg_p": _mean([x["p"] for x in mine]), "avg_p_market": _mean([x["p_market"] for x in mine]),
             "avg_ev": _mean([x["ev"] for x in mine]), "avg_ev_conservative": _mean([x["ev_conservative"] for x in mine]),
             "avg_quality": _mean([x["quality"] for x in mine]), "avg_robustness": _mean([x["robustness"] for x in mine]),
             "approved_share": (sum(x["decision"] == APPROVED for x in mine) / len(mine)) if mine else None}
        # Bonferroni: 4 pasma = 4 srovnani -> interval 98,75 % (snizuje riziko nahodne "nejlepsiho" pasma)
        _, lo, hi = (cluster_ci(_clusters(mine, lambda r: r["profit"]), reps=REPS, level=1 - 0.05 / k)
                     if mine else (math.nan,) * 3)
        e["yield_ci_bonferroni"] = None if math.isnan(lo) or _small(e["matches"]) else (lo, hi)
        e["sufficiency"] = sufficiency(e["matches"], len(mine))
        e["status"] = (e["sufficiency"]["level"] + " – " + ("závěr zatím nelze udělat" if e["matches"] < 100 else
                       IG.verdict(*(e["yield_ci_bonferroni"] or (math.nan, math.nan)), e["matches"],
                                  pos="yield průkazně kladný (i po korekci na 4 pásma)",
                                  neg="yield průkazně záporný (i po korekci na 4 pásma)")))
        out.append(e)
    return out


def added_value(rows: list[dict]) -> dict:
    """Prinasi ranking informaci navic proti EV, konzervativnimu EV, robustnosti, kvalite dat, trhu a rozhodnuti?

    1) Spearmanova korelace rankingu s kazdou promennou (kopiruje-li ranking jednu z nich, |rho| > 0,9).
    2) Stratifikovany test: v tercilech konzervativniho EV se porovna yield vyberu s rankingem nad a pod medianem
       terciu; vazeny prumer rozdilu, interval z bootstrapu po zapasech. Ranking ma pridanou hodnotu jen tehdy,
       kdyz vyssi ranking je lepsi i PRI STEJNEM EV.
    3) Rezidualni test: ma vyssi ranking lepsi vysledek nez trzni pravdepodobnost (y − p_trh)?"""
    m = len({r["fixture_id"] for r in rows})
    corr = {}
    for name, key in (("EV", "ev"), ("konzervativní EV", "ev_conservative"), ("Tip Robustness", "robustness"),
                      ("Quality of Data", "quality"), ("tržní pravděpodobnost", "p_market"), ("rozhodnutí APPROVED", None)):
        pr = [(r["rank"], (1.0 if r["decision"] == APPROVED else 0.0) if key is None else r[key]) for r in rows
              if r["rank"] is not None and (key is None or r[key] is not None)]
        corr[name] = _spearman([a for a, _ in pr], [b for _, b in pr]) if pr else None
    copies = [k for k, v in corr.items() if v is not None and abs(v) > 0.9]

    def strat(rs):
        evs = sorted(r["ev_conservative"] for r in rs if r["ev_conservative"] is not None)
        if len(evs) < 9:
            return None
        cuts = [evs[len(evs) // 3], evs[2 * len(evs) // 3]]
        tot = wsum = 0.0
        for lo, hi in ((-math.inf, cuts[0]), (cuts[0], cuts[1]), (cuts[1], math.inf)):
            g = [r for r in rs if r["ev_conservative"] is not None and lo <= r["ev_conservative"] < hi]
            if len(g) < 4:
                continue
            med = float(np.median([r["rank"] for r in g]))
            a, b = [r for r in g if r["rank"] > med], [r for r in g if r["rank"] <= med]
            if not a or not b:
                continue
            tot += len(g) * (sum(r["profit"] for r in a) / len(a) - sum(r["profit"] for r in b) / len(b))
            wsum += len(g)
        return tot / wsum if wsum else None

    def resid(rs):
        g = [r for r in rs if r["y"] is not None and r["p_market"] is not None]
        if len(g) < 4:
            return None
        med = float(np.median([r["rank"] for r in g]))
        a, b = [r for r in g if r["rank"] > med], [r for r in g if r["rank"] <= med]
        if not a or not b:
            return None
        return float(np.mean([r["y"] - r["p_market"] for r in a]) - np.mean([r["y"] - r["p_market"] for r in b]))

    s, sci = _boot(rows, strat)
    rr, rci = _boot(rows, resid)

    def st(ci):
        if m < 100 or ci is None:
            return f"MÁLO DAT ({m} zápasů) – přidaná hodnota rankingu zatím neověřena"
        if ci[0] > 0:
            return "průkazně kladná – vyšší ranking lepší i při stejném EV"
        if ci[1] < 0:
            return "průkazně záporná – vyšší ranking horší při stejném EV (jen ruční přezkum vah)"
        return "neprůkazná (interval obsahuje 0)"

    return {"matches": m, "n": len(rows), "correlations": corr,
            "copies": copies, "copies_note": ("ranking převážně kopíruje: " + ", ".join(copies)) if copies else
            "ranking nekopíruje žádnou jednotlivou proměnnou (|rho| ≤ 0,9)",
            "stratified_yield_diff": s, "stratified_ci95": sci, "stratified_status": st(sci),
            "residual_diff": rr, "residual_ci95": rci, "residual_status": st(rci),
            "note": "Vysoký ranking neznamená vyšší pravděpodobnost výhry. Přidaná hodnota se počítá jen uvnitř "
                    "skupin se stejným konzervativním EV (terciy) a proti tržní pravděpodobnosti bez marže."}


def ranking_performance(rank_rows, pred_rows, tip_rows) -> dict:
    rows = ranking_rows(rank_rows, pred_rows, tip_rows)
    out = {"version": IG.RANK_VERSION, "sports": {}, "rankings_total": len(rank_rows),
           "note": "Poslední předzápasový ranking každého výběru (verze " + IG.RANK_VERSION + "), výsledek a tržní "
                   "pravděpodobnost z předpovědi modelu C. Výběry z jednoho zápasu nejsou nezávislé – nezávislá "
                   "jednotka je zápas. Pásma: 0–39, 40–59, 60–79, 80–100. Intervaly 95 % z bootstrapu po zápasech, "
                   "u yieldu navíc Bonferroniho korekce na 4 pásma. Váhy rankingu se NELADÍ."}
    for sp in SPORTS:
        mine = [r for r in rows if r["sport"] == sp]
        ks = sorted(r["kickoff"] for r in mine)
        hi, lo = [r for r in mine if r["rank"] >= 60], [r for r in mine if r["rank"] < 60]
        d, dlo, dhi = IG._boot_diff([{**r, "staked": 1.0} for r in hi], [{**r, "staked": 1.0} for r in lo])
        m = len({r["fixture_id"] for r in mine})
        ev_hi = [r for r in mine if r["ev"] is not None]
        # monotonie: prumerne EV, CLV a yield po pasmech (bez testu – jen popis smeru)
        bands = _band_table(mine)
        out["sports"][sp] = {
            "matches": m, "selections": len(mine),
            "sufficiency": sufficiency(m, len(mine), ks[0] if ks else None, ks[-1] if ks else None,
                                       ["zatím jen jedna verze rankingu"] + (
                                           ["méně než 100 nezávislých zápasů"] if m < 100 else [])),
            "bands": bands,
            "hi_minus_lo": {"diff": d, "ci95": None if d is None or math.isnan(dlo) else (dlo, dhi),
                            "status": (f"MÁLO DAT ({m} zápasů)" if m < 100 or d is None
                                       else IG.verdict(dlo, dhi, m, pos="ranking ≥ 60 průkazně lepší",
                                                       neg="ranking ≥ 60 průkazně horší"))},
            "trend": {"ev": [b["avg_ev"] for b in bands], "clv": [b["clv"] for b in bands],
                      "yield": [b["yield"] for b in bands], "brier": [b["brier"] for b in bands]},
            "added_value": added_value(mine) if ev_hi else None,
        }
    return out


# ===========================================================================================================
# 3. Modely A–D proti trhu
# ===========================================================================================================

def ece(bins: list[dict]) -> float | None:
    n = sum(b["n"] for b in bins)
    return sum(b["n"] / n * abs(b["predicted"] - b["actual"]) for b in bins) if n else None


def chrono_split(common: list[dict], shares=(0.6, 0.2, 0.2)) -> list[dict]:
    """Chronologicke rozdeleni podle vykopu ZAPASU (zadne nahodne michani, zapas nikdy ve dvou castech)."""
    fx = sorted({(r["kickoff"] or "", r["fixture_id"]) for r in common})
    n = len(fx)
    c1, c2 = int(round(n * shares[0])), int(round(n * (shares[0] + shares[1])))
    parts = (("historická / trénovací", fx[:c1]), ("validační", fx[c1:c2]), ("out-of-sample test", fx[c2:]))
    out = []
    for name, ids in parts:
        keep = {f for _, f in ids}
        rs = [r for r in common if r["fixture_id"] in keep]
        e = {"part": name, "matches": len(keep), "n": len(rs), "from": ids[0][0] if ids else None,
             "to": ids[-1][0] if ids else None}
        for key in IG.MODELS + ("trh",):
            sc = prob_scores([IG._getp(r, key) for r in rs], [r["outcome"] for r in rs]) if rs else {}
            e[key] = {"brier": _f(sc.get("brier")), "log_loss": _f(sc.get("log_loss"))}
        out.append(e)
    return out


def _timeline(rows: list[dict]) -> dict:
    """Casova osa a kontrola uniku informaci nad pouzitymi (cistymi) predikcemi."""
    def mm(key):
        v = sorted(r[key] for r in rows if r.get(key))
        return (v[0], v[-1]) if v else (None, None)

    odds_after = sum(1 for r in rows if parse_dt(r.get("odds_time")) and parse_dt(r.get("kickoff"))
                     and parse_dt(r["odds_time"]) >= parse_dt(r["kickoff"]))
    settled_before = sum(1 for r in rows if parse_dt(r.get("settled_at")) and parse_dt(r.get("kickoff"))
                         and parse_dt(r["settled_at"]) < parse_dt(r["kickoff"]))
    return {"predicted": mm("predicted_at"), "kickoff": mm("kickoff"), "settled": mm("settled_at"),
            "odds_after_kickoff": odds_after, "settled_before_kickoff": settled_before,
            "ok": odds_after == 0 and settled_before == 0,
            "note": "Předpověď vzniká před výkopem (predicted_at < výkop, ostatní se vyřazují), kurz vstupu je z doby "
                    "předpovědi (odds_time < výkop), výsledek až po zápase. Závěrečné kurzy slouží jen k CLV a do "
                    "předpovědí se nikdy nedostanou."}


def _bias_bins(common: list[dict], fn, edges, labels) -> list[dict]:
    out = []
    for (lo, hi), lab in zip(edges, labels):
        rs = [r for r in common if lo <= fn(r) < hi]
        if not rs:
            continue
        m = len({r["fixture_id"] for r in rs})
        b, blo, bhi = cluster_ci(_clusters(rs, lambda r: r["p"]["C"] - r["outcome"]), reps=REPS)
        d, dlo, dhi = cluster_ci(_clusters(rs, lambda r: _ll(r["p"]["C"], r["outcome"]) - _ll(r["p_market"], r["outcome"])),
                                 reps=REPS)
        if _small(m):
            blo = dlo = math.nan
        out.append({"bin": lab, "matches": m, "n": len(rs), "p_c": _mean([r["p"]["C"] for r in rs]),
                    "p_market": _mean([r["p_market"] for r in rs]), "actual": _mean([r["outcome"] for r in rs]),
                    "bias_c": _f(b), "bias_c_ci95": None if math.isnan(blo) else (blo, bhi),
                    "ll_vs_market": _f(d), "ll_vs_market_ci95": None if math.isnan(dlo) else (dlo, dhi),
                    "status": IG.verdict(blo, bhi, m, pos="model C průkazně PŘECEŇUJE", neg="model C průkazně PODCEŇUJE")})
    return out


def model_comparison(pred_rows: list[dict], cfg) -> dict:
    clean, dropped = IG.clean_predictions(pred_rows)
    out = {"dropped": dropped, "sports": {},
           "note": "Modely A–D a trh bez marže (de-vig z doby předpovědi) na STEJNÝCH řádcích: zápas, trh a výběr, "
                   "kde mají předpověď všechny čtyři modely i trh. Fotbal a NHL zvlášť.",
           "reconstruction": "Ukládá se čas předpovědi, verze modelu, hash konfigurace, kurz a čas kurzu i seznam zdrojů "
                             "dat. Snímek samotných vstupních dat (formy, absencí, sestav) se NEUKLÁDÁ, takže historické "
                             "vstupy nelze plně zrekonstruovat a předpovědi znovu přepočítat. Out-of-sample znamená: "
                             "předpověď uložená před zápasem z modelu, který výsledek ještě neznal; vyhodnocuje se "
                             "jen to, co bylo skutečně předpovězeno v daný čas."}
    for sp in SPORTS:
        mine = [r for r in clean if IG.sport_of(r.get("league")) == sp]
        common = IG.common_sample(mine)
        m = len({r["fixture_id"] for r in common})
        ks = sorted(r["kickoff"] or "" for r in common)
        table = IG.model_table(common, cfg) if common else {}
        for key, sc in table.items():
            if _small(m):  # interval z mene nez 30 zapasu by byl falesne presny
                sc["brier_ci95"] = sc["log_loss_ci95"] = None
                if sc.get("vs_market"):
                    sc["vs_market"]["ci95"] = None
            sc["ece"] = ece(sc["bins"]) if m >= IG.MIN_MATCHES_CI else None
            sc["ece_note"] = None if m >= IG.MIN_MATCHES_CI else f"ECE až od {IG.MIN_MATCHES_CI} zápasů (nyní {m})"
        limits = []
        if m < 100:
            limits.append("méně než 100 nezávislých zápasů – srovnání s trhem je jen orientační")
        if len({(r["kickoff"] or "")[:7] for r in common}) < 2:
            limits.append("data jen z jednoho měsíce – stabilitu v čase nelze posoudit")
        out["sports"][sp] = {
            "matches": m, "rows": len(common), "predictions": len(mine),
            "sufficiency": sufficiency(m, len(common), ks[0] if ks else None, ks[-1] if ks else None, limits),
            "models": table, "split": chrono_split(common) if common else [],
            "split_status": ("MÁLO DAT – rozdělení jen popisné" if m < 60 else "chronologické rozdělení 60 / 20 / 20 % zápasů"),
            "timeline": _timeline(mine),
            "favourite_outsider": _bias_bins(common, lambda r: r["p_market"], ((0, .25), (.25, .5), (.5, .75), (.75, 1.01)),
                                             ("outsider (trh < 25 %)", "25–50 %", "50–75 %", "favorit (trh ≥ 75 %)")),
            "divergence": _bias_bins(common, lambda r: r["p"]["C"] - r["p_market"],
                                     ((-1, -.1), (-.1, -.03), (-.03, .03), (.03, .1), (.1, 1.01)),
                                     ("C o >10 p.b. pod trhem", "C 3–10 p.b. pod trhem", "C ≈ trh (±3 p.b.)",
                                      "C 3–10 p.b. nad trhem", "C o >10 p.b. nad trhem")),
            "by_band": IG._group_profile(common, lambda r: odds_band(r["odds"]), [], lambda t: "", cfg),
            "by_league": IG._group_profile(common, lambda r: r["league"] or "?", [], lambda t: "", cfg),
            "by_market": IG._group_profile(common, lambda r: r["market_type"], [], lambda t: "", cfg),
        }
    return out


# ===========================================================================================================
# 4. CLV
# ===========================================================================================================

CLV_CLASS = {CK_CLOSING: "skutečný závěrečný kurz (stejná kancelář)", CK_FOREIGN: "skutečný závěrečný kurz (zahraniční kanc.)",
             CK_LAST: "poslední pozorovaný kurz (není závěrečný)", CK_STALE: "zastaralý kurz",
             CK_LINE: "změna trhu nebo linie", CK_MISSING: "chybí závěrečný kurz"}


def clv_class(t: dict) -> str:
    if not t.get("result"):
        return "zatím nevyhodnoceno"
    return CLV_CLASS.get(t.get("closing_kind") or CK_MISSING, t.get("closing_kind") or CK_MISSING)


def _models_with_value(t: dict) -> list[str]:
    """Modely, jejichz pravdepodobnost vyberu je nad trzni pravdepodobnosti bez marze (model 'videl hodnotu')."""
    pm = t.get("p_market")
    probs = json.loads(t["probs"]) if t.get("probs") else {}
    if pm is None:
        return []
    return [m for m in IG.MODELS if probs.get(m) is not None and probs[m] > pm]


def _group(rows, fn, stats) -> dict:
    g = defaultdict(list)
    for r in rows:
        for k in (fn(r) if isinstance(fn(r), list) else [fn(r)]):
            g[k].append(r)
    return {k: stats(v) for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}


def clv_performance(tip_rows: list[dict], rank_rows: list[dict], now: datetime) -> dict:
    tips = [t for t in unique_bets(tip_rows)[0] if t.get("result") != mathcore.VOID]
    ranks = IG._latest_rank(rank_rows, only_valid=False)
    for t in tips:
        r = ranks.get((t["fixture_id"], t["market"], t["selection"]))
        t["_band"] = IG.rank_band(r["rank"]) if r and r.get("rank") is not None else "bez rankingu (tip před Fází 3.1)"
        t["_sport"] = IG.sport_of(t.get("league"))
    classes = defaultdict(int)
    for t in tips:
        classes[clv_class(t)] += 1
    valid = [t for t in tips if t.get("clv") is not None and (t.get("closing_kind") or "") in CLV_KINDS]
    weeks = defaultdict(list)
    for t in valid:
        ko = parse_dt(t.get("kickoff"))
        if ko:
            y, w, _ = ko.isocalendar()
            weeks[f"{y}-T{w:02d}"].append(t)
    series = [{"week": k, "n": len(v), "clv": _mean([t["clv"] for t in v]),
               "pos": sum(t["clv"] > 1e-9 for t in v) / len(v)} for k, v in sorted(weeks.items())]
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    recent = [t["clv"] for t in valid if parse_dt(t.get("kickoff")) and parse_dt(t["kickoff"]) >= now - timedelta(days=7)]
    older = [t["clv"] for t in valid if parse_dt(t.get("kickoff")) and now - timedelta(days=35) <= parse_dt(t["kickoff"])
             < now - timedelta(days=7)]
    change = None
    if len(recent) >= 10 and len(older) >= 10:
        change = {"recent": float(np.mean(recent)), "older": float(np.mean(older)), "n_recent": len(recent),
                  "n_older": len(older), "diff": float(np.mean(recent) - np.mean(older))}
    pend = len([t for t in tips if t.get("result")])
    out = {"tips": len(tips), "settled": pend, "valid": len(valid), "classes": dict(classes),
           "coverage": len(valid) / pend if pend else None, "overall": _clv_stats(valid), "series": series,
           "change_7d": change, "by_sport": {}, "note":
           "CLV = kurz tipu / férový závěrečný kurz − 1 (bez marže, pokud trh má obě strany; jinak proti samotnému "
           "závěrečnému kurzu). Počítá se JEN proti skutečnému závěrečnému kurzu: stejné kanceláře pozorovanému těsně "
           "před výkopem, nebo zahraniční kanceláře z kurzového API aktualizované nejvýše limitní dobu před výkopem. "
           "Poslední pozorovaný, zastaralý nebo chybějící kurz se do CLV nepočítá (není to CLV 0). Žádné další "
           "požadavky na API – jen kurzy stažené stávající strategií."}
    for sp in SPORTS:
        mine = [t for t in valid if t["_sport"] == sp]
        out["by_sport"][sp] = {
            "overall": _clv_stats(mine),
            "by_market": _group(mine, lambda t: market_type(t["market"]), _clv_stats),
            "by_book": _group(mine, lambda t: t.get("bookmaker") or "?", _clv_stats),
            "by_band": _group(mine, lambda t: t["_band"], _clv_stats),
            "by_decision": _group(mine, lambda t: t.get("decision") or "?", _clv_stats),
            "by_model": _group(mine, lambda t: _models_with_value(t) or ["žádný model nad trhem"], _clv_stats),
            "by_source": _group(mine, lambda t: CLV_CLASS[t["closing_kind"]], _clv_stats),
        }
    return out


# ===========================================================================================================
# 5. Financni vykonnost Profilu 4 (virtualni jednotky)
# ===========================================================================================================

def finance(tip_rows: list[dict], rank_rows: list[dict]) -> dict:
    """Vyhodnocene virtualni sazky Profilu 4 po skupinach. NO BET se uklada jen jako kategorie – je zvlast."""
    bets, removed = unique_bets(tip_rows)
    late = sum(1 for t in tip_rows if parse_dt(t.get("predicted_at")) and parse_dt(t.get("kickoff"))
               and parse_dt(t["predicted_at"]) >= parse_dt(t["kickoff"]))
    void = sum(1 for t in bets if t.get("result") == mathcore.VOID)
    ranks = IG._latest_rank(rank_rows, only_valid=False)
    rows = []
    for t in bets:
        if not t.get("result") or t["result"] == mathcore.VOID:
            continue
        r = ranks.get((t["fixture_id"], t["market"], t["selection"]))
        rows.append({**t, "_band": IG.rank_band(r["rank"]) if r and r.get("rank") is not None else "bez rankingu",
                     "_sport": IG.sport_of(t.get("league"))})

    def dec(t):
        d = t.get("decision") or "?"
        return "APPROVED (oficiální)" if d == APPROVED else "NO BET (jen evidence)" if d.endswith("NO BET)") or d == "NO BET" \
            else "WATCH / BEST AVAILABLE (sledované)"

    out = {"method": METHOD, "excluded": {"opakované záznamy stejného výběru": removed - late, "uloženo po výkopu": late,
                                           "neplatné (void)": void},
           "by_sport": {}, "currency": "j. (virtuální jednotky – nikdy se nesčítají s Kč)"}
    for sp in SPORTS:
        mine = [t for t in rows if t["_sport"] == sp]
        out["by_sport"][sp] = {
            "all": _bet_stats(mine), "all_clv": _clv_stats([t for t in mine if (t.get("closing_kind") or "") in CLV_KINDS]),
            "by_decision": _group(mine, dec, _bet_stats),
            "by_model": _group(mine, lambda t: _models_with_value(t) or ["žádný model nad trhem"], _bet_stats),
            "by_league": _group(mine, lambda t: t.get("league") or "?", _bet_stats),
            "by_market": _group(mine, lambda t: market_type(t["market"]), _bet_stats),
            "by_band": _group(mine, lambda t: t["_band"], _bet_stats),
            "by_odds": _group(mine, lambda t: odds_band(t["odds"]), _bet_stats),
        }
    return out


# ===========================================================================================================
# 11. Monitoring – upozorneni (nikdy nic nemeni)
# ===========================================================================================================

def calibration_drift(model_cmp: dict) -> dict:
    """Log loss modelu C proti trhu: novejsi tretina zapasu proti starsim (jen popis, bez zmeny modelu)."""
    out = {}
    for sp, sd in model_cmp["sports"].items():
        sp_rows = sd.get("split") or []
        if len(sp_rows) < 3 or sp_rows[2]["matches"] < 30:
            out[sp] = None
            continue
        old = [p for p in sp_rows[:2] if p["matches"]]

        def gap(p):
            return None if p["C"]["log_loss"] is None or p["trh"]["log_loss"] is None else p["C"]["log_loss"] - p["trh"]["log_loss"]

        g_old = _mean([gap(p) for p in old])
        g_new = gap(sp_rows[2])
        out[sp] = {"older_gap": g_old, "recent_gap": g_new,
                   "worse": g_old is not None and g_new is not None and g_new - g_old > 0.02}
    return out


def alerts(perf: dict, audit: dict | None, leakage: dict | None) -> list[dict]:
    out = []
    a = audit or {}
    live_m = (a.get("live") or {}).get("INTEGRITY MISMATCH", 0)
    run_m = (a.get("last_run") or {}).get("mismatched") or 0
    if live_m or run_m:
        out.append({"level": "chyba", "text": f"Settlement Integrity: {live_m} neshod při vyhodnocení, {run_m} v posledním "
                                              "historickém auditu", "reason": "nezávislý přepočet výsledku nebo zisku se "
                                              "neshoduje s produkcí – výsledky nejsou ověřené, nic se nepřepsalo"})
    ch = (perf.get("clv") or {}).get("change_7d")
    if ch and abs(ch["diff"]) > 0.03:
        out.append({"level": "upozornění", "text": f"Prudká změna CLV: posledních 7 dní {ch['recent']:+.1%} proti "
                                                   f"{ch['older']:+.1%} předtím",
                    "reason": f"{ch['n_recent']} a {ch['n_older']} tipů s ověřeným závěrečným kurzem; rozdíl nad 3 p.b."})
    for c in (leakage or {}).get("checks", []):
        if not c["ok"]:
            out.append({"level": "chyba", "text": f"Nekonzistence dat: {c['name']} ({c['count']})", "reason": c["note"]})
    for sp, d in (perf.get("drift") or {}).items():
        if d and d["worse"]:
            out.append({"level": "upozornění", "text": f"{sp}: zhoršení kalibrace modelu C proti trhu v nejnovější třetině "
                                                       "zápasů", "reason": f"rozdíl log loss C − trh {d['older_gap']:+.4f} → "
                                                       f"{d['recent_gap']:+.4f} (o víc než 0,02)"})
    for sp, sd in (perf.get("models") or {}).get("sports", {}).items():
        if sd.get("timeline") and not sd["timeline"]["ok"]:
            out.append({"level": "chyba", "text": f"{sp}: časová nekonzistence předpovědí",
                        "reason": "kurz vstupu po výkopu nebo výsledek před výkopem"})
    if not out:
        out.append({"level": "ok", "text": "Bez upozornění", "reason": "žádná neshoda settlementu, prudká změna CLV ani "
                                                                       "nekonzistence dat"})
    return out


# ===========================================================================================================
# Souhrn, ulozeni a texty
# ===========================================================================================================

def build(pred_rows, tip_rows, rank_rows, now: datetime, cfg, audit: dict | None = None) -> dict:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    models = model_comparison(pred_rows, cfg)
    perf = {"t": iso(now), "models": models, "ranking": ranking_performance(rank_rows, pred_rows, tip_rows),
            "clv": clv_performance(tip_rows, rank_rows, now), "finance": finance(tip_rows, rank_rows),
            "settlement": audit}
    perf["drift"] = calibration_drift(models)
    perf["alerts"] = alerts(perf, audit, IG.leakage_audit(pred_rows, tip_rows))
    perf["sufficiency"] = {sp: {
        "model_comparison": models["sports"][sp]["sufficiency"],
        "ranking": perf["ranking"]["sports"][sp]["sufficiency"],
        "clv": sufficiency(perf["clv"]["by_sport"][sp]["overall"]["clv_matches"],
                           perf["clv"]["by_sport"][sp]["overall"]["clv_n"]),
    } for sp in SPORTS}
    perf["note"] = ("Měření, ne optimalizace: modely A–D, váhy, ranking, EV, settlement ani prahy se nemění. Technická "
                    "funkčnost systému není důkaz predikční výhody – rozhodují intervaly spolehlivosti na nezávislých "
                    "zápasech mimo vzorek.")
    return perf


def store_performance(st, now: datetime, cfg=None, audit_daily: bool = True) -> dict:
    """Prepocet pro panel: kv `web_p4_perf`, denni historicky audit settlementu a tydenni snimek do archivu."""
    from . import settleaudit
    from .config import P4

    last_run = st.query("SELECT run_at FROM settlement_audit_runs ORDER BY id DESC LIMIT 1")
    lr = parse_dt(last_run[0]["run_at"]) if last_run else None
    nn = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if audit_daily and (lr is None or nn - lr >= timedelta(hours=20)):
        settleaudit.audit_history(st, now)
    preds, tips = IG.load_rows(st.query)
    out = build(preds, tips, IG.load_rankings(st.query), now, cfg or P4, settleaudit.summary(st))
    text = json.dumps(out, ensure_ascii=False, default=str)
    st.set(PERF_KEY, text)
    if hasattr(st, "archive"):
        y, w, _ = nn.isocalendar()
        st.archive("p4_perf_week", f"{y}-T{w:02d}", json.dumps({k: v for k, v in out.items() if k != "t"},
                                                                ensure_ascii=False, default=str))
    return out


def _pct(x, d=1):
    return "–" if x is None else f"{x * 100:+.{d}f} %"


def _ci(ci, pct=True):
    if not ci:
        return ""
    return f" ({ci[0] * 100:+.1f} až {ci[1] * 100:+.1f} %)" if pct else f" ({ci[0]:.4f}–{ci[1]:.4f})"


def format_text(perf: dict, section: str = "performance") -> str:
    L = []
    sports = {FOOTBALL: "FOTBAL", HOCKEY: "NHL"}
    if section in ("performance", "model-comparison"):
        L.append("== Model Comparison (A–D a trh na stejném vzorku) ==")
        mc = perf["models"]
        for sp, sd in mc["sports"].items():
            s = sd["sufficiency"]
            L.append(f"[{sports[sp]}] {s['level']}: {s['reason']}")
            for k, m in sd["models"].items():
                L.append(f"  {'Trh' if k == 'trh' else 'Model ' + k}: Brier {m['brier']:.4f}{_ci(m.get('brier_ci95'), False)}, "
                         f"log loss {m['log_loss']:.4f}{_ci(m.get('log_loss_ci95'), False)}, ECE "
                         f"{'–' if m.get('ece') is None else f'{m['ece']:.3f}'}, kalibrace: {m['calibration']['status']}"
                         + (f", proti trhu {m['vs_market']['diff']:+.4f} – {m['vs_market']['status']}" if m.get("vs_market") else ""))
            for p in sd["split"]:
                L.append(f"  {p['part']}: {p['matches']} záp. ({(p['from'] or '')[:10]} – {(p['to'] or '')[:10]}), log loss "
                         + ", ".join(f"{k} {p[k]['log_loss']:.4f}" for k in IG.MODELS + ("trh",) if p[k]["log_loss"] is not None))
            L.append(f"  Časová kontrola: {'OK' if sd['timeline']['ok'] else 'NEKONZISTENCE'} "
                     f"(kurz po výkopu {sd['timeline']['odds_after_kickoff']}, výsledek před výkopem "
                     f"{sd['timeline']['settled_before_kickoff']})")
        L.append("  " + mc["reconstruction"])
    if section in ("performance", "ranking-performance"):
        L.append("== Opportunity Ranking Performance ==")
        for sp, sd in perf["ranking"]["sports"].items():
            L.append(f"[{sports[sp]}] {sd['sufficiency']['level']}: {sd['sufficiency']['reason']}")
            for b in sd["bands"]:
                L.append(f"  {b['band']}: {b['matches']} záp. / {b['selections']} výb., výhry {b['won']} (+{b['half_won']} půl), "
                         f"prohry {b['lost']} (+{b['half_lost']} půl), push {b['push']}, win rate {'–' if b['win_rate'] is None else f"{b['win_rate']:.0%}"}, "
                         f"yield {_pct(b['yield'])}{_ci(b['yield_ci95'])}, ROI {_pct(b['roi'])}, CLV {_pct(b['clv'])} "
                         f"({b['clv_n']}), Brier {'–' if b['brier'] is None else f'{b['brier']:.4f}'} – {b['status']}")
            av = sd.get("added_value")
            if av:
                L.append(f"  Přidaná hodnota nad EV: {av['stratified_status']}; proti trhu: {av['residual_status']}; "
                         f"{av['copies_note']}")
    if section in ("performance", "clv"):
        c = perf["clv"]
        L.append("== CLV ==")
        L.append(f"Tipů {c['tips']}, vyhodnoceno {c['settled']}, s platným závěrečným kurzem {c['valid']}"
                 + (f" ({c['coverage']:.0%})" if c["coverage"] is not None else ""))
        L.append("  Druhy srovnávacího kurzu: " + ", ".join(f"{k} {v}" for k, v in c["classes"].items()))
        for sp, sd in c["by_sport"].items():
            o = sd["overall"]
            L.append(f"  [{sports[sp]}] {o['clv_n']} tipů: průměr {_pct(o['clv'])}{_ci(o['clv_ci95'])}, medián "
                     f"{_pct(o['clv_median'])}, kladné {'–' if o['clv_pos'] is None else f'{o['clv_pos']:.0%}'}")
    if section in ("performance", "settlement-integrity"):
        a = perf.get("settlement") or {}
        L.append("== Settlement Integrity ==")
        lv = a.get("live") or {}
        L.append(f"Záznamy při vyhodnocení: {lv.get('checked', 0)} (shoda {lv.get('SHODA', 0)}, neshoda "
                 f"{lv.get('INTEGRITY MISMATCH', 0)}, neověřitelné {lv.get('NEOVĚŘITELNÉ', 0)}); bez záznamu "
                 f"(vyhodnoceno před Fází 3.2): {a.get('settled_without_record', '–')}")
        r = a.get("last_run")
        if r:
            L.append(f"Poslední historický audit {r['run_at'][:16]}: zkontrolováno {r['checked']}, shoda {r['matched']}, "
                     f"neshoda {r['mismatched']}, neověřitelné {r['unverifiable']}")
            for g, v in (a.get("last_run_groups") or {}).items():
                L.append(f"  {g}: {v['checked']} – shoda {v['SHODA']}, neshoda {v['INTEGRITY MISMATCH']}, "
                         f"neověřitelné {v['NEOVĚŘITELNÉ']}")
            for k, v in (a.get("last_run_unverifiable") or {}).items():
                L.append(f"  neověřitelné: {k} ({v})")
            for it in a.get("last_run_items") or []:
                L.append(f"  NESHODA {it['kind']} {it['id']}: {it.get('match')} {it['market']} {it['selection']} – {it['reason']}")
    if section == "performance":
        L.append("== Upozornění ==")
        for al in perf["alerts"]:
            L.append(f"  [{al['level']}] {al['text']} – {al['reason']}")
        L.append(perf["note"])
    return "\n".join(L)
