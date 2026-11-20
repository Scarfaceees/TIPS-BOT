"""Automat: bez obsluhy kazdych par hodin obnovi data, nacte kurzy na zapasy 48 h dopredu, necha vsechny
profily vybrat sazky do Sledovani a vyhodnoti odehrane zapasy.

Kredity kurzoveho API se setri: kurzy souteze se stahuji znovu nejdriv po `auto_odds_hours` a jen kdyz
ma soutez zapas v pristich 48 h; pod rezervou kreditu se pocita jen z ulozenych kurzu.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

from .config import SETTINGS, Settings
from .storage import Ledger, utcnow

log = logging.getLogger(__name__)

STATUS_KEY = "auto:status"
REFRESH_KEY = "auto:last_refresh"
VERIFY_KEY = "auto:last_verify"
RUNNING_KEY = "auto:running"  # cas zacatku behu (panel ukazuje, ze automat prave pracuje)
REQUEST_KEY = "auto:request"  # tlacitko v panelu: spustit hned
SCORES_KEY = "auto:scores:"


def _due(ledger: Ledger, key: str, now: datetime, every: timedelta) -> bool:
    raw = ledger._get(key, "")
    return not raw or now - datetime.fromisoformat(raw) >= every


def wants_fresh_odds(ledger: Ledger, league: str, now: datetime, settings: Settings = SETTINGS) -> bool:
    cached = ledger.odds_cache(league)
    if cached is None:
        return True
    when, _, credits = cached
    if credits is not None and credits < settings.timing.auto_credit_reserve:
        return False
    return now - when >= timedelta(hours=settings.timing.auto_odds_hours)


def settle_from_scores(ledger: Ledger, now: datetime, settings: Settings = SETTINGS) -> list[str]:
    """Rychle vyhodnoceni z kurzoveho API: jen fotbalove souteze, kde neco ceka na vysledek 2,5 h az 3 dny po
    vykopu (datove zdroje reprezentaci a poharu se dopisuji se zpozdenim). Stoji 2 kredity za soutez."""
    from sqlalchemy import select

    from . import oddsapi, paper
    from .config import LEAGUES, Sport
    from .settlement import settle_open_tickets
    from .storage import TICKET_OPEN, Ticket

    lo, hi = now - timedelta(days=3), now - timedelta(hours=2, minutes=30)
    leagues = {p.league for p in paper.load(ledger) if p.result is None and lo <= datetime.fromisoformat(p.kickoff) <= hi}
    leagues |= {t.league for t in ledger.s.scalars(select(Ticket).where(Ticket.status == TICKET_OPEN))
                if lo.date() <= t.match_day <= hi.date()}
    key, notes = ledger.api_key(), []
    for lg in sorted(leagues):
        if lg not in settings.odds_api.sport_keys or LEAGUES.get(lg) is None or LEAGUES[lg].sport is not Sport.FOOTBALL:
            continue
        cached = ledger.odds_cache(lg)
        if not key or (cached and cached[2] is not None and cached[2] < settings.timing.auto_credit_reserve):
            continue
        if not _due(ledger, SCORES_KEY + lg, now, timedelta(hours=3)):
            continue
        ledger._set(SCORES_KEY + lg, now.isoformat())
        try:
            df, left = oddsapi.fetch_scores(lg, key, settings=settings)
        except Exception as e:  # noqa: BLE001
            notes.append(oddsapi.redact(f"výsledky {lg} z kurzového API: chyba ({e})"))
            continue
        if cached and left is not None:
            ledger.set_odds_cache(lg, cached[0], cached[1], left)  # aktualni stav kreditu
        got = settle_open_tickets(ledger, df) + paper.settle(ledger, df)
        notes += got or [f"výsledky {lg} z kurzového API: zatím nic k vyhodnocení"]
    return notes


def run_once(factory, now: datetime | None = None, settings: Settings = SETTINGS, verify: bool = True) -> list[str]:
    """Jeden pruchod automatu. Kazda cast ma vlastni transakci, aby panel nemusel dlouho cekat na databazi."""
    from . import pipeline
    from .settlement import settle_everything

    now = now or utcnow()
    notes: list[str] = []
    with factory() as s:
        led = Ledger(s)
        if _due(led, REFRESH_KEY, now, timedelta(hours=24)):
            notes += [f"data {n}" for n in pipeline.refresh_data(led)]
            led._set(REFRESH_KEY, now.isoformat())
            s.commit()
    for league in settings.odds_api.sport_keys:
        with factory() as s:
            led = Ledger(s)
            try:
                rep = pipeline.scan_league(league, led, now=now, settings=settings,
                                           cache_only=not wants_fresh_odds(led, league, now, settings))
                s.commit()
                notes.append(rep.summary())
            except Exception as e:  # noqa: BLE001 - chyba jedne souteze nesmi zastavit ostatni
                s.rollback()
                log.exception("automat: sken %s selhal", league)
                notes.append(f"{league}: chyba skenu ({e})")
    with factory() as s:
        led = Ledger(s)
        try:
            notes += settle_everything(led)
            s.commit()
        except Exception as e:  # noqa: BLE001
            s.rollback()
            notes.append(f"vyhodnocení: chyba ({e})")
    with factory() as s:
        led = Ledger(s)
        try:
            notes += settle_from_scores(led, now, settings)
            s.commit()
        except Exception as e:  # noqa: BLE001
            s.rollback()
            notes.append(f"výsledky z kurzového API: chyba ({e})")
    if verify:
        notes += weekly_verify(factory, now)
    from .oddsapi import redact

    notes = [redact(n) for n in notes]
    with factory() as s:
        led = Ledger(s)
        led._set(STATUS_KEY, json.dumps({"t": now.isoformat(), "notes": notes[-60:]}, ensure_ascii=False))
        s.commit()
    return notes


def weekly_verify(factory, now: datetime) -> list[str]:
    """Jednou tydne zpetny test vsech modelu (zalozka Overeni modelu), at je vzdy aktualni."""
    from . import backtest

    with factory() as s:
        led = Ledger(s)
        if not _due(led, VERIFY_KEY, now, timedelta(days=7)) or led._get(backtest.RUNNING_KEY, ""):
            return []
        led._set(VERIFY_KEY, now.isoformat())
        led._set(backtest.RUNNING_KEY, f"{now:%d.%m. %H:%M} (automat)")
        s.commit()
    try:
        backtest.run_and_store(factory)
        return ["ověření modelu: hotovo (záložka Ověření modelu)"]
    except Exception as e:  # noqa: BLE001
        log.exception("automat: overeni modelu selhalo")
        return [f"ověření modelu: chyba ({e})"]


def request_run(ledger: Ledger) -> None:
    ledger._set(REQUEST_KEY, utcnow().isoformat())


def _take_request(factory) -> bool:
    with factory() as s:
        led = Ledger(s)
        if not led._get(REQUEST_KEY, ""):
            return False
        led._set(REQUEST_KEY, "")
        s.commit()
        return True


def _set(factory, key: str, value: str) -> None:
    with factory() as s:
        Ledger(s)._set(key, value)
        s.commit()


def status(ledger: Ledger) -> dict | None:
    raw = ledger._get(STATUS_KEY, "")
    return json.loads(raw) if raw else None


def run_forever(factory, settings: Settings = SETTINGS) -> None:
    every = settings.timing.odds_refresh_seconds
    _start_profil4()
    while True:
        _take_request(factory)
        _set(factory, RUNNING_KEY, utcnow().isoformat())
        try:
            for n in run_once(factory, settings=settings):
                print(n, flush=True)
        except Exception:  # noqa: BLE001 - automat bezi dal
            log.exception("automat: pruchod selhal")
        finally:
            _set(factory, RUNNING_KEY, "")
        waited = 0
        while waited < every and not _take_request(factory):  # tlacitko v panelu zkrati cekani
            time.sleep(60)
            waited += 60


def _start_profil4() -> None:
    """Profil 4: kurzy pred vykopem a vyhodnoceni po zapase ve vlastnim vlakne – bezi i behem pruchodu automatu."""
    try:
        from .profil4.closing import start_thread

        start_thread(log)
    except Exception:  # noqa: BLE001
        log.exception("automat: Profil 4 se nespustil")
