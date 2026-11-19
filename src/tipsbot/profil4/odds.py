"""Kurzy pro Profil 4: kazdy kurz ma kancelar, zapas, trh, vyber, hodnotu, cas ziskani a zdroj.

Zdroje:
- "api"    – predzapasove kurzy z API-Football (/odds). Nazev kancelare se bere presne z API; kurz jine
             kancelare se NIKDY nevydava za Tipsport (ani "Tipsport" z API se nepovazuje za cesky tipsport.cz –
             muze jit o jinou zemskou verzi, proto ma priznak).
- "manual" – rucne opsany kurz (napr. z Tipsportu), radky ve formatu Profilu 2: `kancelar | trh | kurzy`.
- "import" – CSV nebo JSON soubor.

Stav kurzu: AKTUÁLNÍ / ZASTARALÝ (starsi nez limit) / HISTORICKÝ (zapas uz zacal) / ORIENTAČNÍ (jen pro orientaci).
Trhy pouzivaji zapis Profilu 2 (1X2, DC, DNB, BTTS, OU_2.5, TTH_1.5, TTA_0.5, AH_-0.75 ...), takze vypocet
vyher, vraceni a pulvyher je spolecny (profile2.returns).
"""

from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from .. import profile2
from .config import OddsConfig
from .store import Store, iso, parse_dt, utcnow

CURRENT, STALE, HISTORICAL, INDICATIVE = "AKTUÁLNÍ", "ZASTARALÝ", "HISTORICKÝ", "ORIENTAČNÍ"
SOURCES = ("api", "manual", "import")


@dataclass(frozen=True)
class OddsQuote:
    fixture_id: int
    match: str
    bookmaker: str  # klic kancelare (malymi pismeny, bez mezer)
    market: str
    selection: str
    odds: float
    fetched_at: datetime  # kdy jsme kurz ziskali
    source: str  # api | manual | import
    kickoff: datetime | None = None
    source_time: datetime | None = None  # cas posledni aktualizace u zdroje (API "update"), jinak = fetched_at
    rules: str = "90 min + nastavení, bez prodloužení"
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.market}:{self.selection}"

    @property
    def stamp(self) -> datetime:
        return self.source_time or self.fetched_at

    def age_minutes(self, now: datetime) -> float:
        return max(0.0, (now - self.stamp).total_seconds() / 60.0)

    def status(self, now: datetime, cfg: OddsConfig) -> str:
        if self.kickoff and now >= self.kickoff:
            return HISTORICAL
        if "orienta" in self.note.lower():
            return INDICATIVE
        limit = cfg.max_age_api_min if self.source == "api" else cfg.max_age_manual_min
        return CURRENT if self.age_minutes(now) <= limit else STALE

    def source_label(self) -> str:
        return {"api": "API-Football", "manual": "ručně zadaný", "import": "import ze souboru"}.get(self.source, self.source)

    def to_row(self) -> dict:
        d = asdict(self)
        for k in ("fetched_at", "kickoff", "source_time"):
            d[k] = iso(d[k])
        return d


class OddsInputError(ValueError):
    pass


def book_key(name: str) -> str:
    return "".join(ch for ch in name.strip().lower() if ch.isalnum() or ch in "_-") or "neznama"


def validate(market: str, selection: str, odds: float) -> None:
    if not isinstance(odds, (int, float)) or not math.isfinite(odds) or odds <= 1.0:
        raise OddsInputError(f"neplatný kurz {odds!r} (musí být desetinné číslo větší než 1.00)")
    if market == "EXPR":
        profile2.returns("EXPR", selection, float(odds))  # vyhodi ValueError pri nesmyslne podmince
        return
    sels = profile2.selections(market)
    if sels is None:
        raise OddsInputError(f"neznámý trh '{market}'")
    if selection not in sels:
        raise OddsInputError(f"trh {market}: výběr musí být {', '.join(sels)} (je '{selection}')")


# ---------------------------------------------------------------------------
# API-Football /odds
# ---------------------------------------------------------------------------

def _ah(value: str) -> tuple[str, str] | None:
    """'Home -0.75' -> ('AH_-0.75', 'home');  'Away +1' -> ('AH_-1.0', 'away'). Linie je z pohledu domacich."""
    side, _, raw = value.strip().partition(" ")
    side = side.lower()
    if side not in ("home", "away") or not raw:
        return None
    try:
        parts = [float(x) for x in raw.replace("+", "").split(",")]
    except ValueError:
        return None
    hcp = sum(parts) / len(parts)  # "-0.5,-1" = ctvrtinova linie -0.75
    if abs(hcp * 4 - round(hcp * 4)) > 1e-9:
        return None
    line = hcp if side == "home" else -hcp
    return profile2._canon("AH", line + 0.0), side


def _ou(value: str) -> tuple[str, float] | None:
    side, _, raw = value.strip().partition(" ")
    try:
        line = float(raw)
    except ValueError:
        return None
    side = side.lower()
    if side not in ("over", "under"):
        return None
    return side, line


def map_bet(bet_name: str, value: str) -> tuple[str, str] | None:
    """Nazev sazky a hodnota z API -> (trh, vyber) v zapisu Profilu 2, nebo None (nepodporovany trh)."""
    name = bet_name.strip().lower()
    v = str(value).strip()
    if name == "match winner":
        return {"Home": ("1X2", "1"), "Draw": ("1X2", "X"), "Away": ("1X2", "2")}.get(v)
    if name == "home/away":
        return {"Home": ("DNB", "home"), "Away": ("DNB", "away")}.get(v)
    if name == "double chance":
        return {"Home/Draw": ("DC", "1X"), "Home/Away": ("DC", "12"), "Draw/Away": ("DC", "X2")}.get(v)
    if name == "both teams score":
        return {"Yes": ("BTTS", "yes"), "No": ("BTTS", "no")}.get(v)
    if name in ("goals over/under", "total - home", "total - away"):
        ou = _ou(v)
        if not ou:
            return None
        kind = {"goals over/under": "OU", "total - home": "TTH", "total - away": "TTA"}[name]
        return profile2._canon(kind, ou[1]), ou[0]
    if name == "asian handicap":
        return _ah(v)
    return None


def parse_api_odds(payload: list, fetched_at: datetime, match_names: dict[int, str] | None = None) -> list[OddsQuote]:
    out: list[OddsQuote] = []
    for item in payload or []:
        fx = item.get("fixture", {}) or {}
        fid = fx.get("id")
        if not fid:
            continue
        kickoff = parse_dt(fx.get("date"))
        upd = parse_dt(item.get("update"))
        match = (match_names or {}).get(fid, str(fid))
        for bk in item.get("bookmakers", []) or []:
            name = bk.get("name") or ""
            key = book_key(name)
            note = ""
            if key.startswith(("tipsport", "fortuna", "betano", "chance")):
                note = f"kurz z API ({name}) – může jít o jinou zemskou verzi než česká kancelář"
            for bet in bk.get("bets", []) or []:
                for val in bet.get("values", []) or []:
                    mapped = map_bet(bet.get("name", ""), val.get("value", ""))
                    if not mapped:
                        continue
                    try:
                        odd = float(val.get("odd"))
                        validate(mapped[0], mapped[1], odd)
                    except (TypeError, ValueError):
                        continue
                    out.append(OddsQuote(int(fid), match, key, mapped[0], mapped[1], odd, fetched_at, "api",
                                         kickoff, upd, note=note))
    return out


# ---------------------------------------------------------------------------
# Rucni zadani a import
# ---------------------------------------------------------------------------

def parse_manual(text: str, fixture_id: int, match: str, now: datetime | None = None,
                 kickoff: datetime | None = None, default_book: str = "tipsport",
                 source: str = "manual") -> tuple[list[OddsQuote], list[str]]:
    """Radky ve formatu Profilu 2 (`kancelar | trh | kurzy`, kancelar lze vynechat = tipsport)."""
    now = now or utcnow()
    entries, errors = profile2.parse_odds(text, default_book)
    out = []
    for e in entries:
        for sel, odd in e.pairs:
            out.append(OddsQuote(fixture_id, match, book_key(e.book), e.market, sel, float(odd), now, source, kickoff, now))
    return out, errors


def parse_csv(text: str, now: datetime | None = None, match_names: dict[int, str] | None = None) -> tuple[list[OddsQuote], list[str]]:
    """Sloupce: fixture_id, bookmaker, market, selection, odds [, time] [, match]. Oddelovac carka nebo strednik."""
    now = now or utcnow()
    sample = text[:2048]
    dialect = csv.Sniffer().sniff(sample, delimiters=",;") if sample.strip() else csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return _from_records(list(reader), now, match_names, "import")


def parse_json(text: str, now: datetime | None = None, match_names: dict[int, str] | None = None) -> tuple[list[OddsQuote], list[str]]:
    """Seznam objektu {fixture_id, bookmaker, market, selection, odds[, time, match]} nebo {fixture_id, quotes: [...]}."""
    now = now or utcnow()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return [], [f"neplatný JSON: {e}"]
    if isinstance(data, dict):
        fid = data.get("fixture_id")
        data = [{"fixture_id": fid, "match": data.get("match", ""), **q} for q in data.get("quotes", [])]
    if not isinstance(data, list):
        return [], ["JSON musí být seznam kurzů"]
    return _from_records(data, now, match_names, "import")


def _from_records(rows: list[dict], now: datetime, match_names: dict[int, str] | None, source: str) -> tuple[list[OddsQuote], list[str]]:
    out, errors = [], []
    for n, r in enumerate(rows, 1):
        try:
            fid = int(str(r.get("fixture_id", "")).strip())
            book = book_key(str(r.get("bookmaker", "")))
            market = profile2._norm_market(str(r.get("market", "")))
            sel = str(r.get("selection", "")).strip()
            sel = sel if market in ("1X2", "DC") else sel.lower()
            odd = float(str(r.get("odds", "")).replace(",", "."))
            validate(market, sel, odd)
            when = parse_dt(str(r["time"])) if r.get("time") else now
            if when > now:
                raise OddsInputError("čas kurzu je v budoucnosti")
            match = str(r.get("match") or (match_names or {}).get(fid, fid))
            out.append(OddsQuote(fid, match, book, market, sel, odd, now, source, None, when))
        except (ValueError, TypeError, KeyError) as e:
            errors.append(f"řádek {n}: {e}")
    return out, errors


# ---------------------------------------------------------------------------
# Ulozeni a nacteni
# ---------------------------------------------------------------------------

def save(store: Store, quotes: list[OddsQuote]) -> int:
    """Ulozi kurzy; stejny kurz se stejnym casem zdroje (napr. znovu z cache API) se neduplikuje."""
    n = 0
    for q in quotes:
        dup = store.query(
            "SELECT 1 FROM odds_quotes WHERE fixture_id=? AND bookmaker=? AND market=? AND selection=? AND odds=? "
            "AND source=? AND COALESCE(source_time,'')=COALESCE(?, '') LIMIT 1",
            (q.fixture_id, q.bookmaker, q.market, q.selection, q.odds, q.source, iso(q.source_time)))
        if dup:
            continue
        n += 1
        store.execute(
            "INSERT INTO odds_quotes(fixture_id, match, kickoff, bookmaker, market, selection, odds, fetched_at, "
            "source_time, source, rules, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (q.fixture_id, q.match, iso(q.kickoff), q.bookmaker, q.market, q.selection, q.odds, iso(q.fetched_at),
             iso(q.source_time), q.source, q.rules, q.note),
        )
    return n


def _row(r) -> OddsQuote:
    return OddsQuote(r["fixture_id"], r["match"], r["bookmaker"], r["market"], r["selection"], r["odds"],
                     parse_dt(r["fetched_at"]), r["source"], parse_dt(r["kickoff"]), parse_dt(r["source_time"]),
                     r["rules"] or "", r["note"] or "")


def history(store: Store, fixture_id: int) -> list[OddsQuote]:
    return [_row(r) for r in store.query("SELECT * FROM odds_quotes WHERE fixture_id=? ORDER BY id", (fixture_id,))]


def latest(store: Store, fixture_id: int, before: datetime | None = None) -> list[OddsQuote]:
    """Posledni znamy kurz kazde kancelare na kazdy vyber (volitelne jen ziskany pred casem `before`)."""
    best: dict[tuple[str, str, str], OddsQuote] = {}
    for q in history(store, fixture_id):
        if before and q.stamp >= before:
            continue
        k = (q.bookmaker, q.market, q.selection)
        if k not in best or q.stamp >= best[k].stamp:
            best[k] = q
    return list(best.values())


def with_kickoff(quotes: list[OddsQuote], kickoff: datetime | None, match: str | None = None) -> list[OddsQuote]:
    return [replace(q, kickoff=q.kickoff or kickoff, match=match or q.match) for q in quotes]


def best_prices(quotes: list[OddsQuote], now: datetime, cfg: OddsConfig, usable_only: bool = True) -> dict[str, OddsQuote]:
    """Nejvyssi kurz na kazdy vyber (trh:vyber). Zastarale a historicke kurzy se pri usable_only vynechaji."""
    out: dict[str, OddsQuote] = {}
    for q in quotes:
        if usable_only and q.status(now, cfg) not in (CURRENT,):
            continue
        if q.key not in out or q.odds > out[q.key].odds:
            out[q.key] = q
    return out
