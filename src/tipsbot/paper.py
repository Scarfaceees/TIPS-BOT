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
from datetime import datetime, timedelta

import pandas as pd

from . import marketsim
from .config import SETTINGS, Settings
from .settlement import settle_bet

KEY = "paper_picks"
KEEP = 2000
MIN_EV = 0.05  # od teto hrany (podle modelu) se vyber sleduje
SELECTIONS = {"1X2": ("1", "X", "2"), "BTTS": ("yes", "no"), "DC": ("1X", "12", "X2"), "DNB": ("home", "away"),
              "MONEYLINE": ("home", "away"), "PUCKLINE_1.5": ("home-1.5", "away+1.5"),
              "PUCKLINE_1.5R": ("home+1.5", "away-1.5")}


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
    method: str = "edge"  # edge = nejvyssi cekany zisk podle modelu bota; sim = model trhu se simulaci (marketsim);
    # tips = kopie oficialnich tipu bota (mirror_tips), profil sam nic nevybira
    props: bool = False  # True = jen rohy a karty; ostatni profily je nevidi (vlastni sledovani, jine trhy)


PROPS = ("ROHY_", "KARTY_")
# Rohy a karty: sance = smes modelu a trhu (Pinnacle, jinak median kancelari bez marze). Model rohu a karet je ve
# zpetnem testu (2 656 zapasu 2025/26) jen o 1-4 % lepsi nez ligovy prumer, trh zna rozhodci, derby a sestavy –
# samotny model vybiral hlavne zapasy, kde se mylil nejvic (11/2026: 2 z 11, slibovany zisk +24 %).
PROPS_MODEL_WEIGHT = 0.25


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
    Profile("tipy", "Tipy bota",
            "každý oficiální tip bota (TIP / VSADIT), vsazený i nevsazený, se zapíše s kurzem a šancí v okamžiku tipu "
            "a vyhodnotí se – i když ho pak bot zruší (pokles kurzu) nebo vyprší; tak je vidět skutečná úspěšnost tipů",
            method="tips"),
    Profile("kombi", "Kombi tikety",
            "každý kombinovaný tiket, který bot navrhne v Přehledu (doporučený i další kombinace), 2–3 sázky z různých "
            "zápasů, celkový kurz = součin kurzů; vyhodnotí se po všech zápasech – i když ho bot před výkopem přestal "
            "navrhovat", method="kombi"),
    Profile("rohy", "Rohy a karty",
            "jen rohy a karty: šance = 25 % model týmů (a u karet rozhodčího) + 75 % trh (Pinnacle bez marže, jinak "
            "medián kanceláří z API-Football); od 21. 11. 2026, dřív jen model. Proti kurzům kanceláří (Pinnacle, "
            "Bet365, Betano…); u zápasu výběr s nejvyšším čekaným ziskem od 5 %; "
            "jen 6 lig (Anglie, Španělsko, Německo, Itálie, Francie, Portugalsko)", props=True),
)
DEFAULT = PROFILES[0].key


def profile(key: str | None) -> Profile:
    return next((pr for pr in PROFILES if pr.key == key), PROFILES[0])


def selections(market: str) -> tuple[str, ...] | None:
    if market.startswith(("OU_", "OU60_", "TTH_", "TTA_", "ROHY_", "KARTY_")):
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
    legs: list = field(default_factory=list)  # kombi tiket: nohy s vlastnim vysledkem (kombi.py)

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


ANALYSES_KEY = "paper_sim_analyses"  # rozbory Profilu 3 u kazdeho zapasu, i kdyz sazku nenasel


def load_analyses(ledger) -> dict[str, dict]:
    raw = ledger._get(ANALYSES_KEY, "")
    return json.loads(raw) if raw else {}


def _save_analysis(ledger, league: str, ev, info: dict, now: datetime) -> None:
    data = {k: v for k, v in load_analyses(ledger).items()
            if datetime.fromisoformat(v["kickoff"]) > now - timedelta(days=2)}  # odehrane se po 2 dnech mazou
    data[ev.match_id] = {**info, "league": league, "name": ev.name, "kickoff": ev.kickoff.isoformat(), "t": now.isoformat()}
    ledger._set(ANALYSES_KEY, json.dumps(data, ensure_ascii=False))
    # panel drzi rozbory jen 2 dny po zapase, archiv navzdy (bez casu rozboru, at se stejny rozbor neuklada znovu)
    keep = {k: v for k, v in data[ev.match_id].items() if k != "t"}
    ledger.archive("p3", ev.match_id, json.dumps(keep, ensure_ascii=False, default=str), now)


def _sim(pr: Profile, league: str, ev, model: dict | None, entries: list, now: datetime, ledger=None) -> Pick | None:
    """Profil se simulaci trhu (marketsim). Rozbor se ulozi i bez sazky (panel: proc sazku nenasel)."""
    cfg = marketsim.SimConfig(min_odds=pr.min_odds or marketsim.DEFAULT.min_odds,
                              max_odds=pr.max_odds or marketsim.DEFAULT.max_odds)
    sp, info = marketsim.analyse(ev.match_id, model, entries, cfg)
    if ledger is not None:
        _save_analysis(ledger, league, ev, info, now)
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


def props_market_p(ev, market: str, sel: str) -> tuple[float, str] | None:
    """Ferova sance trhu na rohy/karty: Pinnacle bez marze, jinak median ostatnich kancelari (aspon 2)."""
    from statistics import median

    from .devig import DevigError, devig_power

    mk = getattr(ev, "markets", {}).get(market)
    if mk is None or sel not in mk.selections:
        return None
    i = mk.selections.index(sel)
    fair = []
    for book, prices in mk.books.items():
        try:
            f = devig_power([prices[s] for s in mk.selections])[i]
        except (DevigError, KeyError):
            continue
        if book == "pinnacle":
            return f, "Pinnacle"
        fair.append(f)
    return (median(fair), f"medián {len(fair)} kanc.") if len(fair) >= 2 else None


def _best(pr: Profile, league: str, ev, model: dict, entries: list, now: datetime, settings: Settings,
          ledger=None, extra: list | None = None) -> Pick | None:
    """Vyber s nejvyssim cekanym ziskem podle pravidel profilu. `extra` = dalsi trhy (median API-Football) jen pro
    simulaci trhu – ta potrebuje co nejvic linii, ostatni profily zustavaji u svych kurzu."""
    entries = [e for e in entries if e.market.startswith(PROPS) == pr.props]
    if pr.method == "sim":
        have = {e.market for e in entries}
        entries = entries + [e for e in extra or [] if e.market not in have and not e.market.startswith(PROPS)]
        return _sim(pr, league, ev, model, entries, now, ledger) if entries else None
    if not entries:
        return None
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
            detail = ""
            if pr.props:  # rohy a karty: vazeno k trhu; bez kurzu trhu se nesazi
                mkt = props_market_p(ev, e.market, sel)
                if mkt is None:
                    continue
                pm = p
                p = PROPS_MODEL_WEIGHT * pm + (1 - PROPS_MODEL_WEIGHT) * mkt[0]
                detail = (f"Šance {p * 100:.0f} % = {PROPS_MODEL_WEIGHT:.0%} model ({pm * 100:.0f} %) + "
                          f"{1 - PROPS_MODEL_WEIGHT:.0%} trh {mkt[1]} ({mkt[0] * 100:.0f} %).")
            edge = p * odds - 1.0
            if edge >= pr.min_ev and (best is None or edge > best[0]):
                flags = ["čekaný zisk nad 15 % je podezřelý, ověř ručně"] if edge > 0.15 else []
                best = (edge, Pick(
                    id=uuid.uuid4().hex[:8], league=league, match_id=ev.match_id, name=ev.name,
                    day=ev.kickoff.date().isoformat(), kickoff=ev.kickoff.isoformat(), market=e.market,
                    selection=sel, odds=odds, book=e.book, p_model=round(p, 4), source="model",
                    created=now.isoformat(), flags=flags, profile=pr.key, detail=detail,
                ))
    return best[1] if best else None


def track(ledger, league: str, offers: list, now: datetime, settings: Settings = SETTINGS,
          extra: dict[str, list] | None = None) -> int:
    """`offers`: [(udalost, model {trh:vyber: p}, [ceske zaznamy])]. Kazdy aktivni profil vybere u zapasu
    nejvyssi cekany zisk podle svych pravidel; pred vykopem se vyber jen aktualizuje, po vykopu se nemeni.
    `extra`: {match_id: dalsi trhy} pro simulaci trhu (Profil 3)."""
    picks = load(ledger)
    by_match = {(p.profile, p.match_id): p for p in picks if p.source == "model"}
    n = 0
    for ev, model, entries in offers:
        if ev.kickoff <= now:
            continue
        for pr in PROFILES:
            if not pr.active or pr.method in ("tips", "kombi"):
                continue
            if league == "NHL" and (pr.method != "edge" or pr.props):
                continue  # scenare golu, simulace trhu a rohy/karty jsou jen fotbalove
            new = _best(pr, league, ev, model, entries, now, settings, ledger, (extra or {}).get(ev.match_id))
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


TIPS_PROFILE = "tipy"


def mirror_tips(ledger, tips, now: datetime) -> int:
    """Oficialni tipy bota do Sledovani (profil "tipy"): kazdy tip jednou, s kurzem, kancelari a sanci v okamziku
    tipu. Pred zacatkem zapasu se zaznam aktualizuje (obnoveny tip), zruseni / vyprseni se jen poznamena –
    vyhodnoti se vzdy, aby statistika nebyla vylepsena vynechanim tipu, ktere bot pozdeji stahl."""
    picks = load(ledger)
    have = {(p.match_id, p.market, p.selection) for p in picks if p.profile == TIPS_PROFILE}
    n = 0
    for t in tips:
        pid = f"tip{t.id}"
        old = next((p for p in picks if p.id == pid), None)
        ko = t.kickoff if t.kickoff.tzinfo is None else t.kickoff.replace(tzinfo=None)
        flags = [f"tip {t.status}" + (f": {t.cancel_reason}" if t.cancel_reason else "")] if t.status not in ("open", "placed") else []
        if t.needs_review:
            flags.append("tip označen ke kontrole kurzu")
        if old is not None:
            if old.result is None and ko > now:
                old.odds, old.book, old.p_model, old.flags = t.raw_odds, t.bookmaker, round(t.model_probability, 4), flags
            elif old.result is None and flags and flags != old.flags:
                old.flags = flags
            continue
        if (t.match_id, t.market, t.selection) in have:
            continue  # stejny vyber u zapasu uz sledovan (novy tip po zruseni)
        picks.append(pick := Pick(
            id=pid, league=t.league, match_id=t.match_id, name=t.match_name, day=ko.date().isoformat(),
            kickoff=ko.isoformat(), market=t.market, selection=t.selection, odds=t.raw_odds, book=t.bookmaker,
            p_model=round(t.model_probability, 4), source="tip", created=(t.timestamp or now).isoformat(), flags=flags,
            profile=TIPS_PROFILE, detail=f"EV {t.ev_percentage:+.1f} % (práh {t.ev_threshold:.1f} %), férový kurz "
                                         f"{t.devigged_sharp_odds:.2f}, sázet od {t.min_odds:.2f}"))
        have.add((pick.match_id, pick.market, pick.selection))
        n += 1
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
        if p.result is not None or p.legs:  # kombi tikety vyhodnocuje kombi.settle po nohach
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
