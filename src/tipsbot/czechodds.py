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


# trhy "vic / min nez linie": goly, goly tymu, rohy a karty
COUNT_PREFIXES = ("OU_", "OU60_", "TTH_", "TTA_", "ROHY_", "KARTY_")


# Trhy ve formulari u zapasu: (trh, vybery v poradi ulozeni, popisky).
FORM_MARKETS = (
    ("1X2", ("1", "X", "2"), ("1", "X", "2")),
    ("DC", ("1X", "12", "X2"), ("1X", "12", "X2")),
    ("DNB", ("home", "away"), ("domácí", "hosté")),
    ("OU_1.5", ("over", "under"), ("víc než 1.5", "míň než 1.5")),
    ("OU_2.5", ("over", "under"), ("víc než 2.5", "míň než 2.5")),
    ("OU_3.5", ("over", "under"), ("víc než 3.5", "míň než 3.5")),
    ("OU_4.5", ("over", "under"), ("víc než 4.5", "míň než 4.5")),
    ("OU_2.0", ("over", "under"), ("víc než 2.0", "míň než 2.0")),
    ("OU_4.0", ("over", "under"), ("víc než 4.0", "míň než 4.0")),
    ("BTTS", ("yes", "no"), ("oba dají gól: ano", "ne")),
    ("TTH_0.5", ("over", "under"), ("domácí dají gól: ano", "ne")),
    ("TTA_0.5", ("over", "under"), ("hosté dají gól: ano", "ne")),
    ("TTH_1.5", ("over", "under"), ("domácí víc než 1.5", "míň než 1.5")),
    ("TTA_1.5", ("over", "under"), ("hosté víc než 1.5", "míň než 1.5")),
    ("ROHY_8.5", ("over", "under"), ("rohů víc než 8.5", "míň než 8.5")),
    ("ROHY_9.5", ("over", "under"), ("rohů víc než 9.5", "míň než 9.5")),
    ("ROHY_10.5", ("over", "under"), ("rohů víc než 10.5", "míň než 10.5")),
    ("KARTY_3.5", ("over", "under"), ("karet víc než 3.5", "míň než 3.5")),
    ("KARTY_4.5", ("over", "under"), ("karet víc než 4.5", "míň než 4.5")),
    ("KARTY_5.5", ("over", "under"), ("karet víc než 5.5", "míň než 5.5")),
)

# NHL ma ve formulari vlastni trhy: pocty golu v zakladni hraci dobe (jak je vypisuje Tipsport) i vc. prodlouzeni.
NHL_FORM_MARKETS = (
    ("MONEYLINE", ("home", "away"), ("domácí (vč. prodl.)", "hosté (vč. prodl.)")),
    *((f"OU60_{x}", ("over", "under"), (f"víc než {x}", f"míň než {x}")) for x in ("4.5", "5.5", "6.5", "7.5")),
    *((f"OU_{x}", ("over", "under"), (f"víc než {x}", f"míň než {x}")) for x in ("5.5", "6.5")),
)


def from_form(book: str, match_name: str, values: dict[str, str], now: datetime) -> tuple[list[Entry], list[str]]:
    """Kurzy z formulare u zapasu (`values`: {"OU_2.5:over": "1.85", ...}). Trh se ulozi, jen kdyz je vyplneny cely."""
    book = normalize(book).replace(" ", "")
    if book not in _BOOKS:
        raise ValueError(f"Neznámá kancelář '{book}'.")
    home, away = (p.strip() for p in match_name.split(" - ", 1))
    entries, errors = [], []
    for market, sels, _ in {m[0]: m for m in FORM_MARKETS + NHL_FORM_MARKETS}.values():
        raw = [(values.get(f"{market}:{s}") or "").strip().replace(",", ".") for s in sels]
        if not any(raw):
            continue
        try:
            odds = tuple(float(x) for x in raw)
        except ValueError:
            errors.append(f"{market}: vyplň všechny kurzy trhu čísly")
            continue
        if any(o <= 1.0 for o in odds):
            errors.append(f"{market}: kurz musí být větší než 1.00")
            continue
        entries.append(Entry(book, home, away, market, odds, now))
    return entries, errors


def for_match(entries: list[Entry], match_name: str) -> dict[tuple[str, str], tuple[float, str]]:
    """Nejlepsi cesky kurz pro kazdy (trh, vyber) zapasu: {(trh, vyber): (kurz, kancelar)}."""
    home, away = (p.strip() for p in match_name.split(" - ", 1))
    sels = {m: s for m, s, _ in FORM_MARKETS}
    out: dict[tuple[str, str], tuple[float, str]] = {}
    for e in entries:
        if not (match_team(_en(e.home), [home]) and match_team(_en(e.away), [away])):
            continue
        names = sels.get(e.market) or (("over", "under") if e.market.startswith(COUNT_PREFIXES) else None)
        if not names or len(names) != len(e.odds):
            continue
        for s, o in zip(names, e.odds):
            if (e.market, s) not in out or o > out[(e.market, s)][0]:
                out[(e.market, s)] = (o, e.book)
    return out


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
        if market is None and e.market.startswith(("ROHY_", "KARTY_", "OU60_")):
            continue  # rohy, karty a goly za 60 minut kurzove API nema, porovnaji se s modelem
        if market is None:
            notes.append(f"{e.book} {ev.name} {e.market}: trh nema ferovou linii, nelze ohodnotit")
            continue
        if len(e.odds) != len(market.selections):
            notes.append(f"{e.book} {ev.name} {e.market}: cekam {len(market.selections)} kurzy, zadano {len(e.odds)}")
            continue
        market.books[e.book] = dict(zip(market.selections, e.odds))
    return notes
