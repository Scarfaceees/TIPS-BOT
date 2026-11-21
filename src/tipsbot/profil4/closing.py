"""Zaviraci kurzy a prubezne vyhodnoceni Profilu 4 bez rucniho skenu.

Prubeh (vlakno automatu bota, kontrola kazdou minutu):
- zapasy, ke kterym Profil 4 ulozil tip nebo predikci, se stahnou v pevnych bodech pred vykopem
  (`closing_slots_min`, vychozi 75, 30 a 10 min); kazdy bod nejvys jednou; zmeskane body se dozenou jednim
  stazenim (napr. kdyz automat neběžel);
- poradi: zapasy se SCHVALENYM tipem (priorita MEDIUM), pak ostatni tipy a predikce (LOW). Rozpocet API klienta
  pri setreni kvoty pozadavek odmitne – i to se zapise;
- kazdy pokus se uklada do `odds_observations` (cas pozorovani, posledni aktualizace u poskytovatele, stav,
  duvod). Z toho se pak urcuje, zda je posledni kurz opravdu zaviraci (history._closing).
- stahuje se mimo cache (`force`), ale API-Football samo aktualizuje predzapasove kurzy jen obcas (podle
  dokumentace zhruba po 3 h); cas aktualizace u poskytovatele (`update`) se uklada a pouziva, ne cas stazeni.
Bez ulozenych tipu a predikci nedela nic a nespotrebuje zadny pozadavek.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from . import odds as O
from .config import P4, Profil4Config
from .provider import Fixture
from .store import iso, open_store, parse_dt, utcnow

SETTLE_EVERY = timedelta(hours=3)


def _done_slots(store, fid: int) -> set[int]:
    return {r["slot_min"] for r in store.query("SELECT slot_min FROM odds_observations WHERE fixture_id=?", (fid,))}


def due(store, now: datetime, cfg: Profil4Config) -> tuple[list[dict], bool]:
    """(zapasy ke stazeni serazene podle priority, je potreba vyhodnotit skoncene zapasy?)."""
    slots = sorted(cfg.odds.closing_slots_min, reverse=True)
    hi = now + timedelta(minutes=max(slots))
    rows = store.query(
        "SELECT fixture_id, match, kickoff, league_id, league, MAX(decision='APPROVED BET') approved FROM tips "
        "WHERE result IS NULL AND kickoff > ? AND kickoff <= ? GROUP BY fixture_id "
        "UNION SELECT fixture_id, match, kickoff, league_id, league, 0 FROM predictions WHERE result IS NULL "
        "AND kickoff > ? AND kickoff <= ? GROUP BY fixture_id", (iso(now), iso(hi)) * 2)
    best: dict[int, dict] = {}
    for r in rows:
        r = dict(r)
        if r["fixture_id"] not in best or r["approved"] > best[r["fixture_id"]]["approved"]:
            best[r["fixture_id"]] = r
    out = []
    for r in best.values():
        ko = parse_dt(r["kickoff"])
        reached = [s for s in slots if now >= ko - timedelta(minutes=s)]
        pending = [s for s in reached if s not in _done_slots(store, r["fixture_id"])]
        if pending:
            out.append({**r, "slots": pending})
    out.sort(key=lambda r: (-r["approved"], r["kickoff"]))
    past = store.query("SELECT 1 FROM tips WHERE result IS NULL AND kickoff < ? UNION "
                       "SELECT 1 FROM predictions WHERE result IS NULL AND kickoff < ? LIMIT 1",
                       (iso(now - timedelta(hours=2, minutes=30)),) * 2)
    last_settle = parse_dt(store.get("auto_settle_at") or None)
    return out, bool(past) and (last_settle is None or now - last_settle >= SETTLE_EVERY)


def observe(st, prov, r: dict, now: datetime, cfg: Profil4Config = P4, foreign_opts: dict | None = None) -> str:
    """Jedno stazeni kurzu zapasu; zapise pozorovani (i neuspesne, s duvodem) pro vsechny dosazene body."""
    if r["league"] == "NHL":  # hokej: kurzy z kurzoveho API, ne z API-Football
        from . import nhl

        try:
            return nhl.observe(st, r, now, cfg, **(foreign_opts or {}))
        except Exception as e:  # noqa: BLE001 - chyba requests obsahuje adresu s klicem kurzoveho API
            from ..oddsapi import redact

            return redact(f"Profil 4 – NHL kurzy před zápasem {r['match']}: chyba ({e})")
    ko = parse_dt(r["kickoff"])
    home, _, away = (r["match"] or "").partition(" - ")
    fx = Fixture(r["fixture_id"], r["league_id"] or 0, r["league"] or "", 0, ko, "NS", 0, home, 0, away)
    priority = "MEDIUM" if r["approved"] else "LOW"
    chk = prov.client.budget.check(priority, fx.id)
    if chk.allowed is False:
        status, quotes, note = "SKIPPED_BUDGET", [], f"kvóta šetřena: {chk.reason}"
    else:
        res = prov.odds_fixture(fx, priority=priority, force=True)
        quotes, status = res.data or [], res.status
        note = "; ".join(res.errors) if res.errors else ("" if quotes else "API nevrátilo žádné kurzy")
    n = O.save(st, O.with_kickoff(quotes, ko, fx.name))
    latest = max((q.stamp for q in quotes), default=None)
    for s in r["slots"]:
        st.execute("INSERT INTO odds_observations(fixture_id, kickoff, slot_min, observed_at, status, n_quotes, "
                   "latest_update, note) VALUES(?,?,?,?,?,?,?,?)",
                   (fx.id, iso(ko), s, iso(now), status, len(quotes), iso(latest), note))
    age = f", poslední aktualizace u poskytovatele {(ko - latest).total_seconds() / 3600:.1f} h před výkopem" if latest else ""
    msg = f"Profil 4 – kurzy před výkopem {fx.name} (bod {min(r['slots'])} min): {status}, {len(quotes)} kurzů, {n} nových{age}"
    try:
        extra = verify_foreign(st, r, now, ko, latest, cfg, **(foreign_opts or {}))
    except Exception as e:  # noqa: BLE001 - overeni je doplnek, nesmi zastavit stahovani kurzu
        from ..oddsapi import redact

        extra = redact(f"ověření zahraniční kanceláří selhalo ({e})")  # chyba requests obsahuje adresu s klicem
    return msg + (f"; {extra}" if extra else "")


def verify_foreign(st, r: dict, now: datetime, ko: datetime, latest: datetime | None, cfg: Profil4Config,
                   fetch=None, ledger_factory=None) -> str:
    """Schvaleny tip v poslednim bodu pred vykopem a API-Football kurz neobnovilo => zaviraci kurz zahranicni
    kancelare z kurzoveho API (foreign.py). Vraci poznamku ('' = nebylo potreba)."""
    from ..config import SETTINGS
    from . import foreign

    if not r["approved"] or min(r["slots"]) != min(cfg.odds.closing_slots_min):
        return ""
    if latest is not None and (ko - latest).total_seconds() / 60 <= cfg.odds.closing_max_source_age_min:
        return ""  # API-Football kurz je cerstvy, zaviraci kurz je z nej
    comp = next((c for c in cfg.competitions if c.league_id == r["league_id"]), None)
    sport = SETTINGS.odds_api.sport_keys.get(comp.bot_league if comp else "")
    tips = st.query("SELECT DISTINCT market FROM tips WHERE fixture_id=? AND decision='APPROVED BET' AND result IS NULL",
                    (r["fixture_id"],))
    markets = sorted({m for m in (foreign.api_market(t["market"]) for t in tips) if m})
    if not sport or not markets:
        return ""
    if ledger_factory is None:
        from ..storage import make_session_factory

        ledger_factory = make_session_factory()
    from ..storage import Ledger

    with ledger_factory() as session:
        led = Ledger(session)
        key, q = led.api_key(), led.odds_api_quota()
        if not key:
            return "zahraniční ověření: chybí klíč kurzového API"
        left = q.get("remaining")
        if left is not None and left - len(markets) < SETTINGS.timing.auto_credit_reserve:
            return f"zahraniční ověření vynecháno: zbývá {left} kreditů kurzového API (rezerva {SETTINGS.timing.auto_credit_reserve})"
        home, _, away = (r["match"] or "").partition(" - ")
        got, quota, note = (fetch or foreign.fetch)(sport, key, home, away, ko, markets, r["fixture_id"], r["match"], now,
                                                    SETTINGS.odds_api.base_url)
        led.set_odds_api_quota(quota.get("remaining"), quota.get("used"), now.replace(tzinfo=None))
        session.commit()
    n = O.save(st, got)
    return f"zahraniční ověření ({', '.join(markets)}): {len(got)} kurzů, {n} nových" + (f" – {note}" if note else "")


def tick(now: datetime | None = None, cfg: Profil4Config = P4, build=None) -> list[str]:
    if not cfg.enabled:
        return []
    now = now or utcnow()
    store = open_store(cfg)
    try:
        store.set("auto_tick_at", iso(now))
        fixtures, settle = due(store, now, cfg)
    finally:
        store.close()
    if not fixtures and not settle:
        return []
    if build is None:
        from .cli import build
    st, prov = build()
    notes = []
    try:
        prov.client.reset_run()
        for r in fixtures:
            notes.append(observe(st, prov, r, now, cfg))
        if settle:
            from . import history

            from . import nhl

            notes += history.settle(st, prov, now) + nhl.settle(st, now)
            st.set("auto_settle_at", iso(now))
    finally:
        st.close()
    return notes


MORNING_MAX_REQUESTS = 500  # strop pozadavku ranniho skenu (denni kvota API-Football je 7 500; rezervy plati dal)
MORNING_DEEP_MAX = 200  # hloubkove se rozebere kazdy zapas okna


def morning_scan(now: datetime | None = None, cfg: Profil4Config = P4, build=None) -> list[str]:
    """Kazde rano (od auto_morning_hour ceskeho casu, jednou denne): sken na 48 h s hloubkovou analyzou VSECH
    zapasu sledovanych soutezi. Ulozi tipy i predikce (kalibrace) a vysledek ukaze v panelu jako posledni sken."""
    import json
    from dataclasses import replace
    from zoneinfo import ZoneInfo

    from ..config import SETTINGS

    if not cfg.enabled:
        return []
    now = now or utcnow()
    day = now.astimezone(ZoneInfo("Europe/Prague"))
    if day.hour < SETTINGS.timing.auto_morning_hour:
        return []
    store = open_store(cfg)
    try:
        if store.get("auto_morning_scan") == day.date().isoformat():
            return []
        started = parse_dt(store.get("auto_morning_started") or None)
        if started is not None and now - started < timedelta(hours=3):
            return []  # prave bezi (nebo selhal pred chvili) – dalsi pokus nejdriv po 3 h, at se neplytva kvotou
        store.set("auto_morning_started", iso(now))
    finally:
        store.close()
    big = replace(cfg, hunter=replace(cfg.hunter, deep_max=MORNING_DEEP_MAX),
                  budget=replace(cfg.budget, max_per_run=MORNING_MAX_REQUESTS))
    if build is None:
        from .cli import build as cli_build

        def build():
            return cli_build(None, big)
    from . import hunter, report, view

    st, prov = build()
    try:
        rep = hunter.run_scan(prov, "upcoming", now, big, hours=SETTINGS.timing.analysis_hours, deep_all=True)
        st.set("web_last_report", report.format_scan(rep))
        st.set("web_last_view", json.dumps(view.scan(rep), ensure_ascii=False, default=str))
        st.set("auto_morning_scan", day.date().isoformat())  # hotovo az po dobehnuti (restart uprostred = novy pokus)
        verdict = rep.overall.verdict if rep.overall else "bez analýzy"
        return [f"Profil 4 – ranní sken 48 h: {rep.fixtures} zápasů, hloubkově {len(rep.analyses)}, verdikt {verdict}, "
                f"kvóta dnes {rep.budget.get('used', '?')}/{rep.budget.get('limit', '?')}"] + rep.notes[:5]
    finally:
        st.close()


def start_thread(log=None, every_s: int = 60) -> list[threading.Thread]:
    """Dve samostatna vlakna, kazde kontroluje kazdou minutu i behem dlouheho pruchodu automatu:
    kurzy pred vykopem + vyhodnoceni, a ranni sken. Ranni sken trva desitky minut (hloubkova analyza vsech
    zapasu), proto nesmi blokovat stahovani kurzu tesne pred vykopem."""
    from ..oddsapi import redact

    def loop(fn, what):
        while True:
            try:
                for n in fn():
                    print(redact(n), flush=True)
            except Exception as e:  # noqa: BLE001 - chyba Profilu 4 nesmi zastavit automat
                if log:  # bez tracebacku: text vyjimky requests muze obsahovat klic v adrese
                    log.error("automat: Profil 4 (%s) selhal: %s", what, redact(f"{type(e).__name__}: {e}"))
            time.sleep(every_s)

    threads = [threading.Thread(target=loop, args=(tick, "kurzy před výkopem / vyhodnocení"), name="profil4-closing",
                                daemon=True),
               threading.Thread(target=loop, args=(morning_scan, "ranní sken"), name="profil4-morning", daemon=True)]
    for t in threads:
        t.start()
    return threads
