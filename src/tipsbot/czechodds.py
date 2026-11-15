"""Rucne zadane kurzy ceskych kancelari (Tipsport, Fortuna, Chance, Betano, Betarena).

Zadne ceske kancelare nemaji verejne API, proto se kurzy vkladaji ručně. Bot je pripoji k
udalostem z kurzoveho API; ferova linie se pocita dal z ostrych zdroju, ceske kurzy slouzi jen
jako nabidka k sazeni. Trh bez ferove linie (napr. BTTS) nelze ohodnotit a preskoci se.

Format radku:  kancelar | Domaci - Hoste | trh | kurzy
  tipsport | Nemecko - Italie | 1X2 | 2.10 3.30 3.40
  tipsport | Nemecko - Italie | OU_2.5 | 1.90 1.95        (over under)
  fortuna  | Boston - Toronto | MONEYLINE | 1.85 2.00     (home away)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .names import match_team, normalize
from .oddsapi import Event
from .storage import Ledger

STATE_KEY = "czech_odds"
MAX_AGE = timedelta(hours=48)
_BOOKS = {"tipsport", "fortuna", "chance", "betano", "betarena"}
_SPLIT = re.compile(r"\s+[-–—]\s+")


# Ceske nazvy reprezentaci -> anglicke nazvy z kurzoveho API (klic je normalizovany).
_CZ_NATIONS = {
    "nemecko": "Germany", "italie": "Italy", "francie": "France", "spanelsko": "Spain",
    "portugalsko": "Portugal", "nizozemsko": "Netherlands", "belgie": "Belgium", "chorvatsko": "Croatia",
    "dansko": "Denmark", "svycarsko": "Switzerland", "rakousko": "Austria", "polsko": "Poland",
    "svedsko": "Sweden", "norsko": "Norway", "skotsko": "Scotland", "wales": "Wales", "irsko": "Ireland",
    "anglie": "England", "cesko": "Czech Republic", "slovensko": "Slovakia", "madarsko": "Hungary",
    "srbsko": "Serbia", "turecko": "Turkey", "recko": "Greece", "ukrajina": "Ukraine",
    "rumunsko": "Romania", "bulharsko": "Bulgaria", "izrael": "Israel", "island": "Iceland",
    "finsko": "Finland", "albanie": "Albania", "gruzie": "Georgia", "slovinsko": "Slovenia",
    "bosna a hercegovina": "Bosnia and Herzegovina", "bosna": "Bosnia and Herzegovina",
    "severni makedonie": "North Macedonia", "kosovo": "Kosovo", "kypr": "Cyprus",
    "lucembursko": "Luxembourg", "belorusko": "Belarus", "armenie": "Armenia", "azerbajdzan": "Azerbaijan",
    "kazachstan": "Kazakhstan", "lotyssko": "Latvia", "litva": "Lithuania", "estonsko": "Estonia",
    "malta": "Malta", "faerske ostrovy": "Faroe Islands", "gibraltar": "Gibraltar", "moldavsko": "Moldova",
    "cerna hora": "Montenegro", "severni irsko": "Northern Ireland", "lichtenstejnsko": "Liechtenstein",
    "san marino": "San Marino", "andorra": "Andorra",
}


def _en(name: str) -> str:
    return _CZ_NATIONS.get(normalize(name), name)


@dataclass(frozen=True)
class Entry:
    book: str
    home: str
    away: str
    market: str
    odds: tuple[float, ...]
    saved: datetime


def parse_line(line: str, now: datetime) -> Entry:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) != 4:
        raise ValueError("Radek musi mit tvar: kancelar | Domaci - Hoste | trh | kurzy")
    book, teams, market, raw = parts
    book = normalize(book).replace(" ", "")
    if book not in _BOOKS:
        raise ValueError(f"Neznama kancelar '{parts[0]}' (povolene: {', '.join(sorted(_BOOKS))})")
    names = _SPLIT.split(teams)
    if len(names) != 2 or not all(names):
        raise ValueError(f"Zapas '{teams}' zapis jako 'Domaci - Hoste'")
    market = market.upper().replace(" ", "")
    try:
        odds = tuple(float(x.replace(",", ".")) for x in raw.split())
    except ValueError:
        raise ValueError(f"Kurzy '{raw}' nejsou cisla") from None
    if any(o <= 1.0 for o in odds):
        raise ValueError("Kurz musi byt vetsi nez 1.00")
    return Entry(book, names[0], names[1], market, odds, now)


def parse_text(text: str, now: datetime) -> tuple[list[Entry], list[str]]:
    entries: list[Entry] = []
    errors: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            entries.append(parse_line(line, now))
        except ValueError as e:
            errors.append(f"radek {n}: {e}")
    return entries, errors


def load(ledger: Ledger, now: datetime) -> list[Entry]:
    raw = ledger._get(STATE_KEY, "")
    out = []
    for d in json.loads(raw) if raw else []:
        e = Entry(d["book"], d["home"], d["away"], d["market"], tuple(d["odds"]), datetime.fromisoformat(d["saved"]))
        if now - e.saved <= MAX_AGE:
            out.append(e)
    return out


def save(ledger: Ledger, entries: list[Entry]) -> None:
    ledger._set(
        STATE_KEY,
        json.dumps([{**e.__dict__, "odds": list(e.odds), "saved": e.saved.isoformat()} for e in entries]),
    )


def add(ledger: Ledger, new: list[Entry], now: datetime) -> None:
    """Novy kurz nahradi starsi stejne kancelare, zapasu a trhu."""
    key = lambda e: (e.book, normalize(e.home), normalize(e.away), e.market)  # noqa: E731
    fresh = {key(e) for e in new}
    save(ledger, [e for e in load(ledger, now) if key(e) not in fresh] + new)


def find_event(entry: Entry, events: list[Event]) -> Event | None:
    return next(
        (v for v in events if match_team(_en(entry.home), [v.home]) and match_team(_en(entry.away), [v.away])),
        None,
    )


def apply(events: list[Event], entries: list[Entry]) -> list[str]:
    """Prida ceske kurzy k udalostem (na miste). Vraci poznamky o nepouzitych radcich."""
    notes: list[str] = []
    for e in entries:
        ev = find_event(e, events)
        if ev is None:
            continue  # zapas neni v dnesnim skenu (jina liga, jiny den)
        market = ev.markets.get(e.market)
        if market is None:
            notes.append(f"{e.book} {ev.name} {e.market}: trh nema ferovou linii, nelze ohodnotit")
            continue
        if len(e.odds) != len(market.selections):
            notes.append(f"{e.book} {ev.name} {e.market}: cekam {len(market.selections)} kurzy, zadano {len(e.odds)}")
            continue
        market.books[e.book] = dict(zip(market.selections, e.odds))
    return notes
