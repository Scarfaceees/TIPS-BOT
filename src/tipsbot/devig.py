"""Ferova linie a hodnota: odstraneni marze, konsensus ostrych zdroju, EV.

Vsechny funkce pracuji s desetinnymi kurzy jednoho trhu (vsechny vysledky
trhu ve stejnem poradi, napr. [1, X, 2] nebo [over, under]).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

from scipy.optimize import brentq

from .config import SETTINGS, DevigConfig, ValueConfig


class DevigError(ValueError):
    """Neplatne kurzy pro vypocet ferove linie."""


def _validate(odds: Sequence[float]) -> list[float]:
    if len(odds) < 2:
        raise DevigError("Trh musi mit aspon dva vysledky.")
    out = [float(o) for o in odds]
    if any(not math.isfinite(o) or o <= 1.0 for o in out):
        raise DevigError(f"Desetinne kurzy musi byt > 1.0: {odds}")
    return out


def implied_probs(odds: Sequence[float]) -> list[float]:
    """Hrube implikovane pravdepodobnosti (vcetne marze)."""
    return [1.0 / o for o in _validate(odds)]


def overround(odds: Sequence[float]) -> float:
    """Marze trhu: soucet implikovanych pravdepodobnosti minus 1."""
    return sum(implied_probs(odds)) - 1.0


def net_exchange_odds(odds: float, commission: float) -> float:
    """Kurz burzy po odecteni provize z cisteho zisku."""
    return 1.0 + (odds - 1.0) * (1.0 - commission)


def devig_multiplicative(odds: Sequence[float]) -> list[float]:
    q = implied_probs(odds)
    total = sum(q)
    return [x / total for x in q]


def devig_power(odds: Sequence[float]) -> list[float]:
    """Mocninna metoda: hleda k tak, aby sum(q_i ** k) == 1."""
    q = implied_probs(odds)
    total = sum(q)
    if abs(total - 1.0) < 1e-12:
        return q
    f = lambda k: sum(x**k for x in q) - 1.0  # noqa: E731
    # total > 1 -> k > 1 (zmensuje), total < 1 -> k < 1 (zvetsuje).
    lo, hi = (1.0, 50.0) if total > 1.0 else (1e-6, 1.0)
    k = brentq(f, lo, hi, xtol=1e-12)
    p = [x**k for x in q]
    s = sum(p)
    return [x / s for x in p]


def shin_z(odds: Sequence[float]) -> float:
    """Shinuv parametr z (podil informovanych sazkaru)."""
    q = implied_probs(odds)
    booksum = sum(q)
    if booksum <= 1.0:
        return 0.0

    def total(z: float) -> float:
        return sum(_shin_prob(x, booksum, z) for x in q) - 1.0

    return float(brentq(total, 0.0, 1.0 - 1e-9, xtol=1e-12))


def _shin_prob(q: float, booksum: float, z: float) -> float:
    return (math.sqrt(z * z + 4.0 * (1.0 - z) * q * q / booksum) - z) / (2.0 * (1.0 - z))


def devig_shin(odds: Sequence[float]) -> list[float]:
    """Shinova metoda. Pri marzi <= 0 (burza, arbitraz) pada na multiplikativni."""
    q = implied_probs(odds)
    booksum = sum(q)
    if booksum <= 1.0:
        return [x / booksum for x in q]
    z = shin_z(odds)
    p = [_shin_prob(x, booksum, z) for x in q]
    s = sum(p)
    return [x / s for x in p]


@dataclass(frozen=True)
class FairLine:
    probs: tuple[float, ...]
    method: str  # "shin" nebo "shin+power"
    overround: float
    shin_z: float

    @property
    def odds(self) -> tuple[float, ...]:
        return tuple(1.0 / p for p in self.probs)


def fair_line(odds: Sequence[float], cfg: DevigConfig | None = None) -> FairLine:
    """Shin jako primarni metoda, u outsideru kontrola pres Power.

    Kdyz se obe metody u outsidera rozejdou nad toleranci, pouzije se nizsi
    (opatrnejsi) pravdepodobnost a linie se znovu normalizuje.
    """
    cfg = cfg or SETTINGS.devig
    q = implied_probs(odds)
    shin = devig_shin(odds)
    power = devig_power(odds)
    probs = list(shin)
    lowered: set[int] = set()
    for i, raw in enumerate(q):
        if raw < cfg.longshot_prob and shin[i] > 0:
            if abs(shin[i] - power[i]) / shin[i] > cfg.longshot_tolerance and power[i] < shin[i]:
                probs[i] = power[i]
                lowered.add(i)
    adjusted = bool(lowered)
    if adjusted:
        # Ubranou pravdepodobnost dostanou ostatni vysledky, outsider zustane snizeny.
        rest = [i for i in range(len(probs)) if q[i] >= cfg.longshot_prob] or [
            i for i in range(len(probs)) if i not in lowered
        ]
        freed = 1.0 - sum(probs)
        rest_sum = sum(probs[i] for i in rest)
        if rest and rest_sum > 0:
            for i in rest:
                probs[i] += freed * probs[i] / rest_sum
        else:
            s = sum(probs)
            probs = [p / s for p in probs]
    return FairLine(
        probs=tuple(probs),
        method="shin+power" if adjusted else "shin",
        overround=sum(q) - 1.0,
        shin_z=shin_z(odds),
    )


@dataclass(frozen=True)
class Consensus:
    probs: tuple[float, ...]
    sources: tuple[str, ...]
    weights: tuple[float, ...]  # prepocitane vahy pouzitych zdroju

    @property
    def odds(self) -> tuple[float, ...]:
        return tuple(1.0 / p for p in self.probs)


def consensus_line(
    sharp_odds: Mapping[str, Sequence[float]],
    weights: Mapping[str, float] | None = None,
    commissions: Mapping[str, float] | None = None,
    min_sources: int | None = None,
    cfg: DevigConfig | None = None,
) -> Consensus:
    """Vazeny konsensus ferovych pravdepodobnosti z ostrych zdroju.

    Chybejici nebo neplatne zdroje se vynechaji a vahy se prepocitaji.
    """
    bm = SETTINGS.bookmakers
    weights = weights or bm.sharp_weights
    commissions = commissions if commissions is not None else bm.exchange_commission
    min_sources = bm.min_sharp_sources if min_sources is None else min_sources

    lines: dict[str, tuple[float, ...]] = {}
    n_outcomes: int | None = None
    for name, odds in sharp_odds.items():
        w = weights.get(name, 0.0)
        if w <= 0 or odds is None:
            continue
        try:
            fee = commissions.get(name, 0.0)
            net = [net_exchange_odds(o, fee) for o in _validate(odds)] if fee else odds
            line = fair_line(net, cfg)
        except DevigError:
            continue
        if n_outcomes is None:
            n_outcomes = len(line.probs)
        elif len(line.probs) != n_outcomes:
            raise DevigError("Zdroje maji ruzny pocet vysledku trhu.")
        lines[name] = line.probs

    if len(lines) < max(1, min_sources):
        raise DevigError(
            f"Nedostatek ostrych zdroju: {len(lines)} z pozadovanych {max(1, min_sources)}."
        )

    total_w = sum(weights[n] for n in lines)
    norm = {n: weights[n] / total_w for n in lines}
    assert n_outcomes is not None
    probs = [sum(norm[n] * lines[n][i] for n in lines) for i in range(n_outcomes)]
    s = sum(probs)
    names = tuple(lines)
    return Consensus(
        probs=tuple(p / s for p in probs),
        sources=names,
        weights=tuple(norm[n] for n in names),
    )


def blend_probability(p_model: float, p_market: float, model_weight: float | None = None) -> float:
    """Smicha modelovou a trzni pravdepodobnost (v logit prostoru)."""
    w = SETTINGS.value.model_weight if model_weight is None else model_weight
    eps = 1e-9
    pm = min(max(p_model, eps), 1 - eps)
    pk = min(max(p_market, eps), 1 - eps)
    logit = w * math.log(pm / (1 - pm)) + (1 - w) * math.log(pk / (1 - pk))
    return 1.0 / (1.0 + math.exp(-logit))


def expected_value(prob: float, odds: float) -> float:
    """EV = p * kurz - 1."""
    return prob * odds - 1.0


def min_acceptable_odds(prob: float, ev_min: float) -> float:
    """Nejnizsi kurz, pri kterem sazka jeste splnuje prah EV (zaokrouhleno nahoru)."""
    if prob <= 0:
        return math.inf
    return math.ceil((1.0 + ev_min) / prob * 100.0 - 1e-9) / 100.0


def arbitrage_yield(best_odds: Sequence[float]) -> float:
    """Cisty vynos arbitraze z nejlepsich kurzu na kazdy vysledek (zaporny = zadna)."""
    return 1.0 / sum(implied_probs(best_odds)) - 1.0


@dataclass(frozen=True)
class ValueAssessment:
    prob: float  # finalni pravdepodobnost pouzita pro EV
    p_model: float
    p_market: float
    offered_odds: float
    ev: float
    ev_threshold: float
    min_odds: float  # pod tento kurz uz nesazet
    is_value: bool
    needs_review: bool
    reasons: tuple[str, ...]


def assess_value(
    p_model: float,
    p_market: float,
    offered_odds: float,
    ev_threshold: float,
    cfg: ValueConfig | None = None,
) -> ValueAssessment:
    """Vyhodnoti jednu nabidku ceske kancelare proti modelu a ferove linii."""
    cfg = cfg or SETTINGS.value
    prob = blend_probability(p_model, p_market, cfg.model_weight)
    ev = expected_value(prob, offered_odds)
    reasons: list[str] = []

    if not (cfg.min_odds <= offered_odds <= cfg.max_odds):
        reasons.append("kurz mimo pasmo")
    if ev < ev_threshold:
        reasons.append("EV pod prahem")
    if prob < cfg.min_probability:
        reasons.append(f"sance pod {cfg.min_probability * 100:.0f} %")
    if abs(p_model - p_market) > cfg.max_model_market_gap:
        reasons.append("model a trh se prilis rozchazeji")
    # Sazka musi mit hodnotu i vuci samotnemu trhu, ne jen diky modelu.
    if expected_value(p_market, offered_odds) <= 0:
        reasons.append("bez hodnoty vuci ferove linii")

    needs_review = ev > cfg.ev_suspicious
    return ValueAssessment(
        prob=prob,
        p_model=p_model,
        p_market=p_market,
        offered_odds=offered_odds,
        ev=ev,
        ev_threshold=ev_threshold,
        min_odds=max(min_acceptable_odds(prob, ev_threshold), cfg.min_odds),
        is_value=not reasons,
        needs_review=needs_review,
        reasons=tuple(reasons),
    )
