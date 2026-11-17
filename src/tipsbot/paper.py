"""Sledovane modelove tipy (bez skutecneho vkladu): hodnoceni modelu na ceskych kurzech, ktere nemaji ferovou linii.

Bot z modelu a rucne zadanych kurzu vybere u zapasu nejvyhodnejsi vyber a sleduje ho jako jednotkovou
sazku (1 jednotka). Po zapase se vyhodnoti z vysledku. Slouzi k overeni, jestli model na trzich bez
ferove linie (pocet golu, BTTS) skutecne vydelava, nez se na nej zacne sazet.
Vlastni sledovane vybery lze pridat rucne (panel).

Profily: kazdy profil hleda vybery podle vlastnich pravidel (prah zisku, pasmo kurzu, trhy) a ma vlastni
sledovani, takze jde porovnat, ktery zpusob hledani tipu vydelava.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

import pandas as pd

from . import marketsim
from .config import SETTINGS, Settings
from .settlement import settle_bet

KEY = "paper_picks"
KEEP = 2000
MIN_EV = 0.05  # od teto hrany (podle modelu) se vyber sleduje
SELECTIONS = {"1X2": ("1", "X", "2"), "BTTS": ("yes", "no"), "DC": ("1X", "12", "X2"), "DNB": ("home", "away")}


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    about: str
    min_ev: float = MIN_EV
    min_odds: float | None = None  # None = podle nastaveni (ValueConfig)
    max_odds: float | None = None
    markets: tuple[str, ...] | None = None  # zacatky nazvu trhu (napr. "OU_", "1X2"); None = vsechny
    active: bool = True
    method: str = "edge"  # edge = nejvyssi cekany zisk podle modelu bota; sim = model trhu se simulaci (marketsim)


PROFILES = (
    Profile("zaklad", "Základ", "u každého zápasu výběr s nejvyšším čekaným ziskem od 5 %, kurzy podle nastavení"),
    Profile("profil2", "Profil 2 – Scénáře model/trh",
            "góly z modelu bota proti trhu (scénář model, kompromis 50/50 a trh), nejistota odhadu, simulace 1000 "
            "zápasů; sázka musí mít kladný čekaný zisk v modelu i v kompromisu; přednost má nejrozumnější sázka "
            "(prohra nejvýš ve 25 % zápasů, i nízký kurz), pak kurz 1,5+ s výhrou aspoň 60 %, pak nejvyšší hodnota. "
            "Ruční rozbor v záložce Profil 2", method="p2"),
    Profile("profil3", "Profil 3 – Simulace trhu",
            "góly fitované z kurzů kanceláří, simulace 1000 zápasů a 4 scénáře; nižší kurz 1.15–1.75, "
            "prohra nejvýš ve 25 % zápasů, sázka musí obstát i při méně příznivém průběhu",
            min_odds=1.15, max_odds=1.75, method="sim"),
)
DEFAULT = PROFILES[0].key


def profile(key: str | None) -> Profile:
    return next((pr for pr in PROFILES if pr.key == key), PROFILES[0])


def selections(market: str) -> tuple[str, ...] | None:
    if market.startswith(("OU_", "TTH_", "TTA_", "ROHY_", "KARTY_")):
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
    profile: str = DEFAULT
    detail: str = ""  # rozbor vyberu (Profil 3: ocekavane goly, simulace, scenare)

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


_OLD_FLAGS = {"hrana nad 15 % je podezřelá, ověř ručně": "čekaný zisk nad 15 % je podezřelý, ověř ručně"}


def load(ledger) -> list[Pick]:
    raw = ledger._get(KEY, "")
    picks = [Pick(**d) for d in json.loads(raw)] if raw else []
    for p in picks:  # starsi zaznamy maji puvodni text upozorneni
        p.flags = [_OLD_FLAGS.get(f, f) for f in p.flags]
    return picks


def save(ledger, picks: list[Pick]) -> None:
    picks = sorted(picks, key=lambda p: p.kickoff)[-KEEP:]
    ledger._set(KEY, json.dumps([asdict(p) for p in picks], ensure_ascii=False))


def _sim(pr: Profile, league: str, ev, model: dict | None, entries: list, now: datetime) -> Pick | None:
    """Profil se simulaci trhu (marketsim)."""
    cfg = marketsim.SimConfig(min_odds=pr.min_odds or marketsim.DEFAULT.min_odds,
                              max_odds=pr.max_odds or marketsim.DEFAULT.max_odds)
    sp = marketsim.best_pick(ev.match_id, model, entries, cfg)
    if sp is None:
        return None
    return Pick(
        id=uuid.uuid4().hex[:8], league=league, match_id=ev.match_id, name=ev.name,
        day=ev.kickoff.date().isoformat(), kickoff=ev.kickoff.isoformat(), market=sp.market,
        selection=sp.selection, odds=sp.odds, book=sp.book, p_model=round(sp.p_equiv, 4), source="model",
        created=now.isoformat(), flags=sp.flags, profile=pr.key, detail=sp.detail,
    )


def _p2(pr: Profile, league: str, ev, model: dict | None, entries: list, now: datetime) -> Pick | None:
    """Profil 2 (profile2): scenare model / kompromis / trh, robustni sazka."""
    from . import profile2

    got = profile2.auto_pick(ev.name, model, entries)
    if got is None:
        return None
    pk, a = got
    o = pk.offer
    p_eff = o.worst_win / max(1e-9, 1.0 - o.worst_push)
    flags = [pk.title] + (["čekaný zisk nad 15 % je podezřelý, ověř ručně"] if o.worst_ev > 0.15 else [])
    return Pick(
        id=uuid.uuid4().hex[:8], league=league, match_id=ev.match_id, name=ev.name,
        day=ev.kickoff.date().isoformat(), kickoff=ev.kickoff.isoformat(), market=o.market, selection=o.selection,
        odds=o.odds, book=o.book, p_model=round(p_eff, 4), source="model", created=now.isoformat(), flags=flags,
        profile=pr.key, detail=profile2.pick_detail(a, pk),
    )


def _best(pr: Profile, league: str, ev, model: dict, entries: list, now: datetime, settings: Settings) -> Pick | None:
    """Vyber s nejvyssim cekanym ziskem podle pravidel profilu."""
    if pr.method == "sim":
        return _sim(pr, league, ev, model, entries, now)
    if pr.method == "p2":
        return _p2(pr, league, ev, model, entries, now)
    if not model:
        return None
    lo = settings.value.min_odds if pr.min_odds is None else pr.min_odds
    hi = settings.value.max_odds if pr.max_odds is None else pr.max_odds
    best: tuple[float, Pick] | None = None
    for e in entries:
        sels = selections(e.market)
        if sels is None or len(sels) != len(e.odds) or (pr.markets and not e.market.startswith(pr.markets)):
            continue
        for sel, odds in zip(sels, e.odds):
            p = model.get(f"{e.market}:{sel}")
            if p is None or not (lo <= odds <= hi):
                continue
            edge = p * odds - 1.0
            if edge >= pr.min_ev and (best is None or edge > best[0]):
                flags = ["čekaný zisk nad 15 % je podezřelý, ověř ručně"] if edge > 0.15 else []
                best = (edge, Pick(
                    id=uuid.uuid4().hex[:8], league=league, match_id=ev.match_id, name=ev.name,
                    day=ev.kickoff.date().isoformat(), kickoff=ev.kickoff.isoformat(), market=e.market,
                    selection=sel, odds=odds, book=e.book, p_model=round(p, 4), source="model",
                    created=now.isoformat(), flags=flags, profile=pr.key,
                ))
    return best[1] if best else None


def track(ledger, league: str, offers: list, now: datetime, settings: Settings = SETTINGS) -> int:
    """`offers`: [(udalost, model {trh:vyber: p}, [ceske zaznamy])]. Kazdy aktivni profil vybere u zapasu
    nejvyssi cekany zisk podle svych pravidel; pred vykopem se vyber jen aktualizuje, po vykopu se nemeni."""
    picks = load(ledger)
    by_match = {(p.profile, p.match_id): p for p in picks if p.source == "model"}
    n = 0
    for ev, model, entries in offers:
        if ev.kickoff <= now:
            continue
        for pr in PROFILES:
            if not pr.active:
                continue
            new = _best(pr, league, ev, model, entries, now, settings)
            old = by_match.get((pr.key, ev.match_id))
            if old is not None and datetime.fromisoformat(old.kickoff) <= now:
                continue
            if new is None:
                if old is not None and old.result is None:
                    picks.remove(old)  # vyhoda zmizela, vyber uz neplati
                continue
            if old is not None:
                picks.remove(old)
            picks.append(new)
            n += 1
    if n or offers:
        save(ledger, picks)
    return n


def add_manual(ledger, *, league: str, name: str, day: str, market: str, selection: str, odds: float,
               book: str, now: datetime, profile_key: str = DEFAULT) -> Pick:
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
                p_model=None, source="rucne", created=now.isoformat(), profile=profile(profile_key).key)
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
        got = settle_bet(results, p.name, datetime.fromisoformat(p.day).date(), p.market, p.selection)
        if got is None:
            continue
        p.score, outcome = got
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
