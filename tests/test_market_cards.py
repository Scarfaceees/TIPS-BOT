"""Responzivni trhy: kategorie filtru, karty na mobilu vedle tabulky na pocitaci, vsechny trhy Profilu 4."""

import json

from test_profil4_flow import FID, NOW, P4, make
from tipsbot.profil4 import hunter, view
from tipsbot.web import MARKET_CATS, create_app, market_cat


def test_market_categories_match_existing_market_codes():
    assert [market_cat(m) for m in ("OU_2.5", "OU60_5.5", "BTTS", "1X2", "DC", "MONEYLINE", "TTH_1.5", "TTA_0.5",
                                     "KARTY_4.5", "ROHY_9.5", "AH_-1.75", "PUCKLINE_1.5R")] == [
        "goly", "goly", "goly", "1x2", "1x2", "1x2", "tymy", "tymy", "karty", "rohy", "handicap", "handicap"]
    assert [k for k, _ in MARKET_CATS] == ["all", "goly", "1x2", "tymy", "karty", "rohy", "handicap"]


def test_profil4_view_lists_all_markets_with_bookmaker_odds():
    prov, _, _, _ = make()
    an = hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000, record=False)
    v = view.analysis(an, NOW)
    assert len(v["markets"]) == len(an.candidates)
    m = v["markets"][0]
    assert {"label", "p_win", "min_odds", "odds", "quote_status", "models", "reasons", "risks", "offers", "market_code"} <= set(m)
    assert m["offers"] and all({"book", "odds", "status"} <= set(o) for o in m["offers"])
    assert set(m["models"]) == {"A", "B", "C", "D"}


def test_profil4_page_renders_table_and_cards_with_real_categories(tmp_path):
    from tipsbot.profil4.store import open_store

    prov, _, _, _ = make()
    v = view.analysis(hunter.analyze_fixture(prov, FID, NOW, P4, simulations=1_000, record=False), NOW)
    st = open_store()
    st.set("web_last_view", json.dumps(v, ensure_ascii=False, default=str))
    st.close()
    html = create_app(f"sqlite:///{tmp_path / 'p.db'}").test_client().get("/?tab=profil4").get_data(as_text=True)
    assert "Všechny trhy" in html and html.count('<details class="mcard') == len(v["markets"])
    assert html.count("<tr data-cat=") == len(v["markets"])  # desktopova tabulka zustava
    cats = {market_cat(m["market_code"]) for m in v["markets"]}
    for k, name in MARKET_CATS[1:]:  # filtr bez trhu je vypnuty, nic se nevymysli
        chip = html.split(f'data-cat="{k}"', 1)[1].split(">", 1)[0]
        assert ("disabled" in chip) == (k not in cats), (k, chip)
    assert "Doporučený trh" in html
