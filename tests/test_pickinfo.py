"""Rozbor vyberu ve Sledovani (pozadavek 23.11.2026: Zaklad – Vyhra Monza @ 3.60 bez jakehokoli vysvetleni)."""

from datetime import datetime, timedelta

from tipsbot import analysis, paper, pickinfo
from tipsbot.analysis import MatchView, Row
from tipsbot.storage import Ledger, make_session_factory, utcnow

NOW = datetime(2026, 11, 23, 12, 0)
KO = "2026-11-23T19:45:00"


def _view(ko=KO):
    r = Row("1X2", "1", "Výhra Monza", 0.3296, 0.2540, 0.2611, 3.94, 3.6, "medián 21 kanc.", -6.0, "none",
            note="čekaný zisk pod prahem", p_second=0.296, offers=[["matchbook", 3.9], ["unibet_se", 3.8]])
    v = MatchView("m1", "ITA1", "Monza - Fiorentina", ko, True, [r], None, "")
    v.sections = [{"title": "Očekávané skóre (model)", "lines": ["Očekávané góly: Monza 1.53 – 1.77 Fiorentina"]}]
    v.absences, v.absence_checked = ["Monza: C. Ngonge – Missing Fixture (Injury)"], True
    v.weather = "8 °C, sucho"
    return v


def _pick(ko=KO):
    return paper.Pick(id="p1", league="ITA1", match_id="m1", name="Monza - Fiorentina", day=ko[:10], kickoff=ko,
                      market="1X2", selection="1", odds=3.6, book="zahraničí (medián)", p_model=0.3296, source="model",
                      created=NOW.isoformat(), flags=["čekaný zisk nad 15 % je podezřelý, ověř ručně"], profile="zaklad")


def _led():
    return Ledger(make_session_factory("sqlite://")())


def test_pick_gets_reason_and_match_analysis():
    led = _led()
    paper.save(led, [_pick()])
    assert pickinfo.enrich(led, [_view()], NOW) == 1
    (p,) = paper.load(led)
    titles = [s["title"] for s in p.info]
    assert titles == ["Proč tento výběr", "Očekávané skóre (model)", "Absence, zprávy a počasí"]
    why = " ".join(p.info[0]["lines"])
    assert "Šance podle profilu 33 %" in why and "trh bez marže 25 %" in why and "druhý model 30 %" in why
    assert "o 8 p. b. víc než trh" in why and "čekaný zisk -6.0 %" in why and "oficiální tip z toho není" in why
    assert "nad 15 %" in why and "medián zahraničních kanceláří" in why
    assert "Monza: C. Ngonge" in " ".join(p.info[2]["lines"])


def test_analysis_frozen_after_kickoff_and_backfill_only_missing():
    led = _led()
    paper.save(led, [_pick()])
    pickinfo.enrich(led, [_view()], NOW)
    later = _view()
    later.sections = [{"title": "Jiný", "lines": ["po výkopu"]}]
    assert pickinfo.enrich(led, [later], datetime(2026, 11, 23, 21, 0)) == 0  # po vykopu se rozbor nemeni
    assert paper.load(led)[0].info[1]["title"] == "Očekávané skóre (model)"
    # starsi vyber bez rozboru: doplni se z archivu skenu pred vykopem
    led2 = _led()
    paper.save(led2, [_pick()])
    led2.set_analysis("ITA1", analysis.dumps([_view()]), datetime(2026, 11, 23, 19, 29))
    led2.set_analysis("ITA1", "[]", datetime(2026, 11, 23, 20, 0))
    assert pickinfo.backfill(led2, datetime(2026, 11, 23, 21, 0)) == 1
    assert paper.load(led2)[0].info[0]["title"] == "Proč tento výběr"


def test_kombi_legs_get_analysis_and_panel_shows_it(tmp_path):
    from tipsbot.web import create_app

    now = utcnow()
    ko = (now + timedelta(days=1)).replace(microsecond=0).isoformat()
    url = f"sqlite:///{tmp_path / 'r.db'}"
    with make_session_factory(url)() as s:
        led = Ledger(s)
        leg = {"name": "Monza - Fiorentina", "league": "ITA1", "kickoff": ko, "day": ko[:10], "market": "1X2",
               "selection": "1", "label": "Výhra Monza", "odds": 3.6, "book": "unibet_se", "p": 0.33, "target": False,
               "result": None, "score": ""}
        k = paper.Pick(id="k1", league="KOMBI", match_id="k1", name="Monza - Fiorentina", day=ko[:10], kickoff=ko,
                       market="KOMBI", selection="1 sázky", odds=3.6, book="kombinace", p_model=0.33, source="model",
                       created=now.isoformat(), profile="kombi", legs=[leg])
        paper.save(led, [_pick(ko), k])
        assert pickinfo.enrich(led, [_view(ko)], now) == 2
        assert paper.load(led)[1].legs[0]["info"][0]["title"] == "Monza - Fiorentina: Proč tento výběr"
        s.commit()
    c = create_app(url).test_client()
    for prof in ("zaklad", "kombi"):
        html = c.get(f"/?tab=sledovani&profile={prof}").get_data(as_text=True)
        assert "<summary>rozbor</summary>" in html and "Proč tento výběr" in html and "Očekávané góly" in html


def test_overview_combo_has_analysis(tmp_path):
    from tipsbot.web import create_app

    ko = (utcnow() + timedelta(days=1)).replace(microsecond=0).isoformat()
    views = []
    for name, odds, p in (("Arsenal - Chelsea", 1.6, 0.70), ("Leeds - Fulham", 1.7, 0.66)):
        r = Row("1X2", "1", f"Výhra {name.split(' - ')[0]}", p, p, p, 1 / p, odds, "pinnacle", (p * odds - 1) * 100, "tip")
        v = MatchView(name, "ENG1", name, ko, True, [r], 0, "TIP")
        v.sections = [{"title": "Očekávané skóre (model)", "lines": [f"Očekávané góly {name}"]}]
        views.append(v)
    url = f"sqlite:///{tmp_path / 'o.db'}"
    with make_session_factory(url)() as s:
        led = Ledger(s)
        led.deposit(10000)
        led.set_analysis("ENG1", analysis.dumps(views), utcnow())
        s.commit()
    html = create_app(url).test_client().get("/?tab=prehled").get_data(as_text=True)
    start = html.index("Doporučený tiket")
    part = html[start:html.index("dashboard-main-column", start)]
    assert "<summary>rozbor</summary>" in part and "Arsenal - Chelsea: Proč tento výběr" in part
    assert "Leeds - Fulham: Očekávané skóre (model)" in part and "Očekávané góly Leeds - Fulham" in part
