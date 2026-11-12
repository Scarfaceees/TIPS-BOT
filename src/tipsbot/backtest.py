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


def walk_forward(
    matches: pd.DataFrame,
    start,
    refit_days: int = 7,
    settings: Settings = SETTINGS,
) -> pd.DataFrame:
    """Predikce 1X2 pro zapasy od `start`, model se odhaduje jen z drivejsich zapasu.

    Vraci puvodni radky doplnene o p1, px, p2. Volitelne sloupce se zachovaji
    (napr. kurzy: sharp_1, sharp_x, sharp_2, odds_1, odds_x, odds_2).
    """
    df = matches.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    start = pd.Timestamp(start).date()
    test = df[df["date"] >= start].sort_values("date")
    rows = []
    params, fitted_on = None, None
    for d, day in test.groupby("date", sort=True):
        if params is None or (d - fitted_on) >= timedelta(days=refit_days):
            try:
                params, fitted_on = football.fit(df, d, cfg=settings.football), d
            except football.ModelError:
                continue
        for _, r in day.iterrows():
            try:
                m = football.match_matrix(params, r["home"], r["away"])
            except football.ModelError:
                continue
            p1, px, p2 = football.probs_1x2(m)
            rows.append({**r.to_dict(), "p1": p1, "px": px, "p2": p2})
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
