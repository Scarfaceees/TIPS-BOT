"""Rozbor vyberu ve Sledovani: proc ho profil vybral a rozbor zapasu, ze ktereho vychazel.

Pozadavek 23.11.2026: vyber bez vysvetleni (napr. Zaklad – Vyhra Monza @ 3.60, cekany zisk +18,7 %) je k nicemu.
Ke kazdemu vyberu (vsechny profily, u kombi tiketu ke kazde noze) se ulozi:
- „Proč tento výběr“: sance profilu, modelu bota, druheho modelu a trhu bez marze, rozdil profilu proti trhu,
  cekany zisk po smichani s trhem a hodnoceni bota, nejvyssi kurzy, vysvetleni varovani;
- rozbor zapasu ze skenu (ocekavane skore, sila tymu, forma, vzajemne zapasy, rohy a karty, zpravy o tymech);
- absence, zpravy a pocasi.
Rozbor se do vykopu obnovuje s kazdym skenem; po vykopu zustava posledni stav pred zapasem (nic se nevymysli
dodatecne – starsi vybery se doplni jen z archivu skenu pred vykopem).
"""

from __future__ import annotations

from datetime import datetime

from . import analysis, paper


def _pct(x: float) -> str:
    return f"{x * 100:.0f} %"


def _row(view, market: str, selection: str):
    return next((r for r in view.rows if r.market == market and r.selection == selection), None)


def why(market: str, selection: str, odds: float, book: str, p: float | None, view) -> list[str]:
    home, _, away = view.name.partition(" - ")
    try:
        lab = analysis.label(market, selection, home, away)
    except (KeyError, IndexError, ValueError):
        lab = f"{market} {selection}"
    lines = [f"Výběr: {lab} @ {odds:.2f} ({book})."]
    if p:
        lines.append(f"Šance podle profilu {_pct(p)} → férový kurz {1 / p:.2f}, čekaný zisk {(p * odds - 1) * 100:+.1f} %"
                     f" (vyplatí se od kurzu {1 / p:.2f}).")
    r = _row(view, market, selection)
    if r is not None:
        src = [f"model bota {_pct(r.p_model)}"] + ([f"druhý model {_pct(r.p_second)}"] if r.p_second else [])
        src += [f"trh bez marže {_pct(r.p_market)}", f"model + trh {_pct(r.p_final)}"]
        lines.append("Šance podle zdrojů: " + ", ".join(src) + ".")
        if p:
            d = (p - r.p_market) * 100
            if d >= 5:
                lines.append(f"Profil věří výběru o {d:.0f} p. b. víc než trh. Buď model vidí něco, co trh podcenil "
                             "(síla týmů, forma), nebo nezná něco, co trh ví (zranění, sestavy, motivace) – "
                             "ověř absence a zprávy níže.")
            elif d <= -5:
                lines.append(f"Profil věří výběru o {-d:.0f} p. b. méně než trh – zisk plyne jen z vysokého kurzu.")
            else:
                lines.append(f"Profil a trh se shodují (rozdíl {d:+.0f} p. b.) – zisk plyne hlavně z kurzu nad "
                             "férovou hodnotou.")
        mix = (r.p_final * odds - 1) * 100
        lines.append(f"Po smíchání modelu s trhem (jako u oficiálních tipů) vychází čekaný zisk {mix:+.1f} %"
                     + (" – oficiální tip z toho není." if r.status == "none" else "."))
        if r.note:
            lines.append(f"Hodnocení bota: {r.note}.")
        if r.offers:
            lines.append("Nejvyšší kurzy: " + ", ".join(f"{b} {o:.2f}" for b, o in r.offers[:5]) + ".")
    if p and p * odds - 1 > 0.15:
        lines.append("Čekaný zisk nad 15 % je u velkých soutěží neobvyklý, proto štítek „ověř ručně“. Neznamená to, "
                     "že je sázka špatná – rozdíl proti trhu je ale potřeba vysvětlit (absence, sestavy, zprávy).")
    if "medián" in book or "zahrani" in book:
        lines.append("Kurz je medián zahraničních kanceláří, ne kurz Tipsportu – u nás může být jiný.")
    return lines


def context(view) -> dict | None:
    lines = ([] if view.absence_checked else ["Absence nebyly ověřeny."]) + list(view.absences) + list(view.news)
    if view.weather:
        lines.append(f"Počasí: {view.weather}")
    return {"title": "Absence, zprávy a počasí", "lines": lines} if lines else None


def build(market: str, selection: str, odds: float, book: str, p: float | None, view, prefix: str = "") -> list[dict]:
    """Sekce rozboru jednoho vyberu ({"title", "lines"} jako rozbor zapasu v Prehledu)."""
    secs = [{"title": "Proč tento výběr", "lines": why(market, selection, odds, book, p, view)}, *view.sections]
    c = context(view)
    secs += [c] if c else []
    return [{"title": prefix + s["title"], "lines": list(s["lines"])} for s in secs]


def enrich(ledger, views: list, now: datetime, only_missing: bool = False) -> int:
    """Doplni / obnovi rozbor vyberu ze Sledovani z rozboru zapasu `views`. Po vykopu se rozbor nemeni
    (jen chybejici se doplni). Vraci pocet zmenenych vyberu."""
    by_id = {v.match_id: v for v in views}
    by_name = {v.name: v for v in views}
    picks = paper.load(ledger)
    n = 0
    for p in picks:
        if p.legs:  # kombi: rozbor ke kazde noze (nohy byvaji z ruznych soutezi)
            hit = False
            for x in p.legs:
                v = by_name.get(x["name"])
                started = datetime.fromisoformat(x["kickoff"]) <= now
                if v is None or (x.get("info") and (started or only_missing)):
                    continue
                new = build(x["market"], x["selection"], x["odds"], x["book"], x.get("p"), v, f"{x['name']}: ")
                if new != x.get("info"):
                    x["info"], hit = new, True
            n += hit
            continue
        v = by_id.get(p.match_id) or by_name.get(p.name)
        if v is None or (p.info and (datetime.fromisoformat(p.kickoff) <= now or only_missing)):
            continue
        new = build(p.market, p.selection, p.odds, p.book, p.p_model, v)
        if new != p.info:
            p.info = new
            n += 1
    if n:
        paper.save(ledger, picks)
    return n


def backfill(ledger, now: datetime) -> int:
    """Vyberum bez rozboru doplni rozbor z posledniho archivovaneho skenu PRED vykopem zapasu."""
    latest: dict[str, object] = {}
    for _key, t, text in ledger.archived("scan"):
        for v in analysis.loads(text):
            ko = datetime.fromisoformat(v.kickoff)
            ko = ko.replace(tzinfo=None) if ko.tzinfo else ko
            if t <= ko:
                latest[v.match_id] = v  # archiv je od nejstarsiho: zustane posledni sken pred vykopem
    return enrich(ledger, list(latest.values()), now, only_missing=True)
