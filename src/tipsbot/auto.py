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


def run_once(factory, now: datetime | None = None, settings: Settings = SETTINGS) -> list[str]:
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
        led._set(STATUS_KEY, json.dumps({"t": now.isoformat(), "notes": notes[-40:]}, ensure_ascii=False))
        s.commit()
    return notes


def status(ledger: Ledger) -> dict | None:
    raw = ledger._get(STATUS_KEY, "")
    return json.loads(raw) if raw else None


def run_forever(factory, settings: Settings = SETTINGS) -> None:
    every = settings.timing.odds_refresh_seconds
    while True:
        try:
            for n in run_once(factory, settings=settings):
                print(n, flush=True)
        except Exception:  # noqa: BLE001 - automat bezi dal
            log.exception("automat: pruchod selhal")
        time.sleep(every)
