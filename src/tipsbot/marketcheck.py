"""Spolecna Market Integrity pro vsechny profily (Faze 3.2.1).

Kazda nabidka (zapas, trh, vyber, kancelar, kurz) se posoudi STEJNOU funkci bez ohledu na to, ktery profil ji
vybral – stejna podezrela nabidka tak nemuze byt jednim profilem oznacena za overenou a jinym za podezrelou.
Prahy jsou prevzate z Profilu 4 (profil4.config: stari kurzu, odchylka od medianu, nadhodnota proti trhu,
tolerance potvrzeni, rozsah marze), aby obe vrstvy merily totez.

Kontrola je JEN INFORMACNI: nemeni pravdepodobnosti, EV, vyber ani prahy profilu a historicke tipy neprepisuje.
Kazdy profil si k vyberu ulozi vysledek (`Pick.integrity`); vyber bez dat pro kontrolu = NEOVĚŘENO, ne „v poradku“.

Oddelene vlastnosti (nejsou zamenitelne):
- platnost: trh a vyber jsou znamy a linie je presne tataz (OU_2.5 ≠ OU_2.75, strana podle vyberu);
- cerstvost: stari kurzu u kancelare (cas posledni zmeny u poskytovatele, ne cas stazeni);
- protistrana: kancelar nabizi vsechny vysledky trhu ze stejne aktualizace (jinak nejde overit sparovani);
- dostupnost: kurz skutecne nabizi aspon jedna kancelar (u medianu: kolik kancelari nabizi aspon tento kurz);
- nezavisle potvrzeni: jina kancelar ma kurz nejvys o 5 % nizsi;
- duveryhodnost reference: ferova cena trhu z aspon `min_ref_books` NEZAVISLYCH kancelari BEZ hodnocene kancelare
  (leave-one-bookmaker-out). Kancelare stejne skupiny (unibet_se/unibet_nl…) maji stejne ceny, pocitaji se jako
  jeden zdroj a hodnocena kancelar vynecha celou svou skupinu.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from .devig import DevigError, devig_power

VERSION = "mi-1"
EXCHANGES = ("betfair_ex_eu", "betfair_ex_uk", "matchbook", "smarkets")
MEDIAN_BOOKS = ("medián", "median", "zahrani")  # kurz je median kancelari, ne nabidka jedne kancelare

OK, PARTIAL, UNVERIFIED, SUSPECT = "OVĚŘENO", "ČÁSTEČNĚ OVĚŘENO", "NEOVĚŘENO", "PODEZŘELÉ"


def _p4():
    from .profil4.config import P4

    return P4


@dataclass(frozen=True)
class MIConfig:
    min_ref_books: int = 3  # nezavislych kancelari mimo hodnocenou pro duveryhodnou referenci
    max_age_min: int = 6 * 60  # = Profil 4 OddsConfig.max_age_api_min
    max_time_gap_min: int = 3 * 60  # kurz kancelare a reference z ruzne doby => nesrovnatelne
    outlier_pct: float = 0.10  # = Profil 4 IntelligenceConfig.outlier_pct (nad medianem ostatnich)
    max_market_edge: float = 0.10  # = Profil 4 DecisionConfig.max_market_edge (nad ferovou cenou bez potvrzeni)
    confirm_tolerance: float = 0.05  # = Profil 4 DecisionConfig.confirm_tolerance
    min_overround: float = -0.01  # = Profil 4 DecisionConfig.min_book_overround
    max_overround: float = 0.25  # = Profil 4 DecisionConfig.max_book_overround

    @classmethod
    def from_p4(cls) -> "MIConfig":
        p4 = _p4()
        return cls(max_age_min=p4.odds.max_age_api_min, outlier_pct=p4.intel.outlier_pct,
                   max_market_edge=p4.decision.max_market_edge, confirm_tolerance=p4.decision.confirm_tolerance,
                   min_overround=p4.decision.min_book_overround, max_overround=p4.decision.max_book_overround)


DEFAULT = MIConfig.from_p4()


def family(book: str) -> str:
    """Skupina kancelare: stejny provozovatel na vic trzich ma stejne ceny (unibet_se, unibet_nl -> unibet)."""
    return book.split("_")[0].lower()


def is_median(book: str) -> bool:
    b = (book or "").lower()
    return any(m in b for m in MEDIAN_BOOKS)


def _age(stamp: datetime | None, now: datetime) -> float | None:
    if stamp is None:
        return None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0.0, (now - stamp).total_seconds() / 60.0)  # cas zmeny tesne po stazeni = 0 min


def _overround(prices: dict[str, float], sels) -> float:
    return sum(1.0 / prices[s] for s in sels) - (2.0 if len(sels) == 3 and set(sels) == {"1X", "12", "X2"} else 1.0)


def consensus(books: dict[str, dict[str, float]], sels: tuple[str, ...], selection: str, now: datetime,
              stamps: dict[str, datetime] | None = None, exclude: str | None = None,
              cfg: MIConfig = DEFAULT) -> dict:
    """Ferova pravdepodobnost vyberu z kancelari MIMO `exclude` (a jeho skupinu): de-vig kazde kancelare
    mocninnou metodou (devig_power), skupina = median svych kancelari, reference = median skupin.

    Vraci {"p", "fair", "n" (nezavislych skupin), "included", "excluded" {kancelar: duvod}, "median_odds",
    "max_odds", "ages"}; "p" je None, kdyz neni zadna pouzitelna kancelar."""
    if selection not in sels:
        return {"p": None, "fair": None, "n": 0, "included": [], "excluded": {}, "median_odds": None,
                "max_odds": None, "ages": []}
    i = sels.index(selection)
    fam_p: dict[str, list[float]] = {}
    included, excluded, raw, ages = [], {}, [], []
    skip = family(exclude) if exclude else None
    for book, prices in sorted(books.items()):
        if book == exclude:
            excluded[book] = "hodnocená kancelář"
            continue
        if skip and family(book) == skip:
            excluded[book] = "stejná skupina jako hodnocená kancelář"
            continue
        if book in EXCHANGES:
            excluded[book] = "burza (provize, jiná cena)"
            continue
        if any(s not in prices for s in sels):
            excluded[book] = "neúplný trh (chybí protistrana)"
            continue
        age = _age((stamps or {}).get(book), now)
        if age is not None and age > cfg.max_age_min:
            excluded[book] = f"zastaralý kurz ({age:.0f} min)"
            continue
        over = _overround(prices, sels)
        if not (cfg.min_overround <= over <= cfg.max_overround):
            excluded[book] = f"marže {over * 100:.1f} % mimo rozsah"
            continue
        try:
            p = devig_power([prices[s] for s in sels])[i]
        except DevigError:
            excluded[book] = "kurzy nejdou zbavit marže"
            continue
        fam_p.setdefault(family(book), []).append(p)
        included.append(book)
        raw.append(prices[selection])
        if age is not None:
            ages.append(age)
    groups = [statistics.median(v) for v in fam_p.values()]
    p = statistics.median(groups) if groups else None
    return {"p": p, "fair": (1.0 / p) if p else None, "n": len(groups), "included": included, "excluded": excluded,
            "median_odds": statistics.median(raw) if raw else None, "max_odds": max(raw) if raw else None,
            "ages": ages}


def check(market: str, selection: str, odds: float, book: str, books: dict[str, dict[str, float]] | None,
          sels: tuple[str, ...] | None, now: datetime, stamps: dict[str, datetime] | None = None,
          cfg: MIConfig = DEFAULT) -> dict:
    """Posouzeni jedne nabidky. Vysledek se uklada k vyberu (JSON) – nic jineho nemeni."""
    out = {"version": VERSION, "t": now.isoformat(timespec="minutes"), "market": market, "selection": selection,
           "odds": odds, "book": book}
    reasons: list[str] = []
    if not books or not sels:
        out.update(status=UNVERIFIED, validity=UNVERIFIED, freshness=UNVERIFIED, counterside=UNVERIFIED,
                   availability=UNVERIFIED, confirmation=UNVERIFIED, reference=UNVERIFIED,
                   reasons=["trh není v kurzech kanceláří (nelze ověřit)"])
        return out
    if selection not in sels:
        out.update(status=UNVERIFIED, validity="NESHODA", reasons=[f"výběr {selection} není výběrem trhu {market}"])
        return out
    out["validity"] = OK
    median_price = is_median(book)
    own = None if median_price else books.get(book)
    # cerstvost a protistrana u hodnocene kancelare
    if median_price:
        out["counterside"] = "nehodnotí se (medián kanceláří)"
        age = None
        ref_ages = [a for a in (_age(s, now) for s in (stamps or {}).values()) if a is not None]
        age = statistics.median(ref_ages) if ref_ages else None
    elif own is None:
        out["counterside"] = UNVERIFIED
        reasons.append(f"{book} nemá v kurzech tento trh – nabídku nelze ověřit")
        age = None
    else:
        missing = [s for s in sels if s not in own]
        out["counterside"] = OK if not missing else "CHYBÍ"
        if missing:
            reasons.append(f"{book} nenabízí protistranu ({', '.join(missing)})")
        else:
            over = _overround(own, sels)
            if over < cfg.min_overround:
                reasons.append(f"kurzy {book} dávají dohromady {(over + 1) * 100:.1f} % – nejsou jeden trh")
                out["counterside"] = "NESHODA"
        age = _age((stamps or {}).get(book), now)
    out["age_min"] = None if age is None else round(age)
    out["freshness"] = UNVERIFIED if age is None else OK if age <= cfg.max_age_min else "ZASTARALÝ"
    if out["freshness"] == "ZASTARALÝ":
        reasons.append(f"kurz je {age:.0f} min starý (limit {cfg.max_age_min} min)")
    # reference bez hodnocene kancelare (leave-one-bookmaker-out)
    ref = consensus(books, sels, selection, now, stamps, None if median_price else book, cfg)
    out.update(ref_p=ref["p"], ref_fair=ref["fair"], ref_n=ref["n"], ref_books=ref["included"],
               ref_excluded=ref["excluded"], ref_median_odds=ref["median_odds"])
    out["reference"] = OK if ref["p"] and ref["n"] >= cfg.min_ref_books else UNVERIFIED
    if out["reference"] != OK:
        reasons.append(f"málo nezávislých kanceláří pro férovou cenu ({ref['n']} < {cfg.min_ref_books})")
    edge = odds * ref["p"] - 1.0 if ref["p"] else None
    dev = odds / ref["median_odds"] - 1.0 if ref["median_odds"] else None
    out.update(edge=edge, dev_median=dev)
    if age is not None and ref["ages"] and abs(age - statistics.median(ref["ages"])) > cfg.max_time_gap_min:
        reasons.append("kurz a reference trhu jsou z různé doby")
        out["time_match"] = "NESROVNATELNÉ"
    else:
        out["time_match"] = OK if age is not None and ref["ages"] else UNVERIFIED
    # dostupnost a nezavisle potvrzeni
    others = [(b, pr[selection]) for b, pr in books.items() if selection in pr and (median_price or b != book)]
    avail = [b for b, o in others if o >= odds - 1e-9]
    if median_price:
        out["availability"] = OK if avail else "NEDOSTUPNÝ"
        out["available_at"] = sorted(avail)
        if not avail:
            reasons.append("žádná kancelář nenabízí aspoň tento kurz")
    else:
        out["availability"] = OK if own is not None and abs(own.get(selection, 0) - odds) < 0.011 else UNVERIFIED
        if out["availability"] != OK and own is not None:
            reasons.append(f"{book} má teď kurz {own.get(selection)}, ne {odds:.2f}")
    conf = sorted(b for b, o in others if b != book and family(b) != family(book) and o >= odds * (1 - cfg.confirm_tolerance))
    out["confirmed_by"] = conf
    out["confirmation"] = OK if conf else "NEPOTVRZENO"
    # vysledny stav
    suspect = []
    if dev is not None and ref["n"] >= cfg.min_ref_books and dev > cfg.outlier_pct:
        suspect.append(f"kurz o {dev * 100:+.0f} % nad mediánem {ref['n']} kanceláří")
    if edge is not None and edge > cfg.max_market_edge and not conf:
        suspect.append(f"kurz o {edge * 100:+.0f} % nad férovou cenou trhu a nepotvrzen")
    if out.get("counterside") == "NESHODA":
        suspect.append("kurzy kanceláře netvoří jeden trh")
    if suspect:
        status = SUSPECT
    elif out["reference"] != OK or out["freshness"] == UNVERIFIED or out["counterside"] == UNVERIFIED:
        status = UNVERIFIED
    elif out["freshness"] != OK or out["counterside"] == "CHYBÍ" or out["time_match"] == "NESROVNATELNÉ" \
            or out["availability"] != OK:
        status = PARTIAL
    else:
        status = OK
    out["status"] = status
    out["reasons"] = suspect + reasons
    return out


def stamps_from_payload(payload: list | None) -> dict[str, dict[str, dict[str, datetime]]]:
    """Casy posledni zmeny kurzu z ulozene odpovedi kurzoveho API: {match_id: {kancelar: {trh API: cas}}}."""
    out: dict[str, dict[str, dict[str, datetime]]] = {}
    for ev in payload or []:
        mid = f"oddsapi:{ev.get('id')}"
        for bm in ev.get("bookmakers", []):
            for m in bm.get("markets", []):
                t = m.get("last_update") or bm.get("last_update")
                if not t:
                    continue
                try:
                    dt = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
                except ValueError:
                    continue
                out.setdefault(mid, {}).setdefault(bm.get("key", ""), {})[m.get("key", "")] = dt
    return out


def api_market(market: str) -> str:
    """Nas trh -> klic trhu kurzoveho API (pro cas kurzu)."""
    if market in ("1X2", "MONEYLINE"):
        return "h2h"
    if market.startswith("OU"):
        return "totals"
    if market.startswith("PUCKLINE"):
        return "spreads"
    return market


def market_stamps(stamps: dict | None, match_id: str, market: str) -> dict[str, datetime]:
    key = api_market(market)
    return {b: m[key] for b, m in ((stamps or {}).get(match_id) or {}).items() if key in m}


def for_event(ev, market: str, selection: str, odds: float, book: str, now: datetime, stamps: dict | None = None,
              cfg: MIConfig = DEFAULT) -> dict:
    """Kontrola nabidky proti kurzum udalosti z kurzoveho API (ev.markets; rohy/karty z API-Football)."""
    mk = getattr(ev, "markets", {}).get(market) if ev is not None else None
    return check(market, selection, odds, book, mk.books if mk else None, mk.selections if mk else None, now,
                 market_stamps(stamps, ev.match_id, market) if ev is not None else None, cfg)


def summary(items: list[dict | None]) -> dict[str, int]:
    """Pocet vyberu podle stavu kontroly; vyber bez kontroly (starsi zaznam) = NEOVĚŘENO."""
    out = {OK: 0, PARTIAL: 0, UNVERIFIED: 0, SUSPECT: 0}
    for it in items:
        out[(it or {}).get("status", UNVERIFIED)] = out.get((it or {}).get("status", UNVERIFIED), 0) + 1
    return out


# --- Faze 3.2.2: tvrde bezpecnostni odmitnuti -------------------------------------------------------------------
HARD = (SUSPECT,)  # PODEZŘELÉ = cenovy extrem nad medianem / nad ferovou cenou bez potvrzeni / kurzy netvori trh


def is_hard_reject(it: dict | None) -> bool:
    """Tvrde odmitnuti: vyber zustava jako diagnosticky zaznam, ale nesmi byt prezentovan jako schvalena sazka
    ani se pocitat do vysledku profilu. NEOVĚŘENO (malo dat) tvrde odmitnuti NENI – jen se tak oznaci."""
    return bool(it) and it.get("status") in HARD


def log_rejection(session, profile: str, match_id: str, match: str, kickoff, it: dict) -> bool:
    """Zapise tvrde odmitnuti do spolecne stopy (jednou pro profil + nabidku + kurz). Vraci True, kdyz je novy."""
    import json

    from sqlalchemy import select

    from .storage import IntegrityRejection

    q = select(IntegrityRejection.id).where(
        IntegrityRejection.profile == profile, IntegrityRejection.match_id == match_id,
        IntegrityRejection.market == it.get("market"), IntegrityRejection.selection == it.get("selection"),
        IntegrityRejection.book == it.get("book"), IntegrityRejection.odds == it.get("odds"))
    if session.scalar(q) is not None:
        return False
    if isinstance(kickoff, str):
        kickoff = datetime.fromisoformat(kickoff)
    session.add(IntegrityRejection(
        profile=profile, match_id=match_id, match=match, kickoff=kickoff, market=it.get("market"),
        selection=it.get("selection"), book=it.get("book"), odds=it.get("odds"), ref_fair=it.get("ref_fair"),
        ref_median=it.get("ref_median_odds"), age_min=it.get("age_min"), ref_n=it.get("ref_n") or 0,
        reasons=json.dumps(it.get("reasons") or [], ensure_ascii=False),
        check=json.dumps(it, ensure_ascii=False, default=str)))
    return True
