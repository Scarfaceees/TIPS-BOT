"""Historie predikci a tipu Profilu 4, vyhodnoceni a kalibrace.

Uklada se: zapas, predikce kazdeho modelu, ferovy kurz, kurz pri doporuceni a jeho zdroj/cas, kancelar,
cas predikce, zdroje dat, verze modelu a otisk konfigurace; po zapase vysledek, settlement, zisk a CLV.

Metriky: Brier score a log loss (1X2, po modelech A–D), ROI, yield, win rate, prumerne CLV – po modelech,
trzich, soutezich, kategoriich, kancelarich, rozhodnutich a mesicich. Mala data se oznaci jako neprukazna.

Backtest: walk-forward z vysledku API-Football (model se pro kazdy den odhaduje jen z drivejsich zapasu –
zadny data leakage), testovaci obdobi oddelene od trenovaciho, porovnani s naivnim modelem (cetnosti
z trenovaciho obdobi). Kurzy API uchovava jen 7 dni, proto historicky ROI z API spocitat nelze – ROI a CLV
se meri dopredu na ulozenych tipech (a zpetny test proti kurzum Pinnacle je v puvodnim `tipsbot backtest`).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np

from . import mathcore
from . import models as M
from . import odds as O
from .analysis import MatchAnalysis
from .config import MODEL_VERSION, Profil4Config
from .decision import APPROVED, BEST
from .provider import VOID_STATUSES, DataProvider, Fixture
from .store import Store, iso, parse_dt

MIN_SAMPLE = 100


def record(store: Store, an: MatchAnalysis, cfg: Profil4Config) -> int:
    """Ulozi predikci 1X2 vsech modelu a vybrane tipy (schvalene, kategorie, nejlepsi dostupny)."""
    fx, ms = an.fixture, an.models
    when = iso(an.created)
    sources = json.dumps({k: v.status for k, v in an.coverage.items.items()})
    h = cfg.config_hash()
    if ms is not None and ms.c.available:
        probs = {}
        for name, mat in (("A", ms.a.matrix if ms.a.available else None), ("B", ms.b.matrix if ms.b.available else None),
                          ("C", ms.c.matrix)):
            if mat is not None:
                probs[name] = [mathcore.evaluate("1X2", s, mat).p_win for s in ("1", "X", "2")]
        dp = [ms.d_probability("1X2", s) for s in ("1", "X", "2")]
        if all(x is not None for x in dp):
            tot = sum(dp)
            probs["D"] = [x / tot for x in dp]  # D je spodni odhad; pro Brier normalizovano
        store.execute("INSERT OR IGNORE INTO forecasts(fixture_id, match, league_id, league, kickoff, market, probs, "
                      "predicted_at, model_version, config_hash, data_sources) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (fx.id, fx.name, fx.league_id, fx.league, iso(fx.kickoff), "1X2", json.dumps(probs), when,
                       MODEL_VERSION, h, sources))
    chosen = {c.key: c for c in an.selection.approved}
    for c in an.selection.categories.values():
        if c:
            chosen.setdefault(c.key, c)
    if an.selection.best_available:
        chosen.setdefault(an.selection.best_available.key, an.selection.best_available)
    n = 0
    for c in chosen.values():
        dup = store.query("SELECT 1 FROM tips WHERE fixture_id=? AND market=? AND selection=? AND bookmaker=? AND odds=? "
                          "AND decision=?", (fx.id, c.market, c.selection, c.quote.bookmaker, c.odds, c.decision))
        if dup:
            continue
        decision = c.decision if c is not an.selection.best_available or c.decision == APPROVED else f"{BEST} ({c.decision})"
        store.execute(
            "INSERT INTO tips(fixture_id, match, league_id, league, kickoff, market, selection, label, probs, fair_odds, "
            "odds, bookmaker, odds_source, odds_time, ev, ev_conservative, robustness, decision, category, predicted_at, "
            "model_version, config_hash, data_sources) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fx.id, fx.name, fx.league_id, fx.league, iso(fx.kickoff), c.market, c.selection, c.label, json.dumps(c.p),
             c.fair_odds_c, c.odds, c.quote.bookmaker, c.quote.source, iso(c.quote.stamp), c.ev_c, c.ev_d, c.robustness,
             decision, c.category, when, MODEL_VERSION, h, sources))
        n += 1
    return n


def _closing(store: Store, fid: int, book: str, market: str, sel: str, kickoff: datetime) -> tuple[float | None, float | None]:
    """Posledni kurz stejne kancelare pred vykopem a jeho ferova hodnota (bez marze, kdyz je trh kompletni)."""
    last = [q for q in O.latest(store, fid, before=kickoff) if q.bookmaker == book and q.market == market]
    mine = next((q for q in last if q.selection == sel), None)
    if mine is None:
        return None, None
    from .. import profile2

    sels = profile2.selections(market)
    per = {q.selection: q.odds for q in last}
    fair = None
    if sels and all(s in per for s in sels) and market not in ("DNB",) and not market.startswith("AH_"):
        _, probs = mathcore.devig([per[s] for s in sels])
        fair = 1.0 / probs[sels.index(sel)]
    return mine.odds, fair


def settle(store: Store, provider: DataProvider, now: datetime) -> list[str]:
    """Vyhodnoti tipy a predikce zapasu, ktere uz skoncily (1 pozadavek na 20 zapasu)."""
    rows = store.query("SELECT DISTINCT fixture_id FROM tips WHERE result IS NULL AND kickoff < ? "
                       "UNION SELECT DISTINCT fixture_id FROM forecasts WHERE result IS NULL AND kickoff < ?",
                       (iso(now - timedelta(hours=2, minutes=30)),) * 2)
    ids = [r["fixture_id"] for r in rows]
    if not ids:
        return []
    got = provider.fixtures_by_ids(ids, priority="MEDIUM")
    msgs = []
    for fx in got.data or []:
        if fx.status in VOID_STATUSES:
            for t in store.query("SELECT * FROM tips WHERE fixture_id=? AND result IS NULL", (fx.id,)):
                store.execute("UPDATE tips SET result=?, profit=0, settled_at=? WHERE id=?", (mathcore.VOID, iso(now), t["id"]))
            store.execute("UPDATE forecasts SET result='void' WHERE fixture_id=? AND result IS NULL", (fx.id,))
            msgs.append(f"{fx.name}: zápas {fx.status} – sázky neplatné")
            continue
        if not fx.finished:
            continue
        score = f"{fx.ft_home}:{fx.ft_away}"
        res1x2 = "1" if fx.ft_home > fx.ft_away else "X" if fx.ft_home == fx.ft_away else "2"
        store.execute("UPDATE forecasts SET result=?, score=? WHERE fixture_id=? AND result IS NULL", (res1x2, score, fx.id))
        for t in store.query("SELECT * FROM tips WHERE fixture_id=? AND result IS NULL", (fx.id,)):
            try:
                out = mathcore.settle(t["market"], t["selection"], fx.ft_home, fx.ft_away)
            except ValueError as e:
                msgs.append(f"{fx.name} {t['label']}: nelze vyhodnotit ({e})")
                continue
            profit = mathcore.unit_profit(out, t["odds"])
            close, close_fair = _closing(store, fx.id, t["bookmaker"], t["market"], t["selection"], fx.kickoff)
            clv = (t["odds"] / close_fair - 1.0) if close_fair else (t["odds"] / close - 1.0) if close else None
            store.execute("UPDATE tips SET result=?, score=?, profit=?, closing_odds=?, closing_fair_odds=?, clv=?, "
                          "settled_at=? WHERE id=?", (out, score, profit, close, close_fair, clv, iso(now), t["id"]))
            msgs.append(f"{fx.name} {score}: {t['label']} @ {t['odds']:.2f} – {mathcore.SETTLE_CZ[out]} ({profit:+.2f} j.)")
    return msgs


# ---------------------------------------------------------------------------
# Metriky
# ---------------------------------------------------------------------------

def brier_multi(probs: list[list[float]], outcomes: list[int]) -> float:
    """Brier score pro vice vysledku: prumer sumy (p_i - y_i)^2 (0 = dokonale, 2 = nejhur)."""
    tot = 0.0
    for p, o in zip(probs, outcomes):
        tot += sum((pi - (1.0 if i == o else 0.0)) ** 2 for i, pi in enumerate(p))
    return tot / len(probs) if probs else math.nan


def log_loss_multi(probs: list[list[float]], outcomes: list[int], eps: float = 1e-12) -> float:
    if not probs:
        return math.nan
    return float(-np.mean([math.log(max(p[o], eps)) for p, o in zip(probs, outcomes)]))


def bet_stats(rows: list[dict], bankroll_units: float = 100.0) -> dict:
    """Jednotkove vklady (1 j. na tip). yield = zisk / obrat; ROI = zisk / vychozi kapital (bankroll_units)."""
    settled = [r for r in rows if r["result"] and r["result"] != mathcore.VOID]
    n = len(settled)
    profit = float(sum(r["profit"] for r in settled))
    wins = sum(1 for r in settled if r["result"] in (mathcore.WON, mathcore.HALF_WON))
    clvs = [r["clv"] for r in settled if r["clv"] is not None]
    pnl = [r["profit"] for r in settled]
    se = float(np.std(pnl, ddof=1) / math.sqrt(n)) if n > 1 else math.nan
    return {"n": n, "profit": profit, "yield": profit / n if n else math.nan, "roi": profit / bankroll_units,
            "win_rate": wins / n if n else math.nan, "avg_clv": float(np.mean(clvs)) if clvs else math.nan,
            "clv_n": len(clvs), "yield_ci95": (profit / n - 1.96 * se, profit / n + 1.96 * se) if n > 1 else None,
            "significant": n >= MIN_SAMPLE}


def evaluate(store: Store) -> dict:
    out: dict = {"models": {}, "groups": {}}
    fc = store.query("SELECT * FROM forecasts WHERE result IN ('1','X','2')")
    per_model: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    for r in fc:
        o = {"1": 0, "X": 1, "2": 2}[r["result"]]
        for name, p in json.loads(r["probs"]).items():
            per_model[name][0].append(p)
            per_model[name][1].append(o)
    for name, (ps, os_) in sorted(per_model.items()):
        out["models"][name] = {"n": len(ps), "brier": brier_multi(ps, os_), "log_loss": log_loss_multi(ps, os_)}
    tips = [dict(r) for r in store.query("SELECT * FROM tips")]
    out["all"] = bet_stats(tips)
    keys = {"trh": lambda r: r["market"].split("_")[0], "soutěž": lambda r: r["league"] or "?",
            "kategorie": lambda r: (r["category"] or "bez kategorie").split(" · ")[0],
            "kancelář": lambda r: r["bookmaker"], "rozhodnutí": lambda r: r["decision"],
            "měsíc": lambda r: (r["kickoff"] or "")[:7], "verze": lambda r: f"{r['model_version']}/{r['config_hash']}"}
    for gname, fn in keys.items():
        groups: dict[str, list] = defaultdict(list)
        for r in tips:
            groups[fn(r)].append(r)
        out["groups"][gname] = {k: bet_stats(v) for k, v in sorted(groups.items())}
    return out


# ---------------------------------------------------------------------------
# Backtest modelu A
# ---------------------------------------------------------------------------

def backtest(history: list[Fixture], test_start: date, refit_days: int = 7) -> dict:
    """Walk-forward 1X2 modelu A na vysledcich: trenink = vse pred dnem predikce, test = od test_start."""
    from ..backtest import walk_forward

    df = M.matches_frame(history)
    if df.empty:
        return {"n": 0, "note": "žádná data"}
    preds = walk_forward(df, test_start, refit_days=refit_days)
    if preds.empty:
        return {"n": 0, "note": "model nešlo odhadnout (málo zápasů před testovacím obdobím)"}
    train = df[df["date"] < test_start]
    if train.empty:
        return {"n": 0, "note": "chybí tréninkové období"}
    hg, ag = train["home_goals"].to_numpy(), train["away_goals"].to_numpy()
    base = [float((hg > ag).mean()), float((hg == ag).mean()), float((hg < ag).mean())]
    outcomes = [0 if h > a else 1 if h == a else 2 for h, a in zip(preds["home_goals"], preds["away_goals"])]
    model = [[r.p1, r.px, r.p2] for r in preds.itertuples()]
    naive = [base] * len(outcomes)
    return {
        "n": len(outcomes), "train_n": int(len(train)), "test_start": test_start.isoformat(),
        "model": {"brier": brier_multi(model, outcomes), "log_loss": log_loss_multi(model, outcomes)},
        "naive": {"brier": brier_multi(naive, outcomes), "log_loss": log_loss_multi(naive, outcomes)},
        "calibration": calibration_table([p[0] for p in model], [1 if o == 0 else 0 for o in outcomes]),
        "significant": len(outcomes) >= 300,
    }


def calibration_table(p: list[float], y: list[int], bins: int = 5) -> list[dict]:
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, x in enumerate(p) if lo <= x < hi or (hi == 1 and x == 1)]
        if idx:
            out.append({"bin": f"{lo:.0%}–{hi:.0%}", "n": len(idx), "predicted": float(np.mean([p[i] for i in idx])),
                        "actual": float(np.mean([y[i] for i in idx]))})
    return out


def parse_row_time(s: str | None) -> datetime | None:
    return parse_dt(s)
