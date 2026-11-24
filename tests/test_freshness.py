"""Odds Freshness: cerstvost kurzu jen z ulozenych dat (cas stazeni != cas aktualizace), zdroje zvlast, shoda panel x Excel."""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone

import openpyxl
import pytest

from tipsbot import export, freshness
from tipsbot.profil4 import history as H
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store

UTC = timezone.utc
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
KO = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)


def iso(d: datetime) -> str:
    return d.isoformat()


def _quote(st, fid, src, fetched, update, ko, book="pinnacle", odds=2.0):
    st.execute("INSERT INTO odds_quotes(fixture_id, match, kickoff, bookmaker, market, selection, odds, fetched_at, "
               "source_time, source) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (fid, f"A{fid} - B{fid}", iso(ko), book, "1X2", "1", odds, iso(fetched), iso(update), src))


def _obs(st, fid, ko, slot, at, update, n=10, status="AVAILABLE"):
    st.execute("INSERT INTO odds_observations(fixture_id, kickoff, slot_min, observed_at, status, n_quotes, latest_update) "
               "VALUES(?,?,?,?,?,?,?)", (fid, iso(ko), slot, iso(at), status, n, iso(update) if update else None))


def _tip(st, fid, league, ko, kind, decision="APPROVED BET", result="won"):
    st.execute("INSERT INTO tips(fixture_id, match, league, kickoff, market, selection, probs, odds, bookmaker, odds_source, "
               "decision, predicted_at, model_version, config_hash, result, profit, closing_kind) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (fid, f"A{fid} - B{fid}", league, iso(ko), "1X2", "1", "{}", 2.0, "pinnacle", "api", decision,
                iso(ko - timedelta(hours=8)), "t", "h", result, 1.0 if result == "won" else -1.0, kind))


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "p4.db"
    st = Store(path)
    at = lambda h, m=0: KO.replace(hour=h, minute=m)  # noqa: E731
    for fid in (1, 2):  # fotbal, API-Football: ranni verze 10:00, odpoledni davka 16:30
        _quote(st, fid, "api", at(10, 5), at(10), KO, odds=1.9)
        _quote(st, fid, "api", at(18, 46), at(16, 30), KO, odds=2.0)
        _obs(st, fid, KO, 75, at(18, 45), at(16, 30))
        _obs(st, fid, KO, 30, at(19, 30), at(16, 30))
    _obs(st, 1, KO, 10, at(19, 50), at(16, 30))  # zapas 1: v bodu 10 stejna verze (aktualizace 200 min pred stazenim)
    _quote(st, 2, "api", at(19, 51), at(19, 15), KO, odds=2.1)
    _obs(st, 2, KO, 10, at(19, 50), at(19, 15))  # zapas 2: v bodu 10 nova verze
    # overeni zahranicni kancelari (kurzove API) u zapasu 1 – jiny zdroj, nesmi se slucovat
    _quote(st, 1, "oddsapi", at(19, 50), at(19, 49), KO, odds=2.05)
    # NHL – kurzove API
    _quote(st, 3, "oddsapi", at(19, 50), at(19, 49), KO)
    for s, t in ((75, at(18, 45)), (30, at(19, 30)), (10, at(19, 50))):
        _obs(st, 3, KO, s, t, at(19, 49) if s == 10 else at(6))
    # zapas, ktery jeste nezacal: do cerstvosti pri vykopu nepatri
    fut = NOW + timedelta(hours=5)
    _quote(st, 4, "api", NOW - timedelta(hours=3), NOW - timedelta(hours=3), fut)
    _obs(st, 4, fut, 75, NOW, NOW - timedelta(hours=3))
    for fid, lg in ((1, "Premier League"), (2, "Premier League"), (3, "NHL"), (4, "Premier League")):
        st.execute("INSERT INTO predictions(fixture_id, league, kickoff, market, selection, market_type, model, p, "
                   "predicted_at, model_version, config_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (fid, lg, iso(KO), "1X2", "1", "1X2", "C", 0.5, iso(KO), "t", "h"))
    _tip(st, 1, "Premier League", KO, H.CK_STALE)
    _tip(st, 2, "Premier League", KO, H.CK_CLOSING, decision="WATCH")
    _tip(st, 3, "NHL", KO, H.CK_FOREIGN)
    _tip(st, 3, "NHL", KO, None, result="lost")
    st.conn.commit()
    st.conn.close()
    return path


def test_fetch_time_is_not_update_time_and_sources_separate(db):
    r = freshness.report(db, NOW)
    pl = [c for c in r["competitions"] if c["league"] == "Premier League"]
    assert {c["source"] for c in pl} == {"API-Football", "kurzové API"}  # stejna soutez, zdroje zvlast
    api = next(c for c in pl if c["source"] == "API-Football")
    assert api["n"] == 2 and api["min"] == pytest.approx(45) and api["max"] == pytest.approx(210)  # aktualizace u zdroje
    assert api["median"] == pytest.approx(127.5) and api["mean"] == pytest.approx(127.5)
    assert api["fetch_median"] == pytest.approx(9.5)  # posledni STAZENI ~10 min pred vykopem (19:50 a 19:51)
    assert api["lag_median"] > 100  # stazeno cerstve, ale kurz byl stary – to je rozdil, ktery se meri
    assert api["fresh60"] == pytest.approx(0.5) and api["fresh20"] == 0
    odds = next(c for c in pl if c["source"] == "kurzové API")
    assert odds["n"] == 1 and odds["median"] == pytest.approx(11) and odds["fresh20"] == 1
    assert r["fixtures"] == 3  # nezacaty zapas 4 se do cerstvosti pri vykopu nepocita
    assert all(c["league"] != "?" for c in r["competitions"])


def test_slots_count_new_versions_by_update_time(db):
    r = freshness.report(db, NOW)
    s = {(x["source"], x["slot"]): x for x in r["slots"]}
    s75 = s[("API-Football", 75)]  # zapas 4: verze uz stazena drive -> stejna, i kdyz stazeni probehlo znovu
    assert (s75["planned"], s75["new"], s75["same"]) == (3, 2, 1)
    assert s[("API-Football", 30)]["new"] == 0 and s[("API-Football", 30)]["same"] == 2
    s10 = s[("API-Football", 10)]
    assert (s10["new"], s10["same"], s10["new_share"]) == (1, 1, 0.5)
    assert s[("kurzové API", 10)]["new"] == 1 and s[("kurzové API", 75)]["planned"] == 1  # NHL zvlast


def test_catchup_and_failed_slots(tmp_path):
    raw = {"leagues": {9: "Serie A"}, "versions": [], "tips": [],
           "obs": [{"fixture_id": 9, "kickoff": iso(KO), "slot": s, "observed_at": iso(KO - timedelta(minutes=25)),
                    "status": "AVAILABLE", "n_quotes": 5, "latest_update": iso(KO - timedelta(hours=3))} for s in (75, 30)]
           + [{"fixture_id": 9, "kickoff": iso(KO), "slot": 10, "observed_at": iso(KO - timedelta(minutes=9)),
               "status": "SKIPPED_BUDGET", "n_quotes": 0, "latest_update": None}]}
    s = {x["slot"]: x for x in freshness.build(raw, NOW)["slots"]}
    assert s[75]["catchup"] == 1 and s[30]["new"] == 1 and s[10]["failed"] == 1  # jedno stazeni = jeden pozadavek


def test_batches_and_hours(db):
    b = {x["source"]: x for x in freshness.report(db, NOW)["batches"]}
    api = b["API-Football"]
    assert sum(api["hours"]) == api["versions"] == 6  # verze 10:00 a 16:30 (zapasy 1, 2), 19:15 (2), zapas 4
    assert api["hours"][16] == 2 and api["batch_max"] == 2  # davka 16:30 = oba zapasy najednou
    assert b["kurzové API"]["versions"] == 2


def test_clv_coverage_uses_stored_kind(db):
    rows = {(x["scope"], x["sport"]): x for x in freshness.report(db, NOW)["clv"]}
    fb = rows[("všechny vyhodnocené tipy", "fotbal")]
    assert (fb["tips"], fb[freshness.CLV_OK_API], fb[freshness.CLV_STALE], fb["coverage"]) == (2, 1, 1, 0.5)
    nhl = rows[("všechny vyhodnocené tipy", "hokej")]
    assert nhl[freshness.CLV_OK_ODDSAPI] == 1 and nhl[freshness.CLV_MISSING] == 1 and nhl[freshness.CLV_OK_API] == 0
    assert rows[("jen APPROVED BET", "fotbal")]["tips"] == 1


def test_weeks(db):
    r = freshness.report(db, NOW)
    w = {(x["week"], x["source"]): x for x in r["weeks"]}
    assert w[("2026-11-16", "API-Football")]["matches"] == 2
    assert r["clv_weeks"][0]["week"] == "2026-11-16"


def test_rules_unchanged():
    assert P4.odds.closing_slots_min == (75, 30, 10)
    assert P4.odds.closing_max_source_age_min == freshness.FRESH_MIN == 60
    assert P4.odds.closing_observed_within_min == freshness.CLOSE_MIN == 20


def test_report_is_read_only(db):
    before = db.read_bytes()
    freshness.report(db, NOW)
    assert db.read_bytes() == before
    assert freshness.report(None, NOW)["missing"]


def _excel_tables(ws) -> dict:
    """Tabulky listu Odds Freshness: nadpis sekce -> radky hodnot (pod hlavickou)."""
    out, rows = {}, list(ws.iter_rows(min_row=7))
    keys = {v: k for k, v in export.FRESH_TITLES.items()}
    i = 0
    while i < len(rows):
        t = rows[i][0].value
        if t in keys:
            head = [c.value for c in rows[i + 1] if c.value is not None]
            body, j = [], i + 2
            while j < len(rows) and rows[j][0].value is not None and rows[j][0].value not in keys:
                body.append([(c.value, c.number_format) for c in rows[j][:len(head)]])
                j += 1
            out[keys[t]] = body
            i = j
        else:
            i += 1
    return out


def test_excel_sheet_and_panel_show_same_values(db, tmp_path, monkeypatch):
    from tipsbot.web import create_app, export_fcell

    d = export.collect(_session(tmp_path), db, NOW)
    wb = openpyxl.load_workbook(io.BytesIO(export.to_xlsx(d)))
    assert "Odds Freshness" in wb.sheetnames and len(wb.sheetnames) == 30
    assert wb.sheetnames.index("Odds Freshness") == wb.sheetnames.index("Ranking Diagnostics") + 1
    xl = _excel_tables(wb["Odds Freshness"])
    assert set(xl) == set(export.FRESH_TITLES) and all(xl.values())

    real = freshness.report
    monkeypatch.setattr(freshness, "report", lambda p, now=None: real(db, NOW))
    html = create_app(f"sqlite:///{tmp_path / 'w.db'}").test_client().get("/?tab=sledovani&fresh=1").get_data(as_text=True)
    assert "Čerstvost kurzů (Odds Freshness)" in html
    for key, body in xl.items():
        m = re.search(rf'<table class="fresh" data-key="{key}">(.*?)</table>', html, re.S)
        assert m, key
        cells = [re.findall(r"<td>(.*?)</td>", tr) for tr in re.findall(r"<tr>(.*?)</tr>", m.group(1), re.S)[1:]]
        assert len(cells) == len(body), key
        for prow, xrow in zip(cells, body):
            fmts = export.FRESH_FMT[key]
            want = [export_fcell(v, fmts.get(j)) for j, (v, _) in enumerate(xrow, 1)]
            assert [c.replace("&amp;", "&") for c in prow] == want, key
            for j, (v, nf) in enumerate(xrow, 1):  # Excel zobrazuje stejny format jako panel
                if isinstance(v, float) and j in fmts:
                    assert nf == fmts[j]


def _session(tmp_path):
    from tipsbot.storage import make_session_factory

    return make_session_factory(f"sqlite:///{tmp_path / 'x.db'}")()
