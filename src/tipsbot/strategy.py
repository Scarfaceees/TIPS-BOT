"""Srovnani strategii (Faze 3.2.1): jednotne definice metrik pro vsechny profily – panel, CLI i Excel.

Definice (stejne pro vsechny profily; vyberova logika profilu se nemeni):
- vyber = jeden zaznam profilu (zapas + trh + vyber); stejny vyber v jednom profilu se pocita jednou;
- nezavislych zapasu = pocet ruznych zapasu mezi vybery (vybery jednoho zapasu nejsou nezavisle);
- predikce = pravdepodobnost modelu ulozena u vyberu (Brier a Log Loss jen z cistych vyher/proher);
- oficialni tip = doporuceni ke vsazeni (Tipy bota, Profil 4 APPROVED); virtualni sazka = 1 j. bez penez;
  skutecna sazka = Moje tikety v Kc (nikdy se nescita s jednotkami);
- yield = ROI na 1 j. = zisk v jednotkach / pocet sazek s vysledkem vyhra nebo prohra (vcetne pul);
  vraceno (PUSH) a neplatne (INVALID) se do jmenovatele nepocitaji a nejsou totez;
- win rate = vyhry / (vyhry + prohry);
- CLV = kurz / ferovy zaviraci kurz − 1, jen z overeneho zaviraciho kurzu (posledni pozorovany se nevydava za CLV);
- spolehlivost = 95% interval yieldu z bootstrapu po ZAPASECH (profil4.history.cluster_ci); pod 30 zapasy MÁLO DAT.
"""

from __future__ import annotations

import re

from . import export as X
from . import marketcheck as MC

METHODS = {
    "Základ": "model bota (Dixon-Coles / NHL), u zápasu výběr s nejvyšším EV od 5 %, kurz = medián zahraničních kanceláří",
    "Profil 2 – Scénáře model/trh": "λ modelu proti trhu, scénáře A model / B kompromis 50/50 / C trh (kontrola), EV>0 v A i B, "
                                    "prohra ≤ 25 % v horším scénáři",
    "Profil 3 – Simulace trhu": "λ fitované z kurzů kanceláří, 4 scénáře (trh 50 %, model 25 %, ±12 % gólů), kurz 1,15–1,75, "
                                "prohra ≤ 25 %; tržní scénář je kruhový (viz LOBO experiment)",
    "Tipy bota": "oficiální tipy bota (model + Pinnacle, práh EV), zapsané v okamžiku tipu",
    "Rohy": "25 % model rohů + 75 % trh (od 21.11.2026 22:09 UTC), EV ≥ 5 %",
    "Karty": "25 % model karet a rozhodčího + 75 % trh (od 21.11.2026 22:09 UTC), EV ≥ 5 %",
    "Profil 4 – oficiální (APPROVED)": "modely A–D, Market Integrity, ranking, rozhodnutí APPROVED",
    "Profil 4 – WATCH": "Profil 4: jen sledovat (něco chybí nebo nesplněno)",
    "Profil 4 – NO BET": "Profil 4: nedoporučeno (kontrola)",
    "Profil 5 – Market Value (SHADOW)": "kurz kanceláře proti férové ceně ostatních kanceláří (bez hodnocené kanceláře a její "
                                        "skupiny, ≥ 3 nezávislé), bez modelu týmů; jen stínová evidence",
}


def method_of(group: str) -> str:
    for k, v in METHODS.items():
        if group.startswith(k):
            return v
    if group.startswith("Kombi"):
        return "2–3 výběry z různých zápasů z Přehledu, kurz = součin; ceny podle ověření (cílový kurz ≠ dostupný kurz)"
    if group.startswith(X.TICKETS):
        return "skutečně vsazené tikety v Kč"
    return ""


def match_key(name: str, day: str) -> tuple:
    """Pribizne parovani zapasu mezi zdroji (kurzove API vs API-Football): datum + prvni 4 znaky obou tymu."""
    home, _, away = (name or "").partition(" - ")
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())[:4]  # noqa: E731
    return (day or "")[:10], norm(home), norm(away)


def _reliability(recs: list) -> tuple[float | None, tuple | None, str]:
    from .profil4.history import cluster_ci, verdict

    staked = [r for r in recs if r.outcome in (X.WIN, X.LOSS)]
    if not staked:
        return None, None, "bez výsledků"
    cl: dict = {}
    for r in staked:
        c = cl.setdefault(match_key(r.match, r.day), [0.0, 0.0])
        c[0] += r.profit_units or 0.0
        c[1] += 1.0
    point, lo, hi = cluster_ci({k: tuple(v) for k, v in cl.items()})
    ci = None if lo != lo else (lo, hi)  # nan
    return point, ci, verdict(lo, hi, len(cl))


def row(group: str, sport: str, recs: list) -> dict:
    st = X.stats(recs)
    y, ci, rel = _reliability(recs)
    integ = [r.integrity for r in recs if r.integrity]
    return {"group": group, "sport": sport, "method": method_of(group), "kind": recs[0].kind if recs else "",
            "picks": st["n"], "matches": len({match_key(r.match, r.day) for r in recs}),
            "settled": st["settled"], "wins": st["wins"], "losses": st["losses"], "push": st["push"],
            "invalid": st["invalid"], "yield": st["roi"], "yield_ci": ci, "reliability": rel, "hit": st["hit"],
            "profit": st["profit"], "brier": st["brier"], "logloss": st["logloss"], "calib_n": st["calib_n"],
            "clv": st["clv"], "clv_n": st["clv_n"],
            "integrity": (f"{sum(1 for s in integ if s == MC.OK)}/{len(recs)} ověřeno" if integ
                          else "vlastní integrita Profilu 4" if group.startswith("Profil 4") else "bez kontroly (starší záznamy)"),
            "real_money": st.get("profit_money")}


def comparison(d) -> list[dict]:
    """Radek na strategii: celkem, pak fotbal a NHL zvlast (kombi a skutecne tikety jen celkem)."""
    out = []
    for g, rs in sorted(X.groups(d.records).items()):
        out.append(row(g, "celkem", rs))
        sports = sorted({r.sport for r in rs} - {"kombi"})
        if len(sports) > 1 or (sports and g.startswith("Profil 4")):
            for sp in sports:
                sub = [r for r in rs if r.sport == sp]
                if sub:
                    out.append(row(g, sp, sub))
    if d.p5 is not None:
        out += p5_rows(d.p5)
    return out


def p5_rows(rep: dict) -> list[dict]:
    out = []
    for sp, perf in [("celkem", rep["all"])] + list(rep["by_sport"].items()):
        out.append({"group": "Profil 5 – Market Value (SHADOW)", "sport": sp, "method": METHODS["Profil 5 – Market Value (SHADOW)"],
                    "kind": "stínový kandidát", "picks": perf["n"], "matches": perf["matches"],
                    "settled": perf["settled"], "wins": perf["wins"], "losses": perf["losses"], "push": perf["push"],
                    "invalid": 0, "yield": perf["yield"], "yield_ci": perf["yield_ci"], "reliability": perf["status"],
                    "hit": (perf["wins"] / perf["staked"]) if perf["staked"] else None, "profit": perf["profit"],
                    "brier": perf["brier"], "logloss": perf["logloss"], "calib_n": perf["staked"],
                    "clv": perf["clv"], "clv_n": perf["clv_n"], "integrity": "jen kandidáti, kteří prošli kontrolou",
                    "real_money": None})
    return out


def p5_versus(d, cands: list) -> list[dict]:
    """Profil 5 proti ostatnim profilum jen na STEJNYCH zapasech (a zvlast stejny trh i vyber)."""
    p5 = {}
    for c in cands:
        p5.setdefault(match_key(c.match, c.kickoff.date().isoformat()), []).append(c)
    out = []
    for g, rs in sorted(X.groups(d.records).items()):
        if g.startswith(X.TICKETS) or g.startswith("Kombi"):
            continue
        same = [r for r in rs if match_key(r.match, r.day) in p5]
        same_mkt = [r for r in same if any(c.market == r.market and c.selection == (r.code or r.selection)
                                           for c in p5[match_key(r.match, r.day)])]
        mine = [c for k in {match_key(r.match, r.day) for r in same} for c in p5[k]]
        st = X.stats(same)
        settled = [c for c in mine if c.result and c.result != "void"]
        from .paper import unit_profit

        out.append({"group": g, "common_matches": len({match_key(r.match, r.day) for r in same}),
                    "same_market": len(same_mkt), "other_yield": st["roi"], "other_clv": st["clv"],
                    "p5_yield": (sum(unit_profit(c.result, c.odds) for c in settled) / len(settled)) if settled else None,
                    "p5_clv": (sum(c.clv for c in mine if c.clv is not None) / max(1, sum(c.clv is not None for c in mine)))
                    if any(c.clv is not None for c in mine) else None,
                    "p5_avg_edge": (sum(c.edge for c in mine) / len(mine)) if mine else None})
    return out


def integrity_consistency(picks: list) -> list[dict]:
    """Stejna nabidka (zapas, trh, vyber, kancelar, kurz, cas kontroly) vybrana vic profily musi mit stejny stav
    kontroly. Vraci neshody (ocekava se prazdny seznam – kontrola je jedna funkce pro vsechny profily)."""
    seen: dict[tuple, tuple[str, str]] = {}
    bad = []
    for p in picks:
        it = p.integrity or {}
        if not it:
            continue
        k = (p.match_id, p.market, p.selection, p.book, round(p.odds, 2), it.get("t"))
        if k in seen and seen[k][1] != it.get("status"):
            bad.append({"offer": k, "profiles": (seen[k][0], p.profile), "status": (seen[k][1], it.get("status"))})
        seen.setdefault(k, (p.profile, it.get("status")))
    return bad


def paired(picks: list, a: str, b: str) -> dict:
    """Dva profily jen na STEJNYCH zapasech (Faze 3.2.2: Profil 3 proti nekruhovemu stinovemu profilu 3x).
    Vybery jednoho zapasu v obou profilech nejsou nezavisle – srovnava se rozdil na tychz zapasech."""
    from . import paper

    pa, pb = paper.of_profile(picks, a), paper.of_profile(picks, b)
    ma, mb = {p.match_id for p in pa}, {p.match_id for p in pb}
    common = ma & mb
    same = sum(1 for p in pa if p.match_id in common
               and any(q.match_id == p.match_id and (q.market, q.selection) == (p.market, p.selection) for q in pb))
    return {"common_matches": len(common), "only_a": len(ma - mb), "only_b": len(mb - ma), "same_bet": same,
            "a": paper.stats([p for p in pa if p.match_id in common]),
            "b": paper.stats([p for p in pb if p.match_id in common]),
            "a_all": paper.stats(pa), "b_all": paper.stats(pb)}
