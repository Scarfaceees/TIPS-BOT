"""Sledovane modelove tipy (bez skutecneho vkladu): hodnoceni modelu na ceskych kurzech, ktere nemaji ferovou linii.

Bot z modelu a rucne zadanych kurzu vybere u zapasu nejvyhodnejsi vyber a sleduje ho jako jednotkovou
sazku (1 jednotka). Po zapase se vyhodnoti z vysledku. Slouzi k overeni, jestli model na trzich bez
ferove linie (pocet golu, BTTS) skutecne vydelava, nez se na nej zacne sazet.
Vlastni sledovane vybery lze pridat rucne (panel).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

import pandas as pd

from .config import SETTINGS, Settings
from .settlement import find_result, grade

KEY = "paper_picks"
KEEP = 2000
MIN_EV = 0.05  # od teto hrany (podle modelu) se vyber sleduje
SELECTIONS = {"1X2": ("1", "X", "2"), "BTTS": ("yes", "no")}


def selections(market: str) -> tuple[str, ...] | None:
    if market.startswith("OU_"):
        return ("over", "under")
    return SELECTIONS.get(market)


@dataclass
class Pick:
    id: str
    league: str
    match_id: str
    name: str
    day: str  # ISO datum zapasu
    kickoff: str  # ISO, UTC
    market: str
    selection: str
    odds: float
    book: str
    p_model: float | None
    source: str  # "model" | "rucne"
    created: str
    result: str | None = None  # won | lost | void | half_won | half_lost
    score: str = ""
    profit: float | None = None  # v jednotkach vkladu
    flags: list[str] = field(default_factory=list)

    @property
    def ev(self) -> float | None:
        return None if self.p_model is None else self.p_model * self.odds - 1.0


def unit_profit(result: str, odds: float) -> float:
    return {
        "won": odds - 1.0,
        "lost": -1.0,
        "void": 0.0,
        "half_won": (odds - 1.0) / 2.0,
        "half_lost": -0.5,
    }[result]


def load(ledger) -> list[Pick]:
    raw = ledger._get(KEY, "")
    return [Pick(**d) for d in json.loads(raw)] if raw else []


def save(ledger, picks: list[Pick]) -> None:
    picks = sorted(picks, key=lambda p: p.kickoff)[-KEEP:]
    ledger._set(KEY, json.dumps([asdict(p) for p in picks], ensure_ascii=False))


def track(ledger, league: str, offers: list, now: datetime, settings: Settings = SETTINGS) -> int:
    """`offers`: [(udalost, model {trh:vyber: p}, [ceske zaznamy])]. Pro kazdy zapas vybere nejvyssi hranu
    podle modelu; stejny zapas se pred vykopem jen aktualizuje, po vykopu se uz nemeni."""
    picks = load(ledger)
    by_match = {p.match_id: p for p in picks if p.source == "model"}
    cfg = settings.value
    n = 0
    for ev, model, entries in offers:
        if not model or ev.kickoff <= now:
            continue
        best: tuple[float, Pick] | None = None
        for e in entries:
            sels = selections(e.market)
            if sels is None or len(sels) != len(e.odds):
                continue
            for sel, odds in zip(sels, e.odds):
                p = model.get(f"{e.market}:{sel}")
                if p is None or not (cfg.min_odds <= odds <= cfg.max_odds):
                    continue
                edge = p * odds - 1.0
                if edge >= MIN_EV and (best is None or edge > best[0]):
                    flags = ["hrana nad 15 % je podezřelá, ověř ručně"] if edge > 0.15 else []
                    best = (
                        edge,
                        Pick(
                            id=uuid.uuid4().hex[:8], league=league, match_id=ev.match_id, name=ev.name,
                            day=ev.kickoff.date().isoformat(), kickoff=ev.kickoff.isoformat(), market=e.market,
                            selection=sel, odds=odds, book=e.book, p_model=round(p, 4), source="model",
                            created=now.isoformat(), flags=flags,
                        ),
                    )
        old = by_match.get(ev.match_id)
        if old is not None and datetime.fromisoformat(old.kickoff) <= now:
            continue
        if best is None:
            if old is not None and old.result is None:
                picks.remove(old)  # hrana zmizela, vyber uz neplati
            continue
        if old is not None:
            picks.remove(old)
        picks.append(best[1])
        n += 1
    if n or offers:
        save(ledger, picks)
    return n


def add_manual(ledger, *, league: str, name: str, day: str, market: str, selection: str, odds: float,
               book: str, now: datetime) -> Pick:
    if " - " not in name:
        raise ValueError("Zápas zapiš jako 'Domácí - Hosté'.")
    if odds <= 1.0:
        raise ValueError("Kurz musí být větší než 1.00.")
    market = market.strip().upper()
    sel = selection.strip().lower() if market != "1X2" else selection.strip().upper()
    sels = selections(market)
    if sels is None or sel not in sels:
        raise ValueError(f"Trh {market}: výběr musí být {', '.join(sels) if sels else 'z 1X2, OU_x.x nebo BTTS'}.")
    d = datetime.fromisoformat(day)
    pick = Pick(id=uuid.uuid4().hex[:8], league=league, match_id=f"manual:{day}:{name}", name=name, day=day,
                kickoff=d.replace(hour=23).isoformat(), market=market, selection=sel, odds=odds, book=book,
                p_model=None, source="rucne", created=now.isoformat())
    save(ledger, load(ledger) + [pick])
    return pick


def remove(ledger, pick_id: str) -> None:
    save(ledger, [p for p in load(ledger) if p.id != pick_id])


def set_result(ledger, pick_id: str, result: str) -> Pick:
    picks = load(ledger)
    pick = next((p for p in picks if p.id == pick_id), None)
    if pick is None:
        raise ValueError("Sledovaný výběr neexistuje.")
    pick.result, pick.profit = result, unit_profit(result, pick.odds)
    save(ledger, picks)
    return pick


def settle(ledger, results: pd.DataFrame) -> list[str]:
    """Vyhodnoti sledovane vybery, jejichz vysledek je v datech; zapise skore."""
    picks = load(ledger)
    notes = []
    for p in picks:
        if p.result is not None:
            continue
        res = find_result(results, p.name, datetime.fromisoformat(p.day).date())
        if res is None:
            continue
        outcome = grade(p.market, p.selection, *res)
        p.score = f"{res[0]}:{res[1]}"
        if outcome is None:
            continue
        p.result, p.profit = outcome, unit_profit(outcome, p.odds)
        notes.append(f"Sledovaný výběr {p.name} {p.score}: {p.market} {p.selection} @ {p.odds:.2f} -> {outcome}")
    if notes:
        save(ledger, picks)
    return notes


def stats(picks: list[Pick]) -> dict:
    done = [p for p in picks if p.result is not None]
    out = {"open": len(picks) - len(done), "settled": len(done), "profit": 0.0, "roi": None, "hit": None,
           "avg_odds": None, "avg_ev": None, "by_market": {}}
    if not done:
        return out
    out["profit"] = sum(p.profit for p in done)
    out["roi"] = out["profit"] / len(done)
    out["hit"] = sum(p.result in ("won", "half_won") for p in done) / len(done)
    out["avg_odds"] = sum(p.odds for p in done) / len(done)
    evs = [p.ev for p in done if p.ev is not None]
    out["avg_ev"] = sum(evs) / len(evs) if evs else None
    for m in sorted({p.market for p in done}):
        sub = [p for p in done if p.market == m]
        out["by_market"][m] = (len(sub), sum(p.profit for p in sub), sum(p.profit for p in sub) / len(sub))
    return out
