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
        q = c.quote
        store.execute(
            "INSERT INTO tips(fixture_id, match, league_id, league, kickoff, market, selection, label, probs, fair_odds, "
            "odds, bookmaker, odds_source, odds_time, ev, ev_conservative, robustness, decision, category, predicted_at, "
            "model_version, config_hash, data_sources, odds_fetched_at, odds_age_min, odds_status, p_market) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fx.id, fx.name, fx.league_id, fx.league, iso(fx.kickoff), c.market, c.selection, c.label, json.dumps(c.p),
             c.fair_odds_c, c.odds, q.bookmaker, q.source, iso(q.stamp), c.ev_c, c.ev_d, c.robustness,
             decision, c.category, when, MODEL_VERSION, h, sources, iso(q.fetched_at), q.age_minutes(an.created),
             c.quote_status, c.p_market))
        n += 1
    record_predictions(store, an, cfg)
    return n


def market_type(market: str) -> str:
    """Typ sazky: 1X2, DC, DNB, BTTS, OU, TTH, TTA, AH, EXPR."""
    return market.split("_", 1)[0]


def odds_band(odds: float | None) -> str:
    if odds is None:
        return "bez kurzu"
    for hi, name in ((1.5, "do 1.50"), (2.0, "1.50–1.99"), (3.0, "2.00–2.99"), (5.0, "3.00–4.99")):
        if odds < hi:
            return name
    return "5.00 a výš"


def record_predictions(store: Store, an: MatchAnalysis, cfg: Profil4Config) -> int:
    """Pravdepodobnost kazdeho modelu (A, B, C, D) pro kazdy trh s kurzem – pro dlouhodobou kalibraci.

    Jen predzapasove predikce (cas analyzy < vykop). Opakovany sken pred vykopem predikci prepise, takze se
    vyhodnocuje posledni predikce pred zapasem; vyhodnocena (settled) predikce se uz nikdy nemeni.
    `p` je efektivni pravdepodobnost vyhry (u linii s vracenim podminena tim, ze sazka neni vracena)."""
    fx = an.fixture
    if an.created >= fx.kickoff or an.models is None:
        return 0
    when, h, n = iso(an.created), cfg.config_hash(), 0
    for c in an.candidates:
        for model, p in c.p.items():
            if p is None or not (0.0 < p < 1.0):
                continue
            store.execute(
                "INSERT INTO predictions(fixture_id, match, league_id, league, kickoff, market, selection, market_type, "
                "model, p, p_market, market_covered, odds, bookmaker, odds_source, odds_time, odds_status, predicted_at, "
                "model_version, config_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(fixture_id, market, selection, model, model_version) DO UPDATE SET p=excluded.p, "
                "p_market=excluded.p_market, market_covered=excluded.market_covered, odds=excluded.odds, "
                "bookmaker=excluded.bookmaker, odds_source=excluded.odds_source, odds_time=excluded.odds_time, "
                "odds_status=excluded.odds_status, predicted_at=excluded.predicted_at, config_hash=excluded.config_hash "
                "WHERE predictions.result IS NULL AND excluded.predicted_at >= predictions.predicted_at",
                (fx.id, fx.name, fx.league_id, fx.league, iso(fx.kickoff), c.market, c.selection, market_type(c.market),
                 model, float(p), c.p_market, int(c.market_covered), c.odds, c.quote.bookmaker, c.quote.source,
                 iso(c.quote.stamp), c.quote_status, when, MODEL_VERSION, h))
            n += 1
    return n


CLV_POS, CLV_NEG, CLV_ZERO, CLV_MISSING, CLV_LINE = (
    "kladné", "záporné", "nulové", "chybí závěrečný kurz", "změna trhu nebo linie")


def _closing(store: Store, fid: int, book: str, market: str, sel: str, kickoff: datetime,
             rec_time: datetime | None = None, max_gap_min: float | None = None) -> dict:
    """Zaviraci kurz STEJNE kancelare na STEJNY trh a vyber: posledni kurz pred vykopem, ktery je novejsi nez kurz
    pri doporuceni (jinak by se porovnaval kurz sam se sebou).

    Vraci {"odds", "fair", "time", "status"}; status None = zaviraci kurz je k dispozici. Ferova hodnota se
    pocita jen z kompletniho trhu te kancelare ze stejne aktualizace (bez marze); jinak CLV z hrubeho kurzu.
    Kdyz kancelar po doporuceni nabizi jen jinou linii stejneho typu trhu, status = zmena trhu nebo linie."""
    from .. import profile2

    after = [q for q in O.history(store, fid) if q.bookmaker == book and q.stamp < kickoff
             and (rec_time is None or q.stamp > rec_time)]
    mine = [q for q in after if q.market == market and q.selection == sel]
    if not mine:
        fam = market_type(market)
        moved = any(market_type(q.market) == fam and q.market != market for q in after) and "_" in market
        return {"odds": None, "fair": None, "time": None, "status": CLV_LINE if moved else CLV_MISSING}
    last = max(mine, key=lambda q: q.stamp)
    if max_gap_min is not None and (kickoff - last.stamp).total_seconds() / 60 > max_gap_min:
        gap = (kickoff - last.stamp).total_seconds() / 3600
        return {"odds": last.odds, "fair": None, "time": last.stamp,
                "status": f"{CLV_MISSING} (poslední kurz {gap:.1f} h před výkopem)"}
    sels = profile2.selections(market)
    per = {q.selection: q.odds for q in after if q.market == market and q.stamp == last.stamp}
    fair = None
    if sels and all(s in per for s in sels) and market not in ("DNB",) and not market.startswith("AH_"):
        _, probs = mathcore.devig([per[s] for s in sels])
        fair = 1.0 / probs[sels.index(sel)]
    return {"odds": last.odds, "fair": fair, "time": last.stamp, "status": None}


def outcome_value(result: str) -> float | None:
    """Vysledek pro kalibraci efektivni pravdepodobnosti: vyhra 1, prohra 0; pulvyhra = vyhrana nevracena cast
    (1), pulprohra = prohrana nevracena cast (0); vraceni a neplatna sazka se nehodnoti (None)."""
    return {mathcore.WON: 1.0, mathcore.HALF_WON: 1.0, mathcore.LOST: 0.0, mathcore.HALF_LOST: 0.0}.get(result)


def settle(store: Store, provider: DataProvider, now: datetime) -> list[str]:
    """Vyhodnoti tipy a predikce zapasu, ktere uz skoncily (1 pozadavek na 20 zapasu)."""
    rows = store.query("SELECT DISTINCT fixture_id FROM tips WHERE result IS NULL AND kickoff < ? "
                       "UNION SELECT DISTINCT fixture_id FROM forecasts WHERE result IS NULL AND kickoff < ? "
                       "UNION SELECT DISTINCT fixture_id FROM predictions WHERE result IS NULL AND kickoff < ?",
                       (iso(now - timedelta(hours=2, minutes=30)),) * 3)
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
            store.execute("UPDATE predictions SET result=?, settled_at=? WHERE fixture_id=? AND result IS NULL",
                          (mathcore.VOID, iso(now), fx.id))
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
            cl = _closing(store, fx.id, t["bookmaker"], t["market"], t["selection"], fx.kickoff,
                          parse_dt(t["odds_time"]), CLOSING_MAX_GAP_MIN)
            clv, status = None, cl["status"]
            if status is None:
                clv = t["odds"] / cl["fair"] - 1.0 if cl["fair"] else t["odds"] / cl["odds"] - 1.0
                status = CLV_POS if clv > 1e-9 else CLV_NEG if clv < -1e-9 else CLV_ZERO
            store.execute("UPDATE tips SET result=?, score=?, profit=?, closing_odds=?, closing_fair_odds=?, clv=?, "
                          "clv_status=?, closing_time=?, settled_at=? WHERE id=?",
                          (out, score, profit, cl["odds"], cl["fair"], clv, status, iso(cl["time"]), iso(now), t["id"]))
            msgs.append(f"{fx.name} {score}: {t['label']} @ {t['odds']:.2f} – {mathcore.SETTLE_CZ[out]} ({profit:+.2f} j.)")
        for p in store.query("SELECT id, market, selection FROM predictions WHERE fixture_id=? AND result IS NULL", (fx.id,)):
            try:
                out = mathcore.settle(p["market"], p["selection"], fx.ft_home, fx.ft_away)
            except ValueError:
                continue
            store.execute("UPDATE predictions SET result=?, outcome=?, score=?, settled_at=? WHERE id=?",
                          (out, outcome_value(out), score, iso(now), p["id"]))
    return msgs


CLOSING_MAX_GAP_MIN = 180  # posledni kurz starsi nez 3 h pred vykopem neni zaviraci kurz


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
            "kurzové pásmo": lambda r: odds_band(r["odds"]),
            "kategorie": lambda r: (r["category"] or "bez kategorie").split(" · ")[0],
            "kancelář": lambda r: r["bookmaker"], "rozhodnutí": lambda r: r["decision"],
            "měsíc": lambda r: (r["kickoff"] or "")[:7], "verze": lambda r: f"{r['model_version']}/{r['config_hash']}"}
    for gname, fn in keys.items():
        groups: dict[str, list] = defaultdict(list)
        for r in tips:
            groups[fn(r)].append(r)
        out["groups"][gname] = {k: bet_stats(v) for k, v in sorted(groups.items())}
    settled = [r for r in tips if r["result"] and r["result"] != mathcore.VOID]
    out["clv_status"] = dict(sorted(_count(r.get("clv_status") or "neurčeno (starší záznam)" for r in settled).items()))
    out["calibration"] = calibration(store)
    out["data_quality"] = data_quality(store)
    return out


def _count(items) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for x in items:
        c[x] += 1
    return c


def prob_scores(p: list[float], y: list[float], eps: float = 1e-12) -> dict:
    """Brier a log loss binarni predikce (y muze byt 0/1)."""
    pa, ya = np.clip(np.asarray(p, float), eps, 1 - eps), np.asarray(y, float)
    return {"n": len(pa), "brier": float(np.mean((pa - ya) ** 2)) if len(pa) else math.nan,
            "log_loss": float(-np.mean(ya * np.log(pa) + (1 - ya) * np.log(1 - pa))) if len(pa) else math.nan,
            "mean_p": float(pa.mean()) if len(pa) else math.nan, "rate": float(ya.mean()) if len(pa) else math.nan}


def calibration(store: Store, bins: int = 10) -> dict:
    """Kalibrace modelu A–D na vyhodnocenych predpovedich vsech trhu (ne jen 1X2).

    Celkove a po ligach, typech trhu a kurzovych pasmech; kalibracni krivka (predpoved vs. skutecnost po decilech).
    Jeden zapas dava mnoho korelovanych predpovedi, proto se uvadi i pocet zapasu – prukaznost se posuzuje podle
    nej (MIN_SAMPLE zapasu), ne podle poctu radku. Model B na trzich bez aktualnich kurzu je jen dopocet; jeho
    hodnoceni 'B (tržní trhy)' bere jen predpovedi, kde trh mel aktualni kurzy."""
    rows = [dict(r) for r in store.query("SELECT * FROM predictions WHERE outcome IS NOT NULL")]
    out: dict = {"models": {}, "by": {}, "curves": {}, "matches": len({r["fixture_id"] for r in rows})}
    groups = {"liga": lambda r: r["league"] or "?", "typ trhu": lambda r: r["market_type"],
              "kurzové pásmo": lambda r: odds_band(r["odds"])}
    for model in ("A", "B", "C", "D"):
        mine = [r for r in rows if r["model"] == model]
        variants = [(model, mine)]
        if model == "B":
            variants.append(("B (tržní trhy)", [r for r in mine if r["market_covered"]]))
        for name, rs in variants:
            if not rs:
                continue
            sc = prob_scores([r["p"] for r in rs], [r["outcome"] for r in rs])
            sc["matches"] = len({r["fixture_id"] for r in rs})
            sc["significant"] = sc["matches"] >= MIN_SAMPLE
            out["models"][name] = sc
            out["curves"][name] = calibration_table([r["p"] for r in rs], [r["outcome"] for r in rs], bins)
            for gname, fn in groups.items():
                g: dict[str, list] = defaultdict(list)
                for r in rs:
                    g[fn(r)].append(r)
                out["by"].setdefault(gname, {})[name] = {
                    k: {**prob_scores([r["p"] for r in v], [r["outcome"] for r in v]),
                        "matches": len({r["fixture_id"] for r in v})} for k, v in sorted(g.items())}
    # trh (ferova pravdepodobnost bez marze) jako meritko pro stejne radky
    mk = [r for r in rows if r["model"] == "C" and r["p_market"] is not None and 0 < r["p_market"] < 1]
    if mk:
        out["models"]["trh (bez marže)"] = {**prob_scores([r["p_market"] for r in mk], [r["outcome"] for r in mk]),
                                            "matches": len({r["fixture_id"] for r in mk})}
        out["models"]["C na stejných řádcích"] = {**prob_scores([r["p"] for r in mk], [r["outcome"] for r in mk]),
                                                  "matches": len({r["fixture_id"] for r in mk})}
    return out


def data_quality(store: Store) -> dict:
    """Prehled kvality dat: stari kurzu pri stazeni a pri doporuceni, pokryti trhu aktualnimi kurzy, CLV."""
    q = store.query("SELECT source, fetched_at, source_time FROM odds_quotes")
    lag = []
    for r in q:
        f, s = parse_dt(r["fetched_at"]), parse_dt(r["source_time"])
        if f and s:
            lag.append((f - s).total_seconds() / 3600)
    lag_a = np.asarray(lag) if lag else np.asarray([0.0])
    tips = [dict(r) for r in store.query("SELECT odds_age_min, odds_status, clv_status, result FROM tips")]
    ages = [r["odds_age_min"] for r in tips if r["odds_age_min"] is not None]
    preds = store.query("SELECT market_type, market_covered, result FROM predictions WHERE model='C'")
    cover: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in preds:
        cover[r["market_type"]][0] += 1
        cover[r["market_type"]][1] += int(bool(r["market_covered"]))
    return {
        "quotes": len(q), "books": len({r[0] for r in store.query("SELECT DISTINCT bookmaker FROM odds_quotes")}),
        "lag_median_h": float(np.median(lag_a)) if lag else None,
        "lag_over_6h": float(np.mean(lag_a > 6)) if lag else None,
        "tips": len(tips), "tip_age_median_min": float(np.median(ages)) if ages else None,
        "tip_status": dict(_count(r["odds_status"] or "neuloženo (starší záznam)" for r in tips)),
        "predictions": len(preds), "predictions_settled": sum(1 for r in preds if r["result"]),
        "market_covered": {k: (v[1], v[0]) for k, v in sorted(cover.items())},
    }


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


def calibration_table(p: list[float], y: list[float], bins: int = 5) -> list[dict]:
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
