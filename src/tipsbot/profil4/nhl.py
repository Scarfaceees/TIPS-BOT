"""NHL v Profilu 4: stejny postup jako u fotbalu (modely A–D, kontrolni vrstva, APPROVED BET / WATCH / NO BET,
kategorie A/B/C, ulozeni tipu a predikci, zaviraci kurzy a CLV, vyhodnoceni) – jen zdroje dat jsou hokejove.

Zdroje (nic navic se neplati):
- kurzy: posledni stazeni kurzoveho API hlavnim botem (cache `odds_cache:NHL`, kredity se tu nespotrebuji);
  cas kurzu = `last_update` kazde kancelare, ne cas stazeni;
- zapasy a jejich id: rozpis NHL API (api-web.nhle.com/v1/schedule, zdarma);
- vyhodnoceni: vysledky NHL API (nhl_settle: konecne skore vc. prodlouzeni a najezdu, skore po 60 min).

Modely (pravdepodobnost presne tohoto vyberu):
- A – model bota pro NHL (sila tymu z xG a strel MoneyPuck, brankari vc. rucne nastavenych v panelu, unava
      back-to-back; uroven golu ukotvena na trzni total, jak to dela hlavni bot);
- B – trh: Pinnacle bez marze, jinak median ferovych pravdepodobnosti aspon dvou kancelari (burzy bez provize ne);
- C – smes A a B s vahou modelu ze zpetneho testu bota pro NHL (SETTINGS.value.league_model_weight["NHL"]);
- D – konzervativni odhad: nizsi z B a C.
Trhy: MONEYLINE (vitez vc. prodlouzeni a najezdu), PUCKLINE ±1,5 a OU s polovicni linii (vc. prodlouzeni) –
presne ty, ktere kurzove API pro NHL dava a NHL API umi vyhodnotit. Cele linie (vraceni) se nepouzivaji.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import requests

from .. import analysis as bot_analysis
from . import integrity as IN
from . import mathcore
from .config import P4, Profil4Config
from .coverage import CoverageReport
from .decision import APPROVED, Candidate, Selection, decide, select
from .odds import CURRENT, OddsQuote
from .provider import AVAILABLE, NOT_YET, PARTIAL, SKIPPED, UNAVAILABLE, Fixture
from .store import iso, parse_dt

LEAGUE = "NHL"
LEAGUE_ID = 0  # NHL neni soutez API-Football; id zapasu = id NHL API (2025020345...), s fotbalem se neprekryva
SOURCE = "oddsapi"
EXCHANGES = ("betfair_ex_eu", "betfair_ex_uk", "matchbook")
SCHEDULE_URL = "https://api-web.nhle.com/v1/schedule/{day}"
REPORT_KEY, VIEW_KEY = "web_last_nhl_report", "web_last_nhl_view"
SELS = {"MONEYLINE": ("home", "away"), "PUCKLINE_1.5": ("home-1.5", "away+1.5"), "PUCKLINE_1.5R": ("home+1.5", "away-1.5")}
API_MARKET = {"MONEYLINE": "h2h", "PUCKLINE": "spreads", "OU": "totals"}


def selections(market: str) -> tuple[str, ...] | None:
    if market in SELS:
        return SELS[market]
    if market.startswith("OU_"):
        try:
            x = float(market[3:])
        except ValueError:
            return None
        return ("over", "under") if abs(x - round(x)) > 1e-9 else None  # jen polovicni linie (bez vraceni)
    return None


def api_market(market: str) -> str | None:
    return API_MARKET.get(market.split("_")[0])


def _t(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def fetch_schedule(day: date, timeout: int = 20) -> dict:
    r = requests.get(SCHEDULE_URL.format(day=day.isoformat()), headers={"User-Agent": "tipsbot"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def game_ids(payload: dict) -> list[dict]:
    """Zapasy z rozpisu NHL API: id, zkratky tymu a start (UTC)."""
    out = []
    for wk in payload.get("gameWeek", []):
        for g in wk.get("games", []):
            start = _t(g.get("startTimeUTC"))
            if g.get("id") and start:
                out.append({"id": int(g["id"]), "home": g.get("homeTeam", {}).get("abbrev"),
                            "away": g.get("awayTeam", {}).get("abbrev"), "start": start})
    return out


def match_game(games: list[dict], home: str, away: str, kickoff: datetime) -> dict | None:
    from ..names import nhl_abbr

    h, a = nhl_abbr(home), nhl_abbr(away)
    hits = [g for g in games if g["home"] == h and g["away"] == a and abs(g["start"] - kickoff) <= timedelta(hours=6)]
    return min(hits, key=lambda g: abs(g["start"] - kickoff)) if hits else None


def quotes_from_event(ev: dict, fixture_id: int, now: datetime) -> list[OddsQuote]:
    """Surova udalost kurzoveho API -> kurzy Profilu 4; cas kurzu = posledni zmena trhu u kancelare."""
    from .. import oddsapi

    home, away = ev.get("home_team"), ev.get("away_team")
    kickoff = _t(ev.get("commence_time"))
    match = f"{home} - {away}"
    out = []
    for bm in ev.get("bookmakers", []):
        one = {**ev, "bookmakers": [bm]}
        stamps = {m.get("key"): _t(m.get("last_update") or bm.get("last_update")) for m in bm.get("markets", [])}
        for parsed in oddsapi.parse_events([one], LEAGUE):
            for mk, mo in parsed.markets.items():
                if selections(mk) != mo.selections:
                    continue
                when = stamps.get(api_market(mk)) or _t(bm.get("last_update"))
                for sel, price in mo.books.get(bm.get("key"), {}).items():
                    out.append(OddsQuote(fixture_id, match, bm.get("key", ""), mk, sel, float(price), now, SOURCE,
                                         kickoff, when, rules="vč. prodloužení a nájezdů", note="kurzové API"))
    return out


# ---------------------------------------------------------------------------
# Modely
# ---------------------------------------------------------------------------

def market_b(quotes: list[OddsQuote]) -> dict[str, tuple[float, str]]:
    """Ferova pravdepodobnost trhu {trh:vyber: (p, zdroj)}: Pinnacle bez marze, jinak median >= 2 kancelari."""
    by: dict[tuple[str, str, datetime], dict[str, float]] = {}
    for q in quotes:
        if q.bookmaker not in EXCHANGES:
            by.setdefault((q.market, q.bookmaker, q.stamp), {})[q.selection] = q.odds
    fair: dict[str, dict[str, list[float]]] = {}
    pin: dict[str, dict[str, float]] = {}
    for (mk, book, _), prices in sorted(by.items(), key=lambda x: x[0][2]):  # novejsi cas prepise starsi
        sels = selections(mk)
        if not sels or not all(s in prices for s in sels):
            continue
        _, probs = mathcore.devig([prices[s] for s in sels])
        if book == "pinnacle":
            pin[mk] = dict(zip(sels, probs))
        for s, p in zip(sels, probs):
            fair.setdefault(mk, {}).setdefault(s, []).append(p)
    out: dict[str, tuple[float, str]] = {}
    for mk, per in fair.items():
        for s, ps in per.items():
            if mk in pin:
                out[f"{mk}:{s}"] = (pin[mk][s], "Pinnacle bez marže")
            elif len(ps) >= 2:
                out[f"{mk}:{s}"] = (statistics.median(ps), f"medián {len(ps)} kanc. bez marže")
    return out


def _pairing(q: OddsQuote, quotes: list[OddsQuote], cfg) -> tuple[list[str], list[str]]:
    """Protistrana u stejne kancelare ze stejne aktualizace a rozumna marze (jako integrity.pairing)."""
    sels = selections(q.market) or ()
    per = {x.selection: x.odds for x in quotes if x.bookmaker == q.bookmaker and x.market == q.market and x.stamp == q.stamp}
    per[q.selection] = q.odds
    missing = [s for s in sels if s not in per]
    if missing:
        return [], [f"{q.bookmaker} nenabízí protistranu ({', '.join(missing)}) – spárování trhu nelze ověřit"]
    over = sum(1.0 / per[s] for s in sels) - 1.0
    if over < cfg.min_book_overround:
        return [f"kurzy {q.bookmaker} na trhu {q.market} dávají dohromady {(over + 1) * 100:.1f} % – nejsou jeden trh"], []
    if over > cfg.max_book_overround:
        return [], [f"marže {q.bookmaker} na trhu {q.market} je {over:.0%} – neúplný nebo chybný trh"]
    return [], []


@dataclass
class NHLModels:
    """Kompatibilni s ModelSet tam, kde to potrebuje ukladani (history.record): 1X2 predikce u NHL nejsou."""
    a_note: str
    b_note: str
    c_note: str
    weight: float

    class _Off:
        available = False

    c = _Off()


@dataclass
class NHLAnalysis:
    fixture: Fixture
    created: datetime
    coverage: CoverageReport
    models: NHLModels | None
    candidates: list[Candidate]
    selection: Selection
    quotes: list[OddsQuote]
    notes: list[str] = field(default_factory=list)
    goalies: str = ""


def _robustness(p: dict, odds: float) -> tuple[float, list[tuple[str, float]]]:
    """Podil scenaru s kladnym EV (x100): model A, trh B, smes C, C o 2 a 4 p.b. horsi, kurz o 2 % nizsi."""
    pc = p["C"]
    sc = [("model A", p["A"]), ("trh B", p["B"]), ("smes C", pc), ("C −2 p.b.", pc - 0.02), ("C −4 p.b.", pc - 0.04)]
    out = [(n, x * odds - 1.0) for n, x in sc if x is not None]
    out.append(("kurz −2 %", pc * odds * 0.98 - 1.0))
    return 100.0 * sum(ev > 0 for _, ev in out) / len(out), out


def candidates(fx: Fixture, model: dict | None, quotes: list[OddsQuote], now: datetime, cfg: Profil4Config,
               dq: float, weight: float, goalie_known: bool) -> list[Candidate]:
    b = market_b(quotes)
    best: dict[str, OddsQuote] = {}
    for q in quotes:
        if q.bookmaker in EXCHANGES or selections(q.market) is None or q.status(now, cfg.odds) == "HISTORICKÝ":
            continue
        cur = best.get(q.key)
        fresh = q.status(now, cfg.odds) == CURRENT
        if cur is None or (fresh, q.odds) > (cur.status(now, cfg.odds) == CURRENT, cur.odds):
            best[q.key] = q
    out = []
    home, _, away = fx.name.partition(" - ")
    for key, q in best.items():
        pa = model.get(key) if model else None
        pb = b.get(key, (None, ""))[0]
        if pa is None and pb is None:
            continue
        pc = weight * pa + (1 - weight) * pb if pa is not None and pb is not None else (pa if pa is not None else pb)
        pd = min(x for x in (pb, pc) if x is not None)
        p = {"A": pa, "B": pb, "C": pc, "D": pd}
        rob, stress = _robustness(p, q.odds)
        ev_c, ev_d = pc * q.odds - 1.0, pd * q.odds - 1.0
        hard = [] if 0.0 < pc < 1.0 and q.odds > 1.0 else [f"neplatná pravděpodobnost {pc} nebo kurz {q.odds}"]
        ph, ps = _pairing(q, quotes, cfg.decision)
        c = Candidate(
            fixture_id=fx.id, match=fx.name, market=q.market, selection=q.selection,
            label=bot_analysis.label(q.market, q.selection, home, away), quote=q, quote_status=q.status(now, cfg.odds), p=p,
            p_win_c=pc, p_push_c=0.0, fair_odds_c=1.0 / pc, fair_odds_d=1.0 / pd, ev_c=ev_c, ev_d=ev_d,
            min_odds=(1.0 + cfg.decision.ev_min) / pc, robustness=rob, data_quality=dq,
            models_complete=pa is not None and pb is not None, lineups="confirmed" if goalie_known else "pending",
            stress=stress, robustness_parts={})
        c.validation = (hard + ph, ps)
        c.p_market = pb
        c.ev_market = pb * q.odds - 1.0 if pb is not None else None
        c.market_src = b.get(key, (None, "trh chybí"))[1]
        c.market_covered = pb is not None
        c.confirmed_by = IN.confirmations(q, [x for x in quotes if x.bookmaker not in EXCHANGES], now, cfg.odds,
                                          cfg.decision.confirm_tolerance)
        decide(c, cfg.decision)
        c.risks = [("brankáři nejsou potvrzení (NHL API je předem nedává; ručně v Nastavení)"
                    if r == "sestavy nejsou potvrzené" else r) for r in c.risks]
        out.append(c)
    return out


def analyse(fx: Fixture, model: dict | None, quotes: list[OddsQuote], now: datetime, cfg: Profil4Config,
            weight: float, injuries: bool, goalie_note: str, goalie_known: bool) -> NHLAnalysis:
    cov = CoverageReport(fx.id)
    cov.set("fixture", AVAILABLE, now, "rozpis NHL API")
    cov.set("model_data", AVAILABLE if model else UNAVAILABLE, now, "síla týmů MoneyPuck (xG, střely)")
    fresh = [q for q in quotes if q.status(now, cfg.odds) == CURRENT]
    cov.set("odds", AVAILABLE if fresh else PARTIAL if quotes else UNAVAILABLE,
            max((q.stamp for q in quotes), default=None), f"{len({q.bookmaker for q in quotes})} kanceláří (kurzové API)")
    cov.set("injuries", AVAILABLE if injuries else UNAVAILABLE, now, "ESPN" if injuries else "zdroj nedostupný")
    cov.set("lineups", AVAILABLE if goalie_known else NOT_YET, None, goalie_note)
    cov.set("team_stats", AVAILABLE if model else UNAVAILABLE, now, "MoneyPuck")
    cov.set("standings", PARTIAL, None, "tabulka je v rozboru hlavního bota")
    cov.set("h2h", SKIPPED, None, "u NHL se nepoužívá")
    cov.set("predictions", SKIPPED, None, "u NHL se nepoužívá")
    cands = candidates(fx, model, quotes, now, cfg, cov.quality(), weight, goalie_known)
    notes = [] if model else ["model A není k dispozici (tým chybí v datech MoneyPuck) – jen trh"]
    ms = NHLModels("model bota NHL (xG, brankáři, únava)", "Pinnacle / medián kanceláří bez marže",
                   f"{weight:.0%} model A, {1 - weight:.0%} trh B (váha ze zpětného testu bota pro NHL)", weight)
    return NHLAnalysis(fx, now, cov, ms, cands, select(cands, cfg.decision), quotes, notes, goalie_note)


# ---------------------------------------------------------------------------
# Sken (spousti automat po stazeni kurzu, kazdych 6 h a rano)
# ---------------------------------------------------------------------------

@dataclass
class NHLScan:
    when: datetime
    analyses: list[NHLAnalysis]
    notes: list[str]
    odds_time: datetime | None


def run(ledger, store, now: datetime, cfg: Profil4Config = P4, schedule=fetch_schedule, provider=None,
        injuries: bool | None = None) -> NHLScan:
    """Rozbor vsech zapasu NHL na 48 h z ulozenych kurzu; ulozi tipy a predikce do Profilu 4."""
    from .. import oddsapi
    from ..config import SETTINGS
    from ..names import nhl_abbr
    from . import history, odds as O

    notes: list[str] = []
    cached = ledger.odds_cache(LEAGUE)
    if not cached:
        return NHLScan(now, [], ["NHL: žádné stažené kurzy (hlavní bot je ještě nenačetl)"], None)
    odds_time = cached[0].replace(tzinfo=timezone.utc)
    hi = now + timedelta(hours=SETTINGS.timing.analysis_hours)
    raw = [ev for ev in cached[1] if (k := _t(ev.get("commence_time"))) and now < k <= hi]
    if not raw:
        return NHLScan(now, [], ["NHL: v příštích 48 h není žádný zápas s kurzy"], odds_time)
    games: list[dict] = []
    for d in sorted({(_t(ev["commence_time"]) - timedelta(hours=10)).date() for ev in raw}):
        try:
            games += game_ids(schedule(d))
        except Exception as e:  # noqa: BLE001 - bez id zapasu nejde ukladat ani vyhodnotit
            notes.append(f"NHL rozpis {d:%d.%m.}: chyba ({e})")
    if provider is None:
        from ..pipeline import ModelProvider

        provider = ModelProvider(LEAGUE, now.date(), SETTINGS, goalie_overrides=ledger.goalie_overrides())
    if provider.note:
        notes.append(f"NHL: {provider.note}")
    if injuries is None:
        from ..context import fetch_nhl_injuries

        injuries = bool(fetch_nhl_injuries())
    overrides = ledger.goalie_overrides()
    weight = float(SETTINGS.value.league_model_weight.get(LEAGUE, cfg.model.model_weight))
    events = {e.match_id: e for e in oddsapi.parse_events(raw, LEAGUE)}
    out = []
    for ev in raw:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        kickoff = _t(ev["commence_time"])
        g = match_game(games, home, away, kickoff)
        if g is None:
            notes.append(f"NHL {home} - {away}: zápas v rozpisu NHL API nenalezen, přeskočen")
            continue
        fx = Fixture(g["id"], LEAGUE_ID, LEAGUE, 0, kickoff, "NS", 0, home, 0, away)
        quotes = quotes_from_event(ev, fx.id, now)
        O.save(store, quotes)
        parsed = events.get(f"oddsapi:{ev.get('id')}")
        model = provider.probabilities(parsed) if parsed is not None else None
        h, a = nhl_abbr(home), nhl_abbr(away)
        known = bool(h in overrides and a in overrides)
        gnote = ("ručně: " + ", ".join(f"{t} {overrides[t]}" for t in (h, a) if t in overrides)) if any(
            t in overrides for t in (h, a)) else "podle podílu odchytaných zápasů"
        an = analyse(fx, model, quotes, now, cfg, weight, injuries, gnote, known)
        history.record(store, an, cfg)  # tipy i predikce (record_predictions) pro kalibraci
        out.append(an)
    return NHLScan(now, out, notes, odds_time)


# ---------------------------------------------------------------------------
# Kurzy pred vykopem a vyhodnoceni
# ---------------------------------------------------------------------------

def observe(st, r: dict, now: datetime, cfg: Profil4Config, ledger_factory=None, fetch=None) -> str:
    """Bod pred vykopem u zapasu NHL: kurzy z posledniho stazeni hlavnim botem (zdarma); u SCHVALENEHO tipu
    v poslednim bodu navic kurzy udalosti z kurzoveho API (jen trhy tipu, pod rezervou kreditu nic)."""
    from ..config import SETTINGS
    from ..storage import Ledger
    from . import odds as O

    if ledger_factory is None:
        from ..storage import make_session_factory

        ledger_factory = make_session_factory()
    ko = parse_dt(r["kickoff"])
    home, _, away = (r["match"] or "").partition(" - ")
    quotes, note, status = [], "", AVAILABLE
    with ledger_factory() as session:
        led = Ledger(session)
        cached = led.odds_cache(LEAGUE)
        ev = next((e for e in (cached[1] if cached else []) if e.get("home_team") == home and e.get("away_team") == away
                   and (k := _t(e.get("commence_time"))) and abs(k - ko) <= timedelta(hours=6)), None)
        if ev is not None:
            quotes = quotes_from_event(ev, r["fixture_id"], now)
        last = r["approved"] and min(r["slots"]) == min(cfg.odds.closing_slots_min)
        latest = max((q.stamp for q in quotes), default=None)
        stale = latest is None or (ko - latest).total_seconds() / 60 > cfg.odds.closing_max_source_age_min
        if last and stale:
            tips = st.query("SELECT DISTINCT market FROM tips WHERE fixture_id=? AND decision=? AND result IS NULL",
                            (r["fixture_id"], APPROVED))
            markets = sorted({m for m in (api_market(t["market"]) for t in tips) if m})
            key, q = led.api_key(), led.odds_api_quota()
            left = q.get("remaining")
            if not key or ev is None or not markets:
                note = "kurzy události nelze stáhnout (chybí klíč, zápas nebo trh)"
            elif left is not None and left - len(markets) < SETTINGS.timing.auto_credit_reserve:
                status, note = "SKIPPED_BUDGET", f"zbývá {left} kreditů kurzového API (rezerva {SETTINGS.timing.auto_credit_reserve})"
            else:
                got, quota, note = (fetch or _fetch_event)(SETTINGS.odds_api.sport_keys[LEAGUE], key, ev["id"], markets,
                                                           SETTINGS.odds_api.base_url)
                led.set_odds_api_quota(quota.get("remaining"), quota.get("used"), now.replace(tzinfo=None))
                session.commit()
                if got is not None:
                    quotes = quotes_from_event(got, r["fixture_id"], now)
        elif not quotes:
            note = "zápas v uložených kurzech není"
    n = O.save(st, quotes)
    latest = max((q.stamp for q in quotes), default=None)
    for s in r["slots"]:
        st.execute("INSERT INTO odds_observations(fixture_id, kickoff, slot_min, observed_at, status, n_quotes, "
                   "latest_update, note) VALUES(?,?,?,?,?,?,?,?)",
                   (r["fixture_id"], iso(ko), s, iso(now), status, len(quotes), iso(latest), note))
    age = f", poslední změna kurzu {(ko - latest).total_seconds() / 60:.0f} min před začátkem" if latest else ""
    return f"Profil 4 – NHL kurzy před zápasem {r['match']} (bod {min(r['slots'])} min): {len(quotes)} kurzů, {n} nových{age}" + (
        f" – {note}" if note else "")


def _fetch_event(sport: str, key: str, event_id: str, markets: list[str], base_url: str, timeout: int = 20):
    r = requests.get(f"{base_url}/sports/{sport}/events/{event_id}/odds",
                     params={"apiKey": key, "regions": "eu", "markets": ",".join(markets), "oddsFormat": "decimal",
                             "dateFormat": "iso"}, timeout=timeout)

    def num(h: str) -> int | None:
        try:
            return int(float(r.headers.get(h, "")))
        except ValueError:
            return None

    quota = {"remaining": num("x-requests-remaining"), "used": num("x-requests-used")}
    if r.status_code != 200:
        return None, quota, f"kurzové API vrátilo {r.status_code}"
    return r.json(), quota, ""


def settle(store, now: datetime, fetch=None) -> list[str]:
    """Vyhodnoti tipy a predikce NHL z NHL API (zapas zacal pred vic nez 3,5 h, nejvys 10 dni zpet)."""
    from .. import nhl_settle
    from . import history

    fetch = fetch or nhl_settle.fetch_scores
    lo, hi = iso(now - timedelta(days=10)), iso(now - timedelta(hours=3, minutes=30))
    rows = store.query("SELECT DISTINCT fixture_id, kickoff FROM tips WHERE league=? AND result IS NULL AND kickoff > ? AND kickoff < ? "
                       "UNION SELECT DISTINCT fixture_id, kickoff FROM predictions WHERE league=? AND result IS NULL "
                       "AND kickoff > ? AND kickoff < ?", (LEAGUE, lo, hi) * 2)
    if not rows:
        return []
    days = sorted({(parse_dt(r["kickoff"]) - timedelta(hours=d)).date() for r in rows for d in (0, 12)})
    games, msgs = {}, []
    for d in days:
        try:
            for g in nhl_settle.parse_scores(fetch(d)):
                games[g["id"]] = g
        except Exception as e:  # noqa: BLE001
            msgs.append(f"výsledky NHL {d:%d.%m.}: chyba ({e})")
    for fid in {r["fixture_id"] for r in rows}:
        g = games.get(fid)
        if g is None:
            continue
        ko = g["start"].replace(tzinfo=timezone.utc) if g["start"] else None
        score = f"{g['final'][0]}:{g['final'][1]}" + ("" if g["end"] == "REG" else f" ({g['end']})")
        for t in store.query("SELECT * FROM tips WHERE fixture_id=? AND result IS NULL", (fid,)):
            out = nhl_settle.grade(t["market"], t["selection"], g)
            if out is not None:
                msgs.append(history.settle_tip(store, t, out, score, ko or parse_dt(t["kickoff"]), now))
        for p in store.query("SELECT id, market, selection FROM predictions WHERE fixture_id=? AND result IS NULL", (fid,)):
            out = nhl_settle.grade(p["market"], p["selection"], g)
            if out is not None:
                store.execute("UPDATE predictions SET result=?, outcome=?, score=?, settled_at=? WHERE id=?",
                              (out, history.outcome_value(out), score, iso(now), p["id"]))
    return msgs


# ---------------------------------------------------------------------------
# Vystup
# ---------------------------------------------------------------------------

def report(scan: NHLScan) -> str:
    from .report import local

    L = [f"PROFIL 4 – NHL ({local(scan.when)})", ""]
    if scan.odds_time:
        L.append(f"Kurzy: poslední stažení hlavním botem {local(scan.odds_time)} (kredity se tu nespotřebovaly).")
    for n in scan.notes:
        L.append(f"! {n}")
    approved = [c for an in scan.analyses for c in an.selection.approved]
    L += [f"Zápasů: {len(scan.analyses)} · schválených sázek: {len(approved)}", ""]
    for an in scan.analyses:
        s = an.selection
        L.append(f"{local(an.fixture.kickoff)}  {an.fixture.name} – {s.verdict}")
        for c in s.approved[:3]:
            L.append(f"   ✓ {c.label} @ {c.odds:.2f} ({c.quote.bookmaker}) EV {c.ev_c:+.1%}, konzervativně {c.ev_d:+.1%}, "
                     f"robustnost {c.robustness:.0f}")
        if not s.approved and s.best_available is not None:
            b = s.best_available
            L.append(f"   nejlepší dostupné: {b.label} @ {b.odds:.2f} ({b.quote.bookmaker}) EV {b.ev_c:+.1%} – "
                     + "; ".join(b.reasons[:3]))
        L.append(f"   brankáři: {an.goalies}; kvalita dat {an.coverage.quality():.0%}")
    return "\n".join(L)


def view(scan: NHLScan) -> dict:
    from .report import local
    from . import view as V

    rows = []
    for an in scan.analyses:
        s = an.selection
        best = s.approved[0] if s.approved else s.best_available
        rows.append({"fixture_id": an.fixture.id, "kickoff": local(an.fixture.kickoff), "name": an.fixture.name,
                     "verdict": s.verdict, "explanation": s.explanation, "goalies": an.goalies,
                     "quality": round(100 * an.coverage.quality()), "best": V.candidate(best, scan.when) if best else None,
                     "approved": [V.candidate(c, scan.when) for c in s.approved[:5]],
                     "markets": V.markets(an.candidates, an.quotes, scan.when)})
    return {"when": local(scan.when), "odds_time": local(scan.odds_time) if scan.odds_time else "",
            "notes": scan.notes, "matches": rows, "approved": sum(len(r["approved"]) for r in rows)}


def run_and_store(ledger, now: datetime | None = None, cfg: Profil4Config = P4, **kw) -> list[str]:
    """Sken NHL z automatu: ulozi tipy/predikce a vysledek pro panel (zalozka Profil 4)."""
    from .store import open_store, utcnow

    if not cfg.enabled:
        return []
    now = now or utcnow()
    store = open_store(cfg)
    try:
        scan = run(ledger, store, now, cfg, **kw)
        store.set(REPORT_KEY, report(scan))
        store.set(VIEW_KEY, json.dumps(view(scan), ensure_ascii=False, default=str))
    finally:
        store.close()
    n = sum(len(an.selection.approved) for an in scan.analyses)
    return [f"Profil 4 – NHL: {len(scan.analyses)} zápasů, schválených sázek {n}"] + scan.notes[:3]
