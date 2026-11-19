"""Watchlist: sledovani zapasu, trhu, minimalnich kurzu a cekani na sestavy.

Kontrola znovu spusti hlubokou analyzu sledovaneho zapasu (vetsina dat jde z cache; nove pozadavky jen po
vyprseni TTL – kurzy 3 h, sestavy 10 min tesne pred vykopem) a hlasi jen skutecne zmeny s duvodem:
dosazeni minimalniho kurzu, rust/pokles kurzu, zmena EV o vic nez 2 p.b., nove potvrzene sestavy, zmena
rozhodnuti. Zapas po vykopu se automaticky ukonci.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .analysis import MatchAnalysis
from .config import Profil4Config
from .provider import DataProvider
from .store import Store, iso, parse_dt


@dataclass
class Alert:
    watch_id: int
    match: str
    text: str


def add(store: Store, fixture_id: int, match: str, kickoff: datetime | None, market: str = "", selection: str = "",
        min_odds: float | None = None, wait_lineups: bool = False, now: datetime | None = None, note: str = "") -> int:
    from .store import utcnow

    cur = store.execute("INSERT INTO watchlist(fixture_id, match, kickoff, market, selection, min_odds, wait_lineups, "
                        "created, note) VALUES(?,?,?,?,?,?,?,?,?)",
                        (fixture_id, match, iso(kickoff), market, selection, min_odds, 1 if wait_lineups else 0,
                         iso(now or utcnow()), note))
    return int(cur.lastrowid)


def items(store: Store, active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM watchlist" + (" WHERE active=1" if active_only else "") + " ORDER BY kickoff"
    return [dict(r) for r in store.query(q)]


def remove(store: Store, watch_id: int) -> None:
    store.execute("UPDATE watchlist SET active=0 WHERE id=?", (watch_id,))


def evaluate_item(w: dict, an: MatchAnalysis, now: datetime) -> tuple[list[str], dict]:
    """Porovna stav sledovane polozky s novou analyzou. Vraci (duvody upozorneni, nove hodnoty)."""
    msgs = []
    cand = None
    if w["market"]:
        cand = next((c for c in an.candidates if c.market == w["market"] and c.selection == w["selection"]), None)
    else:
        cand = an.selection.approved[0] if an.selection.approved else an.selection.best_available
    lineups = 1 if an.lineup_state == "confirmed" else 0
    new = {"last_odds": cand.odds if cand else None, "last_ev": cand.ev_c if cand else None,
           "last_decision": cand.decision if cand else None, "last_lineups": lineups}
    if cand is None:
        if w["market"]:
            msgs.append("sledovaný trh teď nemá žádný kurz")
        return msgs, new
    lab = f"{cand.label} ({cand.quote.bookmaker})"
    if w["last_odds"] is not None and abs(cand.odds - w["last_odds"]) >= 0.02:
        msgs.append(f"{lab}: kurz {'vzrostl' if cand.odds > w['last_odds'] else 'klesl'} {w['last_odds']:.2f} → {cand.odds:.2f}")
    if w["min_odds"] and cand.odds >= w["min_odds"] and (w["last_odds"] is None or w["last_odds"] < w["min_odds"]):
        msgs.append(f"{lab}: dosažen minimální kurz {w['min_odds']:.2f} (teď {cand.odds:.2f})")
    if w["last_ev"] is not None and abs(cand.ev_c - w["last_ev"]) >= 0.02:
        msgs.append(f"{lab}: EV {w['last_ev']:+.1%} → {cand.ev_c:+.1%}")
    if w["last_decision"] and cand.decision != w["last_decision"]:
        msgs.append(f"{lab}: rozhodnutí {w['last_decision']} → {cand.decision} ({'; '.join(cand.reasons[:2])})")
    if lineups and not w["last_lineups"]:
        msgs.append("sestavy jsou potvrzené" + (" – můžeš vyhodnotit sázku" if w["wait_lineups"] else ""))
    return msgs, new


def check(store: Store, provider: DataProvider, now: datetime, cfg: Profil4Config, analyse=None) -> list[Alert]:
    """`analyse(fixture_id)` -> MatchAnalysis (vychozi = hunter.analyze_fixture bez zapisu do historie)."""
    if analyse is None:
        from .hunter import analyze_fixture

        def analyse(fid):
            return analyze_fixture(provider, fid, now, cfg, record=False, simulations=10_000)
    alerts = []
    for w in items(store):
        ko = parse_dt(w["kickoff"])
        if ko and now >= ko:
            store.execute("UPDATE watchlist SET active=0 WHERE id=?", (w["id"],))
            alerts.append(Alert(w["id"], w["match"], "zápas začal – sledování ukončeno"))
            continue
        try:
            an = analyse(w["fixture_id"])
        except Exception as e:  # noqa: BLE001 - jedna polozka nesmi shodit kontrolu ostatnich
            alerts.append(Alert(w["id"], w["match"], f"kontrola selhala: {e}"))
            continue
        msgs, new = evaluate_item(w, an, now)
        store.execute("UPDATE watchlist SET last_odds=?, last_ev=?, last_decision=?, last_lineups=?, last_check=? "
                      "WHERE id=?", (new["last_odds"], new["last_ev"], new["last_decision"], new["last_lineups"],
                                     iso(now), w["id"]))
        alerts.extend(Alert(w["id"], w["match"], m) for m in msgs)
    return alerts
