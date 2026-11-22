"""Exportni centrum: spravnost statistik, prazdna historie, jen cteni databaze, archiv rozboru."""

import hashlib
import io
import json
from datetime import date, datetime, timedelta

import pytest
from openpyxl import load_workbook

from tipsbot import export, paper
from tipsbot.profil4.store import Store
from tipsbot.storage import Ledger, make_session_factory

NOW = datetime(2026, 11, 22, 12, 0)
SHEETS = ["Souhrn", "Všechny tipy", "Kombi tikety", "Kurzová pásma", "Soutěže a sporty", "Vývoj bankrollu",
          "Kalibrace modelů", "EV analýza", "Kvalita dat", "Analýza chyb", "Opportunity Ranking", "Market Integrity",
          "Ranking Validation", "Portfolio Risk", "Predictive Performance", "Ranking Performance", "Model Comparison",
          "CLV Performance", "Settlement Integrity", "Metodika"]


def _pick(pid, result, odds, profile="zaklad", p=0.6, match=None, market="1X2", sel="1"):
    profit = None if result is None else paper.unit_profit(result, odds)
    return paper.Pick(id=pid, league="ENG1", match_id=match or pid, name=f"A{pid} - B{pid}", day="2026-11-20",
                      kickoff="2026-11-20T15:00:00", market=market, selection=sel, odds=odds, book="pinnacle",
                      p_model=p, source="model", created=NOW.isoformat(), result=result, profit=profit, profile=profile)


def _p4_tip(st, fid, decision, odds, result, profit, clv=None, kind=None, sel="home", pid=None):
    st.execute("INSERT INTO tips(fixture_id, match, league, kickoff, market, selection, label, probs, odds, bookmaker,"
               " odds_source, decision, category, predicted_at, model_version, config_hash, result, profit, clv,"
               " closing_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (fid, f"H{fid} - A{fid}", "Bundesliga", "2026-11-21T14:30:00+00:00", "1X2", sel, "výhra domácích",
                json.dumps({"C": 0.55}), odds, "pinnacle", "api", decision, "", "2026-11-21T10:00:00+00:00", "p4-1.0.0", "x",
                result, profit, clv, kind))


def _data(tmp_path, fill=None, p4=None):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    f = make_session_factory(url)
    with f() as s:
        led = Ledger(s)
        if fill:
            fill(led)
        s.commit()
    p4path = None
    if p4:
        p4path = tmp_path / "p4.db"
        st = Store(p4path)
        p4(st)
        st.close()
    with f() as s:
        return export.collect(s, p4path, NOW)


def test_empty_history_exports_everything(tmp_path):
    d = _data(tmp_path)
    assert d.records == [] and d.bankroll == []
    wb = load_workbook(io.BytesIO(export.to_xlsx(d)))
    assert wb.sheetnames == SHEETS
    assert "Vytvořeno" in wb["Souhrn"]["A2"].value and "Verze modelu" in wb["Souhrn"]["A4"].value
    assert export.to_pdf(d)[:4] == b"%PDF"
    text = export.to_csv(d).decode("utf-8-sig")
    assert "Záznamů: 0" in text and "Profil;Typ;Sport" in text


def test_only_pending_tips(tmp_path):
    d = _data(tmp_path, lambda led: paper.save(led, [_pick("a", None, 2.0), _pick("b", None, 1.8)]))
    s = export.stats(d.records)
    assert (s["n"], s["pending"], s["settled"], s["wins"], s["losses"]) == (2, 2, 0, 0, 0)
    assert s["roi"] is None and s["hit"] is None and s["profit"] == 0
    assert export.to_pdf(d)[:4] == b"%PDF" and load_workbook(io.BytesIO(export.to_xlsx(d))).sheetnames == SHEETS


def test_metrics_exclude_pending_push_and_duplicates(tmp_path):
    picks = [_pick("w", "won", 2.0, p=0.6), _pick("hw", "half_won", 3.0, p=0.5), _pick("l", "lost", 1.5, p=0.7),
             _pick("hl", "half_lost", 2.0), _pick("v", "void", 2.5), _pick("o", None, 2.2),
             _pick("w2", "won", 2.0, match="w")]  # stejny vyber stejneho zapasu = duplicita
    d = _data(tmp_path, lambda led: paper.save(led, picks))
    assert d.duplicates == 1
    s = export.stats(d.records)
    assert (s["n"], s["wins"], s["losses"], s["push"], s["pending"], s["staked"]) == (6, 2, 2, 1, 1, 4)
    assert s["profit"] == pytest.approx(1.0 + 1.0 - 1.0 - 0.5)
    assert s["roi"] == pytest.approx(0.5 / 4) and s["hit"] == pytest.approx(0.5)
    assert s["avg_odds"] == pytest.approx((2.0 + 3.0 + 1.5 + 2.0) / 4)
    # Brier jen z ciste vyhry/prohry se sanci: (0.6-1)^2 a (0.7-0)^2
    assert s["calib_n"] == 2 and s["brier"] == pytest.approx((0.16 + 0.49) / 2)
    assert s["logloss"] > 0


def test_real_tickets_money_vs_normalized_roi_and_bankroll(tmp_path):
    def fill(led):
        led.deposit(10000)
        a = led.add_ticket(bookmaker="tipsport", odds=1.5, stake=1000, match_id="m1", market="1X2", selection="1",
                           match_day=date(2026, 11, 15), match_name="A - B")
        b = led.add_ticket(bookmaker="tipsport", odds=2.0, stake=100, match_id="m2", market="1X2", selection="1",
                           match_day=date(2026, 11, 15), match_name="C - D")
        led.add_ticket(bookmaker="tipsport", odds=2.0, stake=100, match_id="m3", market="1X2", selection="1",
                       match_day=date(2026, 11, 16), match_name="E - F")  # otevreny
        led.settle_ticket(a.id, "won", fair_closing_odds=1.4)
        led.settle_ticket(b.id, "lost")

    d = _data(tmp_path, fill)
    s = export.stats([r for r in d.records if r.group == export.TICKETS])
    assert (s["wins"], s["losses"], s["pending"]) == (1, 1, 1)
    assert s["profit_money"] == pytest.approx(400) and s["stake_sum"] == pytest.approx(1100)
    assert s["roi_real"] == pytest.approx(400 / 1100)  # skutecne ROI podle vkladu
    assert s["roi_avg"] == pytest.approx((0.5 - 1.0) / 2)  # prumer na tiket – jine cislo
    assert s["clv"] == pytest.approx(1.5 / 1.4 - 1) and s["clv_n"] == 1  # jen tiket se zavíracím kurzem
    assert [b["balance"] for b in d.bankroll] == [10000, 10500, 10400]
    ws = load_workbook(io.BytesIO(export.to_xlsx(d)))["Vývoj bankrollu"]
    assert ws.cell(9, 4).value == 10400 and ws._charts


def test_profil4_categories_dedupe_invalid_and_verified_clv(tmp_path):
    def p4(st):
        _p4_tip(st, 1, "APPROVED BET", 2.0, "won", 1.0, clv=0.05, kind="závěrečný kurz")
        _p4_tip(st, 1, "APPROVED BET", 2.1, "won", 1.1)  # opakovany sken stejne sazky = duplicita
        _p4_tip(st, 2, "WATCH", 1.8, "lost", -1.0, clv=0.10, kind="zastaralý kurz")  # neovereny zaviraci kurz
        _p4_tip(st, 3, "NO BET", 7.0, "void", 0.0)  # neplatna sazka
        _p4_tip(st, 4, "BEST AVAILABLE (WATCH)", 1.9, "push", 0.0)
        _p4_tip(st, 5, "APPROVED BET", 1.7, None, None)

    d = _data(tmp_path, p4=p4)
    g = export.groups(d.records)
    assert set(g) == set(export.P4_GROUPS.values()) and d.duplicates == 1
    off = export.stats(g[export.P4_GROUPS[export.OFFICIAL]])
    assert (off["n"], off["wins"], off["pending"], off["profit"]) == (2, 1, 1, 1.0)
    assert off["clv"] == pytest.approx(0.05) and off["clv_n"] == 1
    w = export.stats(g[export.P4_GROUPS[export.WATCH]])
    assert (w["losses"], w["push"], w["clv"]) == (1, 1, None)  # zastaraly kurz neni overene CLV
    nb = export.stats(g[export.P4_GROUPS[export.NOBET]])
    assert (nb["invalid"], nb["staked"], nb["roi"]) == (1, 0, None)


def test_bands_and_progression(tmp_path):
    picks = [_pick("a", "won", 1.4), _pick("b", "lost", 2.2), _pick("c", "won", 6.0)]
    d = _data(tmp_path, lambda led: paper.save(led, picks))
    assert [export.band(o) for o in (1.4, 2.2, 6.0)] == ["1.00–1.50", "2.00–2.50", "5.50 a více"]
    days, series = export.progression(d.records)
    assert days == ["2026-11-20"] and series["Základ"] == [pytest.approx(2 / 3)]


def test_panel_export_is_read_only(tmp_path, monkeypatch):
    from tipsbot.profil4.config import P4
    from tipsbot.storage import utcnow
    from tipsbot.web import create_app

    monkeypatch.setattr(type(P4), "db_path", property(lambda self: tmp_path / "p4.db"))
    st = Store(tmp_path / "p4.db")
    _p4_tip(st, 1, "APPROVED BET", 2.0, "won", 1.0)
    st.close()
    url = f"sqlite:///{tmp_path / 'm.db'}"
    with make_session_factory(url)() as s:
        paper.save(Ledger(s), [_pick("a", "won", 2.0), _pick("b", None, 1.9)])
        s.commit()
    c = create_app(url).test_client()
    c.get("/")  # panel si pri startu muze dotvorit tabulky; export pak uz nic menit nesmi
    digest = {f: hashlib.sha1((tmp_path / f).read_bytes()).hexdigest() for f in ("m.db", "p4.db")}
    html = c.get("/?tab=export").get_data(as_text=True)
    assert "Export výsledků" in html and "Profil 4 – oficiální (APPROVED)" in html and "Export se nepodařil" not in html
    for fmt, magic in (("xlsx", b"PK"), ("csv", "﻿# Vytvořeno".encode()), ("pdf", b"%PDF")):
        r = c.get(f"/export/{fmt}")
        assert r.status_code == 200 and r.data.startswith(magic) and "attachment" in r.headers["Content-Disposition"]
    assert {f: hashlib.sha1((tmp_path / f).read_bytes()).hexdigest() for f in digest} == digest
    assert utcnow()


def test_archive_keeps_every_changed_analysis(tmp_path):
    with make_session_factory(f"sqlite:///{tmp_path / 'a.db'}")() as s:
        led = Ledger(s)
        led.set_analysis("ENG1", '[{"a": 1}]', NOW)
        led.set_analysis("ENG1", '[{"a": 1}]', NOW + timedelta(hours=1))  # beze zmeny = zadny novy zaznam
        led.set_analysis("ENG1", '[{"a": 2}]', NOW + timedelta(hours=2))
        led.set_analysis("ESP1", "[]", NOW)  # prazdny sken se neukladá
        assert [t for _, _, t in led.archived("scan", "ENG1")] == ['[{"a": 1}]', '[{"a": 2}]']
        assert led.analysis("ENG1") == '[{"a": 2}]' and led.archive_counts() == {"scan": 2}

    st = Store(tmp_path / "p4.db")
    st.set("web_last_view", json.dumps({"kind": "scan", "n": 1}))
    st.set("web_last_view", json.dumps({"kind": "scan", "n": 1}))
    st.set("web_last_view", json.dumps({"kind": "scan", "n": 2}))
    st.archive("p4_detail", "77", json.dumps({"fixture_id": 77}))
    assert st.archive_counts() == {"p4_scan": 2, "p4_detail": 1}
    st.close()
    assert export.p4_archive_counts(tmp_path / "p4.db") == {"p4_scan": 2, "p4_detail": 1}


def test_profil3_analysis_survives_panel_cleanup(tmp_path):
    from types import SimpleNamespace

    with make_session_factory(f"sqlite:///{tmp_path / 'a.db'}")() as s:
        led = Ledger(s)
        ev = SimpleNamespace(match_id="m1", name="A - B", kickoff=NOW - timedelta(days=5))
        paper._save_analysis(led, "ENG1", ev, {"verdict": "žádná vhodná sázka"}, NOW - timedelta(days=5))
        ev2 = SimpleNamespace(match_id="m2", name="C - D", kickoff=NOW)
        paper._save_analysis(led, "ENG1", ev2, {"verdict": "sázka"}, NOW)
        assert set(paper.load_analyses(led)) == {"m2"}  # panel: staré po 2 dnech pryč
        assert {k for k, _, _ in led.archived("p3")} == {"m1", "m2"}  # archiv: navždy


def test_external_text_is_never_a_formula(tmp_path):
    evil = _pick("x", "won", 2.0)
    evil.name = '=HYPERLINK("http://example.invalid","klik")'
    d = _data(tmp_path, lambda led: paper.save(led, [evil]))
    assert "'=HYPERLINK" in export.to_csv(d).decode("utf-8-sig")
    ws = load_workbook(io.BytesIO(export.to_xlsx(d)))["Všechny tipy"]
    cell = ws.cell(7, 5)
    assert cell.value.startswith("=HYPERLINK") and cell.data_type == "s"
