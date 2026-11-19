"""DEEP ANALYSIS jednoho zapasu: data -> coverage -> modely A–D -> Monte Carlo -> kandidati (EV, stress,
robustnost, rozhodnuti) -> kategorie A/B/C -> korelace -> sure bety -> kontext.

Pozadavky na API se planuji podle dulezitosti a pokryti souteze: sestavy jen tesne pred vykopem, endpointy,
ktere soutez nepokryva, se nevolaji vubec.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import datetime

from .. import profile2
from . import analyst, correlation, mathcore, montecarlo
from . import models as M
from . import odds as O
from . import surebet as SB
from .config import Competition, Profil4Config
from .coverage import CoverageReport, covered
from .decision import APPROVED, Candidate, Selection, decide, select
from .provider import (AVAILABLE, NOT_COVERED, NOT_YET, PARTIAL, SKIPPED, UNAVAILABLE, DataProvider, Fixture)
from .stress import freshness_factor, robustness, scenario_matrices, stress_test

SUPPORTED_PREFIX = ("OU_", "TTH_", "TTA_", "AH_")
SUPPORTED = ("1X2", "DC", "DNB", "BTTS", "EXPR")


@dataclass
class MatchAnalysis:
    fixture: Fixture
    created: datetime
    competition: Competition | None
    coverage: CoverageReport
    models: M.ModelSet | None
    mc: montecarlo.SimResult | None
    candidates: list[Candidate]
    selection: Selection
    context: analyst.ContextReport
    quotes: list[O.OddsQuote]
    correlations: list[correlation.Pair] = field(default_factory=list)
    surebets: list[SB.Surebet] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lineup_state: str = "pending"

    @property
    def data_quality(self) -> float:
        return self.coverage.quality()


def supported(market: str) -> bool:
    return market in SUPPORTED or market.startswith(SUPPORTED_PREFIX)


def lineup_state(status: str, kickoff: datetime, now: datetime) -> str:
    if status == AVAILABLE:
        return "confirmed"
    return "missing_near_kickoff" if (kickoff - now).total_seconds() < 30 * 60 else "pending"


def gather(provider: DataProvider, fx: Fixture, now: datetime, cfg: Profil4Config, comp: Competition | None):
    """Stahne data zapasu podle priorit a pokryti. Vraci (data dict, CoverageReport, poznamky)."""
    cov = CoverageReport(fx.id)
    cov.set("fixture", AVAILABLE, now)
    notes: list[str] = []
    info = provider.league_info(fx.league_id)
    coverage = info.data["coverage"] if info.data else None
    season = info.data["season"] if info.data else fx.season

    cur = provider.season_results(fx.league_id, fx.season or season)
    past = provider.season_results(fx.league_id, (fx.season or season) - 1, past=True, priority="LOW")
    history = list(cur.data or []) + list(past.data or [])
    finished = [f for f in history if f.finished and f.kickoff < fx.kickoff]
    st = AVAILABLE if len(finished) >= cfg.model.min_matches_model_a else PARTIAL if finished else cur.status
    cov.set("model_data", st, cur.fetched_at, f"{len(finished)} odehraných zápasů")
    team_games = sum(1 for f in finished if fx.home in (f.home, f.away)) + sum(1 for f in finished if fx.away in (f.home, f.away))
    cov.set("team_stats", AVAILABLE if team_games >= 10 else PARTIAL if team_games else UNAVAILABLE, cur.fetched_at,
            "forma a statistiky z výsledků soutěže")

    # kurzy
    if covered(coverage, "odds") is False:
        odds_res = None
        cov.set("odds", NOT_COVERED)
    else:
        odds_res = provider.odds_fixture(fx)
        O.save(provider.client.store, O.with_kickoff(odds_res.data or [], fx.kickoff, fx.name))
        cov.set("odds", odds_res.status, odds_res.fetched_at)
    quotes = O.with_kickoff(O.latest(provider.client.store, fx.id), fx.kickoff, fx.name)
    manual = [q for q in quotes if q.source != "api"]
    if manual and cov.status("odds") != AVAILABLE:
        cov.set("odds", PARTIAL, now, "jen ručně zadané / importované kurzy")
    if odds_res is not None and odds_res.errors:
        notes.extend(odds_res.errors)

    # absence
    injuries = inj_at = None
    if covered(coverage, "injuries") is False:
        cov.set("injuries", NOT_COVERED)
    else:
        r = provider.injuries([fx.id])
        if r.status not in (SKIPPED, "ERROR"):
            injuries, inj_at = (r.data or {}).get(fx.id, []), r.fetched_at
        cov.set("injuries", r.status if r.status != AVAILABLE or injuries else PARTIAL, r.fetched_at,
                "" if injuries else "API nehlásí absence")

    # sestavy (jen tesne pred vykopem)
    lineups = lu_at = None
    mins = (fx.kickoff - now).total_seconds() / 60
    if covered(coverage, "lineups") is False:
        cov.set("lineups", NOT_COVERED)
    elif mins > cfg.hunter.lineup_window_min:
        cov.set("lineups", NOT_YET, None, f"stahují se až {cfg.hunter.lineup_window_min} min před výkopem")
    else:
        r = provider.lineups(fx)
        lineups, lu_at = r.data, r.fetched_at
        cov.set("lineups", r.status, r.fetched_at)

    # tabulka, predikce, vzajemne zapasy
    standings = st_at = None
    if covered(coverage, "standings") is False:
        cov.set("standings", NOT_COVERED)
    else:
        r = provider.standings(fx.league_id, fx.season or season)
        standings, st_at = r.data, r.fetched_at
        cov.set("standings", r.status, r.fetched_at)
    preds = pr_at = None
    if not cfg.hunter.fetch_predictions:
        cov.set("predictions", SKIPPED, None, "vypnuto v konfiguraci")
    elif covered(coverage, "predictions") is False:
        cov.set("predictions", NOT_COVERED)
    else:
        r = provider.predictions(fx)
        preds, pr_at = r.data, r.fetched_at
        cov.set("predictions", r.status, r.fetched_at)
    h2h = h_at = None
    if cfg.hunter.fetch_h2h and fx.home_id and fx.away_id:
        r = provider.head_to_head(fx)
        h2h, h_at = r.data, r.fetched_at
        cov.set("h2h", r.status, r.fetched_at)
    else:
        cov.set("h2h", SKIPPED, None, "vypnuto")
    data = dict(history=history, quotes=quotes, injuries=injuries, inj_at=inj_at, lineups=lineups, lu_at=lu_at,
                standings=standings, st_at=st_at, preds=preds, pr_at=pr_at, h2h=h2h, h_at=h_at)
    return data, cov, notes


def build_candidates(fx: Fixture, ms: M.ModelSet, quotes: list[O.OddsQuote], now: datetime, cfg: Profil4Config,
                     dq: float, lstate: str) -> list[Candidate]:
    if not ms.c.available:
        return []
    cur = O.best_prices(quotes, now, cfg.odds, usable_only=True)
    every = O.best_prices(quotes, now, cfg.odds, usable_only=False)
    best = {**every, **cur}  # aktualni kurz ma prednost; zastaraly jen kdyz jiny neni (rozhodnuti ho oznaci)
    mats = scenario_matrices(ms, cfg, lstate == "confirmed")
    out = []
    for key, q in best.items():
        if not supported(q.market):
            continue
        try:
            ec = mathcore.evaluate(q.market, q.selection, ms.c.matrix, q.odds)
        except ValueError:
            continue
        p = {"C": ec.p_effective,
             "A": mathcore.evaluate(q.market, q.selection, ms.a.matrix).p_effective if ms.a.available else None,
             "B": mathcore.evaluate(q.market, q.selection, ms.b.matrix).p_effective if ms.b.available else None,
             "D": ms.d_probability(q.market, q.selection)}
        d = ms.evaluate_d(q.market, q.selection, q.odds)
        st = stress_test(ms, q.market, q.selection, q.odds, cfg, lstate == "confirmed", mats)
        limit = cfg.odds.max_age_api_min if q.source == "api" else cfg.odds.max_age_manual_min
        fresh = freshness_factor(q.age_minutes(now), limit) if q.status(now, cfg.odds) != O.HISTORICAL else 0.0
        rb = robustness(ms, q.market, q.selection, st, dq, fresh, lstate, cfg)
        cand = Candidate(
            fixture_id=fx.id, match=fx.name, market=q.market, selection=q.selection,
            label=profile2.label(q.market, q.selection), quote=q, quote_status=q.status(now, cfg.odds), p=p,
            p_win_c=ec.p_win + 0.5 * ec.p_half_win, p_push_c=ec.p_push, fair_odds_c=ec.fair_odds,
            fair_odds_d=(d[0].fair_odds if d else None), ev_c=ec.ev, ev_d=(d[0].ev if d else None),
            min_odds=mathcore.min_odds_for_ev(ec.a, ec.b, cfg.decision.ev_min), robustness=rb.score,
            data_quality=dq, models_complete=ms.a.available and ms.b.available, lineups=lstate,
            stress=st.scenarios, robustness_parts=rb.parts)
        out.append(decide(cand, cfg.decision))
    return out


def deep_analysis(provider: DataProvider, fx: Fixture, now: datetime, cfg: Profil4Config,
                  comp: Competition | None = None, news_fetcher=None, llm=None,
                  simulations: int | None = None) -> MatchAnalysis:
    data, cov, notes = gather(provider, fx, now, cfg, comp)
    dq = cov.quality()
    ms = M.build(fx, data["history"], [q for q in data["quotes"] if q.status(now, cfg.odds) == O.CURRENT],
                 cfg, dq, comp.bot_league if comp else "")
    for m in (ms.a, ms.b, ms.c):
        if not m.available or m.degraded:
            notes.append(f"{m.name}: {m.note}")
    lstate = lineup_state(cov.status("lineups"), fx.kickoff, now)
    cands = build_candidates(fx, ms, data["quotes"], now, cfg, dq, lstate)
    sel = select(cands, cfg.decision)

    mc = None
    if ms.c_unc is not None:
        top = sorted(cands, key=lambda c: (c.decision != APPROVED, -c.ev_c))[:8]
        sels = [("1X2", "1", None), ("1X2", "X", None), ("1X2", "2", None), ("OU_2.5", "over", None),
                ("BTTS", "yes", None)] + [(c.market, c.selection, c.odds) for c in top if c.market != "EXPR"]
        seen, uniq = set(), []
        for s in sels:
            if (s[0], s[1]) not in seen:
                seen.add((s[0], s[1]))
                uniq.append(s)
        mc = montecarlo.simulate(ms.c_unc, uniq, simulations or cfg.mc.simulations,
                                 cfg.mc.seed ^ zlib.crc32(str(fx.id).encode()), cfg.mc.ci_z)

    picks = [c for c in sel.categories.values() if c] + sel.approved[:3]
    uniq_picks = list({c.key: c for c in picks}.values())
    corr = []
    if ms.c.available:
        for i in range(len(uniq_picks)):
            for j in range(i + 1, len(uniq_picks)):
                a, b = uniq_picks[i], uniq_picks[j]
                if "EXPR" in (a.market, b.market):
                    continue
                corr.append(correlation.pair(ms.c.matrix, (a.market, a.selection), (b.market, b.selection)))
    sbs = SB.scan(data["quotes"], now, cfg.odds)
    ctx = analyst.analyse(fx, data["history"], data["injuries"], data["inj_at"], data["lineups"], data["lu_at"],
                          data["standings"], data["st_at"], data["h2h"], data["h_at"], data["preds"], data["pr_at"],
                          now, news_fetcher, llm)
    for c in cands:
        c.risks.extend(r for r in ctx.risks() if r not in c.risks)
    return MatchAnalysis(fx, now, comp, cov, ms, mc, cands, sel, ctx, data["quotes"], corr, sbs, notes, lstate)
