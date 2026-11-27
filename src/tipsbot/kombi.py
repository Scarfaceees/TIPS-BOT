"""Sledovani kombi tiketu: kazdy kombinovany tiket, ktery bot navrhne (Prehled – Doporuceny tiket a Dalsi
kombinace), se zapise do Sledovani (profil "kombi", 1 jednotka bez penez) a po zapasech se vyhodnoti.

Tiket = vice nohou z ruznych zapasu; celkovy kurz = soucin kurzu nohou. Vyhodnoceni: kazda noha zvlast (fotbal
z vysledku, NHL z NHL API), tiket se uzavre, az maji vysledek vsechny nohy (nebo kdyz nektera prohraje).
Vraceni vkladu nohy = kurz 1, pul vyhra / pul prohra = polovina (jako u kancelari).

Pred vykopem prvni nohy se kurz tiketu aktualizuje podle posledniho navrhu; tiket, ktery bot prestal navrhovat,
se jen oznaci (vyhodnoti se stejne – statistika nesmi byt vylepsena vynechanim stazenych navrhu).

Statistika (27.11.2026, `evaluation`): pravidlo „kazda sazka jen v prvnim tiketu“ (23.11.) zruseno – vysledek
zavisel na poradi navrhu (nahodne poradi dalo ROI od -3 % do +46 %) a 17 z 61 sazek se do statistiky nedostalo nikdy.
Misto nej tri pohledy, ktere se nescitaji:
- A: vsechny tikety po 1 j. (= vysledek strategie „vsadit kazdy navrzeny tiket“). Tikety sdileji sazky a zapasy,
  proto se nezavislost neresi vyhazovanim tiketu, ale intervalem se SDILENYMI ZAPASY (rozptyl pocita kovariance
  vsech dvojic tiketu se spolecnym zapasem, `roi_ci`) a poctem nezavislych zapasu;
- C: jen doporuceny tiket (prvni navrh kola, ktery by se realne vsadil);
- F: kazda sazka (zapas + trh + vyber) jednou jako single – zda ma uz vyber sazek vyhodu proti kurzu.
Kontrola dat: vysledek a zisk tiketu se prepocita z vysledku obou noh, kurz tiketu = soucin kurzu noh a stejna sazka
musi mit ve vsech tiketech stejny vysledek; neshoda se zobrazi, nic se neopravuje.
"""

from __future__ import annotations

import copy
import hashlib
import math
from datetime import date, datetime

from . import paper

PROFILE = "kombi"
MARKET = "KOMBI"
WITHDRAWN = "návrh stažen před výkopem"
TARGET = "cílová noha: kurz = minimální, ověř u kanceláře"
REC = "doporučený tiket (nejlepší)"
Z95 = 1.96
ODDS_TOL = 0.01  # kurz tiketu je zaokrouhleny soucin kurzu noh (relativni tolerance)
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
    """Statistika kombi tiketu oddelene podle overeni cen (vsechny tikety po 1 j., bez tvrde odmitnutych).
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


def _match(x: dict) -> tuple:
    return x["name"], x["day"]


def _leg_profit(result: str, odds: float) -> float:
    return {"won": odds - 1, "half_won": (odds - 1) / 2, "half_lost": -0.5, "lost": -1.0}.get(result, 0.0)


def roi_ci(items: list[tuple[list, float]]) -> tuple | None:
    """95% interval prumerneho zisku na 1 j. pri zavislosti pres zapasy. `items` = [(zapasy polozky, zisk)].
    Rozptyl prumeru = (sum e_i^2 + sum e_i*e_j pres dvojice polozek se spolecnym zapasem) / n^2, e = odchylka od
    prumeru (robustni odhad pro shlukovani pres vice zapasu, jako cluster-robust SE). Nezavisle tikety -> bezny
    interval; tikety sdilejici zapas se nepocitaji jako nova informace. Bez nahody, vzdy stejny vysledek."""
    n = len(items)
    if n < 2 or len({m for ms, _ in items for m in ms}) < 2:
        return None
    mean = sum(pr for _, pr in items) / n
    e = [pr - mean for _, pr in items]
    by: dict = {}
    for i, (ms, _) in enumerate(items):
        for m in set(ms):
            by.setdefault(m, []).append(i)
    pairs = {(i, j) for ix in by.values() for i in ix for j in ix if i < j}
    diag = sum(x * x for x in e)
    s = max(diag + 2 * sum(e[i] * e[j] for i, j in pairs), diag)  # zaporna kovariance interval nezuzi
    se = math.sqrt(s * n / (n - 1)) / n
    return mean - Z95 * se, mean + Z95 * se


def _view(tickets: list) -> dict:
    """Souhrn skupiny tiketu: paper.stats + nezavisle zapasy + interval po zapasech."""
    from .metrics import is_staked

    st = paper.stats(tickets)
    staked = [p for p in tickets if p.result is not None and is_staked(p.result)]
    st["matches"] = len({_match(x) for p in staked for x in p.legs})
    st["ci"] = roi_ci([([_match(x) for x in p.legs], p.profit) for p in staked])
    return st


def ticket_issues(p) -> list[str]:
    """Neshody tiketu s jeho nohama (nic se neopravuje, jen se ukaze)."""
    out = []
    if len(p.legs) < 2:
        out.append(f"tiket má {len(p.legs)} nohu")
    if len({_match(x) for x in p.legs}) < len(p.legs):
        out.append("dvě nohy ze stejného zápasu")
    prod = math.prod(x["odds"] for x in p.legs) if p.legs else None
    if prod and abs(p.odds - prod) > ODDS_TOL * prod:
        out.append(f"kurz tiketu {p.odds:.2f} ≠ součin kurzů nohou {prod:.2f}")
    if p.result is not None:
        q = copy.deepcopy(p)
        if _close(q) is None:
            out.append(f"tiket má výsledek {p.result}, ale nohy ještě nejsou rozhodnuté")
        elif q.result != p.result or abs((q.profit or 0) - (p.profit or 0)) > 1e-3:
            out.append(f"výsledek tiketu {p.result} {p.profit:+.2f} j. ≠ podle nohou {q.result} {q.profit:+.2f} j.")
    return out


def evaluation(picks) -> dict:
    """Vyhodnoceni kombi tiketu (A / C / F + kontrola dat), viz docstring modulu. Hypoteticke a overene ceny
    se tu nerozlisuji – to dela `verification`."""
    mine = paper.counted([p for p in picks if p.profile == PROFILE])
    out = {"all": _view(mine), "rec": _view([p for p in mine if REC in (p.flags or [])])}
    legs, results = {}, {}
    for p in mine:  # sazka jednou: kurz z prvniho tiketu, ve kterem se objevila (poradi zapisu, ne vysledek)
        for x in p.legs:
            k = _leg_key(x)
            legs.setdefault(k, x)
            if x.get("result") is not None:
                results.setdefault(k, set()).add(x["result"])
    staked = [(k, x) for k, x in legs.items() if len(results.get(k, ())) == 1
              and next(iter(results[k])) in ("won", "lost", "half_won", "half_lost")]
    res = {k: next(iter(results[k])) for k, _ in staked}
    n = len(staked)
    out["legs"] = {
        "unique": len(legs), "settled": n, "open": sum(1 for k in legs if k not in results),
        "void": sum(1 for k in legs if results.get(k) == {"void"}),
        "hit": sum(1.0 if res[k] == "won" else 0.5 if res[k].startswith("half") else 0.0 for k, _ in staked) / n if n else None,
        "implied": sum(1 / x["odds"] for _, x in staked) / n if n else None,
        "roi": sum(_leg_profit(res[k], x["odds"]) for k, x in staked) / n if n else None,
        "ci": roi_ci([([_match(x)], _leg_profit(res[k], x["odds"])) for k, x in staked]),
        "matches": len({_match(x) for _, x in staked})}
    issues = [(p.name, i) for p in mine for i in ticket_issues(p)]
    issues += [(f"{k[0]} {k[2]} {k[3]}", f"stejná sázka má v tiketech různé výsledky: {', '.join(sorted(v))}")
               for k, v in results.items() if len(v) > 1]
    out["issues"], out["checked"] = issues, len(mine)
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
        flags = ([REC] if rank == 0 else []) + ([TARGET] if any(x["target"] for x in legs) else [])
        old = mine.get(pid)
        if old is not None:
            if old.result is None:
                prev = {_leg_key(x): x for x in old.legs}
                for x in legs:  # prvni kurz nohy zustava (zmena kurzu pred vykopem = jina cena nez pri navrhu)
                    x["first_odds"] = prev.get(_leg_key(x), {}).get("first_odds", prev.get(_leg_key(x), {}).get("odds", x["odds"]))
                old.legs, old.odds, old.p_model, old.detail = legs, round(c.odds, 2), round(c.prob, 4), _detail(legs)
                old.flags = sorted(set(flags) | ({REC} & set(old.flags)))
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
