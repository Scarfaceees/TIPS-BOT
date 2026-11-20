"""Backtest fotbaloveho modelu: walk-forward predikce, Brier, simulace sazeni, Sharpe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from . import calibration, devig, risk
from .config import SETTINGS, Settings, Tier
from .models import football


@dataclass(frozen=True)
class BacktestReport:
    matches: int
    brier: float  # prumer binarnich Brier score pres 1 / X / 2
    log_loss: float
    bets: int
    win_rate: float
    yield_pct: float
    sharpe: float
    max_drawdown: float
    passed: bool
    market_brier: float | None = None  # uzaviraci linie Pinnacle (po devigu) - nejtezsi benchmark
    blend_brier: float | None = None  # smes modelu a trhu tak, jak ji pouziva bot
    baseline_brier: float = 0.0  # naivni model: dlouhodobe cetnosti vysledku

    def summary(self) -> str:
        bench = ""
        if self.market_brier is not None:
            bench = (f" | trh {self.market_brier:.4f}, smes modelu a trhu {self.blend_brier:.4f}"
                     f"{' (smes porazila trh)' if self.blend_brier < self.market_brier else ' (smes trh neporazila)'}")
        return (
            f"zapasu {self.matches} | Brier {self.brier:.4f} (naivni {self.baseline_brier:.4f}){bench} | logloss {self.log_loss:.4f} | "
            f"sazek {self.bets} | uspesnost {self.win_rate * 100:.1f} % | yield {self.yield_pct:+.2f} % | "
            f"Sharpe {self.sharpe:.2f} | max propad {self.max_drawdown * 100:.1f} % | "
            f"{'SPLNENO' if self.passed else 'NESPLNENO'}"
        )


# trhy hodnocene ve zpetnem testu krome 1X2 (klic = "ano" strana: over / oba daji gol)
EVAL_KEYS = ("OU_1.5:over", "OU_2.5:over", "OU_3.5:over", "OU_4.5:over", "BTTS:yes",
             "TTH_0.5:over", "TTH_1.5:over", "TTA_0.5:over", "TTA_1.5:over")


def _outcome(key: str, hg: np.ndarray, ag: np.ndarray) -> np.ndarray:
    """Index vysledku 0 = over / ano, 1 = under / ne."""
    market = key.split(":")[0]
    if market == "BTTS":
        yes = (hg > 0) & (ag > 0)
    else:
        kind, line = market.split("_")
        goals = {"OU": hg + ag, "TTH": hg, "TTA": ag}[kind]
        yes = goals > float(line)
    return np.where(yes, 0, 1)


def _ou25_anchor(r) -> tuple[str, float] | None:
    """Ferova pravdepodobnost over 2.5 z PREDZAPASOVEHO kurzu Pinnacle v radku dat (None, kdyz kurzy chybi).

    Zaviraci kurz (sharp_o25) se zamerne nepouziva: v case predikce neni znamy (data leakage) a slouzi jen jako
    meritko trhu pri vyhodnoceni."""
    o, u = r.get("pre_o25"), r.get("pre_u25")
    try:
        o, u = float(o), float(u)
    except (TypeError, ValueError):
        return None
    if not (o > 1.0 and u > 1.0):  # chybejici (NaN) nebo nesmyslny kurz
        return None
    return "OU_2.5:over", (1 / o) / (1 / o + 1 / u)


def walk_forward(
    matches: pd.DataFrame,
    start,
    refit_days: int = 7,
    settings: Settings = SETTINGS,
    second: bool = False,
    cfg=None,
    goal_shrink: float | None = None,
    only=None,
    goal_base: dict[str, float] | None = None,
    anchor_goals: bool = False,
) -> pd.DataFrame:
    """Predikce 1X2 a over 2.5 pro zapasy od `start`, model se odhaduje jen z drivejsich zapasu.

    Vraci puvodni radky doplnene o p1, px, p2, p_over. Volitelne sloupce se zachovaji
    (napr. kurzy: sharp_1, sharp_x, sharp_2, odds_1, odds_x, odds_2). `second` prida druhy
    model (penaltyblog) jako q1, qx, q2, q_over. `cfg` = jine nastaveni modelu (reprezentace),
    `goal_shrink` = stazeni golovych trhu k prumeru jako u reprezentaci. `only(df)` vybere,
    ktere zapasy se predpovidaji (model se dal uci ze vsech). `goal_base` = prumery, ke kterym
    se golove trhy stahuji (vychozi: reprezentace). `anchor_goals` = uroven golu se sladi s ferovou linii
    Pinnacle na 2,5 golu jako v ostrem provozu – jen predzapasovy kurz (pre_o25/pre_u25), nikdy zaviraci;
    bez linie se pouzije stazeni.
    """
    from . import nations, secondopinion

    cfg = cfg or settings.football
    other = None
    df = matches.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    start = pd.Timestamp(start).date()
    test = df[df["date"] >= start].sort_values("date")
    if only is not None:
        test = test[only(test)]
    rows = []
    params, fitted_on = None, None
    for d, day in test.groupby("date", sort=True):
        if params is None or (d - fitted_on) >= timedelta(days=refit_days):
            try:
                params, fitted_on = football.fit(df, d, cfg=cfg), d
            except football.ModelError:
                continue
            if second:
                other = secondopinion.fit(df, d, cfg.xi, cfg.max_history_seasons)
        for _, r in day.iterrows():
            anchor = _ou25_anchor(r) if anchor_goals else None
            try:
                probs = (football.anchored_probabilities(params, r["home"], r["away"], anchor) if anchor
                         else football.market_probabilities(params, r["home"], r["away"]))
            except football.ModelError:
                continue
            if goal_shrink is not None and anchor is None:  # stejne stazeni golovych trhu jako v ostrem provozu
                probs = nations.shrink_goal_markets(probs, goal_shrink, goal_base)
            row = {**r.to_dict(), "p1": probs["1X2:1"], "px": probs["1X2:X"], "p2": probs["1X2:2"],
                   "p_over": probs["OU_2.5:over"], **{f"p:{k}": probs[k] for k in EVAL_KEYS if k in probs}}
            q = secondopinion.probabilities(other, r["home"], r["away"], bool(r.get("neutral", False))) if second else None
            if q and goal_shrink is not None:
                q = nations.shrink_goal_markets(q, goal_shrink, goal_base)
            if q:
                row.update(q1=q["1X2:1"], qx=q["1X2:X"], q2=q["1X2:2"], q_over=q["OU_2.5:over"],
                           **{f"q:{k}": q[k] for k in EVAL_KEYS if k in q})
            rows.append(row)
    return pd.DataFrame(rows)


def evaluate(
    preds: pd.DataFrame,
    tier: Tier = Tier.MAJOR,
    bankroll: float = 10_000.0,
    settings: Settings = SETTINGS,
) -> BacktestReport:
    """Vyhodnoti predikce. Sazeni se simuluje, jen kdyz jsou k dispozici kurzy."""
    if len(preds) == 0:
        return BacktestReport(0, 1.0, float("inf"), 0, 0.0, 0.0, 0.0, 0.0, False)
    res = np.where(
        preds["home_goals"] > preds["away_goals"], 0, np.where(preds["home_goals"] == preds["away_goals"], 1, 2)
    )
    p = preds[["p1", "px", "p2"]].to_numpy()
    y = np.eye(3)[res]
    brier = float(np.mean([calibration.brier_score(p[:, k], y[:, k]) for k in range(3)]))
    ll = float(-np.mean(np.log(np.clip(p[np.arange(len(p)), res], 1e-9, 1))))

    freq = y.mean(axis=0)
    baseline = float(np.mean([calibration.brier_score(np.full(len(y), freq[k]), y[:, k]) for k in range(3)]))
    have_odds = all(c in preds.columns for c in ("odds_1", "odds_x", "odds_2"))
    have_sharp = all(c in preds.columns for c in ("sharp_1", "sharp_x", "sharp_2"))
    market_brier = blend_brier = None
    if have_sharp:
        mk = []
        for r in preds.itertuples(index=False):
            try:
                mk.append(devig.fair_line([r.sharp_1, r.sharp_x, r.sharp_2]).probs)
            except (devig.DevigError, ValueError, TypeError):
                mk.append([np.nan] * 3)
        mk = np.asarray(mk, dtype=float)
        ok = ~np.isnan(mk).any(axis=1)
        if ok.sum() >= 50:
            w = settings.value.model_weight
            mix = np.array([[devig.blend_probability(p[i, k], mk[i, k], w) for k in range(3)] for i in np.where(ok)[0]])
            mix = mix / mix.sum(axis=1, keepdims=True)
            market_brier = float(np.mean([calibration.brier_score(mk[ok][:, k], y[ok][:, k]) for k in range(3)]))
            blend_brier = float(np.mean([calibration.brier_score(mix[:, k], y[ok][:, k]) for k in range(3)]))
    bets = wins = 0
    staked = pnl_total = 0.0
    equity = [bankroll]
    daily: dict = {}
    if have_odds:
        thr = settings.value.ev_threshold(tier)
        for i, r in enumerate(preds.itertuples(index=False)):
            offered = [r.odds_1, r.odds_x, r.odds_2]
            if any(pd.isna(o) for o in offered):
                continue
            src = [r.sharp_1, r.sharp_x, r.sharp_2] if have_sharp else offered
            try:
                market = devig.fair_line(src).probs
            except devig.DevigError:
                continue
            options = []
            for k in range(3):
                a = devig.assess_value(p[i, k], market[k], offered[k], thr, settings.value)
                if a.is_value:
                    options.append((a.ev, k, a))
            if not options:
                continue
            _, k, a = max(options)
            exposure = daily.get(r.date, {}).get("open", 0.0)
            st = risk.size_stake(a.prob, offered[k], equity[-1], exposure, cfg=settings.risk)
            if not st.allowed:
                continue
            won = res[i] == k
            pnl = st.stake * (offered[k] - 1.0) if won else -st.stake
            bets += 1
            wins += int(won)
            staked += st.stake
            pnl_total += pnl
            day = daily.setdefault(r.date, {"open": 0.0, "pnl": 0.0, "start": equity[-1]})
            day["open"] += st.stake
            day["pnl"] += pnl
            equity.append(equity[-1] + pnl)

    returns = [d["pnl"] / d["start"] for d in daily.values()]
    sharpe = calibration.sharpe_ratio(returns)
    v = settings.validation
    # Kvalita modelu: bud absolutni limit, nebo (kdyz je uzaviraci trh k dispozici) smes nesmi byt horsi nez trh.
    quality = brier < v.max_brier or (blend_brier is not None and blend_brier <= market_brier + 0.0005)
    passed = quality and brier < baseline and (not have_odds or sharpe > v.min_sharpe)
    return BacktestReport(
        matches=len(preds),
        brier=brier,
        log_loss=ll,
        bets=bets,
        win_rate=wins / bets if bets else 0.0,
        yield_pct=100.0 * pnl_total / staked if staked else 0.0,
        sharpe=sharpe,
        max_drawdown=calibration.max_drawdown(equity),
        passed=passed,
        market_brier=market_brier,
        blend_brier=blend_brier,
        baseline_brier=baseline,
    )


def calibrate(preds: pd.DataFrame, min_samples: int = 300) -> tuple[dict, float, float] | None:
    """Casove rozdeleny test kalibrace: skalery z prvni poloviny, mereni na druhe.

    Vraci (skalery z celeho vzorku, Brier pred, Brier po) nebo None pri malem vzorku.
    """
    if len(preds) < 2 * min_samples:
        return None
    preds = preds.sort_values("date").reset_index(drop=True)
    res = np.where(preds["home_goals"] > preds["away_goals"], 0, np.where(preds["home_goals"] == preds["away_goals"], 1, 2))
    half = len(preds) // 2
    cols = [preds[c].to_numpy() for c in ("p1", "px", "p2")]
    sc = calibration.fit_1x2_scalers(*(c[:half] for c in cols), res[:half], min_samples)
    test = [c[half:] for c in cols]
    y = np.eye(3)[res[half:]]
    before = float(np.mean([calibration.brier_score(test[k], y[:, k]) for k in range(3)]))
    cal = np.array([calibration.apply_1x2(sc, *(t[i] for t in test)) for i in range(len(test[0]))])
    after = float(np.mean([calibration.brier_score(cal[:, k], y[:, k]) for k in range(3)]))
    full = calibration.fit_1x2_scalers(*cols, res, min_samples)
    return full, before, after


def _scores(p: np.ndarray, res: np.ndarray) -> tuple[float, float]:
    """(log-loss, Brier) pro matici pravdepodobnosti a indexy skutecnych vysledku."""
    p = np.clip(p / p.sum(axis=1, keepdims=True), 1e-9, 1.0)
    y = np.eye(p.shape[1])[res]
    return float(-np.mean(np.log(p[np.arange(len(p)), res]))), float(np.mean((p - y) ** 2))


def _fair(rows: np.ndarray) -> np.ndarray:
    out = np.full(rows.shape, np.nan)
    for i, odds in enumerate(rows):
        if np.isnan(odds).any():
            continue
        try:
            out[i] = devig.fair_line(list(odds)).probs
        except (devig.DevigError, ValueError):
            pass
    return out


def _pair(a: np.ndarray) -> np.ndarray:
    """Dvoucestny trh je ulozen jednim sloupcem (over); druha strana je doplnek."""
    return np.column_stack([a[:, 0], 1.0 - a[:, 0]]) if a.shape[1] == 1 else a


NO_MODEL, OURS, SECOND, BOTH, MARKET, BLEND = (
    "bez modelu (jen četnosti)", "náš model", "druhý model (penaltyblog)", "průměr obou modelů",
    "zavírací kurz (trh)", "směs našeho modelu a trhu")


def compare(preds: pd.DataFrame, weight: float = SETTINGS.value.model_weight) -> list[dict]:
    """Srovnani zdroju na stejnych zapasech: nas model, druhy model, jejich prumer, zaviraci trh, smes modelu a trhu.

    Radky {"market", "source", "n", "logloss", "brier"}; v ramci trhu se pocita jen na zapasech,
    kde jsou vsechny dostupne zdroje. Nizsi cislo je lepsi.
    """
    out: list[dict] = []
    if len(preds) == 0:
        return out
    hg, ag = preds["home_goals"].to_numpy(), preds["away_goals"].to_numpy()
    specs = (
        ("1X2", np.where(hg > ag, 0, np.where(hg == ag, 1, 2)), ["p1", "px", "p2"], ["q1", "qx", "q2"],
         ["sharp_1", "sharp_x", "sharp_2"]),
        ("OU_2.5", np.where(hg + ag > 2.5, 0, 1), ["p_over"], ["q_over"], ["sharp_o25", "sharp_u25"]),
        *((k.split(":")[0], _outcome(k, hg, ag), [f"p:{k}"], [f"q:{k}"], []) for k in EVAL_KEYS if k != "OU_2.5:over"),
    )
    for market, res, pcols, qcols, ocols in specs:
        if not set(pcols) <= set(preds.columns):
            continue
        src = {OURS: _pair(preds[pcols].to_numpy(dtype=float))}
        if set(qcols) <= set(preds.columns):
            src[SECOND] = _pair(preds[qcols].to_numpy(dtype=float))
            src[BOTH] = (src[OURS] + src[SECOND]) / 2
        if ocols and set(ocols) <= set(preds.columns):
            mk = _fair(preds[ocols].to_numpy(dtype=float))
            if (~np.isnan(mk).any(axis=1)).sum() >= 30:
                src[MARKET] = mk
                src[BLEND] = weight * src[OURS] + (1 - weight) * mk
        ok = ~np.any([np.isnan(a).any(axis=1) for a in src.values()], axis=0)
        if ok.sum() < 30:
            continue
        freq = np.bincount(res[ok], minlength=src[OURS].shape[1]) / ok.sum()
        src = {NO_MODEL: np.tile(freq, (len(res), 1)), **src}
        for name, a in src.items():
            ll, br = _scores(a[ok], res[ok])
            out.append({"market": market, "source": name, "n": int(ok.sum()), "logloss": ll, "brier": br})
    return out


def nhl_walk_forward(df: pd.DataFrame, seasons: int = 2, settings: Settings = SETTINGS) -> pd.DataFrame:
    """Predikce NHL po dnech: sily tymu jen z drivejsich zapasu, bez brankaru a bez ukotveni na trh
    (historicke kurzy ani brankari po dnech v datech nejsou). Zapasy rozhodnute v najezdech maji
    v datech shodne skore a z vyhodnoceni viteze se vynechavaji."""
    from . import nhl_data
    from .models import nhl

    last = int(df["season"].max())
    home = df[(df["home_or_away"] == "HOME") & (df["season"] > last - seasons)]
    hist = df[df["season"] >= last - seasons - 1]
    rows = []
    for day, games in home.groupby("date", sort=True):
        try:
            ratings = nhl_data.NHLRatings(hist, day, settings.nhl)
        except ValueError:
            continue
        for g in games.itertuples(index=False):
            ctx = ratings.context(g.team, g.opposingTeam, day)
            if ctx is None:
                continue
            lam_h, lam_a = nhl.expected_goals(ctx, settings.nhl)
            sim = nhl.simulate(lam_h, lam_a, settings.nhl, np.random.default_rng(int(g.gameId)))
            rows.append({"date": day, "home": g.team, "away": g.opposingTeam, "home_goals": int(g.goalsFor),
                         "away_goals": int(g.goalsAgainst), "p_home": sim["MONEYLINE:home"],
                         "p_over55": sim["OU_5.5:over"], "p_over65": sim["OU_6.5:over"], "exp_total": lam_h + lam_a})
    return pd.DataFrame(rows)


def nhl_compare(preds: pd.DataFrame) -> list[dict]:
    """Model NHL proti odhadu bez modelu (cetnosti). Kurzy v datech nejsou, proto bez srovnani s trhem."""
    out: list[dict] = []
    if len(preds) == 0:
        return out
    hg, ag = preds["home_goals"].to_numpy(), preds["away_goals"].to_numpy()
    total = hg + ag + (hg == ag)  # najezdy pridavaji vitezi jeden gol
    for market, col, res, ok in (
        ("MONEYLINE", "p_home", np.where(hg > ag, 0, 1), hg != ag),
        ("OU_5.5", "p_over55", np.where(total > 5.5, 0, 1), np.ones(len(hg), bool)),
        ("OU_6.5", "p_over65", np.where(total > 6.5, 0, 1), np.ones(len(hg), bool)),
    ):
        p = preds[col].to_numpy(dtype=float)[ok]
        r = res[ok]
        freq = np.bincount(r, minlength=2) / len(r)
        for name, a in ((NO_MODEL, np.tile(freq, (len(r), 1))), (OURS, np.column_stack([p, 1 - p]))):
            ll, br = _scores(a, r)
            out.append({"market": market, "source": name, "n": int(len(r)), "logloss": ll, "brier": br})
    return out


PROPS_LINES = ("ROHY_9.5", "ROHY_10.5", "KARTY_3.5", "KARTY_4.5", "KARTY_5.5")


def props_compare(start) -> list[dict]:
    """Rohy a karty: model (tymy + rozhodci) proti cetnosti dane ligy pred testem."""
    from . import cups, ingestion, referees
    from .models import props

    preds, bases = [], []
    for lg in cups.DOMESTIC:
        try:
            df = referees.attach(ingestion.load_football(lg), lg)
        except (FileNotFoundError, ValueError):
            continue
        wf = props.walk_forward(df, max(start, df["date"].min()))
        before = df[df["date"] < start]
        for kind, (hc, ac, _) in props.KINDS.items():
            if len(wf) and hc in before:
                tot = (before[hc] + before[ac]).dropna()
                for m in PROPS_LINES:
                    if m.startswith(kind) and len(tot):
                        wf[f"base_{m}"] = float((tot > float(m.split("_")[1])).mean())
        preds.append(wf)
    out: list[dict] = []
    d = pd.concat(preds, ignore_index=True) if preds else pd.DataFrame()
    for m in PROPS_LINES:
        kind, line = m.split("_")
        if f"base_{m}" not in d or f"{m}:over" not in d:
            continue
        ok = d[[f"{kind}_total", f"base_{m}", f"{m}:over"]].notna().all(axis=1).to_numpy()
        r = np.where(d[f"{kind}_total"].to_numpy()[ok] > float(line), 0, 1)
        for name, p in ((NO_MODEL, d[f"base_{m}"].to_numpy()[ok]), (OURS, d[f"{m}:over"].to_numpy()[ok])):
            ll, br = _scores(np.column_stack([p, 1 - p]).astype(float), r)
            out.append({"market": m, "source": name, "n": int(ok.sum()), "logloss": ll, "brier": br})
    return out


STATE_KEY = "backtest_report"
RUNNING_KEY = "backtest_running"


def _group(name: str, note: str, rows: list[dict]) -> dict:
    for market in {r["market"] for r in rows}:
        best = min((r for r in rows if r["market"] == market and r["source"] != NO_MODEL), key=lambda r: r["logloss"])
        best["best"] = True
    return {"name": name, "note": note, "rows": rows}


def run_and_store(factory, seasons: int = 3) -> dict:
    """Zpetny test vsech modelu a ulozeni vysledku pro zalozku Overeni modelu."""
    import json

    from .storage import Ledger

    try:
        report = run_all(seasons)
    finally:
        with factory() as session:
            Ledger(session)._set(RUNNING_KEY, "")
            session.commit()
    with factory() as session:
        Ledger(session)._set(STATE_KEY, json.dumps(report, ensure_ascii=False))
        session.commit()
    return report


def run_all(seasons: int = 3, settings: Settings = SETTINGS, progress=print) -> dict:
    """Zpetny test vsech modelu: ligy a reprezentace proti zaviracim kurzum a druhemu modelu, NHL proti cetnostem."""
    from dataclasses import replace
    from datetime import date, datetime

    from . import ingestion, nations, nhl_data
    from .config import LEAGUES

    start = date(ingestion.current_season_start_year() - seasons + 1, 7, 1)
    groups = []
    for lg in ingestion.FOOTBALL_DATA_CODES:
        try:
            df = ingestion.load_football(lg)
        except (FileNotFoundError, ValueError):
            continue
        first = max(start, df["date"].min())
        before = df[df["date"] < first]
        base = nations.goal_base_rates(before if len(before) >= 200 else df)  # prumery z drivejska, bez nahledu do testu
        preds = walk_forward(df, first, second=True, settings=settings, goal_shrink=settings.football.league_goal_shrink,
                             goal_base=base, anchor_goals=True)
        rows = compare(preds, settings.value.for_league(lg).model_weight)
        if rows:
            groups.append(_group(LEAGUES[lg].name, "zavírací kurzy Pinnacle", rows))
        progress(f"{lg}: hotovo")
    rows = props_compare(start)
    if rows:
        groups.append(_group("Rohy a karty (6 lig)", "kurzy na rohy a karty v datech nejsou; bez modelu = četnost ligy před testem, karty včetně rozhodčího", rows))
    progress("rohy a karty: hotovo")
    try:
        intl = nations.load_international(neutral=True)
        cfg = replace(settings.football, xi=settings.football.national_xi,
                      max_history_seasons=settings.football.national_history_seasons)
        preds = walk_forward(intl, start, second=True, cfg=cfg, goal_shrink=settings.football.national_goal_shrink,
                             only=lambda d: d["tournament"].str.contains("UEFA"), settings=settings)
        rows = compare(preds)
        if rows:
            groups.append(_group("Reprezentace (Liga národů a další soutěže UEFA)", "historické kurzy v datech nejsou, proto bez srovnání s trhem", rows))
        progress("reprezentace: hotovo")
    except FileNotFoundError:
        pass
    try:
        rows = nhl_compare(nhl_walk_forward(nhl_data.load_nhl(min_season=date.today().year - 4), seasons, settings))
        if rows:
            groups.append(_group("NHL", "bez brankářů a bez kurzů (v historických datech nejsou); zápasy rozhodnuté v nájezdech se u vítěze nepočítají", rows))
        progress("NHL: hotovo")
    except FileNotFoundError:
        pass
    return {"when": datetime.now().strftime("%d.%m.%Y %H:%M"), "seasons": seasons, "groups": groups}
