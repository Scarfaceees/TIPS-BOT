"""Profil 4 pro panel: vysledek skenu / analyzy jako slovnik (JSON) pro karty v panelu.

Jen prevod existujicich vysledku do zobrazitelne podoby – nic se zde nepocita znovu.
"""

from __future__ import annotations

from datetime import datetime

from . import mathcore
from .analysis import MatchAnalysis
from .decision import APPROVED, CAT_A, CAT_B, CAT_C, ORDER, Candidate
from .hunter import ScanReport
from .report import STATUS_CZ, age, local, odds_s, pct
from .stress import Robustness


def _cfg():
    from .config import P4

    return P4


def candidate(c: Candidate, now: datetime) -> dict:
    q = c.quote
    return {
        "label": c.label, "key": f"{c.market}:{c.selection}", "match": c.match, "fixture_id": c.fixture_id,
        "book": q.bookmaker, "odds": odds_s(c.odds), "source": q.source_label(), "quote_status": q.status(now, _cfg().odds),
        "quote_time": local(q.stamp), "quote_age": age(q.stamp, now), "note": q.note,
        "quote_fetched": local(q.fetched_at) if q.fetched_at != q.stamp else "",
        "p_win": pct(c.p_win_c), "p_push": pct(c.p_push_c) if c.p_push_c > 0.005 else "",
        "fair_c": odds_s(c.fair_odds_c), "fair_d": odds_s(c.fair_odds_d),
        "ev_c": pct(c.ev_c), "ev_d": pct(c.ev_d), "ev_pos": c.ev_c >= 0, "min_odds": odds_s(c.min_odds),
        "robustness": round(c.robustness), "models": {k: pct(c.p.get(k)) for k in ("A", "B", "C", "D")},
        "decision": c.decision, "approved": c.decision == APPROVED, "reasons": list(c.reasons),
        "risks": list(dict.fromkeys(c.risks)), "stress": [(n, pct(ev), ev >= 0) for n, ev in c.stress],
        "robust_parts": Robustness(0, c.robustness_parts).lines(),
        "market": ({"p": pct(c.p_market), "ev": pct(c.ev_market), "src": c.market_src}
                   if c.p_market is not None else None),
        "intel": _intel(c),
    }


def _intel(c: Candidate) -> dict | None:
    """Faze 3 pro kartu tipu: kvalita dat, kurzova inteligence, EV po modelech a ranking (uz spocitane)."""
    i = c.intel or {}
    if not i or "error" in i:
        return None
    ev, oi = i.get("ev") or {}, i.get("odds") or {}
    return {
        "quality": i["quality"]["score"], "quality_parts": [f"{p['name']}: {p['text']} ({p['points']:.0f}/{p['max']:.0f})"
                                                           for p in i["quality"]["parts"]],
        "uncertain": i["quality"].get("uncertain", []),
        "rank": i["rank"]["score"], "rank_parts": [f"{p['name']}: {p['text']} ({p['points']:.0f}/{p['max']:.0f})"
                                                   for p in i["rank"]["parts"]],
        "ev_models": {k: pct(v) for k, v in (ev.get("models") or {}).items()},
        "ev_market": pct(ev.get("market")), "value_model": pct(ev.get("value_model")), "value_price": pct(ev.get("value_price")),
        "market_backed": ev.get("market_backed"),
        "sensitivity": [(f"{d * 100:+.0f} p.b.", pct(v), v >= 0) for d, v in ev.get("sensitivity", [])],
        "median": odds_s(oi.get("median")), "fair_market": odds_s(oi.get("fair_market")),
        "dev": pct(oi.get("dev")), "books": oi.get("books"), "flags": oi.get("flags", []), "suspicious": oi.get("suspicious"),
    }


def markets(cands: list[Candidate], quotes: list, now: datetime, limit_books: int = 8) -> list[dict]:
    """Vsechny trhy zapasu pro panel (tabulka na pocitaci, karty na mobilu): kandidat + kurzy vsech kancelari
    na presne tentyz vyber (posledni kurz kazde kancelare, nejvyssi nahore). Jen zobrazeni – nic se nepocita znovu."""
    by_key: dict[str, dict] = {}
    for q in quotes:
        cur = by_key.setdefault(q.key, {}).get(q.bookmaker)
        if cur is None or q.stamp >= cur.stamp:
            by_key[q.key][q.bookmaker] = q
    out = []
    for c in sorted(cands, key=lambda c: (ORDER.get(c.decision, 9), -c.ev_c)):
        d = candidate(c, now)
        books = sorted(by_key.get(c.key, {}).values(), key=lambda q: -q.odds)[:limit_books]
        d["offers"] = [{"book": q.bookmaker, "odds": odds_s(q.odds), "status": q.status(now, _cfg().odds),
                        "time": local(q.stamp)} for q in books]
        d["market_code"] = c.market
        out.append(d)
    return out


def _categories(sel, now: datetime) -> list[dict]:
    out = []
    a = sel.categories.get(CAT_A)
    for cat in (CAT_A, CAT_B, CAT_C):
        c = sel.categories.get(cat)
        out.append({"name": cat, "tip": candidate(c, now) if c else None,
                    "same_as_a": bool(c is not None and cat != CAT_A and c is a)})
    return out


def scan(rep: ScanReport) -> dict:
    now = rep.started
    o = rep.overall
    matches = []
    for an in rep.analyses:
        s = an.selection
        best = s.approved[0] if s.approved else s.best_available
        matches.append({"fixture_id": an.fixture.id, "kickoff": local(an.fixture.kickoff), "name": an.fixture.name,
                        "league": an.fixture.league, "verdict": s.verdict, "quality": pct(an.data_quality, 0),
                        "best": candidate(best, now) if best else None})
    return {
        "kind": "scan", "mode": "Denní sken" if rep.mode == "daily" else "Nadcházejících 48 h", "when": local(now),
        "fixtures": rep.fixtures, "deep": len(rep.analyses), "budget": dict(rep.budget),
        "quick": [{"kickoff": local(q.fixture.kickoff), "name": q.fixture.name, "league": q.fixture.league,
                   "fixture_id": q.fixture.id, "candidate": q.candidate, "reason": q.reason}
                  for q in sorted(rep.quick, key=lambda q: -q.signal)[:25]],
        "categories": _categories(o, now) if o else [],
        "verdict": o.verdict if o else "", "explanation": o.explanation if o else "",
        "best_available": candidate(o.best_available, now) if o and o.best_available else None,
        "matches": matches, "notes": list(dict.fromkeys(rep.notes)),
    }


def analysis(an: MatchAnalysis, now: datetime | None = None) -> dict:
    now = now or an.created
    fx, ms, cov = an.fixture, an.models, an.coverage
    models = []
    if ms:
        for name, part in (("A – vlastní", ms.a), ("B – tržní", ms.b), ("C – kombinovaný", ms.c)):
            if not part.available:
                models.append({"name": name, "probs": None, "fair": None, "note": part.note})
                continue
            p = [mathcore.evaluate("1X2", s, part.matrix).p_win for s in ("1", "X", "2")]
            models.append({"name": name, "probs": [pct(x, 0) for x in p], "fair": [odds_s(1 / x) for x in p], "note": part.note})
        dp = [ms.d_probability("1X2", s) for s in ("1", "X", "2")]
        if all(x is not None for x in dp):
            models.append({"name": "D – konzervativní", "probs": [pct(x, 0) for x in dp], "fair": [odds_s(1 / x) for x in dp],
                           "note": f"spodní odhad (nejistota ±{100 / ms.k ** 0.5:.0f} % gólů); součet < 100 %"})
    mc = None
    if an.mc:
        m = an.mc
        mc = {"n": f"{m.n:,}".replace(",", " "), "one_x_two": [pct(x) for x in m.one_x_two],
              "expected": f"{m.expected[0]:.2f} : {m.expected[1]:.2f}",
              "top_scores": [(s, pct(p)) for s, p in m.top_scores[:6]],
              "markets": [{"key": k, "prob": pct(v.prob), "ci": f"{pct(v.ci_low)}–{pct(v.ci_high)}", "fair": odds_s(v.fair_odds),
                           "ev": pct(v.ev) if v.ev is not None else ""} for k, v in list(m.markets.items())[:10]]}
    grouped: dict[str, tuple[list[str], Candidate]] = {}
    for cat, c in an.selection.categories.items():
        if c:
            grouped.setdefault(c.key, ([], c))[0].append(cat)
    tips = [{"title": " + ".join(cats), "tip": candidate(c, now)} for cats, c in grouped.values()]
    tips += [{"title": "SCHVÁLENO", "tip": candidate(c, now)} for c in an.selection.approved[:3] if c.key not in grouped]
    top = sorted(an.candidates, key=lambda c: (c.decision != APPROVED, -c.ev_c))[:1]
    return {
        "kind": "analysis", "when": local(now), "fixture_id": fx.id, "name": fx.name, "league": f"{fx.league} {fx.season}/{fx.season + 1} {fx.round}".strip(),
        "kickoff": local(fx.kickoff), "status": STATUS_CZ.get(fx.status, fx.status), "quality": pct(cov.quality(), 0),
        "coverage": cov.lines(), "missing": cov.missing(),
        "tipsport": any(q.bookmaker == "tipsport" and q.source != "api" for q in an.quotes),
        "context": [f.line() for kind in ("forma", "h2h", "odpocinek", "sestava", "absence", "motivace", "predikce", "zpravy")
                    for f in an.context.by(kind)],
        "narrative": an.context.narrative, "models": models, "mc": mc,
        "stress": candidate(top[0], now) if top else None, "tips": tips, "categories": _categories(an.selection, now),
        "markets": markets(an.candidates, an.quotes, now),
        "verdict": an.selection.verdict, "explanation": an.selection.explanation,
        "best_available": candidate(an.selection.best_available, now) if an.selection.best_available else None,
        "notes": list(dict.fromkeys(an.notes)),
    }
