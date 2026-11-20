"""Zaviraci kurzy a prubezne vyhodnoceni Profilu 4 bez rucniho skenu.

CLV porovnava kurz pri doporuceni se zaviracim kurzem STEJNE kancelare na STEJNY trh. Kurzy se ale bez skenu
nestahuji, takze by zaviraci kurz skoro vzdy chybel. `tick` proto (volany z automatu bota kazdou minutu, sam se
omezi) stahne kurzy zapasu, ke kterym Profil 4 ulozil tip nebo predikci, v okne `closing_window_min` pred vykopem
(nejvys jednou za `closing_refresh_min` na zapas) a po zapasech spusti vyhodnoceni. Bez ulozenych tipu nedela nic
a nespotrebuje zadny pozadavek; pri setreni kvoty ho rozpoctova vrstva API klienta odmitne.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import odds as O
from .config import P4, Profil4Config
from .provider import Fixture
from .store import iso, open_store, parse_dt, utcnow

TICK_MIN = 5  # jak casto se vubec divat do databaze
SETTLE_EVERY = timedelta(hours=3)


def due(store, now: datetime, cfg: Profil4Config) -> tuple[list[dict], bool]:
    """(zapasy, kterym je potreba stahnout zaviraci kurzy; je potreba vyhodnotit skoncene zapasy?)."""
    hi = now + timedelta(minutes=cfg.odds.closing_window_min)
    rows = store.query(
        "SELECT fixture_id, match, kickoff, league_id, league FROM tips WHERE result IS NULL AND kickoff > ? AND kickoff <= ? "
        "UNION SELECT fixture_id, match, kickoff, league_id, league FROM predictions WHERE result IS NULL "
        "AND kickoff > ? AND kickoff <= ?", (iso(now), iso(hi)) * 2)
    out, seen = [], set()
    for r in rows:
        if r["fixture_id"] in seen:
            continue
        seen.add(r["fixture_id"])
        last = parse_dt(store.get(f"closing:{r['fixture_id']}") or None)
        if last is None or now - last >= timedelta(minutes=cfg.odds.closing_refresh_min):
            out.append(dict(r))
    past = store.query("SELECT 1 FROM tips WHERE result IS NULL AND kickoff < ? UNION "
                       "SELECT 1 FROM predictions WHERE result IS NULL AND kickoff < ? LIMIT 1",
                       (iso(now - timedelta(hours=2, minutes=30)),) * 2)
    last_settle = parse_dt(store.get("auto_settle_at") or None)
    return out, bool(past) and (last_settle is None or now - last_settle >= SETTLE_EVERY)


def tick(now: datetime | None = None, cfg: Profil4Config = P4, build=None) -> list[str]:
    if not cfg.enabled:
        return []
    now = now or utcnow()
    store = open_store(cfg)
    try:
        last = parse_dt(store.get("auto_tick_at") or None)
        if last is not None and now - last < timedelta(minutes=TICK_MIN):
            return []
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
            ko = parse_dt(r["kickoff"])
            home, _, away = (r["match"] or "").partition(" - ")
            fx = Fixture(r["fixture_id"], r["league_id"] or 0, r["league"] or "", 0, ko, "NS", 0, home, 0, away)
            res = prov.odds_fixture(fx, priority="MEDIUM", force=True)
            n = O.save(st, O.with_kickoff(res.data or [], ko, fx.name))
            st.set(f"closing:{fx.id}", iso(now))
            notes.append(f"Profil 4 – závěrečné kurzy {fx.name}: {n} nových ({res.status})")
        if settle:
            from . import history

            notes += history.settle(st, prov, now)
            st.set("auto_settle_at", iso(now))
    finally:
        st.close()
    return notes
