"""Exportni centrum vysledku: XLSX (7 listu + grafy), CSV vsech tipu a PDF souhrn – vzdy z aktualni databaze.

Export jen cte: hlavni databazi pres predanou session (nic se necommituje), databazi Profilu 4 v rezimu
jen pro cteni. Zadny vypocet modelu se tu nespousti – pocitaji se jen statistiky z ulozenych vysledku.

Metodika (podrobne na listu Metodika):
- vyhra = won + half_won, prohra = lost + half_lost, vraceno = vraceni vkladu (napr. DNB remiza, AH push),
  neplatne = zrusena / neplatna sazka (napr. chybne sparovany kurz). Cekajici, vracene a neplatne se do
  vyher/proher ani do ROI nepocitaji.
- ROI na 1 j. = zisk v jednotkach / pocet vyhodnocenych sazek s vkladem (vyhry + prohry), kazdy tip 1 j.
- Skutecne tikety navic: skutecne ROI = zisk v Kc / souctem vkladu v Kc a prumerne ROI na tiket (prumer zisk/vklad).
- Jeden vyber jednoho zapasu se v ramci profilu pocita jen jednou (opakovane ulozeny tip = duplicita).
- CLV jen ze skutecne overenych zaviracich kurzu (Profil 4 „závěrečný kurz…“, tikety se zapsanym zaviracim kurzem).
"""

from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import paper
from .storage import BankrollEntry, Ledger, Ticket, utcnow

WIN, LOSS, PUSH, INVALID, PENDING = "výhra", "prohra", "vráceno", "neplatné", "čeká"
OUTCOME = {"won": WIN, "half_won": WIN, "lost": LOSS, "half_lost": LOSS, "push": PUSH}
RESULT_CZ = {"won": "výhra", "half_won": "půl výhra", "lost": "prohra", "half_lost": "půl prohra",
             "void": "vráceno", "push": "vráceno", None: "čeká"}
OFFICIAL, WATCH, NOBET, PLACED, TRACKED, KOMBI = ("oficiální doporučení", "WATCH", "NO BET", "vsazený tiket",
                                                  "sledovaný výběr (bez vkladu)", "kombi návrh")
BANDS = ((1.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 3.5), (3.5, 5.5), (5.5, math.inf))
P4_GROUPS = {OFFICIAL: "Profil 4 – oficiální (APPROVED)", WATCH: "Profil 4 – WATCH", NOBET: "Profil 4 – NO BET"}
TICKETS = "Moje tikety (vsazeno)"
COLUMNS = ["Profil", "Typ", "Sport", "Soutěž", "Zápas", "Výkop (UTC)", "Trh", "Volba", "Kurz", "Kancelář",
           "Šance modelu", "Výsledek", "Třída výsledku", "Skóre", "Vklad", "Zisk (j.)", "Zisk (Kč)", "CLV (ověřené)",
           "Verze modelu", "ID"]


def band(odds: float | None) -> str:
    for lo, hi in BANDS:
        if odds is not None and lo <= odds < hi:
            return f"{lo:.2f}–{hi:.2f}" if hi != math.inf else f"{lo:.2f} a více"
    return "bez kurzu"


@dataclass
class Rec:
    group: str
    kind: str
    sport: str
    league: str
    match: str
    kickoff: str
    market: str
    selection: str
    odds: float | None
    book: str
    p: float | None
    result: str | None  # surovy vysledek (won/lost/half_won/half_lost/void/push/neplatne)
    score: str = ""
    stake: float = 1.0  # jednotky; u skutecnych tiketu Kc
    profit_units: float | None = None
    profit_money: float | None = None
    clv: float | None = None
    model: str = ""
    id: str = ""
    code: str = ""  # interni kod vyberu (home/away/over/1/X/2…) – pro kontrolu protichudnych vyberu

    @property
    def outcome(self) -> str:
        if self.result is None:
            return PENDING
        return OUTCOME.get(self.result, INVALID if self.result == "invalid" else PUSH)

    @property
    def day(self) -> str:
        return (self.kickoff or "")[:10]

    def row(self) -> list:
        return [self.group, self.kind, self.sport, self.league, self.match, self.kickoff[:16].replace("T", " "),
                self.market, self.selection, self.odds, self.book, self.p, RESULT_CZ.get(self.result, self.result),
                self.outcome, self.score, self.stake, self.profit_units, self.profit_money, self.clv, self.model, self.id]


@dataclass
class Data:
    created: datetime
    records: list[Rec]
    legs: list[dict]
    bankroll: list[dict]
    versions: list[str]
    duplicates: int = 0
    p4_eval: dict | None = None
    intel: dict | None = None
    notes: list[str] = field(default_factory=list)
    perf: dict | None = None  # Faze 3.2: Predictive Performance Profilu 4 (jen cteni)
    ticket_kinds: dict = field(default_factory=dict)  # id zaznamu tiketu -> AKU / z tipu bota / rucne zadany


def _sport(league: str) -> str:
    return "hokej" if "NHL" in (league or "").upper() else "fotbal"


def _p4_rows(path: Path | None) -> tuple[list[dict], dict | None]:
    if path is None or not Path(path).exists():
        return [], None
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)  # jen cteni
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM tips ORDER BY id")]
        ev = con.execute("SELECT value FROM kv WHERE key='web_p4_eval'").fetchone()
        return rows, (json.loads(ev[0]) if ev and ev[0] else None)
    except sqlite3.DatabaseError:
        return [], None
    finally:
        con.close()


def _p4_intel(path: Path | None, now: datetime) -> dict | None:
    """Faze 3: kalibrace, EV, kvalita dat, chyby a ranking z databaze Profilu 4 (jen cteni, bez API)."""
    if path is None or not Path(path).exists():
        return None
    from .profil4 import intelligence as IG
    from .profil4.config import P4

    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        preds, tips = IG.load_rows(lambda q: con.execute(q).fetchall())
        ranks = IG.load_rankings(lambda q: con.execute(q).fetchall())
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()
    clean, _ = IG.clean_predictions(preds)
    common = {sp: IG.common_sample([r for r in clean if IG.sport_of(r.get("league")) == sp]) for sp in (IG.FOOTBALL, IG.HOCKEY)}
    uniq = IG.tip_rows_unique(tips)
    return {"summary": IG.build(preds, tips, now, P4, ranks), "common": common, "tips": uniq,
            "tips_all": tips, "unique_ids": {t["id"] for t in uniq}, "rankings": ranks,
            "integrity_all": IG.integrity_summary(ranks, now, limit=None)}


def xdt(v):
    """ISO cas (UTC) -> datetime bez pasma pro Excel (bunka typu datum, ne text). None/neplatne -> None."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        from datetime import timezone

        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


DT = "yyyy-mm-dd hh:mm"
INTEL_SHEETS = ("Market Integrity", "Ranking Validation", "Portfolio Risk")
PERF_SHEETS = ("Predictive Performance", "Ranking Performance", "Model Comparison", "CLV Performance",
               "Settlement Integrity")
NA = "není k dispozici"  # chybejici hodnota se nikdy nezobrazuje jako 0


def _intel_sheets(d, sheet, table, chart, F, FB) -> None:
    from openpyxl.chart import LineChart, Reference

    ig = d.intel
    note = "Data Profilu 4 nejsou k dispozici." if ig is None else None
    # Kalibrace modelu
    ws = sheet("Kalibrace modelů")
    if note:
        ws.cell(6, 1, note).font = F
    else:
        rows = []
        for sp, sd in ig["summary"]["sports"].items():
            for k, m in sd["models"].items():
                vm = m.get("vs_market") or {}
                rows.append([sp, "trh (bez marže)" if k == "trh" else f"model {k}", m["matches"], m["n"], m["brier"],
                             *(m["brier_ci95"] or (None, None)), m["log_loss"], *(m["log_loss_ci95"] or (None, None)),
                             m["mean_p"], m["rate"], m["calibration"]["status"], vm.get("diff"),
                             *((vm.get("ci95") or (None, None)) if vm else (None, None)), vm.get("status", "–")])
        end = table(ws, 6, ["Sport", "Model", "Zápasů", "Předpovědí", "Brier", "Brier 95% od", "Brier 95% do", "Log Loss",
                            "LL 95% od", "LL 95% do", "Prům. odhad", "Skutečnost", "Kalibrace", "Rozdíl LL proti trhu",
                            "Rozdíl 95% od", "Rozdíl 95% do", "Proti trhu"], rows,
                    {5: DEC4, 6: DEC4, 7: DEC4, 8: DEC4, 9: DEC4, 10: DEC4, 11: PCT, 12: PCT, 14: "+0.0000;-0.0000",
                     15: "+0.0000;-0.0000", 16: "+0.0000;-0.0000"}, [10, 16, 8, 10, 8, 9, 9, 8, 9, 9, 9, 9, 34, 10, 9, 9, 34], False)
        top = end + 2
        ws.cell(top, 1, "Všechny modely i trh na STEJNÝCH řádcích (společný vzorek). Intervaly 95 % z bootstrapu po zápasech; "
                        "o průkaznosti se rozhoduje od 100 zápasů.").font = FB
        cal = []
        for sp, rs in ig["common"].items():
            for r in sorted(rs, key=lambda r: (r["kickoff"] or "", r["match"] or "")):
                cal.append([sp, r["league"], r["match"], (r["kickoff"] or "")[:16].replace("T", " "), r["market_type"],
                            r["market"], r["selection"], r["odds"], r["p"]["A"], r["p"]["B"], r["p"]["C"], r["p"]["D"],
                            r["p_market"], r["outcome"]])
        table(ws, top + 2, ["Sport", "Soutěž", "Zápas", "Výkop (UTC)", "Typ trhu", "Trh", "Výběr", "Kurz", "p(A)", "p(B)",
                            "p(C)", "p(D)", "Trh bez marže", "Výsledek (1 = výhra)"], cal,
              {8: ODDS, 9: PCT, 10: PCT, 11: PCT, 12: PCT, 13: PCT}, filt=False)
        series = ig["summary"]["sports"]["fotbal"]["series"]
        if len(series) > 1:
            st = top + 3 + len(cal) + 2
            srows = [[s["month"]] + [s[k]["log_loss"] for k in ("A", "B", "C", "D", "trh")] for s in series]
            se = table(ws, st, ["Měsíc (fotbal)", "A", "B", "C", "D", "trh"], srows, {j: DEC4 for j in range(2, 7)}, filt=False)
            ch = chart(LineChart(), "Log Loss v čase (fotbal)", "log loss")
            ch.add_data(Reference(ws, min_col=2, max_col=6, min_row=st, max_row=se), titles_from_data=True)
            ch.set_categories(Reference(ws, min_col=1, min_row=st + 1, max_row=se))
            ws.add_chart(ch, f"S{st}")

    tips = [] if note else ig["tips"]
    # EV analyza
    ws = sheet("EV analýza")
    ev_rows = []
    for t in tips:
        probs = json.loads(t["probs"]) if t.get("probs") else {}
        intel = json.loads(t["intel"]) if t.get("intel") else {}
        ev = intel.get("ev") or {}
        ev_rows.append([t["match"], t.get("league"), (t.get("kickoff") or "")[:16].replace("T", " "), t["market"],
                        safe_text(t.get("label") or t["selection"]), t["odds"], t.get("bookmaker"), probs.get("A"), probs.get("B"),
                        probs.get("C"), probs.get("D"), t.get("fair_odds"), t.get("p_market"), t.get("ev"),
                        t.get("ev_conservative"), ev.get("market"), ev.get("min_odds"), t.get("decision")])
    table(ws, 6, ["Zápas", "Soutěž", "Výkop (UTC)", "Trh", "Výběr", "Kurz", "Kancelář", "p(A)", "p(B)", "p(C)", "p(D)",
                  "Férový kurz (C)", "Trh bez marže", "EV (C)", "Konzervativní EV (D)", "EV proti trhu", "Minimální kurz",
                  "Rozhodnutí"], ev_rows,
          {6: ODDS, 8: PCT, 9: PCT, 10: PCT, 11: PCT, 12: ODDS, 13: PCT, 14: "+0.0%;-0.0%", 15: "+0.0%;-0.0%",
           16: "+0.0%;-0.0%", 17: ODDS}, [34, 14, 16, 10, 24, 7, 12, 8, 8, 8, 8, 9, 9, 9, 10, 9, 9, 24])
    ws.cell(5, 1, "Minimální kurz a EV proti trhu jsou uložené od Fáze 3 (starší tipy je nemají). Jeden výběr zápasu = jeden řádek.").font = F

    # Kvalita dat
    ws = sheet("Kvalita dat")
    q_rows = []
    uniq_ids = set() if note else ig.get("unique_ids", set())
    for t in ([] if note else ig.get("tips_all", tips)):
        intel = json.loads(t["intel"]) if t.get("intel") else {}
        qd = intel.get("quality") or {}
        det, grp = qd.get("details") or {}, qd.get("groups") or {}
        src = json.loads(t["data_sources"]) if t.get("data_sources") else {}
        avail = sorted(k for k, v in src.items() if v == "AVAILABLE")
        missing = sorted(k for k, v in src.items() if v != "AVAILABLE")
        books = det.get("books", (intel.get("odds") or {}).get("books"))
        cs = det.get("counterside")
        origin = ("uloženo při analýze (Fáze 3.1)" if det else "uloženo při analýze (Fáze 3)" if qd
                  else "tip před Fází 3 – skóre a počet kanceláří se tehdy neukládaly")
        q_rows.append([t["match"], safe_text(t.get("label") or t["selection"]), t.get("odds_age_min"),
                       xdt(t.get("odds_time")), xdt(t.get("odds_fetched_at")), t.get("odds_status"), t.get("odds_source"),
                       t.get("bookmaker"), books, None if cs is None and not det else
                       ("ověřena" if cs else "nelze spárovat" if cs is None else "chybí"),
                       src.get("model_data"), src.get("lineups"), src.get("injuries"), len(avail) if src else None,
                       len(src) if src else None, ", ".join(missing), grp.get("kvalita kurzu"), grp.get("sportovní data"),
                       grp.get("úplnost zdrojů"), qd.get("score"), "; ".join(qd.get("uncertain", [])), origin,
                       t.get("id"), xdt(t.get("predicted_at")),
                       "ano" if t.get("id") in uniq_ids else "ne – opakovaný záznam výběru (do ROI se nepočítá)"])
    table(ws, 6, ["Zápas", "Výběr", "Stáří kurzu (min)", "Poslední aktualizace u zdroje", "Čas stažení", "Stav kurzu",
                  "Zdroj kurzu", "Kancelář", "Kanceláří", "Protistrana", "Statistiky", "Sestavy", "Absence",
                  "Dostupných zdrojů", "Zdrojů celkem", "Chybějící zdroje", "Kvalita kurzu (0–100)",
                  "Sportovní data (0–100)", "Úplnost zdrojů (0–100)", "Kvalita dat (0–100)", "Nejistota", "Původ hodnot",
                  "ID tipu", "Uloženo (UTC)", "Počítá se do výsledků"],
          q_rows, {3: "0", 4: DT, 5: DT, 17: "0", 18: "0", 19: "0", 20: "0", 24: DT},
          [34, 24, 9, 17, 17, 12, 9, 12, 9, 12, 12, 17, 12, 9, 9, 28, 9, 9, 9, 9, 40, 40, 7, 16, 22])
    ws.cell(5, 1, "Prázdná buňka = hodnota nebyla uložena (ne nula). Časy v UTC. Chybějící data = nejistota, "
                  "ne negativní sportovní informace.").font = F

    # Analyza chyb
    ws = sheet("Analýza chyb")
    errs = [] if note else ig["summary"]["errors"]
    erows = []
    for e in errs:
        cl = (e.get("classification") or {}).get("items") or [{}]
        erows.append([e["match"], e["sport"], safe_text(e["label"]), e["p"], e["p_market"], e["odds"], e["result"],
                      e["decision"], (e.get("classification") or {}).get("primary"),
                      "; ".join(x.get("evidence", "") for x in cl), "; ".join(x.get("expected", "") for x in cl),
                      "; ".join(x.get("fixed", "") for x in cl), "; ".join(x.get("test", "") for x in cl if x.get("test")),
                      "; ".join(e["diagnosis"])])
    end = table(ws, 6, ["Zápas", "Sport", "Tip", "Šance (C)", "Trh bez marže", "Kurz", "Výsledek", "Rozhodnutí",
                        "Kategorie", "Doklad (data)", "Očekávaná hodnota", "Opraveno", "Test", "Diagnóza (jen doložené)"],
                erows, {4: PCT, 5: PCT, 6: ODDS}, [34, 8, 24, 9, 9, 7, 10, 22, 24, 50, 24, 20, 40, 80])
    if not note:
        top = end + 3
        ws.cell(top - 1, 1, "Známé a opravené chyby systému (registr)").font = FB
        table(ws, top, ["Datum", "Kategorie", "Co se stalo", "Původní vstup", "Očekávaná hodnota", "Co bylo chybně",
                        "Opraveno", "Test, který chybu zachytí"],
              [[k["date"], k["category"], k["what"], k["input"], k["expected"], k["wrong"], k["fixed"], k["test"]]
               for k in ig["summary"].get("known_issues", [])], filt=False)
    ws.cell(5, 1, "Příčina prohry se uvádí jen tehdy, když je doložena daty. Jednotlivý výsledek nedokazuje chybu modelu.").font = F

    # Opportunity Ranking
    ws = sheet("Opportunity Ranking")
    opp = [] if note else ig["summary"]["opportunities"]
    end = table(ws, 6, ["Pořadí", "Ranking (0–100)", "Zápas", "Výkop (UTC)", "Trh", "Výběr", "Kurz", "EV", "Konzervativní EV",
                        "Robustnost", "Kvalita dat", "Rozhodnutí", "Integrita", "Čas výpočtu (UTC)", "Verze rankingu",
                        "Důvody pořadí"],
                [[i, o["rank"], o["match"], xdt(o["kickoff"]), o["market"], safe_text(o["label"]), o["odds"], o["ev"],
                  o["ev_conservative"], o["robustness"], o["quality"], o["decision"], o.get("integrity"),
                  xdt(o.get("computed_at")), o.get("version"), "; ".join(o["reasons"])] for i, o in enumerate(opp, 1)],
                {4: DT, 7: ODDS, 8: "+0.0%;-0.0%", 9: "+0.0%;-0.0%", 14: DT},
                [7, 9, 34, 16, 10, 24, 7, 8, 9, 9, 9, 22, 26, 16, 18, 100])
    ws.cell(5, 1, "Ranking = pořadí zajímavosti, NE pravděpodobnost výhry. BEST AVAILABLE a WATCH nejsou doporučení k sázce. "
                  "Jen platné předzápasové příležitosti (aktuální kurz, nezávislá kontrola bez chyby, ne NO BET). Váhy experimentální.").font = F
    if not note:
        from .profil4 import intelligence as IG

        hist = sorted(IG._latest_rank(ig.get("rankings") or [], only_valid=False).values(),
                      key=lambda r: (r.get("kickoff") or "", -(r.get("rank") or 0)))
        top = end + 3
        ws.cell(top - 1, 1, f"Poslední předzápasový ranking každého výběru – i odehrané zápasy ({len(hist)} výběrů, "
                            f"uloženo celkem {len(ig.get('rankings') or [])} výpočtů)").font = FB
        table(ws, top, ["Výkop (UTC)", "Zápas", "Trh", "Výběr", "Kurz", "Kancelář", "Ranking", "Pásmo", "Kvalita dat",
                        "Rozhodnutí", "Integrita", "Platný", "Čas výpočtu (UTC)", "Verze"],
              [[xdt(r.get("kickoff")), r["match"], r["market"], safe_text(r.get("label") or r["selection"]), r["odds"],
                r.get("bookmaker"), r["rank"], IG.rank_band(r["rank"]), r.get("quality"), r.get("decision"),
                r.get("integrity"), "ano" if r.get("valid") else "ne", xdt(r.get("computed_at")), r.get("version")]
               for r in hist], {1: DT, 5: ODDS, 13: DT}, filt=False)

    # Market Integrity
    ws = sheet("Market Integrity")
    ints = [] if note else (ig.get("integrity_all") or {}).get("items", [])  # vsechny, ne jen nahled panelu
    table(ws, 6, ["Zápas", "Výkop (UTC)", "Trh", "Výběr", "Kancelář", "Kurz", "Tržní medián", "Férový kurz trhu",
                  "Odchylka od mediánu", "Kanceláří", "Protistrana", "Stáří (min)", "Nezávislý přepočet", "Integrita",
                  "Rozhodnutí", "Důvod"],
          [[i["match"], xdt(i["kickoff"]), i["market"], safe_text(i.get("label") or i["selection"]), i.get("book"), i.get("odds"),
            i.get("median"), i.get("fair_market"), i.get("dev"), i.get("books"),
            None if i.get("counterside") is None else ("ověřena" if i["counterside"] else "chybí"), i.get("age_min"),
            i.get("independent"), i["verdict"], i.get("decision"), "; ".join(i.get("reasons") or [])] for i in ints],
          {2: DT, 6: ODDS, 7: ODDS, 8: ODDS, 9: "+0.0%;-0.0%"},
          [34, 16, 10, 24, 12, 7, 8, 8, 9, 8, 10, 8, 12, 34, 22, 90])
    ws.cell(5, 1, "Nadcházející výběry, poslední výpočet. Nezávislý přepočet = vlastní implementace settlementu (fotbal) "
                  "nebo vzorce binárního trhu (NHL); neshoda = NO BET. Vysoké EV samo o sobě neznamená schválení.").font = F

    # Ranking Validation
    ws = sheet("Ranking Validation")
    rv = {} if note else (ig["summary"].get("ranking_validation") or {})
    end = table(ws, 6, ["Rankingové pásmo", "Nezávislých zápasů", "Výběrů", "Vsazeno (j.)", "Zisk (j.)", "ROI / yield (1 j.)",
                        "ROI 95% od", "ROI 95% do", "Brier", "Log Loss", "CLV (ověřené)", "CLV n", "Prům. konz. EV",
                        "Prům. kvalita dat", "Podíl APPROVED", "Průkaznost"],
                [[b["band"], b["matches"], b["n"], b["staked"], b["profit"], b["roi"], *(b["roi_ci95"] or (None, None)),
                  b["brier"], b["log_loss"], b["clv"], b["clv_n"], b["ev_conservative"], b["quality"], b["approved_share"],
                  b["status"]] for b in rv.get("bands", [])],
                {5: UNITS, 6: "+0.0%;-0.0%", 7: "+0.0%;-0.0%", 8: "+0.0%;-0.0%", 9: DEC4, 10: DEC4, 11: "+0.0%;-0.0%",
                 13: "+0.0%;-0.0%", 14: "0", 15: PCT}, [16, 10, 8, 9, 9, 10, 9, 9, 8, 8, 9, 6, 9, 9, 9, 40], False)
    hl = rv.get("hi_minus_lo") or {}
    lines = [f"Stav: {rv.get('status', 'Data Profilu 4 nejsou k dispozici.')} – {rv.get('matches', 0)} zápasů, "
             f"{rv.get('n', 0)} výběrů, verze {rv.get('version', '')}.",
             "Rozdíl ROI (ranking ≥ 60) − (ranking < 60): " + ("–" if hl.get("diff") is None else
                                                              f"{hl['diff'] * 100:+.1f} %" + (
                 f" (95 % {hl['ci95'][0] * 100:+.1f} až {hl['ci95'][1] * 100:+.1f} %)" if hl.get("ci95") else "")),
             *[f"{x['period']}: {x['matches']} zápasů, rozdíl ROI " + ("–" if x["roi_diff"] is None else f"{x['roi_diff'] * 100:+.1f} %")
               for x in rv.get("periods", [])],
             rv.get("note", "")]
    for k, line in enumerate(lines, end + 2):
        ws.cell(k, 1, line).font = F

    # Portfolio Risk
    ws = sheet("Portfolio Risk")
    kinds = [(OFFICIAL, "1. Oficiální tipy bota (APPROVED, virtuálně 1 j.)"), (WATCH, "2. Sledované příležitosti (WATCH)"),
             (TRACKED, "2b. Sledované výběry ostatních profilů (bez vkladu)"), (PLACED, "3. Reálně vsazené tikety (Kč)"),
             (KOMBI, "4. Kombinované tikety (návrhy)")]
    srow = []
    for kind, name in kinds:
        rs = [r for r in d.records if r.kind == kind]
        st = stats(rs)
        real = kind == PLACED
        srow.append([name, len(rs), sum(1 for r in rs if r.outcome == PENDING), st.get("wins", 0) + st.get("losses", 0),
                     None if real else float(sum(1 for r in rs if r.outcome == PENDING)),
                     sum(r.stake for r in rs if r.outcome == PENDING) if real else None,
                     None if real else st.get("profit"), st.get("profit_money") if real else None])
    end = table(ws, 6, ["Skupina", "Záznamů", "Čeká", "Vyhodnoceno", "Virtuálně ve hře (j.)", "Skutečně ve hře (Kč)",
                        "5. Virtuální výsledek (j.)", "6. Skutečný finanční výsledek (Kč)"], srow,
                {5: "0", 6: '#,##0 "Kč"', 7: UNITS, 8: '+#,##0 "Kč";-#,##0 "Kč";0 "Kč"'}, [46, 9, 8, 11, 12, 13, 13, 16], False)
    ws.cell(end + 1, 1, "Virtuální jednotky a skutečné koruny se nikdy nesčítají. Systém sám nesází a vklady nenavyšuje.").font = F
    pend = [r for r in d.records if r.outcome == PENDING and r.kind != NOBET]
    per_match = {}
    for r in pend:
        per_match.setdefault((r.group, r.match), []).append(r)
    prow = []
    for r in sorted(pend, key=lambda r: (r.kickoff or "", r.match, r.group)):
        same = per_match[(r.group, r.match)]
        from .profil4 import intelligence as IG

        opp = any(x is not r and ((x.market == r.market and (x.code or x.selection) != (r.code or r.selection)) or
                                  (r.sport == "fotbal" and r.code and x.code
                                   and IG.never_both_win(r.market, r.code, x.market, x.code)))
                  for x in same)
        corr = ("PROTICHŮDNÉ výběry stejného zápasu (nemohou vyhrát oba)" if opp else
                f"korelované – {len(same)} výběry stejného zápasu" if len(same) > 1 else "jediný výběr zápasu")
        real = r.kind == PLACED
        prow.append([r.match, xdt(r.kickoff), r.group, r.sport, r.league, r.market, safe_text(r.selection), r.odds, corr,
                     None if real else r.stake, r.stake if real else None, RESULT_CZ.get(r.result, r.result)])
    top = end + 4
    ws.cell(top - 1, 1, f"Otevřené pozice ({len(prow)}) – stejný zápas = korelované, ne nezávislé sázky").font = FB
    end = table(ws, top, ["Zápas", "Výkop (UTC)", "Profil / skupina", "Sport", "Soutěž", "Trh", "Výběr", "Kurz", "Korelace",
                          "Virtuální vklad (j.)", "Skutečný vklad (Kč)", "Výsledek"], prow,
                {2: DT, 8: ODDS, 11: '#,##0 "Kč"'}, filt=False)
    for title, fn in (("Koncentrace podle soutěže", lambda r: r.league or "?"), ("Koncentrace podle sportu", lambda r: r.sport),
                      ("Koncentrace podle kurzového pásma", lambda r: band(r.odds))):
        cnt = {}
        for r in pend:
            cnt[fn(r)] = cnt.get(fn(r), 0) + 1
        top = end + 3
        ws.cell(top - 1, 1, title).font = FB
        end = table(ws, top, ["Skupina", "Otevřených výběrů", "Podíl"],
                    [[k, v, v / len(pend)] for k, v in sorted(cnt.items(), key=lambda kv: -kv[1])], {3: PCT}, filt=False)


def _perf_sheets(d, sheet, table, F, FB) -> None:
    pf = d.perf
    missing = "Data Profilu 4 nejsou k dispozici – prázdné buňky znamenají chybějící data, ne nulu."
    SP = {"fotbal": "Fotbal", "hokej": "NHL"}
    PC = "+0.0%;-0.0%;0.0%"

    def ci(c, i):
        return c[i] if c else None

    def lines(ws, start, items):
        for k, line in enumerate(items, start):
            ws.cell(k, 1, line).font = F
        return start + len(items)

    # Predictive Performance – souhrn, upozorneni, dostatecnost dat, finance
    ws = sheet("Predictive Performance")
    if pf is None:
        ws.cell(5, 1, missing).font = F
    else:
        ws.cell(5, 1, pf["note"]).font = F
        end = table(ws, 7, ["Úroveň", "Upozornění", "Důvod"], [[a["level"], a["text"], a["reason"]] for a in pf["alerts"]],
                    widths=[14, 60, 90], filt=False)
        top = end + 3
        ws.cell(top - 1, 1, "Data Sufficiency – zda je dat dost pro závěr (počet zápasů sám nezaručuje významnost)").font = FB
        rows = [[SP[sp], part, x["level"], x["matches"], x["n"], x["days"], x["reason"], "; ".join(x["limits"]) or None]
                for sp, s in pf["sufficiency"].items()
                for part, x in (("Modely A–D proti trhu", s["model_comparison"]), ("Opportunity Ranking", s["ranking"]),
                                ("CLV", s["clv"]))]
        end = table(ws, top, ["Sport", "Část", "Stav", "Nezávislých zápasů", "Výběrů", "Období (dní)", "Zdůvodnění",
                              "Omezení"], rows, filt=False)
        top = end + 3
        ws.cell(top - 1, 1, "Finanční výkonnost – všechny profily (jednotky a Kč ve zvláštních sloupcích, nikdy se nesčítají)").font = FB
        end = table(ws, top, ["Skupina", "Typ", "Měna", "Záznamů", "Vyhodnoceno", "Výhry", "Prohry", "Vráceno", "Neplatné",
                              "Obrat (j.)", "Zisk (j.)", "Yield (j.)", "Vklady (Kč)", "Zisk (Kč)", "ROI (Kč)",
                              "CLV (ověřené)", "CLV n"], finance_rows(d),
                    {10: "0", 11: UNITS, 12: PC, 13: '#,##0 "Kč"', 14: '+#,##0 "Kč";-#,##0 "Kč";0 "Kč"', 15: PC, 16: PC},
                    [40, 26, 6, 8, 10, 7, 7, 8, 8, 9, 9, 9, 10, 10, 9, 10, 7], False)
        fi = pf["finance"]
        top = end + 3
        ws.cell(top - 1, 1, "Profil 4 – virtuální sázky podle skupin (1 tip = 1 j.)").font = FB
        rows = []
        for sp, sd in fi["by_sport"].items():
            for gname, key in (("celkem", None), ("rozhodnutí", "by_decision"), ("model nad trhem", "by_model"),
                               ("soutěž", "by_league"), ("trh", "by_market"), ("rankingové pásmo", "by_band"),
                               ("kurzové pásmo", "by_odds")):
                items = {"vše": sd["all"]} if key is None else sd[key]
                for k, v in items.items():
                    rows.append([SP[sp], gname, k, v["matches"], v["n"], v["won"], v["half_won"], v["push"], v["half_lost"],
                                 v["lost"], v["turnover"], v["profit"], v["yield"], ci(v["yield_ci95"], 0),
                                 ci(v["yield_ci95"], 1), v["roi"], v["win_rate"], v["avg_odds"], v["yield_status"]])
        end = table(ws, top, ["Sport", "Dělení", "Skupina", "Zápasů", "Sázek", "Výhry", "Půl výhry", "Vráceno", "Půl prohry",
                              "Prohry", "Obrat (j.)", "Zisk (j.)", "Yield", "Yield 95% od", "Yield 95% do", "ROI (100 j.)",
                              "Win rate", "Ø kurz", "Průkaznost"], rows,
                    {11: "0", 12: UNITS, 13: PC, 14: PC, 15: PC, 16: PC, 17: PCT, 18: ODDS}, None)
        end = lines(ws, end + 2, ["Vyřazeno z výsledků: " + ", ".join(f"{k} {v}" for k, v in fi["excluded"].items())]
                    + [f"{k}: {v}" for k, v in fi["method"].items()])

    # Ranking Performance
    ws = sheet("Ranking Performance")
    if pf is None:
        ws.cell(5, 1, missing).font = F
    else:
        rp = pf["ranking"]
        ws.cell(5, 1, rp["note"]).font = F
        rows = []
        for sp, sd in rp["sports"].items():
            for b in sd["bands"]:
                rows.append([SP[sp], b["band"], b["matches"], b["selections"], b["won"], b["half_won"], b["push"],
                             b["half_lost"], b["lost"], b["win_rate"], b["avg_odds"], b["avg_p"], b["avg_p_market"],
                             b["avg_ev"], b["avg_ev_conservative"], b["profit"], b["yield"], ci(b["yield_ci95"], 0),
                             ci(b["yield_ci95"], 1), ci(b["yield_ci_bonferroni"], 0), ci(b["yield_ci_bonferroni"], 1),
                             b["roi"], b["clv"], b["clv_n"], b["brier"], b["log_loss"], b["status"]])
        end = table(ws, 7, ["Sport", "Pásmo", "Nezávislých zápasů", "Výběrů", "Výhry", "Půl výhry", "Push", "Půl prohry",
                            "Prohry", "Win rate", "Ø kurz", "Ø p modelu", "Ø p trhu", "Ø EV", "Ø konz. EV", "Zisk (j.)",
                            "Yield", "Yield 95% od", "Yield 95% do", "Bonferroni od", "Bonferroni do", "ROI (100 j.)",
                            "CLV", "CLV n", "Brier", "Log Loss", "Stav"], rows,
                    {10: PCT, 11: ODDS, 12: PCT, 13: PCT, 14: PC, 15: PC, 16: UNITS, 17: PC, 18: PC, 19: PC, 20: PC, 21: PC,
                     22: PC, 23: PC, 25: DEC4, 26: DEC4}, None, False)
        items = []
        for sp, sd in rp["sports"].items():
            items.append(f"{SP[sp]}: {sd['sufficiency']['level']} – {sd['sufficiency']['reason']}")
            hl = sd["hi_minus_lo"]
            items.append(f"  Yield ranking ≥ 60 − < 60: " + ("není k dispozici" if hl["diff"] is None else f"{hl['diff'] * 100:+.1f} %")
                         + f" – {hl['status']}")
            av = sd.get("added_value")
            if av:
                items.append(f"  Přidaná hodnota při stejném konzervativním EV: {av['stratified_status']}")
                items.append(f"  Proti tržní pravděpodobnosti: {av['residual_status']}")
                items.append("  Korelace rankingu: " + ", ".join(f"{k} {'není k dispozici' if v is None else f'{v:+.2f}'}"
                                                            for k, v in av["correlations"].items()))
                items.append(f"  {av['copies_note']}")
        lines(ws, end + 2, items + ["Nejlepší historické pásmo není automaticky nejlepší budoucí strategie; váhy se neladí."])

    # Model Comparison
    ws = sheet("Model Comparison")
    if pf is None:
        ws.cell(5, 1, missing).font = F
    else:
        mc = pf["models"]
        ws.cell(5, 1, mc["note"]).font = F
        rows = []
        for sp, sd in mc["sports"].items():
            for k, m in sd["models"].items():
                vm = m.get("vs_market") or {}
                rows.append([SP[sp], "Trh (bez marže)" if k == "trh" else f"Model {k}", m["matches"], sd["rows"], m["brier"],
                             ci(m.get("brier_ci95"), 0), ci(m.get("brier_ci95"), 1), m["log_loss"], ci(m.get("log_loss_ci95"), 0),
                             ci(m.get("log_loss_ci95"), 1), m.get("ece"), m["mean_p"], m["rate"], m["calibration"]["status"],
                             vm.get("diff"), vm.get("status"), sd["sufficiency"]["level"]])
        end = table(ws, 7, ["Sport", "Model", "Nezávislých zápasů", "Výběrů", "Brier", "Brier 95% od", "Brier 95% do",
                            "Log Loss", "LL 95% od", "LL 95% do", "ECE", "Ø odhad", "Skutečnost", "Kalibrace",
                            "Log loss − trh", "Proti trhu", "Stav dat"], rows,
                    {5: DEC4, 6: DEC4, 7: DEC4, 8: DEC4, 9: DEC4, 10: DEC4, 11: DEC4, 12: PCT, 13: PCT, 15: "+0.0000;-0.0000"},
                    None, False)
        top = end + 3
        ws.cell(top - 1, 1, "Chronologické rozdělení (trénovací / validační / out-of-sample test) – log loss").font = FB
        rows = [[SP[sp], p["part"], p["matches"], p["n"], xdt(p["from"]), xdt(p["to"]),
                 *[p[k]["log_loss"] for k in ("A", "B", "C", "D", "trh")]] for sp, sd in mc["sports"].items() for p in sd["split"]]
        end = table(ws, top, ["Sport", "Část", "Zápasů", "Výběrů", "Od", "Do", "A", "B", "C", "D", "Trh"], rows,
                    {5: DT, 6: DT, 7: DEC4, 8: DEC4, 9: DEC4, 10: DEC4, 11: DEC4}, None, False)
        top = end + 3
        ws.cell(top - 1, 1, "Kalibrační pásma (reliability diagram) – odhad proti skutečnosti, interval z počtu zápasů").font = FB
        rows = [[SP[sp], "Trh" if k == "trh" else k, b["bin"], b["matches"], b["n"], b["predicted"], b["actual"],
                 b["ci95"][0], b["ci95"][1], "málo dat" if not b["enough"] else ("v intervalu" if b["ok"] else "mimo interval")]
                for sp, sd in mc["sports"].items() for k, m in sd["models"].items() for b in m["bins"]]
        end = table(ws, top, ["Sport", "Model", "Pásmo", "Zápasů", "Výběrů", "Odhad", "Skutečnost", "95% od", "95% do", "Stav"],
                    rows, {6: PCT, 7: PCT, 8: PCT, 9: PCT}, None, False)
        top = end + 3
        ws.cell(top - 1, 1, "Diagnostika: favorit / outsider a rozdíl model C – trh").font = FB
        rows = [[SP[sp], b["bin"], b["matches"], b["n"], b["p_c"], b["p_market"], b["actual"], b["bias_c"],
                 ci(b["bias_c_ci95"], 0), ci(b["bias_c_ci95"], 1), b["ll_vs_market"], b["status"]]
                for sp, sd in mc["sports"].items() for b in sd["favourite_outsider"] + sd["divergence"]]
        end = table(ws, top, ["Sport", "Skupina", "Zápasů", "Výběrů", "Ø C", "Ø trh", "Skutečnost", "C − skutečnost",
                              "95% od", "95% do", "LL C − trh", "Stav"], rows,
                    {5: PCT, 6: PCT, 7: PCT, 8: PC, 9: PC, 10: PC, 11: "+0.0000;-0.0000"}, None, False)
        items = [mc["reconstruction"]]
        for sp, sd in mc["sports"].items():
            tl = sd["timeline"]
            items.append(f"{SP[sp]}: předpovědi {tl['predicted'][0]} – {tl['predicted'][1]}, výkopy {tl['kickoff'][0]} – "
                         f"{tl['kickoff'][1]}, vyhodnoceno {tl['settled'][0]} – {tl['settled'][1]}; kurz vstupu po výkopu "
                         f"{tl['odds_after_kickoff']}, výsledek před výkopem {tl['settled_before_kickoff']}.")
        lines(ws, end + 2, items)

    # CLV Performance
    ws = sheet("CLV Performance")
    if pf is None:
        ws.cell(5, 1, missing).font = F
    else:
        cl = pf["clv"]
        ws.cell(5, 1, cl["note"]).font = F
        end = table(ws, 7, ["Druh srovnávacího kurzu", "Tipů", "Počítá se do CLV"],
                    [[k, v, "ano" if k.startswith("skutečný závěrečný") else "ne"] for k, v in cl["classes"].items()],
                    widths=[46, 8, 14], filt=False)
        rows = []
        for sp, sd in cl["by_sport"].items():
            groups = [("celkem", {"vše": sd["overall"]})] + [(n, sd[k]) for n, k in (
                ("rankingové pásmo", "by_band"), ("rozhodnutí", "by_decision"), ("model nad trhem", "by_model"),
                ("trh", "by_market"), ("kancelář", "by_book"), ("zdroj závěrečného kurzu", "by_source"))]
            for gname, items in groups:
                for k, v in items.items():
                    rows.append([SP[sp], gname, k, v["clv_n"], v["clv_matches"], v["clv"], v["clv_median"], v["clv_pos"],
                                 ci(v["clv_ci95"], 0), ci(v["clv_ci95"], 1), v.get("clv_status")])
        # profily hlavniho bota a tikety: CLV jen overene (vsechny skupiny exportu)
        for grp in sorted({r.group for r in d.records}):
            vals = [r.clv for r in d.records if r.group == grp and r.clv is not None]
            if vals:
                rows.append(["všechny", "profil / skupina", grp, len(vals), None, sum(vals) / len(vals),
                             sorted(vals)[len(vals) // 2], sum(v > 1e-9 for v in vals) / len(vals), None, None, None])
        top = end + 3
        ws.cell(top - 1, 1, "CLV jen proti skutečnému závěrečnému kurzu").font = FB
        end = table(ws, top, ["Sport", "Dělení", "Skupina", "Tipů s closing", "Zápasů", "Průměr CLV", "Medián CLV",
                              "Kladné CLV", "95% od", "95% do", "Průkaznost"], rows,
                    {6: PC, 7: PC, 8: PCT, 9: PC, 10: PC}, None)
        top = end + 3
        ws.cell(top - 1, 1, "Vývoj CLV po týdnech (Profil 4)").font = FB
        end = table(ws, top, ["Týden", "Tipů", "Průměr CLV", "Kladné"], [[w["week"], w["n"], w["clv"], w["pos"]] for w in cl["series"]],
                    {3: PC, 4: PCT}, None, False)
        ch = cl.get("change_7d")
        lines(ws, end + 2, [("Změna CLV (7 dní proti 28 dnům předtím): " + (
            NA + " (potřeba aspoň 10 tipů v obou obdobích)" if ch is None else
            f"{ch['recent'] * 100:+.1f} % proti {ch['older'] * 100:+.1f} % ({ch['n_recent']} / {ch['n_older']} tipů)"))])

    # Settlement Integrity
    ws = sheet("Settlement Integrity")
    if pf is None:
        ws.cell(5, 1, missing).font = F
    else:
        sa, now_run = pf.get("settlement") or {}, pf.get("audit_now")
        ws.cell(5, 1, sa.get("note") or "Settlement Integrity není k dispozici (starší databáze).").font = F
        rows = []
        if now_run:
            for g, v in now_run["groups"].items():
                rows.append(["přepočet při exportu", g, v["checked"], v["SHODA"], v["INTEGRITY MISMATCH"], v["NEOVĚŘITELNÉ"]])
        lr = sa.get("last_run")
        if lr:
            rows.append([f"poslední uložený audit {lr['run_at'][:16]}", "celkem", lr["checked"], lr["matched"], lr["mismatched"],
                         lr["unverifiable"]])
        lv = sa.get("live")
        if lv:
            rows.append(["záznamy při vyhodnocení (od Fáze 3.2)", "tipy", lv["checked"], lv["SHODA"], lv["INTEGRITY MISMATCH"],
                         lv["NEOVĚŘITELNÉ"]])
        end = table(ws, 7, ["Kontrola", "Skupina", "Zkontrolováno", "Shoda", "INTEGRITY MISMATCH", "Neověřitelné"], rows,
                    widths=[40, 24, 13, 9, 18, 13], filt=False)
        items = [f"Vyhodnocené tipy bez záznamu z doby vyhodnocení (před Fází 3.2): "
                 f"{sa.get('settled_without_record', NA)} – ověřují se jen historickým auditem z uloženého skóre."]
        for k, v in ((now_run or {}).get("unverifiable_reasons") or {}).items():
            items.append(f"Neověřitelné: {k} ({v})")
        end = lines(ws, end + 2, items)
        mism = (now_run or {}).get("items", []) + (sa.get("live_mismatches") or [])
        top = end + 3
        ws.cell(top - 1, 1, f"Neshody ({len(mism)}) – výsledky se nepřepisují, jen se nevydávají za ověřené").font = FB
        table(ws, top, ["Druh", "ID", "Zápas", "Trh", "Výběr", "Skóre", "Výsledek produkce", "Zisk produkce", "Důvod"],
              [[m.get("kind", "tip"), m.get("id") or m.get("tip_id"), m.get("match") or m.get("fixture_id"), m["market"],
                m["selection"], m.get("score") or m.get("normalized"), m.get("result"), m.get("profit"), m.get("reason")]
               for m in mism], {8: UNITS}, None, False)


def p4_archive_counts(path: Path | None) -> dict[str, int]:
    """Pocty zaznamu archivu Profilu 4 (jen cteni)."""
    if path is None or not Path(path).exists():
        return {}
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT kind, COUNT(*) FROM archive GROUP BY kind").fetchall())
    except sqlite3.DatabaseError:
        return {}
    finally:
        con.close()


class _ReadOnly:
    """Store-like pristup jen pro cteni (export nikdy nezapisuje do databaze Profilu 4)."""

    def __init__(self, con):
        self.con = con

    def query(self, sql, params=()):
        return self.con.execute(sql, tuple(params)).fetchall()


def _p4_perf(path: Path | None, now: datetime) -> dict | None:
    """Faze 3.2: Predictive Performance + cerstvy nezavisly audit settlementu (persist=False – nic se nezapise)."""
    if path is None or not Path(path).exists():
        return None
    from .profil4 import intelligence as IG
    from .profil4 import performance as PF
    from .profil4 import settleaudit as SA
    from .profil4.config import P4

    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        ro = _ReadOnly(con)
        preds, tips = IG.load_rows(ro.query)
        ranks = IG.load_rankings(ro.query)
        summ = SA.summary(ro)
        try:
            fresh = SA.audit_history(ro, now, persist=False)
        except sqlite3.DatabaseError:
            fresh = None
        out = PF.build(preds, tips, ranks, now, P4, summ)
        out["audit_now"] = fresh
        return out
    except sqlite3.DatabaseError:
        return None
    finally:
        con.close()


def finance_rows(d: "Data") -> list[list]:
    """Financni prehled vsech skupin: virtualni jednotky a skutecne Kc ve ZVLASTNICH sloupcich (nikdy se nesecitaji)."""
    rows = []
    order = {OFFICIAL: 0, WATCH: 1, NOBET: 2, TRACKED: 3, KOMBI: 4, PLACED: 5}
    keyed = {}
    for r in d.records:
        if r.kind == PLACED:
            key = (TICKETS + " – " + d.ticket_kinds.get(r.id, "ručně zadaný"), r.kind)
        else:
            key = (r.group, r.kind)
        keyed.setdefault(key, []).append(r)
    for (grp, kind), rs in sorted(keyed.items(), key=lambda kv: (order.get(kv[0][1], 9), kv[0][0])):
        st = stats(rs)
        real = kind == PLACED
        rows.append([grp, kind, "Kč" if real else "j.", st["n"], st["settled"], st["wins"], st["losses"], st["push"],
                     st["invalid"], None if real else float(st["staked"]), None if real else st["profit"],
                     None if real else st["roi"], st.get("stake_sum") if real else None,
                     st.get("profit_money") if real else None, st.get("roi_real") if real else None, st["clv"], st["clv_n"]])
    return rows


def collect(session, p4_path: Path | None = None, now: datetime | None = None) -> Data:
    """Nacte vse z databaze (jen cteni) do jednotne tabulky zaznamu."""
    from .config import SETTINGS
    from .profil4 import history
    from .profil4.config import MODEL_VERSION

    led = Ledger(session)
    names = {p.key: p.name for p in paper.PROFILES}
    recs, legs, seen, dup = [], [], set(), 0
    for p in paper.load(led):
        key = (p.profile, p.match_id, p.market, p.selection)
        if key in seen:  # stejny vyber stejneho zapasu v jednom profilu = jedna sazka
            dup += 1
            continue
        seen.add(key)
        kind = OFFICIAL if p.profile == "tipy" else KOMBI if p.profile == "kombi" else TRACKED
        sport = "kombi" if p.legs else _sport(p.league)
        recs.append(Rec(names.get(p.profile, p.profile), kind, sport, p.league, p.name, p.kickoff, p.market,
                        p.selection, p.odds, p.book, p.p_model, p.result, p.score, 1.0,
                        None if p.result is None else p.profit, None, None, SETTINGS.model_version, p.id))
        for x in p.legs:
            legs.append({"ticket": p.name, "ticket_odds": p.odds, "ticket_result": RESULT_CZ.get(p.result, p.result),
                         "ticket_profit": p.profit, **x})
    rows, p4_eval = _p4_rows(p4_path)
    bets, p4_dup = history.unique_bets(rows) if rows else ([], 0)
    dup += p4_dup
    for r in bets:
        dec = r.get("decision") or ""
        kind = OFFICIAL if dec == history.APPROVED else WATCH if "WATCH" in dec else NOBET
        probs = json.loads(r["probs"]) if r.get("probs") else {}
        res = r["result"]
        res = "invalid" if res == "void" else res  # Profil 4: void = neplatna sazka, push = vraceni vkladu
        verified = r.get("clv") is not None and (r.get("closing_kind") or "").startswith("závěrečný kurz")
        recs.append(Rec(P4_GROUPS[kind], kind, _sport(r.get("league")), r.get("league") or "", r.get("match") or "",
                        r.get("kickoff") or "", r["market"], (r.get("label") or r["selection"]).replace("±-0", "±0"),
                        r.get("odds"), r.get("bookmaker") or "", probs.get("C"), res, r.get("score") or "", 1.0,
                        None if res is None else (0.0 if res == "invalid" else r.get("profit")), None,
                        r["clv"] if verified else None, r.get("model_version") or MODEL_VERSION, f"p4-{r['id']}",
                        r["selection"]))
    from .storage import COMBO

    ticket_kinds = {}
    for t in session.query(Ticket).order_by(Ticket.id):
        ticket_kinds[f"t-{t.id}"] = ("kombinovaný (AKU)" if t.market == COMBO else "z tipu bota" if t.tip_id
                                     else "ručně zadaný")
        res = None if t.status == "open" else t.status if t.status in OUTCOME or t.status == "void" else "invalid"
        pl = t.profit_loss if res is not None else None
        recs.append(Rec(TICKETS, PLACED, _sport(t.league), t.league, t.match_name, t.match_day.isoformat(), t.market,
                        t.selection, t.odds, t.bookmaker, None, res, "", t.stake,
                        None if pl is None or not t.stake else pl / t.stake, pl,
                        t.clv_pct / 100 if t.closing_odds and t.clv_pct is not None else None, "", f"t-{t.id}"))
    bal, bank = 0.0, []
    for b in session.query(BankrollEntry).order_by(BankrollEntry.timestamp, BankrollEntry.id):
        bal += b.amount
        bank.append({"t": b.timestamp, "amount": b.amount, "reason": b.reason, "balance": bal, "ticket": b.ticket_id})
    versions = sorted({r.model for r in recs if r.model} | {SETTINGS.model_version, MODEL_VERSION})
    recs.sort(key=lambda r: (r.group, r.kickoff or ""))
    created = now or utcnow()
    return Data(created, recs, legs, bank, versions, dup, p4_eval, _p4_intel(p4_path, created),
                perf=_p4_perf(p4_path, created), ticket_kinds=ticket_kinds)


# --- statistiky -----------------------------------------------------------------------------------------------
def stats(recs: list[Rec]) -> dict:
    """Metriky jedne skupiny (definice v hlavicce modulu)."""
    by = {k: [r for r in recs if r.outcome == k] for k in (WIN, LOSS, PUSH, INVALID, PENDING)}
    staked = by[WIN] + by[LOSS]
    profit = sum(r.profit_units or 0.0 for r in staked)
    binary = [r for r in staked if r.p is not None and r.result in ("won", "lost") and 0 < r.p < 1]
    clv = [r.clv for r in recs if r.clv is not None and r.outcome != PENDING]
    out = {"n": len(recs), "settled": len(recs) - len(by[PENDING]), "pending": len(by[PENDING]),
           "wins": len(by[WIN]), "losses": len(by[LOSS]), "push": len(by[PUSH]), "invalid": len(by[INVALID]),
           "staked": len(staked), "profit": profit, "roi": profit / len(staked) if staked else None,
           "hit": len(by[WIN]) / len(staked) if staked else None,
           "avg_odds": sum(r.odds for r in staked) / len(staked) if staked else None,
           "brier": None, "logloss": None, "calib_n": len(binary),
           "clv": sum(clv) / len(clv) if clv else None, "clv_n": len(clv)}
    if binary:
        ys = [(r.p, 1.0 if r.result == "won" else 0.0) for r in binary]
        out["brier"] = sum((p - y) ** 2 for p, y in ys) / len(ys)
        eps = 1e-6
        out["logloss"] = -sum(y * math.log(max(p, eps)) + (1 - y) * math.log(max(1 - p, eps)) for p, y in ys) / len(ys)
    money = [r for r in staked if r.profit_money is not None]
    if money:  # skutecne tikety: penize
        stakes = sum(r.stake for r in money)
        out.update(stake_sum=stakes, profit_money=sum(r.profit_money for r in money),
                   roi_real=sum(r.profit_money for r in money) / stakes if stakes else None,
                   roi_avg=sum(r.profit_money / r.stake for r in money) / len(money))
    return out


def groups(recs: list[Rec]) -> dict[str, list[Rec]]:
    out: dict[str, list[Rec]] = {}
    for r in recs:
        out.setdefault(r.group, []).append(r)
    return out


def progression(recs: list[Rec]) -> tuple[list[str], dict[str, list[float | None]]]:
    """Kumulativni uspesnost po dnech pro kazdou skupinu (None = jeste nic vyhodnoceno)."""
    days = sorted({r.day for r in recs if r.outcome in (WIN, LOSS) and r.day})
    series = {}
    for g, rs in groups(recs).items():
        done = sorted((r for r in rs if r.outcome in (WIN, LOSS) and r.day), key=lambda r: r.day)
        if not done:
            continue
        vals, w, n, i = [], 0, 0, 0
        for d in days:
            while i < len(done) and done[i].day <= d:
                w += done[i].outcome == WIN
                n += 1
                i += 1
            vals.append(w / n if n else None)
        series[g] = vals
    return days, series


def meta_lines(d: Data) -> list[str]:
    return [f"Vytvořeno: {d.created:%Y-%m-%d %H:%M} UTC (export čte aktuální databázi, nic nemění)",
            f"Záznamů: {len(d.records)} tipů/sázek, {len(d.legs)} kombi nohou, {len(d.bankroll)} pohybů bankrollu; "
            f"vyřazených duplicit a tipů uložených po výkopu: {d.duplicates}",
            f"Verze modelu: {', '.join(d.versions)}"]


METHOD = [
    ("Zdroj dat", "Export vždy čte aktuální databázi panelu (Sledování, Moje tikety, bankroll) a databázi Profilu 4 "
                  "jen pro čtení. Historická data se nezapisují ani nemění."),
    ("Profil", "Sledování: Základ, Profil 2, Profil 3, Tipy bota, Rohy a karty, Kombi tikety. Profil 4 je rozdělen na "
               "oficiální doporučení (APPROVED BET), WATCH a NO BET. Moje tikety = skutečně vsazené sázky."),
    ("Typ", "oficiální doporučení = tip, který bot vydal ke vsazení (Tipy bota, Profil 4 APPROVED); WATCH = jen "
            "sledovat; NO BET = model nedoporučil (jen pro kontrolu); sledovaný výběr = papírové sledování profilu "
            "bez vkladu; kombi návrh = kombinovaný tiket z Přehledu; vsazený tiket = tvoje skutečná sázka."),
    ("Duplicity", "Stejný výběr stejného zápasu se v jednom profilu počítá jednou (opakovaný sken nebo nový kurz "
                  "není nová sázka). Mezi profily se nesčítá – stejný zápas může být ve více profilech, proto "
                  "souhrn nemá řádek „celkem za všechny profily“."),
    ("Výhra / prohra", "Výhra = výhra + půl výhra, prohra = prohra + půl prohra. Vráceno = vrácení vkladu "
                       "(DNB remíza, AH push). Neplatné = zrušená nebo neplatná sázka (např. chybně spárovaný kurz). "
                       "Čekající, vrácené a neplatné se do výher, proher ani ROI nepočítají."),
    ("Úspěšnost", "výhry / (výhry + prohry)."),
    ("Zisk (j.)", "Každý tip 1 jednotka. Výhra = kurz − 1, prohra = −1, půl výhra = (kurz − 1)/2, půl prohra = −0,5. "
                  "U skutečných tiketů = zisk v Kč / vklad."),
    ("ROI na 1 j.", "zisk v jednotkách / počet vsazených vyhodnocených tipů (výhry + prohry). Normalizované – "
                    "každý tip má stejnou váhu."),
    ("Skutečné ROI", "Jen Moje tikety: zisk v Kč / součet skutečných vkladů v Kč. Průměrné ROI na tiket = průměr "
                     "(zisk / vklad) přes tikety. Liší se, když vklady nejsou stejné."),
    ("Průměrný kurz", "Průměr kurzů vsazených vyhodnocených tipů."),
    ("Brier Score", "Průměr (šance − výsledek)², jen tipy s uloženou šancí modelu a čistou výhrou/prohrou. "
                    "0 = dokonalé, 0,25 = hod mincí u 50% sázek. Nižší je lepší."),
    ("Log Loss", "−průměr [y·ln p + (1−y)·ln(1−p)]. Nižší je lepší; trestá sebejisté omyly."),
    ("CLV", "(kurz tipu / férový závěrečný kurz − 1), jen ze skutečně ověřených závěrečných kurzů (Profil 4 "
            "„závěrečný kurz“, tiket se zapsaným závěrečným kurzem). Chybějící závěrečný kurz se nepočítá jako 0."),
    ("Kurzová pásma", "Výsledky podle kurzu tipu: " + ", ".join(band(lo) for lo, _ in BANDS) + "."),
    ("Kombi tikety", "Kurz tiketu = součin kurzů nohou. Cílová noha má jen minimální kurz (ověř u kanceláře), "
                     "výsledek proto počítá s tímto minimálním kurzem."),
    ("Bankroll", "Pohyby bankrollu (vklady, výběry, vyhodnocené tikety) a zůstatek po každém pohybu."),
    ("Malé vzorky", "Pod ~30 vyhodnocenými tipy jsou úspěšnost i ROI jen orientační."),
    ("Kalibrace modelů", "Profil 4: modely A–D a trh bez marže na STEJNÝCH řádcích (zápas, trh, výběr), jen předpovědi "
                         "před výkopem, poslední před zápasem. Kalibrační pásma po 10 %, pásma s < 15 zápasy se slučují. "
                         "Průkaznost (dobře/špatně kalibrovaný, lepší/horší než trh) až od 100 zápasů. Fotbal a NHL zvlášť."),
    ("Kvalita dat", "0–100 z aktuálnosti kurzu, zpoždění stažení, protistrany trhu, počtu kanceláří, konzistence kurzů, "
                    "statistik, sestav, absencí a úplnosti. Není to šance na výhru ani robustnost."),
    ("Opportunity Ranking", "0–100: konzervativní EV, kvalita dat, robustnost, shoda modelu s trhem, aktuálnost a "
                            "potvrzení kurzu, rizika a historie typu trhu. Pořadí zajímavosti, ne šance na výhru; "
                            "váhy experimentální."),
    ("Analýza chyb", "Kategorie vyhodnoceného tipu jen z uložených dat (mapování, settlement nezávislým přepočtem ze "
                     "skóre, kurz, stáří kurzu, protistrana, pravidla trhu, kvalita dat, odhad proti trhu, robustnost). "
                     "Prohra bez doložené příčiny = běžná náhodná prohra. Registr známých chyb uvádí opravu a test."),
    ("Market Integrity", "Poslední výpočet každého nadcházejícího výběru: kurz, kancelář, medián a férová cena trhu, "
                         "odchylka, protistrana, stáří a výsledek nezávislého přepočtu. Neshoda = NO BET – MATEMATICKÁ "
                         "INTEGRITA NEOVĚŘENA; neúplný trh = ČÁSTEČNĚ OVĚŘENO (nelze APPROVED)."),
    ("Ranking Validation", "Poslední předzápasový ranking každého výběru + výsledek. Pásma 0–39 / 40–59 / 60–79 / 80–100; "
                           "ROI na 1 j. ze skutečného kurzu, Brier a Log Loss modelu C, CLV jen ověřené. Intervaly "
                           "bootstrap po zápasech. Váhy se neladí; závěr až od 100 zápasů, jinak MÁLO DAT."),
    ("Portfolio Risk", "Oficiální tipy, sledované výběry, skutečné tikety a kombi zvlášť; virtuální jednotky a Kč se nesčítají. "
                       "Více výběrů jednoho zápasu = korelované, ne nezávislé sázky."),
]


_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def safe_text(v):
    """Text z externich zdroju (nazvy zapasu, tymu) nesmi Excel spustit jako vzorec (CSV/formula injection)."""
    return "'" + v if isinstance(v, str) and v.startswith(_FORMULA) else v


# --- vystupy --------------------------------------------------------------------------------------------------
def to_csv(d: Data) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    for line in meta_lines(d):
        w.writerow([f"# {line}"])
    w.writerow(COLUMNS)
    for r in d.records:
        w.writerow(["" if v is None else (f"{v:.4f}".replace(".", ",") if isinstance(v, float) else safe_text(v))
                    for v in r.row()])
    return ("﻿" + buf.getvalue()).encode("utf-8")  # BOM: cesky Excel otevre UTF-8 se strednikem spravne


def _summary_rows(d: Data) -> list[list]:
    out = []
    for g, rs in groups(d.records).items():
        s = stats(rs)
        out.append([g, rs[0].kind, s["n"], s["settled"], s["pending"], s["wins"], s["losses"], s["push"],
                    s["invalid"], s["hit"], s["profit"], s["roi"], s["avg_odds"], s["brier"], s["logloss"],
                    s["clv"], s["clv_n"]])
    return out


SUMMARY_HEAD = ["Profil", "Typ", "Tipů", "Vyhodnoceno", "Čeká", "Výhry", "Prohry", "Vráceno", "Neplatné",
                "Úspěšnost", "Zisk (j.)", "ROI na 1 j.", "Prům. kurz", "Brier", "Log Loss", "CLV", "CLV (n)"]
PCT, UNITS, ODDS, DEC4 = "0.0%", "+0.00;-0.00;0.00", "0.00", "0.0000"
SUMMARY_FMT = {10: PCT, 11: UNITS, 12: PCT, 13: ODDS, 14: DEC4, 15: DEC4, 16: "+0.0%;-0.0%;0.0%"}


def _breakdown(recs: list[Rec], key) -> list[list]:
    out = []
    for g, rs in groups(recs).items():
        parts: dict[str, list[Rec]] = {}
        for r in rs:
            parts.setdefault(key(r), []).append(r)
        for k in sorted(parts):
            s = stats(parts[k])
            out.append([g, k, s["n"], s["settled"], s["wins"], s["losses"], s["hit"], s["profit"], s["roi"], s["avg_odds"]])
    return out


BREAK_HEAD = ["Tipů", "Vyhodnoceno", "Výhry", "Prohry", "Úspěšnost", "Zisk (j.)", "ROI na 1 j.", "Prům. kurz"]
BREAK_FMT = {7: PCT, 8: UNITS, 9: PCT, 10: ODDS}


def _band_total(recs: list[Rec]) -> list[tuple[str, dict]]:
    pool = [r for r in recs if r.kind != NOBET]
    return [(band(lo), stats([r for r in pool if band(r.odds) == band(lo)])) for lo, _ in BANDS]


def to_xlsx(d: Data) -> bytes:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    F, FB = Font(name="Arial", size=10), Font(name="Arial", size=10, bold=True)
    H = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    HF = PatternFill("solid", fgColor="305496")
    wb = Workbook()

    def sheet(title, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws["A1"] = title
        ws["A1"].font = Font(name="Arial", size=13, bold=True)
        for i, line in enumerate(meta_lines(d), 2):
            ws.cell(i, 1, line).font = Font(name="Arial", size=9, italic=True, color="555555")
        return ws

    def table(ws, top, head, rows, fmt=None, widths=None, filt=True):
        for j, h in enumerate(head, 1):
            c = ws.cell(top, j, h)
            c.font, c.fill, c.alignment = H, HF, Alignment(wrap_text=True, vertical="center")
        for i, r in enumerate(rows, top + 1):
            for j, v in enumerate(r, 1):
                c = ws.cell(i, j, v)
                if isinstance(v, str):  # text vzdy jako text, nikdy jako vzorec
                    c.data_type = "s"
                c.font = F
                if fmt and j in fmt:
                    c.number_format = fmt[j]
        for j, w in enumerate(widths or [], 1):
            ws.column_dimensions[get_column_letter(j)].width = w
        if filt and rows:
            ws.auto_filter.ref = f"A{top}:{get_column_letter(len(head))}{top + len(rows)}"
        return top + len(rows)

    def chart(ch, title, ytitle=None):
        ch.title, ch.roundedCorners, ch.height, ch.width = title, False, 8, 18
        if ch.legend is not None:
            ch.legend.overlay = False
        ch.x_axis.delete = ch.y_axis.delete = False
        if ytitle:
            ch.y_axis.title = ytitle
        return ch

    # 1 Souhrn
    ws = sheet("Souhrn", first=True)
    srows = _summary_rows(d)
    end = table(ws, 6, SUMMARY_HEAD, srows, SUMMARY_FMT, [32, 24, 7, 11, 7, 7, 7, 8, 9, 10, 9, 10, 9, 8, 9, 8, 7], False)
    row = end + 2
    tick = stats([r for r in d.records if r.group == TICKETS])
    ws.cell(row, 1, "Moje tikety – skutečné peníze").font = FB
    money = [("Vsazeno celkem (vyhodnoceno)", tick.get("stake_sum", 0.0), '#,##0 "Kč"'),
             ("Finanční výsledek", tick.get("profit_money", 0.0), '+#,##0 "Kč";-#,##0 "Kč";0 "Kč"'),
             ("Skutečné ROI (zisk / vklady)", tick.get("roi_real"), "+0.0%;-0.0%;0.0%"),
             ("Průměrné ROI na tiket", tick.get("roi_avg"), "+0.0%;-0.0%;0.0%")]
    for i, (lab, v, f) in enumerate(money, row + 1):
        ws.cell(i, 1, lab).font = F
        c = ws.cell(i, 2, "–" if v is None else v)
        c.font, c.number_format = F, f
    row += len(money) + 2
    if srows:
        ch = chart(BarChart(), "ROI na 1 j. podle profilu", "ROI")
        ch.add_data(Reference(ws, min_col=12, min_row=6, max_row=end), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=7, max_row=end))
        ch.legend = None
        ch.y_axis.numFmt = "0%"
        ws.add_chart(ch, f"A{row}")
        row += 18
    days, series = progression(d.records)
    ws.cell(row, 1, "Vývoj úspěšnosti (kumulativně po dnech)").font = FB
    if days and series:
        head = ["Den"] + list(series)
        prow = [[dd] + [series[g][i] for g in series] for i, dd in enumerate(days)]
        pend = table(ws, row + 1, head, prow, {j: PCT for j in range(2, len(head) + 1)}, filt=False)
        ch = chart(LineChart(), "Vývoj úspěšnosti", "úspěšnost")
        ch.add_data(Reference(ws, min_col=2, max_col=len(head), min_row=row + 1, max_row=pend), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=row + 2, max_row=pend))
        ch.y_axis.numFmt = "0%"
        ws.add_chart(ch, f"{get_column_letter(len(head) + 2)}{row + 1}")
    else:
        ws.cell(row + 1, 1, "Zatím nic vyhodnoceno.").font = F
    if d.p4_eval and (d.p4_eval.get("calibration") or {}).get("models"):
        cal = d.p4_eval["calibration"]
        row = ws.max_row + 3
        ws.cell(row, 1, f"Profil 4 – přesnost modelů na všech předpovědích ({cal.get('matches')} zápasů, "
                        f"přepočet {str(d.p4_eval.get('t', ''))[:16]})").font = FB
        crow = [[k, m.get("matches"), m.get("brier"), m.get("log_loss"), m.get("mean_p"), m.get("rate")]
                for k, m in cal["models"].items()]
        table(ws, row + 1, ["Model", "Zápasů", "Brier", "Log Loss", "Prům. odhad", "Skutečnost"], crow,
              {3: DEC4, 4: DEC4, 5: PCT, 6: PCT}, filt=False)

    # 2 Vsechny tipy
    ws = sheet("Všechny tipy")
    table(ws, 6, COLUMNS, [r.row() for r in d.records], {9: ODDS, 11: PCT, 15: "0.##", 16: UNITS,
                                                         17: '+#,##0;-#,##0;0', 18: "+0.0%;-0.0%;0.0%"},
          [26, 18, 8, 14, 38, 16, 10, 24, 7, 14, 9, 10, 10, 9, 7, 9, 9, 9, 10, 16])
    ws.freeze_panes = "A7"

    # 3 Kombi
    ws = sheet("Kombi tikety")
    kombi = [r for r in d.records if r.kind == KOMBI]
    end = table(ws, 6, ["Tiket", "Výkop", "Kurz", "Šance", "Výsledek", "Zisk (j.)", "Poznámka (ID)"],
                [[r.match, r.kickoff[:16].replace("T", " "), r.odds, r.p, RESULT_CZ.get(r.result, r.result),
                  r.profit_units, r.id] for r in kombi], {3: ODDS, 4: PCT, 6: UNITS}, [60, 16, 7, 8, 10, 9, 14], False)
    top = end + 3
    ws.cell(top - 1, 1, "Jednotlivé nohy").font = FB
    table(ws, top, ["Tiket", "Zápas", "Soutěž", "Den", "Trh", "Volba", "Kurz nohy", "Kancelář", "Šance",
                    "Cílová noha", "Výsledek nohy", "Skóre"],
          [[x["ticket"], x["name"], x["league"], x["day"], x["market"], x["label"], x["odds"], x["book"], x["p"],
            "ano" if x.get("target") else "ne", RESULT_CZ.get(x["result"], x["result"]), x.get("score", "")]
           for x in d.legs], {7: ODDS, 9: PCT}, filt=False)

    # 4 Kurzova pasma
    ws = sheet("Kurzová pásma")
    end = table(ws, 6, ["Profil", "Kurzové pásmo"] + BREAK_HEAD, _breakdown(d.records, lambda r: band(r.odds)),
                BREAK_FMT, [32, 14, 7, 11, 7, 7, 10, 9, 10, 9])
    top = end + 3
    ws.cell(top - 1, 1, "Všechny profily kromě NO BET dohromady (stejný zápas může být ve více profilech)").font = FB
    tot = [[b, s["n"], s["settled"], s["wins"], s["losses"], s["hit"], s["profit"], s["roi"]] for b, s in _band_total(d.records)]
    tend = table(ws, top, ["Kurzové pásmo"] + BREAK_HEAD[:-1], tot, {6: PCT, 7: UNITS, 8: PCT}, filt=False)
    ch = chart(BarChart(), "ROI na 1 j. podle kurzového pásma", "ROI")
    ch.add_data(Reference(ws, min_col=8, min_row=top, max_row=tend), titles_from_data=True)
    ch.set_categories(Reference(ws, min_col=1, min_row=top + 1, max_row=tend))
    ch.legend = None
    ch.y_axis.numFmt = "0%"
    ws.add_chart(ch, f"L{top}")

    # 5 Souteze a sporty
    ws = sheet("Soutěže a sporty")
    end = table(ws, 6, ["Profil", "Sport"] + BREAK_HEAD, _breakdown(d.records, lambda r: r.sport), BREAK_FMT,
                [32, 22, 7, 11, 7, 7, 10, 9, 10, 9], False)
    top = end + 3
    ws.cell(top - 1, 1, "Podle soutěží").font = FB
    table(ws, top, ["Profil", "Soutěž"] + BREAK_HEAD, _breakdown(d.records, lambda r: r.league or "–"), BREAK_FMT,
          filt=False)

    # 6 Bankroll
    ws = sheet("Vývoj bankrollu")
    reason = {"deposit": "vklad", "withdrawal": "výběr", "settlement": "vyhodnocený tiket", "correction": "oprava vyhodnocení"}
    end = table(ws, 6, ["Čas (UTC)", "Pohyb (Kč)", "Důvod", "Zůstatek (Kč)", "Tiket"],
                [[b["t"].strftime("%Y-%m-%d %H:%M"), b["amount"], reason.get(b["reason"], b["reason"]), b["balance"],
                  b["ticket"]] for b in d.bankroll], {2: '+#,##0;-#,##0;0', 4: "#,##0"}, [18, 12, 18, 14, 8], False)
    if d.bankroll:
        ch = chart(LineChart(), "Bankroll v čase", "Kč")
        ch.add_data(Reference(ws, min_col=4, min_row=6, max_row=end), titles_from_data=True)
        ch.set_categories(Reference(ws, min_col=1, min_row=7, max_row=end))
        ch.legend = None
        ws.add_chart(ch, "G6")
    else:
        ws.cell(7, 1, "Zatím žádné pohyby bankrollu.").font = F

    # 8–12 Profil 4 – Intelligence & Calibration (Faze 3)
    _intel_sheets(d, sheet, table, chart, F, FB)

    # 16–20 Profil 4 – Predictive Validation & Settlement Integrity (Faze 3.2)
    _perf_sheets(d, sheet, table, F, FB)

    # 7 Metodika
    ws = sheet("Metodika")
    table(ws, 6, ["Pojem", "Vysvětlení"], METHOD, widths=[20, 120], filt=False)
    for i in range(7, 7 + len(METHOD)):
        ws.cell(i, 2).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(i, 1).font = FB

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(d: Data) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    def fmt(v, kind):
        if v is None:
            return "–"
        return {"pct": f"{v * 100:.1f} %", "u": f"{v:+.2f}", "o": f"{v:.2f}", "d": f"{v:.3f}"}[kind] if kind else str(v)

    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        fig = plt.figure(figsize=(11.69, 8.27))
        fig.text(0.04, 0.95, "Tips Bot – souhrnný report výsledků", fontsize=17, weight="bold")
        for i, line in enumerate(meta_lines(d)):
            fig.text(0.04, 0.915 - i * 0.022, line, fontsize=8.5, color="#555")
        rows = _summary_rows(d)
        head = ["Profil", "Tipů", "Vyhodn.", "Čeká", "V", "P", "Vr.", "Neplat.", "Úspěšnost", "Zisk (j.)", "ROI",
                "Prům. kurz", "Brier", "CLV"]
        body = [[r[0], r[2], r[3], r[4], r[5], r[6], r[7], r[8], fmt(r[9], "pct"), fmt(r[10], "u"), fmt(r[11], "pct"),
                 fmt(r[12], "o"), fmt(r[13], "d"), fmt(r[15], "pct")] for r in rows]
        ax = fig.add_axes([0.04, 0.30, 0.92, 0.55])
        ax.axis("off")
        if body:
            tb = ax.table(cellText=body, colLabels=head, loc="upper left", cellLoc="center",
                          colWidths=[0.24] + [0.058] * 13)
            tb.auto_set_font_size(False)
            tb.set_fontsize(8)
            tb.scale(1, 1.35)
        else:
            ax.text(0, 0.9, "Zatím žádné tipy.", fontsize=11)
        t = stats([r for r in d.records if r.group == TICKETS])
        fig.text(0.04, 0.22, "Moje tikety – skutečné peníze", fontsize=11, weight="bold")
        fig.text(0.04, 0.19, f"Vsazeno {t.get('stake_sum', 0):,.0f} Kč · výsledek {t.get('profit_money', 0):+,.0f} Kč · "
                             f"skutečné ROI {fmt(t.get('roi_real'), 'pct')} · průměrné ROI na tiket "
                             f"{fmt(t.get('roi_avg'), 'pct')}".replace(",", " "), fontsize=9.5)
        fig.text(0.04, 0.08, "ROI na 1 j. = zisk / počet vsazených vyhodnocených tipů (čekající, vrácené a neplatné se "
                             "nepočítají). Profily se nesčítají – stejný zápas může být ve více profilech.\nPod ~30 "
                             "vyhodnocenými tipy jsou čísla jen orientační. Podrobná metodika je na poslední straně.",
                 fontsize=8, color="#555")
        pdf.savefig(fig)
        plt.close(fig)

        fig, axs = plt.subplots(2, 2, figsize=(11.69, 8.27))
        fig.suptitle("Grafy", fontsize=14, weight="bold")
        ok = [(r[0], r[11]) for r in rows if r[11] is not None]
        ax = axs[0][0]
        if ok:
            ax.barh([n for n, _ in ok], [v * 100 for _, v in ok], color=["#16794a" if v >= 0 else "#b42318" for _, v in ok])
            ax.axvline(0, color="#333", lw=0.8)
            ax.tick_params(axis="y", labelsize=7)
        ax.set_title("ROI na 1 j. podle profilu (%)", fontsize=10)
        ax = axs[0][1]
        if d.bankroll:
            ax.plot([b["t"] for b in d.bankroll], [b["balance"] for b in d.bankroll], marker="o", color="#1d5fd1")
            ax.tick_params(axis="x", labelrotation=30, labelsize=7)
        else:
            ax.text(0.5, 0.5, "bez pohybů", ha="center")
        ax.set_title("Bankroll v čase (Kč)", fontsize=10)
        ax = axs[1][0]
        days, series = progression(d.records)
        for g, vals in series.items():
            ax.plot(days, [None if v is None else v * 100 for v in vals], label=g, lw=1.2)
        if series:
            ax.legend(fontsize=6)
            ax.tick_params(axis="x", labelrotation=45, labelsize=6)
        ax.set_title("Vývoj úspěšnosti (%)", fontsize=10)
        ax = axs[1][1]
        bt = _band_total(d.records)
        ax.bar([b for b, _ in bt], [(s["roi"] or 0) * 100 for _, s in bt],
               color=["#16794a" if (s["roi"] or 0) >= 0 else "#b42318" for _, s in bt])
        for i, (_, s) in enumerate(bt):
            ax.text(i, 0, f"n={s['staked']}", ha="center", va="bottom", fontsize=7)
        ax.tick_params(axis="x", labelsize=7)
        ax.set_title("ROI podle kurzového pásma (%, bez NO BET)", fontsize=10)
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        fig = plt.figure(figsize=(11.69, 8.27))
        fig.text(0.04, 0.95, "Metodika a vysvětlení metrik", fontsize=14, weight="bold")
        import textwrap

        y = 0.90
        for k, v in METHOD:
            lines = textwrap.wrap(v, 150)
            fig.text(0.04, y, k, fontsize=8.5, weight="bold")
            for ln in lines:
                fig.text(0.18, y, ln, fontsize=8)
                y -= 0.02
            y -= 0.008
        pdf.savefig(fig)
        plt.close(fig)
    return buf.getvalue()
