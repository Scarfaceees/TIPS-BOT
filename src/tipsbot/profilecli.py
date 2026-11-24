"""Prikazy `tipsbot profile …` (Faze 3.2.1). Vsechny jen ctou – historicka data nemeni.

  tipsbot profile audit               prehled profilu, metodik a znamych omezeni z aktualni databaze
  tipsbot profile market-integrity    spolecna kontrola kurzu: stav vyberu podle profilu, neshody mezi profily
  tipsbot profile compare             srovnani strategii (stejne definice metrik, fotbal / NHL zvlast)
  tipsbot profile ranking             diagnostika Opportunity Rankingu (kvalita dat / ranking / EV / vysledek)
  tipsbot profile measure             audit mereni (Faze 3.2.3): Profil 3x, Profil 5, rozsah CLV, pokryti Market
                                      Integrity – stejne tabulky jako panel (Audit mereni) a Excel
  tipsbot profile 5 shadow            Profil 5: stav stinoveho rezimu, nastaveni, posledni skeny
  tipsbot profile 5 report            Profil 5: kandidati, odmitnuti, vysledky, Line Shopping zvlast
  tipsbot profile 5 validate          Profil 5: nezavisla kontrola ulozenych kandidatu (LOBO, p_ref, EV)
"""

from __future__ import annotations

from collections import Counter


def add_parser(sub) -> None:
    p = sub.add_parser("profile", help="audit a srovnani profilu, spolecna kontrola kurzu, Profil 5 (jen cteni)")
    p.add_argument("what", choices=["audit", "market-integrity", "compare", "ranking", "measure", "5"])
    p.add_argument("action", nargs="?", choices=["shadow", "report", "validate"], default="report")


def _pct(x) -> str:
    return "–" if x is None else f"{x * 100:+.1f} %"


def measure_lines(a: dict) -> list[str]:
    """Tabulky auditu mereni jako text – hodnoty formatuje export.fcell (stejne jako panel)."""
    from . import export

    out = []
    for key, (head, rows) in export.audit_tables(a).items():
        fmt = export.AUDIT_FMT[key]
        out += ["", export.AUDIT_TITLES[key], " | ".join(head)]
        out += [" | ".join(export.fcell(v, fmt.get(j)) for j, v in enumerate(r, 1)) for r in rows] or ["Zatím žádná data."]
    return out + ["", export.clv_reconcile(a)]


def run(args) -> int:
    from . import export, kombi, marketcheck, paper, profil5, strategy
    from .profil4.config import P4
    from .storage import Ledger, make_session_factory

    with make_session_factory(args.db)() as session:
        led = Ledger(session)
        picks = paper.load(led)
        if args.what == "audit":
            print("PROFILY (Faze 3.2.1)")
            for pr in paper.PROFILES:
                n = len(paper.of_profile(picks, pr.key))
                print(f"- {pr.name} [{pr.key}, metoda {pr.method}]: {n} výběrů v databázi")
            no_int = sum(1 for p in picks if not p.legs and not p.integrity)
            print(f"\nSpolečná kontrola kurzů: {len(picks) - no_int - sum(1 for p in picks if p.legs)} výběrů zkontrolováno, "
                  f"{no_int} starších bez kontroly (NEOVĚŘENO)")
            v = kombi.verification(picks)
            print(f"Kombi: {v['tickets']} započítaných tiketů, plně ověřených {v['fully_verified']}, s cílovou/neověřenou "
                  f"nohou {v['with_unverified_leg']}; ROI hypotetické {_pct(v['hypothetical']['roi'])}, "
                  f"ověřené {_pct(v['verified']['roi'])}")
            for key in ("rohy", "karty"):
                c = Counter(paper.props_method(p) for p in paper.of_profile(picks, key))
                print(f"{key.capitalize()}: " + ", ".join(f"{m} {n}" for m, n in sorted(c.items())) if c else f"{key}: 0")
            p3 = [p for p in paper.of_profile(picks, "profil3") if p.meta.get("p3_lobo")]
            ok = [p.meta["p3_lobo"] for p in p3 if p.meta["p3_lobo"].get("status") == "OK"]
            if ok:
                d = sum(x["ev_own"] - x["ev_lobo"] for x in ok) / len(ok)
                print(f"Profil 3 LOBO: {len(ok)} měření, průměrně EV vlastní − EV bez hodnoceného trhu = {d * 100:+.1f} p. b.")
            else:
                print("Profil 3 LOBO: zatím žádné měření (vzniká u nových výběrů)")
            from .storage import IntegrityRejection

            rej = Counter(r.profile for r in session.query(IntegrityRejection))
            print("Tvrdá bezpečnostní odmítnutí (PODEZŘELÉ): " + (", ".join(f"{k} {n}" for k, n in sorted(rej.items())) or "0"))
            pr = strategy.paired(picks, "profil3", "profil3x")
            print(f"Profil 3 × stín 3x: společných zápasů {pr['common_matches']}, P3 yield {_pct(pr['a']['roi'])}, "
                  f"stín yield {_pct(pr['b']['roi'])}")
            print("\nPodrobný audit: docs/profil-faze3-2-1-strategy-audit-market-value.md, "
                  "docs/profil-faze3-2-2-safety-guards-settlement-data-quality.md")
        elif args.what == "measure":
            from . import freshness
            from .storage import utcnow

            try:
                p5rep = profil5.report(session)
            except Exception:  # noqa: BLE001 - starsi databaze bez tabulky Profilu 5
                p5rep = None
            a = export.measure_audit(picks, p5rep, P4.db_path, freshness.report(P4.db_path, utcnow()))
            print("AUDIT MĚŘENÍ (Fáze 3.2.3) – jen čtení")
            print("\n".join(measure_lines(a)))
        elif args.what == "market-integrity":
            c = Counter((export.strategy_name(p), (p.integrity or {}).get("status", "bez kontroly")) for p in picks if not p.legs)
            for (prof, st), n in sorted(c.items()):
                print(f"{prof:<36} {st:<20} {n}")
            bad = strategy.integrity_consistency(picks)
            m = strategy.integrity_coverage(picks)
            print(f"\nZáznam kontroly má {m['checked']} z {m['selections']} výběrů (OVĚŘENO {m['ok']}, NEOVĚŘENO "
                  f"{m['unverified']}, PODEZŘELÉ {m['suspect']}); bez záznamu {m['unchecked']}")
            print(f"Neshody stejné nabídky mezi profily: {len(bad)} z {m['comparable']} porovnatelných nabídek")
            for b in bad[:20]:
                print(" ", b)
            print(f"Prahy (= Profil 4): {marketcheck.DEFAULT}")
        elif args.what == "ranking":
            from . import rankdiag

            rd = rankdiag.report(P4.db_path)
            for name, dg in [("celkem", rd["all"])] + list(rd["sports"].items()):
                print(f"[{name}] {dg['selections']} výběrů, {dg['matches']} zápasů – {dg['verdict']}")
                print("  " + ", ".join(f"{k} {v:+.2f}" for k, v in dg["corr"].items() if v is not None))
                for key in ("by_quality", "by_rank", "by_ev", "residual"):
                    for b in dg[key]:
                        print(f"  {b['band']:<44} záp. {b['matches']:>3} sázek {b['staked']:>5} yield {_pct(b['yield'])}")
        elif args.what == "compare":
            d = export.collect(session, P4.db_path, light=True)
            for r in strategy.comparison(d):
                ci = f" [{r['yield_ci'][0] * 100:+.0f}; {r['yield_ci'][1] * 100:+.0f} %]" if r["yield_ci"] else ""
                print(f"{r['group'][:38]:<38} {r['sport']:<7} výběrů {r['picks']:>4} zápasů {r['matches']:>4} "
                      f"vyhodn. {r['settled']:>4} yield {_pct(r['yield']):>9}{ci:<16} CLV {_pct(r['clv'])} ({r['clv_n']}) "
                      f"| {r['reliability']}")
        else:
            if args.action == "validate":
                bad = profil5.validate(session)
                print(f"Profil 5 – kontrola uložených kandidátů: {'v pořádku' if not bad else f'{len(bad)} problémů'}")
                for b in bad:
                    print(" ", b)
                return 1 if bad else 0
            rep = profil5.report(session)
            print(f"Profil 5 – Market Value · {rep['mode']} · {rep['algo']}")
            print(f"Nastavení: {profil5.DEFAULT}")
            if args.action == "shadow":
                for s in rep["last_scans"]:
                    print(" ", s)
                print(f"Otevřených kandidátů: {sum(1 for c in profil5.rows(session) if c.result is None)}")
                return 0
            c = rep["counts"]
            print(f"Unikátních kandidátů {c['candidates']}, unikátních odmítnutých nabídek {c['rejected']} "
                  f"(po skupinách kanceláří {c['rejected_groups']}), jednotlivých důvodů {c['reason_instances']}, "
                  f"nabídek s více důvody {c['multi_reason']}")
            for k, v in c["reasons"].items():
                print(f"  odmítnuto – {k}: nabídek {v['offers']}, důvodů {v['instances']}, "
                      f"z toho i s jiným důvodem {v['overlap']}")
            for sp, pf in [("celkem", rep["all"])] + list(rep["by_sport"].items()):
                print(f"{sp:<7} kandidátů {pf['n']} zápasů {pf['matches']} vyhodn. {pf['staked']} yield {_pct(pf['yield'])} "
                      f"CLV ověřené {_pct(pf['clv'])} ({pf['clv_n']}), poslední pozorovaný {_pct(pf['clv_last'])} "
                      f"({pf['clv_last_n']}) | {pf['status']}")
            ls = rep["line_shopping"]
            print(f"Line Shopping (zvlášť): kancelář {_pct(ls['at_book']['yield'])}, medián {_pct(ls['at_median']['yield'])}, "
                  f"nejlepší kurz {_pct(ls['at_best']['yield'])}, Ø nejlepší/medián {_pct(ls['avg_gain'])}")
    return 0
