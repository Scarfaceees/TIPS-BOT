"""Opportunity Hunter: samostatne prochazi nadchazejici zapasy a hleda prilezitosti.

QUICK SCAN (levny): zapasy dne (1 pozadavek na den pro vsechny souteze) + vysledky souteze (cache 12 h)
  + kurzy 1X2 souteze (strankovane, cache 3 h) -> model A a trh B -> EV a rozdil model/trh.
  Vyradi zapasy, u kterych nema smysl utracet dalsi pozadavky.
DEEP ANALYSIS: jen nejlepsi kandidati (config.hunter.deep_max), poradi podle priority souteze a sily signalu.
Rezimy: DAILY (dnes), UPCOMING (nastavitelne okno), DEEP (konkretni zapas), WATCHLIST (watchlist.py).
Kazdy sken ma vlastni strop pozadavku (config.budget.max_per_run) a ridi se denni kvotou.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import mathcore
from . import models as M
from . import odds as O
from .analysis import MatchAnalysis, deep_analysis
from .config import Competition, Profil4Config
from .decision import Candidate, Selection, select
from .provider import NOT_STARTED, DataProvider, Fixture


@dataclass
class QuickResult:
    fixture: Fixture
    competition: Competition | None
    p_model: tuple[float, float, float] | None
    p_market: tuple[float, float, float] | None
    best_ev: float | None
    best_key: str = ""
    gap: float | None = None
    candidate: bool = False
    reason: str = ""

    @property
    def signal(self) -> float:
        return max(self.best_ev or -1.0, (self.gap or 0.0) - 0.04)


@dataclass
class ScanReport:
    mode: str
    started: datetime
    fixtures: int
    quick: list[QuickResult]
    analyses: list[MatchAnalysis]
    overall: Selection | None
    budget: dict
    notes: list[str] = field(default_factory=list)


def quick_scan(provider: DataProvider, fixtures: list[Fixture], now: datetime, cfg: Profil4Config) -> list[QuickResult]:
    comps = {c.league_id: c for c in provider.competitions()}
    by_league: dict[int, list[Fixture]] = {}
    for f in fixtures:
        by_league.setdefault(f.league_id, []).append(f)
    out: list[QuickResult] = []
    bet = provider.bet_id("Match Winner", 1)
    for lid, fxs in sorted(by_league.items(), key=lambda kv: comps.get(kv[0], Competition(0, "", 9)).priority):
        comp = comps.get(lid)
        season = fxs[0].season
        hist_cur = provider.season_results(lid, season)
        hist_past = provider.season_results(lid, season - 1, past=True, priority="LOW")
        history = list(hist_cur.data or []) + list(hist_past.data or [])
        names = {f.id: f.name for f in fxs}
        od = provider.odds_league(lid, season, bet=bet, names=names)
        quotes = [q for q in (od.data or []) if q.fixture_id in names]
        O.save(provider.client.store, O.with_kickoff(quotes, None))
        for f in fxs:
            fq = O.with_kickoff([q for q in O.latest(provider.client.store, f.id)], f.kickoff, f.name)
            cur = [q for q in fq if q.status(now, cfg.odds) == O.CURRENT]
            a = M.model_a(f, history, cfg)
            b = M.model_b(cur, cfg)
            qr = QuickResult(f, comp, None, None, None)
            if a.available:
                qr.p_model = tuple(mathcore.evaluate("1X2", s, a.matrix).p_win for s in ("1", "X", "2"))
            if b.available:
                qr.p_market = tuple(mathcore.evaluate("1X2", s, b.matrix).p_win for s in ("1", "X", "2"))
            if not a.available and not b.available:
                qr.reason = "chybí model i kurzy"
                out.append(qr)
                continue
            w, _ = M.model_weight(cfg, comp.bot_league if comp else "")
            c = M.model_c(a, b, w, "")
            best = O.best_prices(cur, now, cfg.odds)
            evs = [(mathcore.evaluate(q.market, q.selection, c.matrix, q.odds).ev, k) for k, q in best.items()
                   if q.market == "1X2"]
            if evs:
                qr.best_ev, qr.best_key = max(evs)
            if qr.p_model and qr.p_market:
                qr.gap = max(abs(x - y) for x, y in zip(qr.p_model, qr.p_market))
            hc = cfg.hunter
            if not best:
                qr.candidate, qr.reason = bool(a.available), "bez kurzů v API – hloubková analýza ověří trh / ruční kurzy"
            elif (qr.best_ev or -1) >= hc.quick_ev_min:
                qr.candidate, qr.reason = True, f"EV {qr.best_ev:+.1%} ({qr.best_key})"
            elif (qr.gap or 0) >= hc.quick_gap_min:
                qr.candidate, qr.reason = True, f"model a trh se liší o {qr.gap * 100:.0f} p.b."
            else:
                qr.reason = "bez signálu hodnoty"
            out.append(qr)
    return out


def _pick_deep(quick: list[QuickResult], cfg: Profil4Config, manual_ids: set[int]) -> list[QuickResult]:
    cands = [q for q in quick if q.candidate or q.fixture.id in manual_ids]
    cands.sort(key=lambda q: (q.fixture.id not in manual_ids, q.competition.priority if q.competition else 9, -q.signal))
    return cands[:max(cfg.hunter.deep_max, len(manual_ids & {q.fixture.id for q in cands}))]


def run_scan(provider: DataProvider, mode: str, now: datetime, cfg: Profil4Config, hours: int | None = None,
             news_fetcher=None, deep: bool = True, record=True, simulations: int | None = None) -> ScanReport:
    """DAILY (dnesni zapasy) nebo UPCOMING (okno `hours`)."""
    provider.client.reset_run()
    notes = []
    if mode == "daily":
        from zoneinfo import ZoneInfo

        local = now.astimezone(ZoneInfo(cfg.api.timezone)).date()
        days, end = [local], now + timedelta(days=1, hours=6)
    else:
        hours = hours or cfg.hunter.upcoming_hours
        end = now + timedelta(hours=hours)
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(cfg.api.timezone)
        d0, d1 = now.astimezone(tz).date(), end.astimezone(tz).date()
        days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    fixtures: list[Fixture] = []
    for d in days:
        res = provider.fixtures_on(d)
        notes.extend(res.errors)
        fixtures.extend(res.data or [])
    fixtures = list({f.id: f for f in fixtures if f.status in NOT_STARTED and now < f.kickoff <= end}.values())
    quick = quick_scan(provider, fixtures, now, cfg) if fixtures else []
    manual = {r["fixture_id"] for r in provider.client.store.query(
        "SELECT DISTINCT fixture_id FROM odds_quotes WHERE source != 'api'")}
    analyses = []
    if deep:
        comps = {c.league_id: c for c in provider.competitions()}
        for q in _pick_deep(quick, cfg, manual):
            if provider.client.budget.check("HIGH", q.fixture.id).allowed is False:
                notes.append(f"{q.fixture.name}: hloubková analýza vynechána – {provider.client.budget.check('HIGH').reason}")
                continue
            an = deep_analysis(provider, q.fixture, now, cfg, comps.get(q.fixture.league_id), news_fetcher,
                               simulations=simulations)
            analyses.append(an)
            if record:
                from . import history

                history.record(provider.client.store, an, cfg)
    overall = select(_all_candidates(analyses), cfg.decision) if analyses else None
    if not fixtures and not notes:
        notes.append("V okně nejsou žádné nadcházející zápasy sledovaných soutěží.")
    rep = ScanReport(mode, now, len(fixtures), quick, analyses, overall, provider.client.budget.status(), notes)
    return rep


def _all_candidates(analyses: list[MatchAnalysis]) -> list[Candidate]:
    """Kandidati vsech zapasu pro celkove poradi (kategorie se v celkovem vyberu pocitaji znovu)."""
    import copy

    out = []
    for an in analyses:
        for c in an.candidates:
            cc = copy.copy(c)
            cc.category = ""
            out.append(cc)
    return out


def analyze_fixture(provider: DataProvider, fixture_id: int, now: datetime, cfg: Profil4Config, news_fetcher=None,
                    record=True, simulations: int | None = None) -> MatchAnalysis:
    provider.client.reset_run()
    got = provider.fixtures_by_ids([fixture_id])
    if not got.data:
        raise ValueError(f"Zápas {fixture_id} se nepodařilo načíst: {'; '.join(got.errors) or 'API ho nevrátilo'}")
    fx = got.data[0]
    comps = {c.league_id: c for c in provider.competitions()}
    an = deep_analysis(provider, fx, now, cfg, comps.get(fx.league_id), news_fetcher, simulations=simulations)
    if record:
        from . import history

        history.record(provider.client.store, an, cfg)
    return an
