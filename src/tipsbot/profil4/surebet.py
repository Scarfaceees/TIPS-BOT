"""Surebet Scanner – oddeleny od value bettingu.

Kandidat vznikne, kdyz soucet 1/kurz nejlepsich kurzu vsech vysledku trhu je pod 1. Jako SUREBET se oznaci
jen tehdy, kdyz:
  - vsechny kurzy jsou AKTUÁLNÍ (stejne casove okno, zadny historicky ani zastaraly),
  - vsechny kancelare maji stejna pravidla vyhodnoceni trhu,
  - jsou zname minimalni vklady a limity kazde kancelare (zadava uzivatel) a rozdeleni vkladu se do nich vejde.
Jinak je to jen "POTENCIÁLNÍ – NEOVĚŘENO". Uvazuji se jen trhy s vylucnymi a uplnymi vysledky
(1X2, BTTS, pulove linie golu a asijskeho handicapu); cele a ctvrtinove linie se vynechavaji.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .. import profile2
from .config import OddsConfig
from .odds import CURRENT, OddsQuote


def _eligible(market: str) -> bool:
    if market in ("1X2", "BTTS"):
        return True
    if market.startswith(("OU_", "TTH_", "TTA_", "AH_")):
        line = float(market.split("_", 1)[1])
        return abs(line - round(line)) > 1e-9 and abs(line * 2 - round(line * 2)) < 1e-9
    return False


@dataclass
class Surebet:
    market: str
    legs: list[OddsQuote]
    book_sum: float
    margin: float  # zarucena vynosnost (1/sum - 1)
    stakes: list[float]
    payout: float
    status: str  # SUREBET | POTENCIÁLNÍ – NEOVĚŘENO
    problems: list[str] = field(default_factory=list)


def stake_split(odds: list[float], total: float) -> list[float]:
    s = sum(1.0 / o for o in odds)
    return [total * (1.0 / o) / s for o in odds]


def scan(quotes: list[OddsQuote], now: datetime, cfg: OddsConfig, total_stake: float = 1000.0,
         limits: dict[str, tuple[float, float]] | None = None) -> list[Surebet]:
    """`limits` = {kancelar: (min_vklad, max_vklad)}; bez nich nelze surebet overit."""
    limits = limits or {}
    best: dict[str, dict[str, OddsQuote]] = {}
    for q in quotes:
        if not _eligible(q.market):
            continue
        cur = best.setdefault(q.market, {})
        if q.selection not in cur or q.odds > cur[q.selection].odds:
            cur[q.selection] = q
    out = []
    for mk, per in best.items():
        sels = profile2.selections(mk)
        if not sels or not all(s in per for s in sels):
            continue
        legs = [per[s] for s in sels]
        book_sum = sum(1.0 / q.odds for q in legs)
        if book_sum >= 1.0:
            continue
        stakes = stake_split([q.odds for q in legs], total_stake)
        problems = []
        stale = [q.bookmaker for q in legs if q.status(now, cfg) != CURRENT]
        if stale:
            problems.append("neaktuální kurzy: " + ", ".join(stale))
        if len({q.rules for q in legs}) > 1:
            problems.append("kanceláře mají jiná pravidla vyhodnocení")
        if len({q.bookmaker for q in legs}) == 1:
            problems.append("všechny kurzy od jedné kanceláře – nejspíš chyba dat")
        for q, st in zip(legs, stakes):
            lim = limits.get(q.bookmaker)
            if lim is None:
                problems.append(f"neznámý min. vklad a limit u {q.bookmaker}")
            elif not (lim[0] <= st <= lim[1]):
                problems.append(f"vklad {st:.0f} mimo limit {q.bookmaker} ({lim[0]:.0f}–{lim[1]:.0f})")
        problems = list(dict.fromkeys(problems))
        out.append(Surebet(mk, legs, book_sum, 1.0 / book_sum - 1.0, stakes, total_stake / book_sum,
                           "SUREBET" if not problems else "POTENCIÁLNÍ – NEOVĚŘENO", problems))
    return sorted(out, key=lambda s: -s.margin)
