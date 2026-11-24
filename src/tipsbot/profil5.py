"""Profil 5 – Market Value (Faze 3.2.1). EXPERIMENT, POUZE STINOVY REZIM.

Otazka: nabizi nektera kancelar lepsi kurz, nez odpovida NEZAVISLEMU trznimu konsenzu – bez vlastniho modelu tymu?

Vypocet pro kazdou nabidku (zapas, trh, vyber, kancelar) z ulozenych kurzu kurzoveho API (zadny dalsi dotaz):
1. vezmou se vsechny kancelare s TYMZ trhem (stejny trh, linie i strana – klic trhu kurzoveho API);
2. vynecha se hodnocena kancelar A CELA JEJI SKUPINA (unibet_se/unibet_nl… maji stejne ceny), burzy, neuplne
   trhy (chybi protistrana), zastarale kurzy a trhy s nesmyslnou marzi (marketcheck.consensus);
3. kazda zbyla kancelar se zbavi marze mocninnou metodou (devig_power), skupina = median, reference = median skupin;
4. ferovy kurz = 1 / p_ref, EV proti trhu = kurz × p_ref − 1, odchylka = kurz / median ostatnich − 1;
5. referenci tvori aspon `min_ref_books` (3) NEZAVISLYCH skupin – jinak se kandidat nevytvori (NEOVĚŘENO);
6. kandidat musi projit spolecnou Market Integrity (stari kurzu, protistrana, cenove extremy, potvrzeni) – prahy
   jsou stejne jako v Profilu 4. Nejvyssi odchylka se NEBERE automaticky: nejdriv kontrola, pak vyber.
7. Na zapas nejvys jeden kandidat (prvni zjisteny, ktery prosel). Vstupy kandidata se uz NEMENI (zadne dodatecne
   vylepsovani); dalsi skeny dopisuji jen zaviraci cenu pro CLV, po zapase vysledek.

Market Value a Line Shopping se meri ZVLAST a nescitaji se:
- Market Value = vysledek sazky za kurz hodnocene kancelare (ta, ktera je nad konsenzem);
- Line Shopping = rozdil mezi nejlepsim kurzem trhu a medianem vsech kancelari u tychz vyberu (report ukazuje,
  kolik by stejne vybery vydelaly za median a za nejlepsi kurz).

Stinovy rezim: nevydava oficialni tipy, nemeni zadny jiny profil, Portfolio Risk, rozhodnuti APPROVED/WATCH/NO BET
ani bankroll; nepouziva pravdepodobnosti modelu A–D ani zadneho jineho modelu tymu.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from . import marketcheck as MC
from .storage import P5Candidate, utcnow

ALGO = "p5-1.0"
CANDIDATE, REJECTED = "candidate", "rejected"
SHADOW = "SHADOW MODE – jen evidence, žádné tipy ani vklady"


def _value_cfg():
    from .config import SETTINGS

    return SETTINGS.value


@dataclass(frozen=True)
class P5Config:
    min_ref_books: int = 3  # aspon tolik nezavislych skupin kancelari mimo hodnocenou (automaticky se nesnizuje)
    min_edge: float = 0.03  # EV proti trhu od teto hodnoty je kandidat (experimentalni vychozi hodnota, neladena)
    horizon_h: int = 48
    min_odds: float = field(default_factory=lambda: _value_cfg().min_odds)  # stejne pasmo jako ostatni profily
    max_odds: float = field(default_factory=lambda: _value_cfg().max_odds)
    closing_within_min: int = 20  # = Profil 4: skutecny zaviraci kurz jen z pozorovani do 20 min pred vykopem
    closing_max_source_age_min: int = 60

    @property
    def mi(self) -> MC.MIConfig:
        from dataclasses import replace

        return replace(MC.DEFAULT, min_ref_books=self.min_ref_books)


DEFAULT = P5Config()


def _sport(league: str) -> str:
    return "NHL" if league == "NHL" else "fotbal"


def _id(*parts) -> str:
    return "p5" + hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


def _reason_key(r: str) -> str:
    """Kategorie duvodu odmitnuti (pro report)."""
    for key, label in (("nad mediánem", "cenový extrém (nad mediánem)"), ("nad férovou cenou", "cenový extrém (nad férovou cenou, nepotvrzen)"),
                       ("starý", "zastaralý kurz"), ("protistran", "chybí protistrana"), ("různé doby", "kurz z jiné doby než trh"),
                       ("málo nezávislých", "málo nezávislých kanceláří"), ("pásmo", "kurz mimo pásmo"),
                       ("nejsou jeden trh", "kurzy netvoří jeden trh"), ("stáří", "stáří kurzu neověřeno"),
                       ("nemá v kurzech", "nabídka nejde ověřit")):
        if key in r:
            return label
    return r


def _line_shop(books: dict, sel: str) -> tuple[float | None, str, float | None]:
    prices = sorted(((pr[sel], b) for b, pr in books.items() if sel in pr and b not in MC.EXCHANGES), reverse=True)
    if not prices:
        return None, "", None
    vals = sorted(p for p, _ in prices)
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return prices[0][0], prices[0][1], med


def scan(ledger, league: str, events: list, now: datetime, stamps: dict | None = None,
         cfg: P5Config = DEFAULT) -> dict:
    """Jeden pruchod soutezi: novi kandidati, odmitnute nabidky a zaviraci ceny otevrenych kandidatu.
    Vraci pocty (ulozi se i jako posledni stav skenu)."""
    s = ledger.s
    counts: Counter = Counter()
    mi = cfg.mi
    have = {c.match_id for c in s.scalars(select(P5Candidate).where(P5Candidate.status == CANDIDATE))}
    rejected_ids = set(s.scalars(select(P5Candidate.id).where(P5Candidate.status == REJECTED)))
    horizon = now + timedelta(hours=cfg.horizon_h)
    for ev in events:
        if not (now < ev.kickoff <= horizon):
            continue
        passed = []
        for mk in ev.markets.values():
            st = MC.market_stamps(stamps, ev.match_id, mk.market)
            for book, prices in mk.books.items():
                if book in MC.EXCHANGES:
                    continue
                for sel in mk.selections:
                    if sel not in prices:
                        continue
                    odds = prices[sel]
                    counts["posouzeno nabídek"] += 1
                    ref = MC.consensus(mk.books, mk.selections, sel, now, st, book, mi)
                    if not ref["p"] or ref["n"] < cfg.min_ref_books:
                        counts["bez důvěryhodné reference (málo nezávislých kanceláří)"] += 1
                        continue
                    edge = odds * ref["p"] - 1.0
                    if edge < cfg.min_edge:
                        continue
                    counts["nad prahem EV proti trhu"] += 1
                    chk = MC.check(mk.market, sel, odds, book, mk.books, mk.selections, now, st, mi)
                    reasons = []
                    if not (cfg.min_odds <= odds <= cfg.max_odds):
                        reasons.append(f"kurz {odds:.2f} mimo pásmo {cfg.min_odds:.2f}–{cfg.max_odds:.2f}")
                    if chk["status"] != MC.OK:
                        reasons += chk["reasons"] or [f"kontrola kurzu: {chk['status']}"]
                        if chk.get("freshness") == MC.UNVERIFIED:
                            reasons.append("stáří kurzu neověřeno (chybí čas kurzu v cache)")
                    best, best_book, med = _line_shop(mk.books, sel)
                    row = dict(league=league, sport=_sport(league), match_id=ev.match_id, match=ev.name,
                               kickoff=ev.kickoff, market=mk.market, selection=sel, book=book, odds=odds,
                               p_ref=ref["p"], fair_odds=ref["fair"], edge=edge, dev_median=chk.get("dev_median"),
                               n_ref=ref["n"], age_min=chk.get("age_min"), best_odds=best, best_book=best_book,
                               median_odds=med,
                               data=json.dumps({"books": mk.books, "selections": list(mk.selections),
                                                "stamps": {b: t.isoformat() for b, t in st.items()},
                                                "included": ref["included"], "excluded": ref["excluded"],
                                                "integrity": chk}, ensure_ascii=False, default=str),
                               reasons=json.dumps(reasons, ensure_ascii=False))
                    if MC.is_hard_reject(chk):  # spolecna stopa tvrdych odmitnuti vsech profilu
                        MC.log_rejection(s, "profil5", ev.match_id, ev.name, ev.kickoff, chk)
                    if reasons:
                        for r in reasons:
                            counts["odmítnuto: " + _reason_key(r)] += 1
                        rid = _id(ev.match_id, mk.market, sel, book)
                        if rid not in rejected_ids:
                            s.add(P5Candidate(id=rid, created=now, algo=ALGO, status=REJECTED, **row))
                            rejected_ids.add(rid)
                        continue
                    passed.append((edge, row))
        if not passed:
            continue
        if ev.match_id in have:
            counts["další výběr zápasu s kandidátem (nezapsán)"] += len(passed)
            continue
        passed.sort(key=lambda x: -x[0])
        row = passed[0][1]
        s.add(P5Candidate(id=_id(ev.match_id, row["market"], row["selection"], row["book"], now.isoformat()),
                          created=now, algo=ALGO, status=CANDIDATE, **row))
        have.add(ev.match_id)
        counts["nový kandidát"] += 1
        counts["další výběr zápasu s kandidátem (nezapsán)"] += len(passed) - 1
    counts["aktualizována závěrečná cena"] = update_closing(ledger, events, now, stamps, cfg)
    s.flush()
    out = {"league": league, "t": now.isoformat(timespec="minutes"), "algo": ALGO, **counts}
    ledger._set(f"p5_last_scan:{league}", json.dumps(out, ensure_ascii=False))
    return out


def update_closing(ledger, events: list, now: datetime, stamps: dict | None = None, cfg: P5Config = DEFAULT) -> int:
    """Zaviraci cena otevrenych kandidatu = konsenzus BEZ hodnocene kancelare v poslednim skenu pred vykopem.
    Skutecny zaviraci kurz jen z pozorovani nejvys 20 min pred vykopem a s kurzy nejvys 60 min starymi
    (jako Profil 4); jinak „poslední pozorovaný kurz“ – CLV z nej se nevydava za overene."""
    by_id = {ev.match_id: ev for ev in events if ev.kickoff > now}
    n = 0
    for c in ledger.s.scalars(select(P5Candidate).where(P5Candidate.result.is_(None), P5Candidate.kickoff > now)):
        if c.created >= now:
            continue  # zaviraci cena az z pozdejsiho skenu nez zjisteni kandidata (jinak by CLV = vlastni EV)
        ev = by_id.get(c.match_id)
        mk = ev.markets.get(c.market) if ev else None
        if mk is None:
            continue
        st = MC.market_stamps(stamps, c.match_id, c.market)
        ref = MC.consensus(mk.books, mk.selections, c.selection, now, st, c.book, cfg.mi)
        if not ref["p"] or ref["n"] < cfg.min_ref_books:
            continue
        before = (c.kickoff - now).total_seconds() / 60.0
        fresh = ref["ages"] and max(ref["ages"]) <= cfg.closing_max_source_age_min
        c.closing_p, c.closing_at = ref["p"], now
        c.closing_kind = "závěrečný kurz" if before <= cfg.closing_within_min and fresh else "poslední pozorovaný kurz"
        c.clv = c.odds * ref["p"] - 1.0
        n += 1
    return n


def _unit_profit(result: str, odds: float) -> float:
    from .paper import unit_profit

    return unit_profit(result, odds)


def settle(ledger, results) -> list[str]:
    """Fotbal: vysledky kandidatu (i odmitnutych – kontrola, jestli odmitnuti davalo smysl) z dat vysledku."""
    from .settlement import settle_bet

    notes = []
    for c in ledger.s.scalars(select(P5Candidate).where(P5Candidate.result.is_(None), P5Candidate.sport == "fotbal")):
        got = settle_bet(results, c.match, c.kickoff.date(), c.market, c.selection)
        if got is None or got[1] is None:
            continue
        c.score, c.result = got
        c.profit, c.settled_at = _unit_profit(c.result, c.odds), utcnow()
        if c.status == CANDIDATE:
            notes.append(f"Profil 5 (shadow) {c.match} {c.score}: {c.market} {c.selection} @ {c.odds:.2f} -> {c.result}")
    return notes


def settle_nhl(ledger, games: list[dict], find_game, grade) -> list[str]:
    notes = []
    for c in ledger.s.scalars(select(P5Candidate).where(P5Candidate.result.is_(None), P5Candidate.sport == "NHL")):
        g = find_game(games, c.match, c.kickoff)
        res = grade(c.market, c.selection, g) if g else None
        if res is None:
            continue
        c.score, c.result = f"{g['final'][0]}:{g['final'][1]}", res
        c.profit, c.settled_at = _unit_profit(res, c.odds), utcnow()
        if c.status == CANDIDATE:
            notes.append(f"Profil 5 (shadow) NHL {c.match} {c.score}: {c.market} {c.selection} -> {res}")
    return notes


# --- report -------------------------------------------------------------------------------------------------
def rows(session, status: str | None = CANDIDATE) -> list[P5Candidate]:
    q = select(P5Candidate).order_by(P5Candidate.created)
    if status:
        q = q.where(P5Candidate.status == status)
    return list(session.scalars(q))


def _ci(items: list[tuple[str, float, float]]):
    from .profil4.history import cluster_ci, verdict

    cl: dict = {}
    for key, num, den in items:
        c = cl.setdefault(key, [0.0, 0.0])
        c[0] += num
        c[1] += den
    point, lo, hi = cluster_ci({k: tuple(v) for k, v in cl.items()})
    return point, lo, hi, verdict(lo, hi, len(cl))


def perf(cands: list[P5Candidate], price: str = "odds") -> dict:
    """Vysledky kandidatu pri danem kurzu (odds = kancelar nad konsenzem, median_odds, best_odds).
    Yield = zisk / pocet sazek s vysledkem vyhra/prohra; interval = bootstrap po zapasech."""
    from .metrics import is_staked

    staked = [c for c in cands if is_staked(c.result) and getattr(c, price)]
    profit = [(c.match_id, _unit_profit(c.result, getattr(c, price)), 1.0) for c in staked]
    out = {"n": len(cands), "settled": sum(1 for c in cands if c.result), "staked": len(staked),
           "matches": len({c.match_id for c in cands}), "wins": sum(c.result in ("won", "half_won") for c in staked),
           "losses": sum(c.result in ("lost", "half_lost") for c in staked),
           "push": sum(1 for c in cands if c.result == "void"), "profit": sum(p for _, p, _ in profit)}
    out["yield"] = out["profit"] / len(staked) if staked else None
    if len(profit) >= 2:
        _, lo, hi, st = _ci(profit)
        out["yield_ci"], out["status"] = (None if math.isnan(lo) else (lo, hi)), st
    else:
        out["yield_ci"], out["status"] = None, f"málo dat ({out['matches']} záp.)"
    binary = [c for c in staked if c.result in ("won", "lost") and c.p_ref and 0 < c.p_ref < 1]
    if binary:
        ys = [(c.p_ref, 1.0 if c.result == "won" else 0.0) for c in binary]
        out["brier"] = sum((p - y) ** 2 for p, y in ys) / len(ys)
        out["logloss"] = -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in ys) / len(ys)
    else:
        out["brier"] = out["logloss"] = None
    clv = [c.clv for c in cands if c.clv is not None and c.closing_kind == "závěrečný kurz"]
    last = [c.clv for c in cands if c.clv is not None and c.closing_kind != "závěrečný kurz"]
    out.update(clv=sum(clv) / len(clv) if clv else None, clv_n=len(clv),
               clv_last=sum(last) / len(last) if last else None, clv_last_n=len(last),
               avg_edge=sum(c.edge for c in cands) / len(cands) if cands else None)
    return out


NO_MARKET = ("nabídka nejde ověřit", "chybí protistrana", "kurzy netvoří jeden trh")  # kategorie „bez platného trhu“
NO_REF = "bez důvěryhodné reference (málo nezávislých kanceláří)"


def _offer(c) -> tuple:
    return (c.match_id, c.market, c.selection, c.book)


def counts(cands: list, rej: list, last_scans: list[dict]) -> dict:
    """Faze 3.2.3: unikatni nabidky oddelene od jednotlivych duvodu. Odmitnuta nabidka je ulozena jednou (id podle
    zapasu, trhu, vyberu a kancelare) a muze mit vic duvodu – do poctu nabidek se pocita jednou.

    `reasons`: kategorie -> {"offers": nabidek s duvodem, "instances": jednotlivych duvodu, "overlap": z nich nabidek
    s jeste jinym duvodem}. Nabidky bez duveryhodne reference se po nabidkach neukladaji – jsou jen v pocitadle
    posledniho skenu kazde souteze (ne unikatni, kazdy sken je pocita znovu)."""
    per: list[tuple[tuple, set, int]] = []
    for r in rej:
        raw = [_reason_key(x) for x in json.loads(r.reasons or "[]")]
        per.append((_offer(r), set(raw), len(raw)))
    reasons: dict[str, dict] = {}
    for _, cats, _ in per:
        for k in cats:
            reasons.setdefault(k, {"offers": 0, "instances": 0, "overlap": 0})["offers"] += 1
            reasons[k]["overlap"] += len(cats) > 1
    for r in rej:
        for x in json.loads(r.reasons or "[]"):
            reasons[_reason_key(x)]["instances"] += 1
    reasons = dict(sorted(reasons.items(), key=lambda kv: -kv[1]["offers"]))
    cand_offers, rej_offers = {_offer(c) for c in cands}, {o for o, _, _ in per}
    return {
        "candidates": len(cand_offers), "candidate_rows": len(cands),
        "rejected": len(rej_offers), "rejected_rows": len(rej),
        "rejected_groups": len({(m, mk, s, MC.family(b)) for m, mk, s, b in rej_offers}),
        "reason_instances": sum(n for _, _, n in per), "multi_reason": sum(1 for _, c, _ in per if len(c) > 1),
        "no_market": sum(1 for _, c, _ in per if c & set(NO_MARKET)),
        "no_reference_last_scan": sum(int(s.get(NO_REF, 0)) for s in last_scans),
        "assessed_last_scan": sum(int(s.get("posouzeno nabídek", 0)) for s in last_scans),
        "overlap_cand_rej": len(cand_offers & rej_offers), "reasons": reasons}


def report(session) -> dict:
    cands, rej = rows(session, CANDIDATE), rows(session, REJECTED)
    reasons: Counter = Counter()  # jednotlive duvody (jedna nabidka jich muze mit vic) – viz counts()
    for r in rej:
        for x in json.loads(r.reasons or "[]"):
            reasons[_reason_key(x)] += 1
    shop = [c for c in cands if c.best_odds and c.median_odds]
    out = {"algo": ALGO, "mode": SHADOW, "cfg": DEFAULT, "candidates": len(cands), "rejected": len(rej),
           "reasons": dict(reasons.most_common()), "by_sport": {}, "all": perf(cands),
           "rejected_perf": perf(rej),
           "line_shopping": {"n": len(shop),
                             "avg_gain": (sum(c.best_odds / c.median_odds - 1 for c in shop) / len(shop)) if shop else None,
                             "at_book": perf(shop, "odds"), "at_median": perf(shop, "median_odds"),
                             "at_best": perf(shop, "best_odds")}}
    for sp in ("fotbal", "NHL"):
        out["by_sport"][sp] = perf([c for c in cands if c.sport == sp])
    from .storage import Ledger

    led = Ledger(session)
    out["last_scans"] = [json.loads(v) for k, v in led.s.execute(
        __import__("sqlalchemy").text("SELECT key, value FROM state WHERE key LIKE 'p5_last_scan:%'")).all()]
    out["counts"] = counts(cands, rej, out["last_scans"])
    return out


def validate(session, cfg: P5Config = DEFAULT) -> list[str]:
    """Nezavisla kontrola ulozenych kandidatu z jejich snapshotu: hodnocena kancelar ani jeji skupina nesmi byt
    v referenci, reference ma aspon `min_ref_books` skupin, p_ref a EV jdou znovu spocitat. Vraci seznam problemu."""
    from .devig import devig_power

    out = []
    for c in rows(session, None):
        d = json.loads(c.data or "{}")
        inc, sels, books = d.get("included", []), d.get("selections", []), d.get("books", {})
        if c.book in inc or any(MC.family(b) == MC.family(c.book) for b in inc):
            out.append(f"{c.id}: hodnocená kancelář {c.book} (nebo její skupina) je v referenci")
        if c.status == CANDIDATE and c.n_ref < cfg.min_ref_books:
            out.append(f"{c.id}: reference jen z {c.n_ref} skupin")
        if not inc or not sels:
            continue
        i = sels.index(c.selection)
        fam: dict[str, list[float]] = {}
        for b in inc:
            fam.setdefault(MC.family(b), []).append(devig_power([books[b][x] for x in sels])[i])
        import statistics

        p = statistics.median(statistics.median(v) for v in fam.values())
        if abs(p - (c.p_ref or 0)) > 1e-9:
            out.append(f"{c.id}: p_ref {c.p_ref} nesouhlasí s přepočtem {p}")
        if c.edge is not None and abs(c.odds * p - 1 - c.edge) > 1e-9:
            out.append(f"{c.id}: EV proti trhu nesouhlasí")
    return out
