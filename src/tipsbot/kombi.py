"""Sledovani kombi tiketu: kazdy kombinovany tiket, ktery bot navrhne (Prehled – Doporuceny tiket a Dalsi
kombinace), se zapise do Sledovani (profil "kombi", 1 jednotka bez penez) a po zapasech se vyhodnoti.

Tiket = vice nohou z ruznych zapasu; celkovy kurz = soucin kurzu nohou. Vyhodnoceni: kazda noha zvlast (fotbal
z vysledku, NHL z NHL API), tiket se uzavre, az maji vysledek vsechny nohy (nebo kdyz nektera prohraje).
Vraceni vkladu nohy = kurz 1, pul vyhra / pul prohra = polovina (jako u kancelari).

Pred vykopem prvni nohy se kurz tiketu aktualizuje podle posledniho navrhu; tiket, ktery bot prestal navrhovat,
se jen oznaci (vyhodnoti se stejne – statistika nesmi byt vylepsena vynechanim stazenych navrhu).

Statistika (23.11.2026): kazda sazka (zapas + trh + vyber) se pocita jen jednou. Tiket, ktery obsahuje sazku uz
zapocitanou v drive navrzenem tiketu, se vyhodnoti a zobrazi, ale do statistik se nepocita (`overlaps`) – jinak by
jedna sazka ve 9 kombinacich vazila devetkrat. Poradi = poradi zapisu (prvni navrh, v kole doporuceny tiket prvni),
takze pozdejsi navrhy uz zapocitane tikety nemeni.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime

from . import paper

PROFILE = "kombi"
MARKET = "KOMBI"
WITHDRAWN = "návrh stažen před výkopem"
TARGET = "cílová noha: kurz = minimální, ověř u kanceláře"
_FACTOR = {"won": None, "half_won": None, "void": 1.0, "half_lost": 0.5, "lost": 0.0}


def _naive(ko: str) -> datetime:
    d = datetime.fromisoformat(ko)
    return d.replace(tzinfo=None) if d.tzinfo else d


SRC_TARGET = "cíl – minimální kurz (neověřeno u kanceláře)"
SRC_API = "kurzové API – nejvyšší nabídka při návrhu"
VERIFIED, CHANGED, HYPO, IMPOSSIBLE = ("ověřené ceny", "ověřené ceny – kurz se změnil", "hypotetický (cílový kurz)",
                                       "nelze sestavit za uvedený kurz")
UNDOCUMENTED = "neověřený (kurz bez dokladu, starší záznam)"


def _legs(c, now: datetime | None = None) -> list[dict]:
    """Nohy tiketu. Od Faze 3.2.1 i evidence ceny: cilovy kurz (hypoteticky), skutecne nabizeny kurz z kurzu
    kancelari v dobe navrhu, kancelar, cas overeni a zdroj ceny. Konstrukce tiketu se nemeni."""
    out = []
    for lg in c.legs:
        r = lg.row
        ko = _naive(lg.kickoff)
        target = r.best_book.startswith("cíl")
        offers = [(str(b), float(o)) for b, o in (r.offers or []) if o]
        real = max(offers, key=lambda x: x[1]) if offers else None
        out.append({"name": lg.match, "league": "", "kickoff": ko.isoformat(),
                    "day": ko.date().isoformat(), "market": r.market, "selection": r.selection, "label": r.label,
                    "odds": round(r.best_odds, 2), "book": r.best_book, "p": round(r.p_final, 4),
                    "target": target, "result": None, "score": "",
                    "target_odds": round(r.best_odds, 2) if target else None,
                    "verified_odds": round(real[1], 2) if real else None, "verified_book": real[0] if real else "",
                    "verified_at": now.isoformat(timespec="minutes") if now and real else "",
                    "available": (real[1] >= r.best_odds - 1e-9) if real else (False if target else None),
                    "price_source": SRC_TARGET if target else SRC_API})
    return sorted(out, key=lambda x: (x["kickoff"], x["name"]))


def leg_price(x: dict) -> str:
    """Stav ceny nohy (Faze 3.2.2 – prisne): overena je jen noha s DOLOZENYM kurzem kancelare (verified_odds),
    ktery byl v dobe navrhu dostupny. Cilovy minimalni kurz neni nikdy overeny kurz. Starsi nohy bez dokladu
    ceny (pred Fazi 3.2.1) jsou neoverene, i kdyz nejde o cilovou nohu."""
    if x.get("target"):
        return IMPOSSIBLE if x.get("verified_odds") is not None and x["verified_odds"] < x["odds"] - 1e-9 else HYPO
    if x.get("verified_odds") is None:
        return UNDOCUMENTED
    if x.get("available") is not True:
        return IMPOSSIBLE
    return CHANGED if x.get("first_odds") not in (None, x["odds"]) else VERIFIED


def price_class(p) -> str | None:
    """Trida tiketu podle nejhorsi nohy: hypoteticky > nelze sestavit > bez dokladu ceny > zmena kurzu > overene."""
    if not p.legs:
        return None
    states = {leg_price(x) for x in p.legs}
    for st in (HYPO, IMPOSSIBLE, UNDOCUMENTED, CHANGED):
        if st in states:
            return st
    return VERIFIED


def is_verified(p) -> bool:
    """Do overene vykonnosti smi jen tiket, jehoz KAZDA noha ma dolozeny dostupny kurz."""
    return price_class(p) in (VERIFIED, CHANGED)


def placed(session) -> dict:
    """Skutecne vsazene kombinovane tikety (Moje tikety, AKU) – v Kc, nikdy se nescitaji s jednotkami."""
    from sqlalchemy import select

    from .metrics import is_staked
    from .storage import COMBO, Ticket

    ts = list(session.scalars(select(Ticket).where(Ticket.market == COMBO)))
    st = [t for t in ts if is_staked(t.status)]
    stake = sum(t.stake for t in st)
    profit = sum(t.profit_loss or 0.0 for t in st)
    return {"tickets": len(ts), "settled": len(st), "open": sum(1 for t in ts if t.status == "open"),
            "stake": stake, "profit": profit, "roi": profit / stake if stake else None}


def verification(picks) -> dict:
    """Statistika kombi tiketu oddelene podle overeni cen (zapocitane tikety, kazda sazka jednou).
    Hypoteticke a overene vysledky se NIKDY nescitaji do jednoho ROI."""
    mine = paper.counted([p for p in picks if p.profile == PROFILE])
    out = {"all": paper.stats(mine), "classes": {}, "tickets": len(mine)}
    for cls in (VERIFIED, CHANGED, UNDOCUMENTED, IMPOSSIBLE, HYPO):
        out["classes"][cls] = paper.stats([p for p in mine if price_class(p) == cls])
    real = [p for p in mine if is_verified(p)]
    out["verified"] = paper.stats(real)
    out["hypothetical"] = paper.stats([p for p in mine if not is_verified(p)])
    out["fully_verified"] = len(real)
    out["with_unverified_leg"] = len(mine) - len(real)
    legs = [_leg_key(x) for p in mine for x in p.legs]
    out["legs"], out["unique_legs"] = len(legs), len(set(legs))
    out["unique_matches"] = len({(x["name"], x["day"]) for p in mine for x in p.legs})
    return out


def _id(legs: list[dict]) -> str:
    key = "|".join(f"{x['name']}:{x['day']}:{x['market']}:{x['selection']}" for x in legs)
    return "k" + hashlib.sha1(key.encode()).hexdigest()[:10]


def _detail(legs: list[dict]) -> str:
    return " · ".join(f"{x['label']} ({x['name']}) @ {x['odds']:.2f}" + (" [cíl]" if x["target"] else "") for x in legs)


def record(ledger, combos, now: datetime, leagues: dict[str, str] | None = None) -> int:
    """Zapise navrzene tikety (`combos` z combo.best_combos, poradi = doporuceni). Vraci pocet novych."""
    picks = paper.load(ledger)
    mine = {p.id: p for p in picks if p.profile == PROFILE}
    seen, n = set(), 0
    for rank, c in enumerate(combos):
        legs = _legs(c, now)
        for x in legs:
            x["league"] = x["league"] or (leagues or {}).get(x["name"], "")
        pid = _id(legs)
        seen.add(pid)
        first = legs[0]["kickoff"]
        if datetime.fromisoformat(first) <= now:
            continue  # zapas uz zacal – tiket se nemeni
        flags = (["doporučený tiket (nejlepší)"] if rank == 0 else []) + ([TARGET] if any(x["target"] for x in legs) else [])
        old = mine.get(pid)
        if old is not None:
            if old.result is None:
                prev = {_leg_key(x): x for x in old.legs}
                for x in legs:  # prvni kurz nohy zustava (zmena kurzu pred vykopem = jina cena nez pri navrhu)
                    x["first_odds"] = prev.get(_leg_key(x), {}).get("first_odds", prev.get(_leg_key(x), {}).get("odds", x["odds"]))
                old.legs, old.odds, old.p_model, old.detail = legs, round(c.odds, 2), round(c.prob, 4), _detail(legs)
                old.flags = sorted(set(flags) | ({"doporučený tiket (nejlepší)"} & set(old.flags)))
            continue
        for x in legs:
            x["first_odds"] = x["odds"]
        picks.append(paper.Pick(
            id=pid, league=MARKET, match_id=pid, name=" + ".join(x["name"] for x in legs), day=legs[0]["day"],
            kickoff=first, market=MARKET, selection=f"{len(legs)} sázky", odds=round(c.odds, 2), book="kombinace",
            p_model=round(c.prob, 4), source="model", created=now.isoformat(), flags=flags, profile=PROFILE,
            detail=_detail(legs), legs=legs))
        n += 1
    for p in mine.values():  # navrh zmizel pred vykopem: jen poznamenat, vyhodnoti se stejne
        if p.id not in seen and p.result is None and datetime.fromisoformat(p.kickoff) > now and WITHDRAWN not in p.flags:
            p.flags = p.flags + [WITHDRAWN]
    paper.save(ledger, picks)
    return n


def _close(p) -> str | None:
    """Uzavre tiket, kdyz to vysledky nohou dovoli. Vraci zpravu."""
    res = [x["result"] for x in p.legs]
    if "lost" in res:
        factor = 0.0
    elif None in res:
        return None
    else:
        factor = 1.0
        for x in p.legs:
            f = _FACTOR[x["result"]]
            factor *= x["odds"] if x["result"] == "won" else (1 + x["odds"]) / 2 if x["result"] == "half_won" else f
    p.profit = round(factor - 1.0, 4)
    p.result = "lost" if factor == 0 else "void" if abs(factor - 1) < 1e-9 else "won" if factor > 1 else "half_lost"
    p.score = ", ".join(x["score"] or "–" for x in p.legs)
    return f"Kombi tiket {p.name} @ {p.odds:.2f} -> {p.result} ({p.profit:+.2f} j.)"


def _settle(ledger, grade_leg) -> list[str]:
    picks = paper.load(ledger)
    notes, changed = [], False
    for p in picks:
        if p.profile != PROFILE:
            continue
        for x in p.legs:  # i nohy uz prohraneho tiketu: vysledek zapasu se propise, tiket se nemeni
            if x["result"] is None:
                got = grade_leg(x)
                if got is not None:
                    x["score"], x["result"] = got
                    changed = True
        if p.result is not None:
            continue
        msg = _close(p)
        if msg:
            notes.append(msg)
            changed = True
    if changed:
        paper.save(ledger, picks)
    return notes


def settle(ledger, results) -> list[str]:
    """Fotbalove nohy z tabulky vysledku (stejna jako u tiketu a Sledovani)."""
    from .settlement import settle_bet

    def grade_leg(x):
        if x["league"] == "NHL":
            return None
        got = settle_bet(results, x["name"], date.fromisoformat(x["day"]), x["market"], x["selection"])
        return got if got is not None and got[1] is not None else None

    return _settle(ledger, grade_leg)


def settle_nhl(ledger, games: list[dict]) -> list[str]:
    """Nohy NHL z odehranych zapasu NHL API (nhl_settle.parse_scores)."""
    from . import nhl_settle

    def grade_leg(x):
        if x["league"] != "NHL":
            return None
        g = nhl_settle.find_game(games, x["name"], datetime.fromisoformat(x["kickoff"]))
        if g is None:
            return None
        out = nhl_settle.grade(x["market"], x["selection"], g)
        return None if out is None else (f"{g['final'][0]}:{g['final'][1]}" + ("" if g["end"] == "REG" else f" ({g['end']})"), out)

    return _settle(ledger, grade_leg)


def _leg_key(x: dict) -> tuple:
    return x["name"], x["day"], x["market"], x["selection"]


def overlaps(picks) -> dict[str, str]:
    """{id kombi tiketu: nazev drivejsiho tiketu} pro tikety, jejichz nektera sazka uz je v zapocitanem tiketu."""
    used: dict[tuple, str] = {}
    out = {}
    for p in picks:
        if p.profile != PROFILE or not p.legs:
            continue
        keys = [_leg_key(x) for x in p.legs]
        hit = next((used[k] for k in keys if k in used), None)
        if hit is not None:
            out[p.id] = hit
            continue
        for k in keys:
            used[k] = p.name
    return out


def open_legs(ledger) -> list[dict]:
    """Nohy kombi tiketu bez vysledku, i u uz uzavrenych tiketu (pro stazeni vysledku)."""
    return [x for p in paper.load(ledger) if p.profile == PROFILE for x in p.legs if x["result"] is None]


def record_current(ledger, now: datetime) -> int:
    """Spocita kombi tikety stejne jako Prehled (z posledni analyzy vsech soutezi) a zapise je."""
    from . import analysis, combo
    from .config import SETTINGS

    views = []
    for lg in SETTINGS.odds_api.sport_keys:
        views += [v for v in analysis.loads(ledger.analysis(lg)) if _naive(v.kickoff) > now]
    combos = combo.best_combos(views, ledger.bankroll(), ledger.open_exposure(now.date()))
    return record(ledger, combos, now, {v.name: v.league for v in views})
