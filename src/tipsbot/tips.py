"""Tipovaci logika: vyber nabidky, vydani tipu, prubezna kontrola, zbytek vkladu, arbitraz."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from . import devig, risk
from .config import LEAGUES, SETTINGS, Settings
from .storage import TIP_OPEN, Ledger, Tip, utcnow


@dataclass(frozen=True)
class Offer:
    bookmaker: str
    odds: float


@dataclass(frozen=True)
class Candidate:
    """Jeden mozny vyber (zapas + trh + strana) s modelem, trhem a nabidkami."""

    match_id: str
    league: str  # klic z config.LEAGUES
    match_name: str
    kickoff: datetime  # UTC, bez tzinfo
    market: str
    selection: str
    p_model: float
    p_market: float  # z konsensu ostrych zdroju
    offers: Sequence[Offer] = field(default_factory=tuple)


@dataclass(frozen=True)
class Rejection:
    candidate: Candidate
    reasons: tuple[str, ...]


def window(kickoff: datetime, now: datetime, settings: Settings = SETTINGS) -> str | None:
    """'early' (12-24 h pred zapasem), 'main' (60-120 min) nebo None."""
    minutes = (kickoff - now).total_seconds() / 60.0
    lo, hi = settings.timing.main_window_minutes
    if lo <= minutes <= hi:
        return "main"
    elo, ehi = settings.timing.early_window_hours
    if elo * 60 <= minutes <= ehi * 60:
        return "early"
    return None


def _betting_offers(offers: Sequence[Offer], settings: Settings) -> list[Offer]:
    """Nabidky pro sazeni. Ceske kancelare maji prednost. Dokud nejsou, pouzije se MEDIAN
    ostatnich evropskych kancelari (nejvyssi kurz je castou odlehlou hodnotou a ceska
    kancelar ho nema), a to nejmene ze dvou."""
    bm = settings.bookmakers
    czech = sorted((o for o in offers if o.bookmaker in set(bm.betting_books)), key=lambda o: -o.odds)
    if czech or not bm.proxy_offers:
        return czech
    blocked = set(bm.sharp_weights) | set(bm.proxy_exclude)
    soft = sorted((o for o in offers if o.bookmaker not in blocked), key=lambda o: -o.odds)
    if len(soft) < 2:
        return []
    odds = sorted(o.odds for o in soft)
    n = len(odds)
    med = odds[n // 2] if n % 2 else (odds[n // 2 - 1] + odds[n // 2]) / 2.0
    return [Offer(f"medián {n} kanc. (max {soft[0].odds:.2f} {soft[0].bookmaker})", round(med, 3))]


def evaluate(
    cand: Candidate,
    ledger: Ledger,
    now: datetime | None = None,
    settings: Settings = SETTINGS,
) -> Tip | Rejection:
    """Vyhodnoti kandidata. Vraci neulozeny Tip, nebo duvody zamitnuti."""
    now = now or utcnow()
    league = LEAGUES.get(cand.league)
    if league is None:
        return Rejection(cand, ("neznama liga",))
    if cand.market not in league.markets:
        return Rejection(cand, ("trh neni povoleny",))
    win = window(cand.kickoff, now, settings)
    if win is None:
        return Rejection(cand, ("mimo sazkove okno",))
    if ledger.paused():
        return Rejection(cand, ("stop-loss aktivni",))
    if ledger.open_tip_on_match(cand.match_id) or ledger.bets_on_match(cand.match_id):
        return Rejection(cand, ("na utkani uz je tip nebo sazka",))
    offers = _betting_offers(cand.offers, settings)
    if not offers:
        return Rejection(cand, ("zadna nabidka ceske kancelare",))

    best = offers[0]
    early = win == "early"
    threshold = settings.value.ev_threshold(league.tier, early=early)
    a = devig.assess_value(cand.p_model, cand.p_market, best.odds, threshold, settings.value.for_league(cand.league))
    if not a.is_value:
        return Rejection(cand, a.reasons)

    stake = risk.size_stake(
        a.prob,
        best.odds,
        ledger.bankroll(),
        open_exposure_today=ledger.open_exposure(cand.kickoff.date()),
        bets_on_match=0,
        paused=False,
        cfg=settings.risk,
    )
    if not stake.allowed:
        return Rejection(cand, (stake.reason,))

    return Tip(
        match_id=cand.match_id,
        sport=league.sport.value,
        league=league.key,
        match_name=cand.match_name,
        kickoff=cand.kickoff,
        market=cand.market,
        selection=cand.selection,
        bookmaker=best.bookmaker,
        timestamp=now,
        valid_until=min(cand.kickoff, now + timedelta(seconds=settings.timing.tip_validity_seconds)),
        raw_odds=best.odds,
        min_odds=a.min_odds,
        devigged_sharp_odds=1.0 / cand.p_market,
        p_model=cand.p_model,
        p_market=cand.p_market,
        model_probability=a.prob,
        ev_percentage=a.ev * 100.0,
        ev_threshold=threshold,
        kelly_stake=stake.stake,
        early=early,
        needs_review=a.needs_review,
        model_version=settings.model_version,
        status=TIP_OPEN,
    )


def best_per_match(results: Sequence[Tip | Rejection], settings: Settings = SETTINGS) -> list[Tip]:
    """Na utkani necha vsechny tipy s dostatecnou hodnotou (od nejvyssiho EV), nejvyse `max_bets_per_match`.

    Limit slouzi jen jako pojistka proti korelovanym sazkam na jeden zapas; tipu je tolik, kolik jich ma vahu.
    """
    per_match: dict[str, list[Tip]] = {}
    for r in results:
        if isinstance(r, Tip):
            per_match.setdefault(r.match_id, []).append(r)
    out: list[Tip] = []
    for tips in per_match.values():
        tips.sort(key=lambda t: -t.ev_percentage)
        out.extend(tips[: settings.risk.max_bets_per_match])
    return sorted(out, key=lambda t: (t.kickoff, -t.ev_percentage))


@dataclass(frozen=True)
class Recheck:
    action: str  # "keep", "refresh", "cancel"
    reason: str
    odds: float | None = None
    ev_pct: float | None = None


def recheck(
    tip: Tip,
    ledger: Ledger,
    current_odds: float | None,
    p_model: float | None = None,
    p_market: float | None = None,
    now: datetime | None = None,
    settings: Settings = SETTINGS,
) -> Recheck:
    """Prubezna kontrola otevreneho tipu po obnove kurzu (bod 17 a 22).

    Kdyz EV spadne pod prah, tip se zrusi. Kdyz vyprsi platnost a hodnota
    trva, platnost se prodlouzi a kurz i EV se aktualizuji.
    """
    now = now or utcnow()
    if tip.status != TIP_OPEN:
        return Recheck("keep", "tip uz neni otevreny")
    if now >= tip.kickoff:
        ledger.cancel_tip(tip, "zapas zacal", expired=True)
        return Recheck("cancel", "zapas zacal")
    if ledger.paused():
        ledger.cancel_tip(tip, "stop-loss aktivni")
        return Recheck("cancel", "stop-loss aktivni")
    if current_odds is None:
        ledger.cancel_tip(tip, "nabidka zmizela")
        return Recheck("cancel", "nabidka zmizela")

    pm = tip.p_model if p_model is None else p_model
    pk = tip.p_market if p_market is None else p_market
    a = devig.assess_value(pm, pk, current_odds, tip.ev_threshold, settings.value.for_league(tip.league))
    if not a.is_value:
        reason = "; ".join(a.reasons)
        ledger.cancel_tip(tip, reason)
        return Recheck("cancel", reason, current_odds, a.ev * 100.0)

    changed = abs(current_odds - tip.raw_odds) > 1e-9 or p_model is not None or p_market is not None
    expired = now >= tip.valid_until
    if changed or expired:
        tip.raw_odds = current_odds
        tip.p_model, tip.p_market = pm, pk
        tip.model_probability = a.prob
        tip.devigged_sharp_odds = 1.0 / pk
        tip.ev_percentage = a.ev * 100.0
        tip.min_odds = a.min_odds
        tip.needs_review = a.needs_review
        tip.valid_until = min(tip.kickoff, now + timedelta(seconds=settings.timing.tip_validity_seconds))
        return Recheck("refresh", "aktualizovano", current_odds, a.ev * 100.0)
    return Recheck("keep", "beze zmeny", current_odds, a.ev * 100.0)


@dataclass(frozen=True)
class RemainderSuggestion:
    bookmaker: str
    odds: float
    stake: float


def remainder(
    tip: Tip,
    ledger: Ledger,
    offers: Sequence[Offer],
    settings: Settings = SETTINGS,
) -> RemainderSuggestion | None:
    """Kancelar omezila vklad: navrhne zbytek u dalsi ceske kancelare (bod 20)."""
    left = tip.kelly_stake - ledger.placed_for_tip(tip.id)
    step = settings.risk.stake_rounding
    if step > 0:
        left = int(left / step + 1e-9) * step
    if left < settings.risk.min_stake:
        return None
    for o in _betting_offers(offers, settings):
        if o.bookmaker == tip.bookmaker:
            continue
        a = devig.assess_value(tip.p_model, tip.p_market, o.odds, tip.ev_threshold, settings.value.for_league(tip.league))
        if a.is_value and o.odds >= tip.min_odds:
            return RemainderSuggestion(o.bookmaker, o.odds, float(left))
    return None


@dataclass(frozen=True)
class ArbSuggestion:
    yield_pct: float
    legs: tuple[tuple[str, str, float, float], ...]  # (vysledek, kancelar, kurz, vklad)


def find_arbitrage(
    market_offers: Mapping[str, Sequence[Offer]],
    total_stake: float,
    settings: Settings = SETTINGS,
) -> ArbSuggestion | None:
    """Bezrizikova arbitraz z nejlepsich kurzu na kazdy vysledek trhu (bod 24).

    `market_offers`: vysledek -> nabidky. Musi obsahovat vsechny vysledky trhu.
    """
    best: list[tuple[str, Offer]] = []
    for outcome, offers in market_offers.items():
        ranked = _betting_offers(offers, settings)
        if not ranked:
            return None
        best.append((outcome, ranked[0]))
    if len(best) < 2:
        return None
    odds = [o.odds for _, o in best]
    y = devig.arbitrage_yield(odds)
    if y <= settings.value.arb_min_yield:
        return None
    stakes = risk.arbitrage_stakes(odds, total_stake)
    legs = tuple((name, o.bookmaker, o.odds, round(s, 2)) for (name, o), s in zip(best, stakes))
    return ArbSuggestion(y * 100.0, legs)


def format_tip(tip: Tip, currency: str = SETTINGS.currency) -> str:
    """Text tipu pro zobrazeni / odeslani."""
    flag = "  [ZKONTROLOVAT: neobvykle vysoke EV]" if tip.needs_review else ""
    kind = "CASNY TIP" if tip.early else "TIP"
    return (
        f"{kind} #{tip.id} | {tip.league} | {tip.match_name}\n"
        f"{tip.market} -> {tip.selection}\n"
        f"Kancelar: {tip.bookmaker} | kurz {tip.raw_odds:.2f} (nesazet pod {tip.min_odds:.2f})\n"
        f"Vklad: {tip.kelly_stake:.0f} {currency} | EV {tip.ev_percentage:+.1f} % | "
        f"pravdepodobnost {tip.model_probability * 100:.1f} %\n"
        f"Plati do {tip.valid_until:%H:%M} UTC | vykop {tip.kickoff:%d.%m. %H:%M} UTC{flag}"
    )
