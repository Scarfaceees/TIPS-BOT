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
    notes: list[str] = field(default_factory=list)


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
                        r["clv"] if verified else None, r.get("model_version") or MODEL_VERSION, f"p4-{r['id']}"))
    for t in session.query(Ticket).order_by(Ticket.id):
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
    return Data(now or utcnow(), recs, legs, bank, versions, dup, p4_eval)


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
            f"vyřazených duplicit: {d.duplicates}",
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
]


# --- vystupy --------------------------------------------------------------------------------------------------
def to_csv(d: Data) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    for line in meta_lines(d):
        w.writerow([f"# {line}"])
    w.writerow(COLUMNS)
    for r in d.records:
        w.writerow(["" if v is None else (f"{v:.4f}".replace(".", ",") if isinstance(v, float) else v) for v in r.row()])
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
    reason = {"deposit": "vklad", "withdrawal": "výběr", "settlement": "vyhodnocený tiket"}
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
