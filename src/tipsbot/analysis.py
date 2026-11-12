"""Rozbor zapasu: vsechny trhy zapasu s pravdepodobnostmi, ferovym kurzem, nejlepsi nabidkou a EV."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import devig
from .config import LEAGUES, SETTINGS, Settings
from .tips import Candidate, _betting_offers, window


@dataclass
class Row:
    market: str
    selection: str
    label: str
    p_model: float  # model (u NHL ukotveny na trzni total)
    p_market: float  # ferova p z ostrych kancelari
    p_final: float  # smes modelu a trhu
    fair_odds: float
    best_odds: float
    best_book: str
    ev_pct: float
    status: str  # "tip" | "value" | "none"
    note: str = ""
    min_odds: float = 0.0  # u trhu bez kurzu: od jakeho kurzu se vyplati sazet


@dataclass
class MatchView:
    match_id: str
    league: str
    name: str
    kickoff: str  # ISO, UTC
    has_model: bool
    rows: list[Row] = field(default_factory=list)
    recommended: int | None = None  # index radku
    verdict: str = ""
    sections: list = field(default_factory=list)  # [{"title", "lines"}]
    absences: list = field(default_factory=list)  # ["Tym: hrac - stav (poznamka)"]
    absence_checked: bool = False
    weather: str = ""
    news: list = field(default_factory=list)
    target: int | None = None  # index radku s cilovym tipem (kdyz neni overena hodnota)


def label(market: str, selection: str, home: str, away: str) -> str:
    if market == "1X2":
        return {"1": f"Výhra {home}", "X": "Remíza", "2": f"Výhra {away}"}[selection]
    if market == "MONEYLINE":
        return f"Výhra {home if selection == 'home' else away} (vč. prodl.)"
    if market == "DC":
        return {"1X": f"{home} nebo remíza (1X)", "X2": f"Remíza nebo {away} (X2)", "12": f"Bez remízy ({home} nebo {away})"}[selection]
    if market == "DNB":
        return f"Sázka bez remízy: {home if selection == 'home' else away}"
    if market == "BTTS":
        return "Oba týmy skórují: " + ("ano" if selection == "yes" else "ne")
    if market == "OT":
        return "Zápas jde do prodloužení: " + ("ano" if selection == "yes" else "ne")
    if market.startswith(("TTH_", "TTA_")):
        team = home if market.startswith("TTH_") else away
        return f"{team}: {'Over' if selection == 'over' else 'Under'} {market[4:]} gólů"
    if market.startswith("AH_"):
        return f"Asijský handicap {home if selection == 'home' else away} {market[3:]}"
    if market == "REG":
        return {"1": f"Základní doba: {home}", "X": "Základní doba: remíza", "2": f"Základní doba: {away}"}[selection]
    if market.startswith("OU_"):
        return f"{'Over' if selection == 'over' else 'Under'} {market[3:]} gólů"
    if market.startswith("PUCKLINE"):
        team = home if selection.startswith("home") else away
        return f"Handicap {team} {selection[4:] if selection.startswith('home') else selection[4:]}"
    return f"{market} {selection}"


def build_view(
    cands: list[Candidate],
    tips: list,
    has_model: bool,
    now: datetime,
    model: dict[str, float] | None = None,
    settings: Settings = SETTINGS,
) -> MatchView | None:
    if not cands:
        return None
    c0 = cands[0]
    home, away = c0.match_name.split(" - ", 1)
    league = LEAGUES[c0.league]
    win = window(c0.kickoff, now, settings)
    thr = settings.value.ev_threshold(league.tier, early=win == "early")
    tip_keys = {(t.market, t.selection) for t in tips}
    view = MatchView(c0.match_id, c0.league, c0.match_name, c0.kickoff.isoformat(), has_model)
    for c in cands:
        offers = _betting_offers(c.offers, settings)
        if not offers:
            continue
        best = offers[0]
        a = devig.assess_value(c.p_model, c.p_market, best.odds, thr, settings.value)
        if (c.market, c.selection) in tip_keys:
            status, note = "tip", ""
        elif a.is_value:
            status, note = "value", "mimo sázkové okno" if win is None else "jiný trh na zápase má vyšší EV"
        else:
            status, note = "none", ", ".join(a.reasons)
        view.rows.append(
            Row(c.market, c.selection, label(c.market, c.selection, home, away), c.p_model, c.p_market, a.prob,
                1.0 / c.p_market, best.odds, best.bookmaker, a.ev * 100.0, status, note)
        )
    if not view.rows:
        return None
    n_priced = len(view.rows)
    if model:
        view.rows += model_rows(model, {(r.market, r.selection) for r in view.rows}, home, away, thr, settings)
    valued = [i for i, r in enumerate(view.rows[:n_priced]) if r.status != "none"]
    lo, hi = settings.tickets.target_prob_min, settings.tickets.target_prob_max
    cands = [i for i in range(n_priced, len(view.rows)) if lo <= view.rows[i].p_model <= hi]
    if cands:  # nejpravdepodobnejsi realny cil
        view.target = max(cands, key=lambda i: view.rows[i].p_model)
    pool = valued or list(range(n_priced))
    view.recommended = max(pool, key=lambda i: view.rows[i].ev_pct)
    r = view.rows[view.recommended]
    if r.status == "tip":
        view.verdict = "TIP"
    elif r.status == "value":
        view.verdict = "VALUE (mimo okno)" if win is None else "VALUE"
    elif view.target is not None:
        view.verdict = "CÍL"
    else:
        view.verdict = "BEZ VALUE – nesázet"
    return view


def model_rows(model: dict[str, float], priced: set[tuple[str, str]], home: str, away: str, thr: float,
               settings: Settings = SETTINGS, min_p: float = 0.22, max_p: float = 0.86) -> list[Row]:
    """Dalsi trhy jen z modelu (bez kurzu): pravdepodobnost, ferovy kurz a kurz, od ktereho se vyplati sazet."""
    rows = []
    for key, p in model.items():
        if ":" not in key:
            continue
        market, sel = key.split(":", 1)
        if (market, sel) in priced or market == "REG" or not (min_p <= p <= max_p):
            continue
        if model.get("anchored", 1.0) < 0.5 and market.startswith(("OU_", "TTH_", "TTA_")):
            continue  # NHL bez trzniho totalu: uroven golu modelu je nespolehliva
        min_odds = max((1.0 + thr + 0.02) / p, settings.value.min_odds)
        if min_odds > settings.value.max_odds:
            continue
        rows.append(Row(market, sel, label(market, sel, home, away), p, p, p, 1.0 / p, 0.0, "", 0.0, "model",
                        "jen model, ověř kurz u kanceláře", min_odds))
    return sorted(rows, key=lambda r: -r.p_model)


def dumps(views: list[MatchView]) -> str:
    return json.dumps([asdict(v) for v in views])


def loads(raw: str) -> list[MatchView]:
    out = []
    for d in json.loads(raw or "[]"):
        rows = [Row(**r) for r in d.pop("rows")]
        out.append(MatchView(rows=rows, **d))
    return out
