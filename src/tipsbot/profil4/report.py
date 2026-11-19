"""Cesky report Profilu 4 (prosty text – prikazova radka, panel, kopirovani)."""

from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from . import mathcore
from .analysis import MatchAnalysis
from .decision import APPROVED, CAT_A, CAT_B, CAT_C, Candidate
from .hunter import ScanReport

TZ = ZoneInfo("Europe/Prague")
STATUS_CZ = {"NS": "nezačal", "TBD": "termín neurčen", "1H": "1. poločas", "HT": "poločas", "2H": "2. poločas",
             "FT": "skončil", "AET": "po prodloužení", "PEN": "po penaltách", "PST": "odložen", "CANC": "zrušen"}


def pct(x: float | None, digits: int = 1) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.{digits}f} %"


def odds_s(x: float | None) -> str:
    return "–" if x is None or math.isinf(x) else f"{x:.2f}"


def local(dt: datetime | None) -> str:
    return "–" if dt is None else dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def age(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "neznámé"
    m = (now - dt).total_seconds() / 60
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"


def tip_block(c: Candidate, now: datetime, title: str = "") -> list[str]:
    q = c.quote
    head = f"  {title + ': ' if title else ''}{c.label} ({c.market}:{c.selection})"
    lines = [head,
             f"    kancelář {q.bookmaker} · kurz {c.odds:.2f} · {q.source_label()} · {q.status(now, _cfg().odds)} "
             f"(získáno {local(q.stamp)}, stáří {age(q.stamp, now)})" + (f" · {q.note}" if q.note else ""),
             f"    pravděpodobnost výhry (C) {pct(c.p_win_c)}" + (f", vrácení {pct(c.p_push_c)}" if c.p_push_c > 0.005 else "")
             + f" · férový kurz {odds_s(c.fair_odds_c)} (konzervativně {odds_s(c.fair_odds_d)})",
             f"    EV {pct(c.ev_c)} (konzervativní {pct(c.ev_d)}) · min. přijatelný kurz {odds_s(c.min_odds)} · "
             f"robustnost {c.robustness:.0f}/100",
             f"    modely: A {pct(c.p['A'])} · B {pct(c.p['B'])} · C {pct(c.p['C'])} · D {pct(c.p['D'])}",
             f"    rozhodnutí: {c.decision}" + (f" – {'; '.join(c.reasons)}" if c.reasons else "")]
    if c.risks:
        lines.append("    rizika: " + "; ".join(dict.fromkeys(c.risks)))
    return lines


def _cfg():
    from .config import P4

    return P4


def format_analysis(an: MatchAnalysis, now: datetime | None = None) -> str:
    now = now or an.created
    fx, ms, cov = an.fixture, an.models, an.coverage
    L: list[str] = []
    L += [f"PROFIL 4 – HYBRIDNÍ EXPERT · {fx.name}", "=" * 60, "",
          "ZÁKLADNÍ INFORMACE",
          f"  Zápas: {fx.name} (id {fx.id})", f"  Soutěž: {fx.league} {fx.season}/{fx.season + 1} {fx.round}",
          f"  Datum a čas: {local(fx.kickoff)}", f"  Stav: {STATUS_CZ.get(fx.status, fx.status)}", ""]
    L += ["KVALITA DAT", f"  Kvalita dat: {pct(cov.quality(), 0)}"] + ["  " + x for x in cov.lines()]
    api = [q for q in an.quotes if q.source == "api"]
    man = [q for q in an.quotes if q.source != "api"]
    if api:
        L.append(f"  Kurzy z API: {len({q.bookmaker for q in api})} kanceláří, nejnovější {local(max(q.stamp for q in api))}")
    if man:
        L.append(f"  Ručně zadané/importované kurzy: {len(man)}, kanceláře {', '.join(sorted({q.bookmaker for q in man}))}")
    if not any(q.bookmaker == "tipsport" and q.source != "api" for q in an.quotes):
        L.append("  Tipsport: kurzy nejsou zadané (zadej ručně: tipsbot profil4 odds --fixture ID).")
    L += ["  Chybí: " + (", ".join(cov.missing()) or "nic"), ""]

    L.append("ANALÝZA TÝMŮ")
    for f in an.context.by("forma") + an.context.by("h2h") + an.context.by("odpocinek"):
        L.append("  " + f.line())
    if ms and ms.a.available:
        L.append(f"  Model A – očekávané góly {ms.a.lam_home:.2f} : {ms.a.lam_away:.2f} (z {ms.a.n_matches} zápasů, "
                 "útok/obrana a domácí výhoda z výsledků, starší zápasy s menší vahou)")
    L += ["", "SESTAVY A ABSENCE"] + ["  " + f.line() for f in an.context.by("sestava") + an.context.by("absence")]
    L += ["", "KONTEXT"] + ["  " + f.line() for f in an.context.by("motivace") + an.context.by("predikce")
                             + an.context.by("zpravy")]
    if an.context.narrative:
        L.append("  Shrnutí analytika: " + an.context.narrative)
    L.append("  (Kontext je jen informace – matematické výsledky nemění.)")

    L += ["", "MATEMATICKÉ MODELY (1X2: domácí / remíza / hosté)",
          f"  {'Model':<22}{'Pravděpodobnost':<30}{'Férové kurzy':<24}Poznámka"]
    if ms:
        rows = [("A – vlastní", ms.a.matrix if ms.a.available else None, ms.a.note),
                ("B – tržní", ms.b.matrix if ms.b.available else None, ms.b.note),
                ("C – kombinovaný", ms.c.matrix if ms.c.available else None, ms.c.note)]
        for name, mat, note in rows:
            if mat is None:
                L.append(f"  {name:<22}{'nedostupný':<30}{'':<24}{note}")
                continue
            p = [mathcore.evaluate("1X2", s, mat).p_win for s in ("1", "X", "2")]
            L.append(f"  {name:<22}{' / '.join(pct(x, 0) for x in p):<30}{' / '.join(odds_s(1 / x) for x in p):<24}{note}")
        dp = [ms.d_probability("1X2", s) for s in ("1", "X", "2")]
        if all(x is not None for x in dp):
            L.append(f"  {'D – konzervativní':<22}{' / '.join(pct(x, 0) for x in dp):<30}"
                     f"{' / '.join(odds_s(1 / x) for x in dp):<24}spodní odhad (nejistota ±{100 / ms.k ** 0.5:.0f} % gólů); součet < 100 %")
        tb = ms.b.extra.get("table", {}).get("books", {}).get("1X2", {}) if ms.b.available else {}
        if tb:
            L.append("  Marže 1X2 podle kanceláří (původní kurzy → bez marže):")
            for bk, d in list(tb.items())[:6]:
                L.append(f"    {bk}: {' / '.join(odds_s(o) for o in d['odds'])} (marže {pct(d['overround'])}) → "
                         f"{' / '.join(pct(x, 1) for x in d['fair'])}")
    else:
        L.append("  Modely nejsou k dispozici.")

    L += ["", "MONTE CARLO"]
    if an.mc:
        mc = an.mc
        L.append(f"  Simulací: {mc.n:,} (seed {mc.seed}); každá simulace zahrnuje i nejistotu očekávaných gólů.".replace(",", " "))
        L.append(f"  1X2: {pct(mc.one_x_two[0])} / {pct(mc.one_x_two[1])} / {pct(mc.one_x_two[2])} · "
                 f"očekávané skóre {mc.expected[0]:.2f} : {mc.expected[1]:.2f}")
        L.append("  Nejčastější výsledky: " + ", ".join(f"{s} ({pct(p)})" for s, p in mc.top_scores[:6]))
        L.append("  Počet gólů: " + ", ".join(f"{k}: {pct(v, 0)}" for k, v in mc.total_goals.items()))
        for k, m in list(mc.markets.items())[:10]:
            ev = f", EV {pct(m.ev)} ± {pct(1.96 * m.ev_se)}" if m.ev is not None else ""
            L.append(f"  {k}: {pct(m.prob)} (95% interval {pct(m.ci_low)}–{pct(m.ci_high)}, sim. chyba {pct(m.se, 2)}), "
                     f"férový kurz {odds_s(m.fair_odds)}{ev}")
        L.append("  Pozn.: víc simulací zmenší jen simulační chybu, ne chybu vstupního modelu.")
    else:
        L.append("  Neproběhla (chybí model).")

    top = sorted([c for c in an.candidates], key=lambda c: (c.decision != APPROVED, -c.ev_c))[:1]
    L += ["", "STRESS TEST" + (f" ({top[0].label} @ {top[0].odds:.2f})" if top else "")]
    if top:
        L += [f"  {n}: EV {pct(ev)}" for n, ev in top[0].stress]
        from .stress import Robustness

        L.append(f"  Robustnost {top[0].robustness:.0f}/100 = " + ", ".join(Robustness(0, top[0].robustness_parts).lines()))

    L += ["", "TOP TIPY"]
    shown = set()
    grouped: dict[str, tuple[list[str], Candidate]] = {}
    for cat, c in an.selection.categories.items():
        if c:
            grouped.setdefault(c.key, ([], c))[0].append(cat)
        else:
            L.append(f"  {cat}: nic nesplňuje podmínky kategorie")
    for cats, c in grouped.values():
        L += tip_block(c, now, " + ".join(cats))
        shown.add(c.key)
    for c in an.selection.approved[:3]:
        if c.key not in shown:
            L += tip_block(c, now, "SCHVÁLENO")
            shown.add(c.key)
    if an.correlations:
        L.append("  Korelace vybraných tipů (společná matice, ne prostý součin):")
        for p in an.correlations:
            warn = " – NEDÁVAT NA JEDEN TIKET" if p.phi >= 0.3 else ""
            L.append(f"    {p.a} + {p.b}: obě vyjdou {pct(p.p_both)} (při nezávislosti {pct(p.p_independent)}), "
                     f"korelace {p.phi:+.2f}{warn}")
    if an.surebets:
        L.append("  Surebety (oddělené od value):")
        for s in an.surebets[:3]:
            L.append(f"    {s.status}: {s.market} " + " | ".join(f"{q.bookmaker} {q.selection} {q.odds:.2f}" for q in s.legs)
                     + f" → výnos {pct(s.margin)}; " + ("; ".join(s.problems) or "ověřeno"))

    L += ["", "ZÁVĚREČNÉ DOPORUČENÍ", f"  {an.selection.verdict}: {an.selection.explanation}"]
    if an.selection.best_available:
        L += tip_block(an.selection.best_available, now, "BEST AVAILABLE (není schválená sázka)")
    if an.notes:
        L += ["", "POZNÁMKY"] + [f"  - {n}" for n in dict.fromkeys(an.notes)]
    L += ["", "Kategorie ani robustnost nejsou zárukou výhry. Robustnost není pravděpodobnost."]
    return "\n".join(L)


def format_scan(rep: ScanReport) -> str:
    now = rep.started
    L = [f"PROFIL 4 – {'DENNÍ' if rep.mode == 'daily' else 'NADCHÁZEJÍCÍ'} SKEN · {local(now)}", "=" * 60,
         f"Zápasů ve sledovaných soutěžích: {rep.fixtures} · hloubková analýza: {len(rep.analyses)}",
         f"API: dnes použito {rep.budget['used']}/{rep.budget['limit']}, zbývá {rep.budget['remaining']}; "
         f"tento sken {rep.budget['run_used']}/{rep.budget['run_limit']} požadavků", ""]
    if rep.quick:
        L.append("QUICK SCAN")
        for q in sorted(rep.quick, key=lambda q: -q.signal)[:25]:
            mark = "→ hloubková" if q.candidate else "  vyřazen  "
            L.append(f"  {mark} {local(q.fixture.kickoff)} {q.fixture.name} ({q.fixture.league}): {q.reason}")
        L.append("")
    if rep.overall:
        o = rep.overall
        L.append("NEJLEPŠÍ PŘÍLEŽITOSTI SKENU")
        for cat in (CAT_A, CAT_B, CAT_C):
            c = o.categories.get(cat)
            if c:
                L += tip_block(c, now, f"{cat} · {c.match}") if cat == CAT_A or c is not o.categories.get(CAT_A) else \
                    [f"  {cat}: stejný tip jako A ({c.label} · {c.match})"]
            else:
                L.append(f"  {cat}: nic nesplňuje podmínky")
        L += ["", f"VERDIKT: {o.verdict}", f"  {o.explanation}"]
        if o.best_available:
            L += tip_block(o.best_available, now, f"BEST AVAILABLE · {o.best_available.match}")
        L.append("")
        L.append("ZÁPASY")
        for an in rep.analyses:
            s = an.selection
            best = s.approved[0] if s.approved else s.best_available
            L.append(f"  {local(an.fixture.kickoff)} {an.fixture.name}: {s.verdict}" +
                     (f" – {best.label} @ {best.odds:.2f} (EV {pct(best.ev_c)}, robustnost {best.robustness:.0f})" if best else "")
                     + f" · data {pct(an.data_quality, 0)} · detail: tipsbot profil4 analyze {an.fixture.id}")
    if rep.notes:
        L += ["", "POZNÁMKY"] + [f"  - {n}" for n in dict.fromkeys(rep.notes)]
    return "\n".join(L)


def format_evaluation(ev: dict) -> str:
    L = ["PROFIL 4 – VYHODNOCENÍ", "=" * 60, "Modely (1X2):"]
    if not ev["models"]:
        L.append("  zatím žádné vyhodnocené předpovědi")
    for name, m in ev["models"].items():
        L.append(f"  {name}: n={m['n']}, Brier {m['brier']:.4f}, log loss {m['log_loss']:.4f}")
    a = ev["all"]
    L += ["", f"Tipy celkem: n={a['n']}, zisk {a['profit']:+.2f} j., yield {pct(a['yield'])}, ROI {pct(a['roi'])} "
          f"(na 100 j.), úspěšnost {pct(a['win_rate'])}, průměrné CLV {pct(a['avg_clv'])} (z {a['clv_n']})"]
    if a.get("yield_ci95"):
        L.append(f"  95% interval yieldu: {pct(a['yield_ci95'][0])} až {pct(a['yield_ci95'][1])}")
    if not a["significant"]:
        L.append("  POZOR: méně než 100 vyhodnocených tipů – výsledky nejsou důkazem dlouhodobé profitability.")
    for g, rows in ev["groups"].items():
        L += ["", f"Podle: {g}"]
        for k, s in rows.items():
            L.append(f"  {k}: n={s['n']}, zisk {s['profit']:+.2f}, yield {pct(s['yield'])}, úspěšnost {pct(s['win_rate'])}, "
                     f"CLV {pct(s['avg_clv'])}")
    return "\n".join(L)
