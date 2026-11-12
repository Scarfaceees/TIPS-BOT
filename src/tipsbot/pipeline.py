"""Sken: zive kurzy -> ferova linie -> model -> tipy. Zaroven kontroluje otevrene tipy."""

from __future__ import annotations

import logging
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

from . import analysis, calibration, context, detail, devig, ingestion, nhl_data, oddsapi
from .config import LEAGUES, SETTINGS, Settings, Sport
from .models import football, nhl
from .names import match_team, nhl_abbr
from .storage import Ledger, Tip, utcnow
from .tips import Candidate, Offer, Rejection, _betting_offers, best_per_match, evaluate, recheck

log = logging.getLogger(__name__)


@dataclass
class ScanReport:
    league: str
    events: int = 0
    with_fair_line: int = 0
    with_model: int = 0
    new_tips: list[Tip] = field(default_factory=list)
    cancelled: int = 0
    refreshed: int = 0
    credits_remaining: int | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        credit = f" | zbyva kreditu {self.credits_remaining}" if self.credits_remaining is not None else ""
        text = (
            f"{self.league}: zapasu {self.events}, s ferovou linii {self.with_fair_line}, s modelem {self.with_model} | "
            f"novych tipu {len(self.new_tips)}, zruseno {self.cancelled}, aktualizovano {self.refreshed}{credit}"
        )
        return text + "".join(f"\n  - {n}" for n in self.notes)


_ANCHOR_LINES = {"OU_4.5", "OU_5.5", "OU_6.5", "OU_7.5"}


def calibration_path(league: str, settings: Settings = SETTINGS):
    return settings.data_dir / "calibration" / f"{league}.json"


def apply_calibration(probs: dict[str, float], scalers) -> dict[str, float]:
    """Kalibrace 1X2 a prepocet odvozenych trhu (dvojita sance, sazka bez remizy)."""
    p1, px, p2 = calibration.apply_1x2(scalers, probs["1X2:1"], probs["1X2:X"], probs["1X2:2"])
    out = dict(probs)
    out.update({"1X2:1": p1, "1X2:X": px, "1X2:2": p2, "DC:1X": p1 + px, "DC:X2": px + p2, "DC:12": p1 + p2,
                "DNB:home": p1 / (p1 + p2), "DNB:away": p2 / (p1 + p2)})
    return out


def _total_anchor(ev: oddsapi.Event, settings: Settings) -> tuple[str, float] | None:
    """Trzni total (nejblizsi 50 %) z ferove linie ostrych kancelari - ukotveni urovne golu modelu."""
    best: tuple[str, float] | None = None
    for market in ev.markets.values():
        if market.market not in _ANCHOR_LINES:  # jen pulbodove linie, ktere model pocita (cela cisla maji vraceni)
            continue
        sharp, _ = oddsapi.sharp_and_offers(market, settings)
        try:
            p_over = devig.consensus_line(sharp).probs[0]
        except devig.DevigError:
            continue
        if best is None or abs(p_over - 0.5) < abs(best[1] - 0.5):
            best = (f"{market.market}:over", p_over)
    return best


class ModelProvider:
    """Modelove pravdepodobnosti pro udalost. Bez dat vraci None (pouzije se jen trh)."""

    def __init__(self, league: str, today: date, settings: Settings = SETTINGS):
        self.league = league
        self.settings = settings
        self.note: str | None = None
        self.params = None
        self.ratings = None
        self.unmatched: set[str] = set()
        self.df = None
        self.scalers = None
        try:
            if LEAGUES[league].sport is Sport.NHL:
                try:
                    goalies = nhl_data.team_goalies()
                except ValueError as e:
                    log.warning("brankari nejsou k dispozici: %s", e)
                    goalies = {}
                self.ratings = nhl_data.NHLRatings(nhl_data.load_nhl(min_season=today.year - 2), today, settings.nhl, goalies)
            else:
                df = self.df = ingestion.load_football(league)
                self.scalers = calibration.load_scalers(calibration_path(league, settings))
                start = date(ingestion.current_season_start_year(today), 7, 1)
                self.params = football.fit(df, today, season_start=start, cfg=settings.football)
        except (FileNotFoundError, ValueError) as e:
            self.note = f"model neni k dispozici ({e}); pouzita jen ferova linie trhu"

    def probabilities(self, ev: oddsapi.Event) -> dict[str, float] | None:
        if self.params is not None:
            home = match_team(ev.home, self.params.teams)
            away = match_team(ev.away, self.params.teams)
            if not home or not away:
                self.unmatched.update(n for n, m in ((ev.home, home), (ev.away, away)) if not m)
                return None
            probs = football.market_probabilities(self.params, home, away)
            return apply_calibration(probs, self.scalers) if self.scalers else probs
        if self.ratings is not None:
            home, away = nhl_abbr(ev.home), nhl_abbr(ev.away)
            ctx = self.ratings.context(home, away, ev.kickoff.date()) if home and away else None
            if ctx is None:
                self.unmatched.update((ev.home, ev.away))
                return None
            seed = zlib.crc32(ev.match_id.encode())
            return nhl.market_probabilities(
                ctx, self.settings.nhl, np.random.default_rng(seed), total_anchor=_total_anchor(ev, self.settings), seed=seed
            )
        return None


    def sections(self, ev: oddsapi.Event, probs: dict[str, float] | None) -> list[dict]:
        """Podrobny rozbor zapasu z dat modelu (prazdny, kdyz model chybi)."""
        if not probs:
            return []
        try:
            if self.params is not None and self.df is not None:
                h, a = match_team(ev.home, self.params.teams), match_team(ev.away, self.params.teams)
                return detail.football_sections(self.params, self.df, h, a, probs)
            if self.ratings is not None:
                return detail.nhl_sections(self.ratings, nhl_abbr(ev.home), nhl_abbr(ev.away), ev.kickoff.date(), probs)
        except Exception as e:  # noqa: BLE001 - rozbor nesmi shodit sken
            log.warning("rozbor %s selhal: %s", ev.name, e)
        return []


def build_candidates(
    ev: oddsapi.Event,
    model: dict[str, float] | None,
    settings: Settings = SETTINGS,
) -> tuple[list[Candidate], bool]:
    """Kandidati ze vsech povolenych trhu udalosti. Vraci i priznak, zda existuje ferova linie."""
    league = LEAGUES[ev.league]
    out: list[Candidate] = []
    has_line = False
    for market in ev.markets.values():
        if market.market not in league.markets:
            continue
        sharp, offers = oddsapi.sharp_and_offers(market, settings)
        try:
            line = devig.consensus_line(sharp)
        except devig.DevigError:
            continue
        has_line = True
        for i, sel in enumerate(market.selections):
            if not offers[sel]:
                continue
            p_market = line.probs[i]
            p_model = model.get(f"{market.market}:{sel}") if model else None
            out.append(
                Candidate(
                    match_id=ev.match_id,
                    league=ev.league,
                    match_name=ev.name,
                    kickoff=ev.kickoff,
                    market=market.market,
                    selection=sel,
                    p_model=p_model if p_model is not None else p_market,
                    p_market=p_market,
                    offers=tuple(Offer(b, o) for b, o in offers[sel]),
                )
            )
    return out, has_line


def scan_league(
    league: str,
    ledger: Ledger,
    now: datetime | None = None,
    settings: Settings = SETTINGS,
    events: list[oddsapi.Event] | None = None,
    provider: ModelProvider | None = None,
    force: bool = False,
) -> ScanReport:
    """Jeden pruchod jednou soutezi. `events` lze predat primo (testy, vlastni zdroj kurzu)."""
    now = now or utcnow()
    report = ScanReport(league)
    live = events is None  # doplnujici informace (zraneni, pocasi, novinky) jen pri zivem stazeni
    horizon = now + timedelta(hours=settings.timing.early_window_hours[1])
    if events is None:
        # Setreni kreditu: soutez bez zapasu v pristich 24 h se nestahuje.
        nxt = ledger.next_kickoff(league)
        if nxt is not None and nxt > horizon and not ledger.has_open_tips(league):
            report.notes.append(f"preskoceno, dalsi zapas {nxt:%d.%m. %H:%M} UTC")
            return report
        cached = ledger.odds_cache(league)
        if cached and not force and (now - cached[0]).total_seconds() < settings.odds_api.cache_seconds:
            events = oddsapi.parse_events(cached[1], league, settings)
            report.credits_remaining = cached[2]
            report.notes.append(f"kurzy z cache ({int((now - cached[0]).total_seconds() / 60)} min staré, kredity se nespotřebovaly)")
        else:
            fetched = oddsapi.fetch_events(league, settings, api_key=ledger.api_key() or None)
            events, report.credits_remaining = fetched.events, fetched.credits_remaining
            if fetched.payload is not None:
                ledger.set_odds_cache(league, now, fetched.payload, fetched.credits_remaining)
        upcoming = [e.kickoff for e in events if e.kickoff > now]
        ledger.set_next_kickoff(league, min(upcoming) if upcoming else now + timedelta(hours=24))
    events = [e for e in events if now < e.kickoff <= horizon]
    report.events = len(events)
    provider = provider or ModelProvider(league, now.date(), settings)
    if provider.note:
        report.notes.append(provider.note)

    by_id = {e.match_id: e for e in events}
    cands_by_match: dict[str, list[Candidate]] = {}
    models_by_match: dict[str, bool] = {}
    model_by_match: dict[str, dict | None] = {}
    sections_by_match: dict[str, list] = {}
    for ev in events:
        model = provider.probabilities(ev)
        cands, has_line = build_candidates(ev, model, settings)
        report.with_fair_line += int(has_line)
        report.with_model += int(model is not None and has_line)
        cands_by_match[ev.match_id] = cands
        models_by_match[ev.match_id] = model is not None
        model_by_match[ev.match_id] = model
        sections_by_match[ev.match_id] = provider.sections(ev, model) if hasattr(provider, "sections") else []

    # 1) kontrola otevrenych tipu teto souteze
    for tip in ledger.open_tips():
        if tip.league != league:
            continue
        cand = next(
            (c for c in cands_by_match.get(tip.match_id, []) if c.market == tip.market and c.selection == tip.selection),
            None,
        )
        if tip.match_id not in by_id and now < tip.kickoff:
            continue  # zapas v tomto behu chybi; tip nechame dobehnout platnost
        if cand is None:
            r = recheck(tip, ledger, None, now=now, settings=settings)
        else:
            bet = _betting_offers(cand.offers, settings)
            if tip.bookmaker.startswith("medián") and bet:
                current = bet[0].odds
                tip.bookmaker = bet[0].bookmaker
            else:
                current = next((o.odds for o in bet if o.bookmaker == tip.bookmaker), None)
            r = recheck(tip, ledger, current, cand.p_model, cand.p_market, now=now, settings=settings)
        report.cancelled += int(r.action == "cancel")
        report.refreshed += int(r.action == "refresh")

    # 2) nove tipy
    results: list[Tip | Rejection] = []
    for cands in cands_by_match.values():
        results.extend(evaluate(c, ledger, now=now, settings=settings) for c in cands)
    for tip in best_per_match(results):
        ledger.add_tip(tip)
        report.new_tips.append(tip)

    # 3) rozbor vsech zapasu pro zobrazeni (vsechny trhy, doporuceni)
    absences, checked = _absences(league, events, ledger, now, settings) if live else ([], False)
    cities: dict[str, str] = {}
    if live and LEAGUES[league].sport is Sport.FOOTBALL and ledger.api_football_key():
        for day in sorted({e.kickoff.date().isoformat() for e in events}):
            cities.update(context.fetch_venue_cities(league, ingestion.current_season_start_year(now.date()), day, ledger.api_football_key()))
    views = []
    for mid, cands in cands_by_match.items():
        tips_here = [t for t in ledger.open_tips() if t.match_id == mid]
        v = analysis.build_view(cands, tips_here, models_by_match.get(mid, False), now, model=model_by_match.get(mid), settings=settings)
        if v:
            ev = by_id[mid]
            v.sections = sections_by_match.get(mid, [])
            v.absence_checked = checked
            v.absences = [a.line() for a in context.for_match(absences, ev.home, ev.away)]
            v.news = [f"{t}: {h}" for t in (ev.home, ev.away) for h in context.fetch_news(t)[:2]] if live else []
            if LEAGUES[league].sport is Sport.FOOTBALL:
                city = cities.get(match_team(ev.home, cities) or "") or ""
                v.weather = (context.fetch_weather(city, ev.kickoff) or "") if city else ""
            views.append(v)
    views.sort(key=lambda v: v.kickoff)
    ledger.set_analysis(league, analysis.dumps(views))

    if provider.unmatched:
        report.notes.append("tymy bez modelu (jen trh): " + ", ".join(sorted(provider.unmatched)))
    return report


def _absences(league, events, ledger, now, settings) -> tuple[list, bool]:
    """Zraneni/absence: NHL z ESPN (bez klice), fotbal z API-Football (klic zdarma v Nastaveni)."""
    if LEAGUES[league].sport is Sport.NHL:
        got = context.fetch_nhl_injuries()
        return got, bool(got)
    key = ledger.api_football_key()
    if not key or not events:
        return [], False
    season = ingestion.current_season_start_year(now.date())
    out, ok = [], False
    for day in sorted({e.kickoff.date().isoformat() for e in events}):
        got = context.fetch_football_injuries(league, season, day, key)
        ok = ok or bool(got)
        out += got
    return out, ok


def scan_all(
    ledger: Ledger, leagues: list[str] | None = None, settings: Settings = SETTINGS, force: bool = False
) -> list[ScanReport]:
    reports = []
    for league in leagues or list(settings.odds_api.sport_keys):
        try:
            reports.append(scan_league(league, ledger, settings=settings, force=force))
        except oddsapi.OddsApiError as e:
            rep = ScanReport(league)
            rep.notes.append(str(e))
            reports.append(rep)
        except Exception as e:  # noqa: BLE001 - chyba jedne souteze nesmi shodit ostatni
            log.exception("sken %s selhal", league)
            rep = ScanReport(league)
            rep.notes.append(f"chyba skenu: {e!r}")
            reports.append(rep)
    return reports
