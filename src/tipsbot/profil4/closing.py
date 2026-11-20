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


def observe(st, prov, r: dict, now: datetime) -> str:
    """Jedno stazeni kurzu zapasu; zapise pozorovani (i neuspesne, s duvodem) pro vsechny dosazene body."""
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
    return f"Profil 4 – kurzy před výkopem {fx.name} (bod {min(r['slots'])} min): {status}, {len(quotes)} kurzů, {n} nových{age}"


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
            notes.append(observe(st, prov, r, now))
        if settle:
            from . import history

            notes += history.settle(st, prov, now)
            st.set("auto_settle_at", iso(now))
    finally:
        st.close()
    return notes


def start_thread(log=None, every_s: int = 60) -> threading.Thread:
    """Samostatne vlakno: kontrola kazdou minutu i behem dlouheho pruchodu automatu (napr. tydenni overeni)."""
    def loop():
        while True:
            try:
                for n in tick():
                    print(n, flush=True)
            except Exception:  # noqa: BLE001 - chyba Profilu 4 nesmi zastavit automat
                if log:
                    log.exception("automat: Profil 4 (kurzy před výkopem / vyhodnocení) selhal")
            time.sleep(every_s)

    t = threading.Thread(target=loop, name="profil4-closing", daemon=True)
    t.start()
    return t
