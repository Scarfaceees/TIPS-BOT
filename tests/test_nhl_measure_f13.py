"""Phase 3.5.2 – opravy F1 (skutecny cas stazeni z evidence B1) a F3 (casy vsech kancelari, anomalie u kancelare).

Evidence B1 se zapisuje stejne jako pri skutecnem stazeni (Ledger.log_odds_api_call s oddsapi.CallRecord) do
izolovane DB v pameti. Nic se nestahuje."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from test_nhl_measure import AWAY, FX, HOME, provider, snapshot, st  # noqa: F401 - fixture st
from test_nhl_measure_m2 import _n, evidence
from test_nhl_profil4 import GID, SCHEDULE, _book, ledger_with
from tipsbot.profil4 import nhl, nhl_measure as M
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store

PASS = datetime(2026, 11, 21, 6, 0, 37, 640097, tzinfo=timezone.utc)  # cas cache = zacatek pruchodu (s mikrosekundami)
REQ = datetime(2026, 11, 21, 6, 11, 34, tzinfo=timezone.utc)  # skutecny pozadavek na kurzy NHL
NOW = datetime(2026, 11, 21, 6, 12, 10, tzinfo=timezone.utc)  # sken Profilu 4 po stazeni


def led(credits=239):
    return ledger_with([], when=PASS)


def ev_of(ledger, credits=239):
    return M.fetch_evidence(ledger, PASS.replace(tzinfo=None), credits)


# --- F1: prirazeni pozadavku k datum kurzu -------------------------------------------------------------------

def test_exact_request_is_assigned_by_window_endpoint_and_credits():
    L = led()
    evidence(L, PASS - timedelta(hours=6), remaining=250)  # predchozi pruchod – mimo okno
    evidence(L, PASS + timedelta(minutes=2), remaining=245, league="ENG1", endpoint="/sports/soccer_epl/odds")  # jina liga
    evidence(L, PASS + timedelta(minutes=8), remaining=242, purpose="foreign_events", endpoint="/sports/icehockey_nhl/events")
    evidence(L, REQ, remaining=239)
    e = ev_of(L)
    assert e["fetched_at"] == REQ and e["call_id"] == 4 and e["remaining"] == 239
    assert e["reason"].startswith("prokázáno") and e["candidates"] == [4] and e["failed"] == 0


def test_missing_evidence_gives_no_timestamp():
    e = ev_of(led())
    assert e["fetched_at"] is None and "žádný požadavek" in e["reason"]


def test_only_failed_requests_give_no_timestamp():
    L = led()
    evidence(L, REQ, remaining=None, http=500, ok=False)
    e = ev_of(L)
    assert e["fetched_at"] is None and "neúspěšné" in e["reason"] and e["failed"] == 1


def test_failed_then_successful_retry_is_assigned_to_the_success():
    L = led()
    evidence(L, REQ - timedelta(seconds=40), remaining=None, http=None, ok=False)
    evidence(L, REQ, remaining=239)
    e = ev_of(L)
    assert e["fetched_at"] == REQ and e["failed"] == 1


def test_duplicate_requests_with_same_credits_are_ambiguous():
    L = led()
    evidence(L, REQ, remaining=239)
    evidence(L, REQ + timedelta(minutes=1), remaining=239)
    e = ev_of(L)
    assert e["fetched_at"] is None and "nejednoznačné" in e["reason"]


def test_two_requests_are_resolved_by_cache_credits():
    """Panel vynuti stazeni behem pruchodu: dva uspesne pozadavky, cache odpovida tomu s jejimi kredity."""
    L = led()
    evidence(L, PASS + timedelta(minutes=5), remaining=242)
    evidence(L, REQ, remaining=239)
    assert ev_of(L, 239)["fetched_at"] == REQ
    assert ev_of(L, 242)["fetched_at"] == (PASS + timedelta(minutes=5)).replace(microsecond=0)  # B1 uklada cele sekundy
    assert ev_of(L, 100)["fetched_at"] is None and "kredity 100" in ev_of(L, 100)["reason"]


def test_without_cache_credits_only_a_single_request_is_proof():
    L = led()
    evidence(L, REQ, remaining=239)
    assert ev_of(L, None)["fetched_at"] == REQ
    evidence(L, REQ + timedelta(minutes=1), remaining=236)
    assert ev_of(L, None)["fetched_at"] is None and "nejednoznačné" in ev_of(L, None)["reason"]


def test_request_outside_window_is_not_used():
    L = led()
    evidence(L, PASS + timedelta(hours=3, seconds=5), remaining=239)
    assert ev_of(L)["fetched_at"] is None


def test_unreadable_evidence_never_raises(caplog):
    class Broken:
        def odds_api_calls(self, since=None):
            raise RuntimeError("no such table: odds_api_calls")

    with caplog.at_level("WARNING"):
        e = M.fetch_evidence_safe(Broken(), (PASS.replace(tzinfo=None), [], 239))
    assert e["fetched_at"] is None and "nečitelná" in e["reason"] and any("evidenci B1" in r.message for r in caplog.records)
    assert M.fetch_evidence_safe(led(), None)["reason"] == "chybí čas cache kurzů"


# --- F1 v celem skenu: realny prubeh casu ---------------------------------------------------------------------

def _event(stamps: dict[str, str | None]):
    books = []
    for key, upd in stamps.items():
        odds = {"pinnacle": (1.95, 1.95), "williamhill": (1.90, 1.90), "unibet_nl": (2.12, 1.80), "betsson": (1.92, 1.92)}[key]
        b = _book(key, upd or "2026-11-21T06:05:00Z", odds, ou=(1.92, 1.98), pl=(2.80, 1.45) if key == "pinnacle" else None)
        if upd is None:  # kancelar bez casu poskytovatele
            b.pop("last_update")
            for m in b["markets"]:
                m.pop("last_update")
        books.append(b)
    return {"id": "abc123", "sport_key": "icehockey_nhl", "commence_time": "2026-11-22T00:00:00Z",
            "home_team": HOME, "away_team": AWAY, "bookmakers": books}


def _scan(st, pl, with_evidence=True):
    L = ledger_with([pl], when=PASS)
    L.set_odds_cache("NHL", PASS.replace(tzinfo=None), [pl], 239)
    if with_evidence:
        evidence(L, REQ, remaining=239)
    s = Store()
    nhl.run(L, s, NOW, P4, schedule=lambda d: SCHEDULE, provider=provider(st), injuries=True)
    snap = dict(s.query("SELECT * FROM nhl_snapshots")[0])
    rows = {(r["market"], r["selection"]): dict(r) for r in s.query("SELECT * FROM nhl_measurements")}
    return s, snap, rows


REAL = {"pinnacle": "2026-11-21T06:09:12Z", "williamhill": "2026-11-21T06:11:30Z", "unibet_nl": "2026-11-21T06:04:00Z"}


def test_real_timeline_is_comparable_with_b1_evidence(st):
    """Cache 06:00:37 (zacatek pruchodu), kurzy kancelari az do 06:11:30, pozadavek 06:11:34 → srovnatelne."""
    _, snap, rows = _scan(st, _event(REAL))
    assert snap["odds_pass_started_at"] == "2026-11-21T06:00:37+00:00" and snap["odds_fetched_at"] == "2026-11-21T06:11:34+00:00"
    assert snap["odds_call_id"] == 1 and json.loads(snap["book_time_anomalies"]) == {}
    assert snap["comparable"] == 1 and rows[("MONEYLINE", "home")]["comparable_sharp"] == 1
    assert rows[("MONEYLINE", "home")]["comparable_p4"] == 1


def test_real_timeline_without_evidence_is_not_comparable(st):
    _, snap, rows = _scan(st, _event(REAL), with_evidence=False)
    assert snap["odds_fetched_at"] is None and snap["odds_pass_started_at"] == "2026-11-21T06:00:37+00:00"
    assert any(m.startswith("S0: čas stažení kurzů neprokázán") for m in json.loads(snap["missing"]))
    assert snap["comparable"] == 0 and all(r["comparable_sharp"] == 0 for r in rows.values())


def test_decisions_do_not_depend_on_evidence(st):
    from test_nhl_measure_m2 import _all_decisions

    a, *_ = _scan(st, _event(REAL), with_evidence=True)
    b, *_ = _scan(st, _event(REAL), with_evidence=False)
    assert _all_decisions(a) == _all_decisions(b) and _all_decisions(a)[1]


# --- F3: casy vsech kancelari ----------------------------------------------------------------------------------

def test_one_bad_book_does_not_affect_others(st):
    """unibet_nl s casem 2 min PO pozadavku: anomalie jen u nej; ostra linie i trh B (oba z Pinnacle) zustavaji
    srovnatelne."""
    stamps = {"unibet_nl": "2026-11-21T06:13:40Z", "pinnacle": "2026-11-21T06:09:12Z", "williamhill": "2026-11-21T06:11:30Z"}
    _, snap, rows = _scan(st, _event(stamps))
    an = json.loads(snap["book_time_anomalies"])
    assert set(an) == {"unibet_nl"} and all("je po stažení" in v for v in an["unibet_nl"].values())
    assert snap["ref_sharp_stamp_max"] == "2026-11-21T06:09:12+00:00"  # cas Pinnacle se sebral i po spatne kancelari
    ml = rows[("MONEYLINE", "home")]
    assert ml["comparable_sharp"] == 1 and ml["mkt_sharp_stamp"] == "2026-11-21T06:09:12+00:00"
    assert ml["mkt_p4_source"] == "Pinnacle bez marže" and json.loads(ml["mkt_p4_books"]) == ["pinnacle"]
    assert ml["comparable_p4"] == 1  # trh B je z Pinnacle – spatna kancelar ho neovlivni


def test_bad_book_inside_median_market_excludes_only_that_reference(st):
    """Bez Pinnacle je trh B median kancelari: spatna kancelar v medianu vyradi trh B, ostra linie tu neni."""
    stamps = {"williamhill": "2026-11-21T06:11:30Z", "unibet_nl": "2026-11-21T06:13:40Z", "betsson": "2026-11-21T06:08:00Z"}
    _, snap, rows = _scan(st, _event(stamps))
    ml = rows[("MONEYLINE", "home")]
    assert ml["mkt_p4_source"].startswith("medián") and "unibet_nl" in json.loads(ml["mkt_p4_books"])
    assert ml["comparable_p4"] == 0 and any("kancelář trhu B unibet_nl" in e for e in json.loads(ml["exclusion"])["p4"])
    assert set(json.loads(snap["book_time_anomalies"])) == {"unibet_nl"}


def test_bad_book_first_in_order_still_lets_later_books_be_collected(st):
    stamps = {"unibet_nl": "2026-11-21T06:30:00Z", "williamhill": "2026-11-21T06:11:30Z", "pinnacle": "2026-11-21T06:09:12Z"}
    _, snap, _ = _scan(st, _event(stamps))
    assert snap["ref_sharp_stamp_max"] == "2026-11-21T06:09:12+00:00"
    assert set(json.loads(snap["book_time_anomalies"])) == {"unibet_nl"}


def test_small_delay_within_request_duration_is_not_an_anomaly(st):
    stamps = {**REAL, "williamhill": "2026-11-21T06:12:04Z"}  # 30 s po zacatku pozadavku (< 60 s)
    _, snap, _ = _scan(st, _event(stamps))
    assert json.loads(snap["book_time_anomalies"]) == {}


def test_missing_provider_time_is_recorded_for_that_book_only(st):
    stamps = {**REAL, "betsson": None}
    _, snap, rows = _scan(st, _event(stamps))
    an = json.loads(snap["book_time_anomalies"])
    assert set(an) == {"betsson"} and set(an["betsson"].values()) == {"chybí čas poskytovatele"}
    assert rows[("MONEYLINE", "home")]["comparable_sharp"] == 1


def test_bad_sharp_book_excludes_sharp_rows_with_reason(st):
    stamps = {**REAL, "pinnacle": "2026-11-21T06:20:00Z"}
    _, snap, rows = _scan(st, _event(stamps))
    ml = rows[("MONEYLINE", "home")]
    assert ml["comparable_sharp"] == 0 and ml["mkt_sharp_stamp"] is None
    assert any(e.startswith("S3: ostrá kancelář pinnacle: čas poskytovatele") for e in json.loads(ml["exclusion"])["sharp"])
    assert _n(Store(), "nhl_snapshots") == 0  # (kontrola izolace: nova DB je prazdna)
