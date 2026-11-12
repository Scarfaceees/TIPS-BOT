"""NHL model: xG z neblokovanych strel + GSAx brankare, Monte Carlo pro vitezstvi vcetne OT/SO."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..config import SETTINGS, NHLModelConfig


@dataclass(frozen=True)
class TeamRating:
    """Tymove metriky na 60 minut (vsechny herni situace)."""

    xgf60: float  # ocekavane goly pro
    xga60: float  # ocekavane goly proti
    ff60: float | None = None  # neblokovane strely pro (Fenwick)
    fa60: float | None = None  # neblokovane strely proti
    games: int = 82


@dataclass(frozen=True)
class Goalie:
    gsax: float = 0.0  # Goals Saved Above Expected (kumulativne)
    shots: float = 0.0  # celene strely, ze kterych je GSAx spocitano


@dataclass(frozen=True)
class GameContext:
    home: TeamRating
    away: TeamRating
    league_xg60: float
    league_ff60: float | None = None
    home_goalie: Goalie = Goalie()
    away_goalie: Goalie = Goalie()
    # True jen pri druhem zapase ve dvou dnech A cestovani pres casova pasma.
    home_b2b_travel: bool = False
    away_b2b_travel: bool = False


def cold_start_rating(prior: TeamRating, current: TeamRating, cfg: NHLModelConfig | None = None) -> TeamRating:
    """V prvnich zapasech sezony michani minule (60 %) a aktualni (40 %) sezony."""
    cfg = cfg or SETTINGS.nhl
    if current.games >= cfg.cold_start_games:
        return current
    w = cfg.cold_start_prior_weight

    def mix(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return b if a is None else a
        return w * a + (1 - w) * b

    return TeamRating(
        xgf60=mix(prior.xgf60, current.xgf60),  # type: ignore[arg-type]
        xga60=mix(prior.xga60, current.xga60),  # type: ignore[arg-type]
        ff60=mix(prior.ff60, current.ff60),
        fa60=mix(prior.fa60, current.fa60),
        games=current.games,
    )


def goalie_gsax60(goalie: Goalie, cfg: NHLModelConfig | None = None) -> float:
    """GSAx na 60 minut, stazene k nule podle velikosti vzorku."""
    cfg = cfg or SETTINGS.nhl
    if goalie.shots <= 0:
        return 0.0
    per_shot = goalie.gsax / (goalie.shots + cfg.goalie_regression_shots)
    return per_shot * cfg.shots_against_per_60


def _offence(t: TeamRating, ctx: GameContext, cfg: NHLModelConfig) -> float:
    if t.ff60 is None or not ctx.league_ff60:
        return t.xgf60
    volume = t.ff60 * ctx.league_xg60 / ctx.league_ff60
    return (1 - cfg.shot_volume_weight) * t.xgf60 + cfg.shot_volume_weight * volume


def _defence(t: TeamRating, ctx: GameContext, cfg: NHLModelConfig) -> float:
    if t.fa60 is None or not ctx.league_ff60:
        return t.xga60
    volume = t.fa60 * ctx.league_xg60 / ctx.league_ff60
    return (1 - cfg.shot_volume_weight) * t.xga60 + cfg.shot_volume_weight * volume


def expected_goals(ctx: GameContext, cfg: NHLModelConfig | None = None) -> tuple[float, float]:
    """Ocekavane goly domacich a hostu v zakladni hraci dobe."""
    cfg = cfg or SETTINGS.nhl
    lg = ctx.league_xg60
    lam_h = _offence(ctx.home, ctx, cfg) * _defence(ctx.away, ctx, cfg) / lg * cfg.home_factor
    lam_a = _offence(ctx.away, ctx, cfg) * _defence(ctx.home, ctx, cfg) / lg * cfg.away_factor
    if ctx.home_b2b_travel:
        lam_h *= 1.0 - cfg.b2b_xg_penalty
    if ctx.away_b2b_travel:
        lam_a *= 1.0 - cfg.b2b_xg_penalty
    lam_h -= goalie_gsax60(ctx.away_goalie, cfg)
    lam_a -= goalie_gsax60(ctx.home_goalie, cfg)
    return max(lam_h, cfg.min_expected_goals), max(lam_a, cfg.min_expected_goals)


def simulate(
    lam_h: float,
    lam_a: float,
    cfg: NHLModelConfig | None = None,
    rng: np.random.Generator | None = None,
) -> dict[str, float]:
    """Monte Carlo simulace zapasu. Vraci pravdepodobnosti hlavnich trhu.

    Puckline a Over/Under se pocitaji z konecneho skore vcetne golu z OT/SO.
    """
    cfg = cfg or SETTINGS.nhl
    rng = rng or np.random.default_rng(cfg.mc_seed)
    n = cfg.mc_iterations

    cov = min(cfg.regulation_covariance, 0.5 * min(lam_h, lam_a))
    shared = rng.poisson(cov, n)
    h = rng.poisson(lam_h - cov, n) + shared
    a = rng.poisson(lam_a - cov, n) + shared

    tie = h == a
    rate = (lam_h + lam_a) / 60.0 * cfg.ot_scoring_multiplier
    p_ot_goal = 1.0 - math.exp(-rate * cfg.ot_length_minutes)
    share = lam_h / (lam_h + lam_a)
    p_home_ot = 0.5 + (share - 0.5) * cfg.ot_strength_shrink
    ot_decided = rng.random(n) < p_ot_goal
    home_ot = rng.random(n) < p_home_ot
    home_so = rng.random(n) < 0.5
    home_extra = np.where(ot_decided, home_ot, home_so)

    fh = h + (tie & home_extra)
    fa = a + (tie & ~home_extra)
    margin = fh - fa
    total = fh + fa

    return {
        "lambda_home": lam_h,
        "lambda_away": lam_a,
        "REG:1": float(np.mean(h > a)),
        "REG:X": float(np.mean(tie)),
        "REG:2": float(np.mean(h < a)),
        "MONEYLINE:home": float(np.mean(margin > 0)),
        "MONEYLINE:away": float(np.mean(margin < 0)),
        "PUCKLINE_1.5:home-1.5": float(np.mean(margin >= 2)),
        "PUCKLINE_1.5:away+1.5": float(np.mean(margin < 2)),
        "PUCKLINE_1.5R:home+1.5": float(np.mean(margin > -2)),
        "PUCKLINE_1.5R:away-1.5": float(np.mean(margin <= -2)),
        "OU_5.5:over": float(np.mean(total > 5.5)),
        "OU_5.5:under": float(np.mean(total < 5.5)),
        "OU_6.5:over": float(np.mean(total > 6.5)),
        "OU_6.5:under": float(np.mean(total < 6.5)),
        "OU_4.5:over": float(np.mean(total > 4.5)),
        "OU_4.5:under": float(np.mean(total < 4.5)),
        "OU_7.5:over": float(np.mean(total > 7.5)),
        "OU_7.5:under": float(np.mean(total < 7.5)),
        "TTH_2.5:over": float(np.mean(fh > 2.5)),
        "TTH_2.5:under": float(np.mean(fh < 2.5)),
        "TTA_2.5:over": float(np.mean(fa > 2.5)),
        "TTA_2.5:under": float(np.mean(fa < 2.5)),
        "TTH_3.5:over": float(np.mean(fh > 3.5)),
        "TTH_3.5:under": float(np.mean(fh < 3.5)),
        "TTA_3.5:over": float(np.mean(fa > 3.5)),
        "TTA_3.5:under": float(np.mean(fa < 3.5)),
        "OT:yes": float(np.mean(tie)),
        "OT:no": float(np.mean(~tie)),
        "p_overtime": float(np.mean(tie)),
    }


def market_probabilities(
    ctx: GameContext,
    cfg: NHLModelConfig | None = None,
    rng: np.random.Generator | None = None,
    total_anchor: tuple[str, float] | None = None,
    seed: int = 0,
) -> dict[str, float]:
    """Pravdepodobnosti trhu. `total_anchor` = (trh napr. "OU_6.5:over", ferova p z trhu):
    uroven golu se doladi tak, aby model trefil trzni total; model pak urcuje jen rozdeleni
    mezi tymy (kdo vyhraje), ne celkovy pocet golu (ten ma trh presnejsi)."""
    lam_h, lam_a = expected_goals(ctx, cfg)
    if total_anchor is not None:
        key, target = total_anchor
        lo, hi = 0.6, 1.6
        for _ in range(14):
            mid = (lo + hi) / 2
            p = simulate(lam_h * mid, lam_a * mid, cfg, np.random.default_rng(seed))[key]
            if (p < target) == key.endswith("over"):
                lo = mid
            else:
                hi = mid
        k = (lo + hi) / 2
        lam_h, lam_a = lam_h * k, lam_a * k
        rng = np.random.default_rng(seed)
    out = simulate(lam_h, lam_a, cfg, rng)
    out["anchored"] = float(total_anchor is not None)  # 0 = uroven golu neni ukotvena na trh (totaly nejsou spolehlive)
    return out
