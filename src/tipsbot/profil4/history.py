"""Historie predikci a tipu Profilu 4, vyhodnoceni a kalibrace.

Uklada se: zapas, predikce kazdeho modelu, ferovy kurz, kurz pri doporuceni a jeho zdroj/cas, kancelar,
cas predikce, zdroje dat, verze modelu a otisk konfigurace; po zapase vysledek, settlement, zisk a CLV.

Metriky: Brier score a log loss (1X2, po modelech A–D), ROI, yield, win rate, prumerne CLV – po modelech,
trzich, soutezich, kategoriich, kancelarich, rozhodnutich a mesicich. Mala data se oznaci jako neprukazna.

Backtest: walk-forward z vysledku API-Football (model se pro kazdy den odhaduje jen z drivejsich zapasu –
zadny data leakage), testovaci obdobi oddelene od trenovaciho, porovnani s naivnim modelem (cetnosti
z trenovaciho obdobi). Kurzy API uchovava jen 7 dni, proto historicky ROI z API spocitat nelze – ROI a CLV
se meri dopredu na ulozenych tipech (a zpetny test proti kurzum Pinnacle je v puvodnim `tipsbot backtest`).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

import numpy as np

from . import mathcore
from . import models as M
from . import odds as O
from .analysis import MatchAnalysis
from .config import MODEL_VERSION, Profil4Config
from .decision import APPROVED, BEST
from .provider import VOID_STATUSES, DataProvider, Fixture
from .store import Store, iso, parse_dt

MIN_SAMPLE = 100


def record(store: Store, an: MatchAnalysis, cfg: Profil4Config) -> int:
    """Ulozi predikci 1X2 vsech modelu a vybrane tipy (schvalene, kategorie, nejlepsi dostupny)."""
    fx, ms = an.fixture, an.models
    when = iso(an.created)
    sources = json.dumps({k: v.status for k, v in an.coverage.items.items()})
    h = cfg.config_hash()
    if ms is not None and ms.c.available:
        probs = {}
        for name, mat in (("A", ms.a.matrix if ms.a.available else None), ("B", ms.b.matrix if ms.b.available else None),
                          ("C", ms.c.matrix)):
            if mat is not None:
                probs[name] = [mathcore.evaluate("1X2", s, mat).p_win for s in ("1", "X", "2")]
        dp = [ms.d_probability("1X2", s) for s in ("1", "X", "2")]
        if all(x is not None for x in dp):
            tot = sum(dp)
            probs["D"] = [x / tot for x in dp]  # D je spodni odhad; pro Brier normalizovano
        store.execute("INSERT OR IGNORE INTO forecasts(fixture_id, match, league_id, league, kickoff, market, probs, "
                      "predicted_at, model_version, config_hash, data_sources) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (fx.id, fx.name, fx.league_id, fx.league, iso(fx.kickoff), "1X2", json.dumps(probs), when,
                       MODEL_VERSION, h, sources))
    if an.created >= fx.kickoff:
        # rozbor po vykopu (napr. tlacitko Detail u odehraneho zapasu) neni predpoved – tip by znal pozdejsi stav
        # a do vysledku by se zapocital neopravnene (chyba do 22.11.2026: 6 tipu ulozenych az po zapase)
        return 0
    record_rankings(store, an)
    chosen = {c.key: c for c in an.selection.approved}
    for c in an.selection.categories.values():
        if c:
            chosen.setdefault(c.key, c)
    if an.selection.best_available:
        chosen.setdefault(an.selection.best_available.key, an.selection.best_available)
    n = 0
    for c in chosen.values():
        decision = c.decision if c is not an.selection.best_available or c.decision == APPROVED else f"{BEST} ({c.decision})"
        # porovnava se ULOZENY text rozhodnuti (drive se porovnavalo "WATCH" s "BEST AVAILABLE (WATCH)" => duplicita)
        dup = store.query("SELECT 1 FROM tips WHERE fixture_id=? AND market=? AND selection=? AND bookmaker=? AND odds=? "
                          "AND decision=?", (fx.id, c.market, c.selection, c.quote.bookmaker, c.odds, decision))
        if dup:
            continue
        q = c.quote
        store.execute(
            "INSERT INTO tips(fixture_id, match, league_id, league, kickoff, market, selection, label, probs, fair_odds, "
            "odds, bookmaker, odds_source, odds_time, ev, ev_conservative, robustness, decision, category, predicted_at, "
            "model_version, config_hash, data_sources, odds_fetched_at, odds_age_min, odds_status, p_market, quality, rank, "
            "intel) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fx.id, fx.name, fx.league_id, fx.league, iso(fx.kickoff), c.market, c.selection, c.label, json.dumps(c.p),
             c.fair_odds_c, c.odds, q.bookmaker, q.source, iso(q.stamp), c.ev_c, c.ev_d, c.robustness,
             decision, c.category, when, MODEL_VERSION, h, sources, iso(q.fetched_at), q.age_minutes(an.created),
             c.quote_status, c.p_market, (c.intel.get("quality") or {}).get("score"),
             (c.intel.get("rank") or {}).get("score"),
             json.dumps({**(c.intel or {}), **({"model_guard": c.guard} if c.guard else {})}, ensure_ascii=False, default=str)
             if (c.intel or c.guard) else None))
        n += 1
    record_predictions(store, an, cfg)
    return n


RANK_STEP = 3.0  # nejmensi zmena rankingu (body), ktera sama o sobe zaklada novy zaznam


def record_rankings(store: Store, an: MatchAnalysis) -> int:
    """Opportunity Ranking VSECH predzapasovych kandidatu analyzy (nejen ulozenych tipu) do tabulky `rankings`.

    Novy radek jen pri zmene kancelare, kurzu, kvality dat nebo rozhodnuti, nebo kdyz se ranking zmeni aspon
    o RANK_STEP bodu – opakovany sken bez zmeny nic nepridava. `valid` = aktualni kurz a nezavisla kontrola bez tvrde chyby. Historicke radky se nemeni."""
    fx = an.fixture
    if an.created >= fx.kickoff:
        return 0
    with store.batch():  # vsechny kandidaty jednim commitem – kratky zamek databaze i pri 100+ radcich
        return _record_rankings(store, an)


def _record_rankings(store: Store, an: MatchAnalysis) -> int:
    fx = an.fixture
    n = 0
    for c in an.candidates:
        intel = c.intel or {}
        rk, qd = intel.get("rank") or {}, intel.get("quality") or {}
        if rk.get("score") is None:
            continue
        integ = intel.get("integrity") or {}
        valid = int(c.quote_status == O.CURRENT and c.validation is not None and not c.validation[0])
        last = store.query("SELECT bookmaker, odds, rank, quality, decision FROM rankings WHERE fixture_id=? AND market=? "
                           "AND selection=? ORDER BY id DESC LIMIT 1", (fx.id, c.market, c.selection))
        if last:
            lb, lo, lr, lq, ld = tuple(last[0])
            # ranking obsahuje aktualnost kurzu, ktera s casem plynule klesa – samotne starnuti kurzu (zmena o par
            # bodu) neni zmena prilezitosti a novy radek nezaklada (zjisteno v ostrem provozu 22.11.2026)
            if ((lb, lo, lq, ld) == (c.quote.bookmaker, c.odds, qd.get("score"), c.decision)
                    and lr is not None and abs(rk["score"] - lr) < RANK_STEP):
                continue
        oi = intel.get("odds") or {}
        detail = {"reasons": rk.get("parts", []), "quality": {k: qd.get(k) for k in ("score", "groups", "details", "parts",
                                                                                         "uncertain")},
                  "odds": {k: oi.get(k) for k in ("median", "fair_market", "dev", "books", "flags", "suspicious")},
                  "integrity": integ, "p": c.p, "p_market": c.p_market, "category": c.category,
                  "reasons_decision": c.reasons[:6], **({"model_guard": c.guard} if c.guard else {})}
        store.execute(
            "INSERT INTO rankings(computed_at, version, fixture_id, match, league, kickoff, market, selection, label, odds, "
            "bookmaker, odds_time, ev, ev_conservative, robustness, quality, rank, decision, integrity, valid, detail) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(an.created), intel.get("version", ""), fx.id, fx.name, fx.league, iso(fx.kickoff), c.market, c.selection,
             c.label, c.odds, c.quote.bookmaker, iso(c.quote.stamp), c.ev_c, c.ev_d, c.robustness, qd.get("score"),
             rk["score"], c.decision, integ.get("verdict"), valid, json.dumps(detail, ensure_ascii=False, default=str)))
        n += 1
    return n


def market_type(market: str) -> str:
    """Typ sazky: 1X2, DC, DNB, BTTS, OU, TTH, TTA, AH, EXPR."""
    return market.split("_", 1)[0]


def odds_band(odds: float | None) -> str:
    if odds is None:
        return "bez kurzu"
    for hi, name in ((1.5, "do 1.50"), (2.0, "1.50–1.99"), (3.0, "2.00–2.99"), (5.0, "3.00–4.99")):
        if odds < hi:
            return name
    return "5.00 a výš"


def record_predictions(store: Store, an: MatchAnalysis, cfg: Profil4Config) -> int:
    """Pravdepodobnost kazdeho modelu (A, B, C, D) pro kazdy trh s kurzem – pro dlouhodobou kalibraci.

    Jen predzapasove predikce (cas analyzy < vykop). Opakovany sken pred vykopem predikci prepise, takze se
    vyhodnocuje posledni predikce pred zapasem; vyhodnocena (settled) predikce se uz nikdy nemeni.
    `p` je efektivni pravdepodobnost vyhry (u linii s vracenim podminena tim, ze sazka neni vracena)."""
    fx = an.fixture
    if an.created >= fx.kickoff or an.models is None:
        return 0
    when, h, n = iso(an.created), cfg.config_hash(), 0
    for c in an.candidates:
        for model, p in c.p.items():
            if p is None or not (0.0 < p < 1.0):
                continue
            store.execute(
                "INSERT INTO predictions(fixture_id, match, league_id, league, kickoff, market, selection, market_type, "
                "model, p, p_market, market_covered, odds, bookmaker, odds_source, odds_time, odds_status, predicted_at, "
                "model_version, config_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(fixture_id, market, selection, model, model_version) DO UPDATE SET p=excluded.p, "
                "p_market=excluded.p_market, market_covered=excluded.market_covered, odds=excluded.odds, "
                "bookmaker=excluded.bookmaker, odds_source=excluded.odds_source, odds_time=excluded.odds_time, "
                "odds_status=excluded.odds_status, predicted_at=excluded.predicted_at, config_hash=excluded.config_hash "
                "WHERE predictions.result IS NULL AND excluded.predicted_at >= predictions.predicted_at",
                (fx.id, fx.name, fx.league_id, fx.league, iso(fx.kickoff), c.market, c.selection, market_type(c.market),
                 model, float(p), c.p_market, int(c.market_covered), c.odds, c.quote.bookmaker, c.quote.source,
                 iso(c.quote.stamp), c.quote_status, when, MODEL_VERSION, h))
            n += 1
    return n


CLV_POS, CLV_NEG, CLV_ZERO = "kladné", "záporné", "nulové"
# druh srovnavaciho kurzu (closing_kind)
CK_CLOSING, CK_LAST, CK_STALE, CK_MISSING, CK_LINE = (
    "závěrečný kurz", "poslední pozorovaný kurz", "zastaralý kurz", "chybí závěrečný kurz", "změna trhu nebo linie")
CK_FOREIGN = "závěrečný kurz zahraniční kanc."  # API-Football neobnovilo, zaviraci kurz z kurzoveho API (foreign.py)
CLV_KINDS = (CK_CLOSING, CK_FOREIGN)  # jen proti temto se pocita CLV
CLV_MISSING, CLV_LINE = CK_MISSING, CK_LINE  # zpetna kompatibilita nazvu


def observations(store: Store, fid: int, kickoff: datetime) -> list[dict]:
    return [dict(r) for r in store.query("SELECT * FROM odds_observations WHERE fixture_id=? AND observed_at < ? "
                                         "ORDER BY observed_at", (fid, iso(kickoff)))]


def _closing(store: Store, fid: int, book: str, market: str, sel: str, kickoff: datetime,
             rec_time: datetime | None = None, cfg: Profil4Config | None = None) -> dict:
    """Zaviraci kurz stejne kancelare (_closing_same); kdyz neni opravdu zaviraci, overi se pres zahranicni
    kancelar z kurzoveho API (Pinnacle prednostne), ktera kurz aktualizovala nejvys `closing_max_source_age_min`
    pred vykopem. Puvodni duvod zustane v closing_reason."""
    from . import foreign
    from .config import P4

    cfg = cfg or P4
    out = _closing_same(store, fid, book, market, sel, kickoff, rec_time, cfg)
    if out["kind"] == CK_CLOSING:
        return out
    ref = foreign.reference(O.history(store, fid), market, sel, kickoff, rec_time)
    if ref is None or (kickoff - ref[2]).total_seconds() / 60 > cfg.odds.closing_max_source_age_min:
        return out
    odds, fair, when, ref_book = ref
    out.update(kind=CK_FOREIGN, odds=odds, fair=fair, time=when,
               reason=f"{ref_book} (kurzové API) aktualizace {(kickoff - when).total_seconds() / 60:.0f} min před výkopem; "
                      f"{book}: {out['kind']}" + (f" – {out['reason']}" if out["reason"] else ""))
    return out


def _closing_same(store: Store, fid: int, book: str, market: str, sel: str, kickoff: datetime,
                  rec_time: datetime | None = None, cfg: Profil4Config | None = None) -> dict:
    """Srovnavaci kurz STEJNE kancelare na STEJNY trh, vyber a linii (market obsahuje linii, napr. AH_-1.5).

    Kandidat = posledni kurz pred vykopem, ktery je NOVEJSI nez doporucovaci kurz (podle casu aktualizace
    u poskytovatele, ne casu stazeni). Druh (kind):
    - zaverecny kurz: kurz byl soucasti posledniho pozorovani, ktere probehlo nejvys `closing_observed_within_min`
      pred vykopem, a poskytovatel ho aktualizoval nejvys `closing_max_source_age_min` pred vykopem;
    - zastaraly kurz: posledni aktualizace u poskytovatele je starsi nez tento limit (API kurz neobnovilo);
    - posledni pozorovany kurz: novejsi kurz existuje, ale chybi pozorovani tesne pred vykopem (pozdejsi stav
      neznamy) – NENI to zaviraci kurz;
    - zmena trhu nebo linie / chybi zaverecny kurz: s duvodem (zadne pozorovani, kvota, API neaktualizovalo...).
    CLV se pocita jen proti zaviracimu kurzu; proti poslednimu pozorovanemu je zvlast (clv_last).
    """
    from .. import profile2
    from .config import P4

    cfg = cfg or P4
    out = {"odds": None, "fair": None, "time": None, "kind": CK_MISSING, "reason": ""}
    obs = observations(store, fid, kickoff)
    ok_obs = [o for o in obs if o["n_quotes"]]
    last_obs = ok_obs[-1] if ok_obs else None
    near = (last_obs is not None and (kickoff - parse_dt(last_obs["observed_at"])).total_seconds() / 60
            <= cfg.odds.closing_observed_within_min)
    hist = [q for q in O.history(store, fid) if q.bookmaker == book and q.stamp < kickoff]
    after = [q for q in hist if rec_time is None or q.stamp > rec_time]
    mine = [q for q in after if q.market == market and q.selection == sel]
    if not mine:
        fam = market_type(market)
        if "_" in market and any(market_type(q.market) == fam and q.market != market for q in after):
            out.update(kind=CK_LINE, reason=f"{book} po doporučení nabízí jen jinou linii trhu {fam}")
            return out
        if not obs:
            out["reason"] = "kurzy před výkopem se nestáhly (zápas nebyl sledován automatem)"
        elif not ok_obs:
            out["reason"] = "stažení před výkopem selhalo: " + "; ".join(sorted({f"{o['status']} {o['note']}".strip()
                                                                                   for o in obs}))
        else:
            upd = parse_dt(last_obs["latest_update"])
            if upd is not None and rec_time is not None and upd <= rec_time:
                out.update(kind=CK_STALE, reason=f"poskytovatel kurzy od doporučení neaktualizoval (poslední aktualizace "
                                                 f"{(kickoff - upd).total_seconds() / 3600:.1f} h před výkopem)")
                return out
            out["reason"] = f"{book} po doporučení na tento výběr kurz nevypsal"
        return out
    last = max(mine, key=lambda q: q.stamp)
    sels = profile2.selections(market)
    per = {q.selection: q.odds for q in after if q.market == market and q.stamp == last.stamp}
    fair = None
    if sels and all(s in per for s in sels) and market not in ("DNB",) and not market.startswith("AH_"):
        _, probs = mathcore.devig([per[s] for s in sels])
        fair = 1.0 / probs[sels.index(sel)]
    src_age = (kickoff - last.stamp).total_seconds() / 60
    out.update(odds=last.odds, fair=fair, time=last.stamp)
    in_last = last_obs is not None and parse_dt(last_obs["latest_update"]) == last.stamp
    if src_age > cfg.odds.closing_max_source_age_min:
        out.update(kind=CK_STALE, reason=f"poslední aktualizace u poskytovatele {src_age / 60:.1f} h před výkopem "
                                         f"(limit {cfg.odds.closing_max_source_age_min} min)")
    elif near and in_last:
        out.update(kind=CK_CLOSING, reason=f"pozorováno {(kickoff - parse_dt(last_obs['observed_at'])).total_seconds() / 60:.0f} "
                                           f"min před výkopem, aktualizace {src_age:.0f} min před výkopem")
    else:
        out.update(kind=CK_LAST, reason="chybí pozorování těsně před výkopem – pozdější stav kurzu neznámý")
    return out


def _clv(odds: float, cl: dict) -> float | None:
    if cl["odds"] is None:
        return None
    return odds / cl["fair"] - 1.0 if cl["fair"] else odds / cl["odds"] - 1.0


def outcome_value(result: str) -> float | None:
    """Vysledek pro kalibraci efektivni pravdepodobnosti: vyhra 1, prohra 0; pulvyhra = vyhrana nevracena cast
    (1), pulprohra = prohrana nevracena cast (0); vraceni a neplatna sazka se nehodnoti (None)."""
    return {mathcore.WON: 1.0, mathcore.HALF_WON: 1.0, mathcore.LOST: 0.0, mathcore.HALF_LOST: 0.0}.get(result)


def settle_tip(store: Store, t, out: str, score: str, kickoff: datetime, now: datetime,
               evidence: dict | None = None) -> str:
    """Zapise vysledek tipu vcetne zaviraciho kurzu a CLV (fotbal i NHL).

    `evidence` (Faze 3.2) = puvodni data vysledku, zdroj a metoda pro auditni zaznam Settlement Integrity;
    vysledek ani zisk neovlivnuje."""
    profit = mathcore.unit_profit(out, t["odds"])
    cl = _closing(store, t["fixture_id"], t["bookmaker"], t["market"], t["selection"], kickoff, parse_dt(t["odds_time"]))
    clv_any = _clv(t["odds"], cl)
    clv = clv_any if cl["kind"] in CLV_KINDS else None  # chybejici / jiny nez zaviraci kurz neni CLV 0
    status = (CLV_POS if clv > 1e-9 else CLV_NEG if clv < -1e-9 else CLV_ZERO) if clv is not None else cl["kind"]
    store.execute("UPDATE tips SET result=?, score=?, profit=?, closing_odds=?, closing_fair_odds=?, clv=?, "
                  "clv_status=?, closing_time=?, closing_kind=?, closing_reason=?, clv_last=?, settled_at=? "
                  "WHERE id=?",
                  (out, score, profit, cl["odds"], cl["fair"], clv, status, iso(cl["time"]), cl["kind"],
                   cl["reason"], clv_any if cl["kind"] in (CK_LAST, CK_STALE) else None, iso(now), t["id"]))
    from . import settleaudit

    settleaudit.safe_record(store, t, out, profit, score, evidence, now)
    return f"{t['match']} {score}: {t['label']} @ {t['odds']:.2f} – {mathcore.SETTLE_CZ[out]} ({profit:+.2f} j.)"


NHL = "NHL"  # soutez NHL v tabulkach Profilu 4 (vyhodnocuje profil4.nhl z NHL API, ne API-Football)


def settle(store: Store, provider: DataProvider, now: datetime) -> list[str]:
    """Vyhodnoti tipy a predikce fotbalovych zapasu, ktere uz skoncily (1 pozadavek na 20 zapasu)."""
    rows = store.query("SELECT DISTINCT fixture_id FROM tips WHERE result IS NULL AND kickoff < ? AND COALESCE(league,'') != ? "
                       "UNION SELECT DISTINCT fixture_id FROM forecasts WHERE result IS NULL AND kickoff < ? AND COALESCE(league,'') != ? "
                       "UNION SELECT DISTINCT fixture_id FROM predictions WHERE result IS NULL AND kickoff < ? AND COALESCE(league,'') != ?",
                       (iso(now - timedelta(hours=2, minutes=30)), NHL) * 3)
    ids = [r["fixture_id"] for r in rows]
    if not ids:
        return []
    got = provider.fixtures_by_ids(ids, priority="MEDIUM")
    msgs = []
    for fx in got.data or []:
        if fx.status in VOID_STATUSES:
            from . import settleaudit

            for t in store.query("SELECT * FROM tips WHERE fixture_id=? AND result IS NULL", (fx.id,)):
                store.execute("UPDATE tips SET result=?, profit=0, settled_at=? WHERE id=?", (mathcore.VOID, iso(now), t["id"]))
                settleaudit.safe_record(store, t, mathcore.VOID, 0.0, None,
                                        {"raw": {"fixture_id": fx.id, "status": fx.status, "void_status": fx.status},
                                         "method": "zrušený / odložený zápas -> neplatná sázka"}, now)
            store.execute("UPDATE forecasts SET result='void' WHERE fixture_id=? AND result IS NULL", (fx.id,))
            store.execute("UPDATE predictions SET result=?, settled_at=? WHERE fixture_id=? AND result IS NULL",
                          (mathcore.VOID, iso(now), fx.id))
            msgs.append(f"{fx.name}: zápas {fx.status} – sázky neplatné")
            continue
        if not fx.finished:
            continue
        score = f"{fx.ft_home}:{fx.ft_away}"
        evidence = {"raw": {"fixture_id": fx.id, "status": fx.status, "ft": [fx.ft_home, fx.ft_away]}}
        res1x2 = "1" if fx.ft_home > fx.ft_away else "X" if fx.ft_home == fx.ft_away else "2"
        store.execute("UPDATE forecasts SET result=?, score=? WHERE fixture_id=? AND result IS NULL", (res1x2, score, fx.id))
        for t in store.query("SELECT * FROM tips WHERE fixture_id=? AND result IS NULL", (fx.id,)):
            try:
                out = mathcore.settle(t["market"], t["selection"], fx.ft_home, fx.ft_away)
            except ValueError as e:
                msgs.append(f"{fx.name} {t['label']}: nelze vyhodnotit ({e})")
                continue
            msgs.append(settle_tip(store, t, out, score, fx.kickoff, now, evidence))
        for p in store.query("SELECT id, market, selection FROM predictions WHERE fixture_id=? AND result IS NULL", (fx.id,)):
            try:
                out = mathcore.settle(p["market"], p["selection"], fx.ft_home, fx.ft_away)
            except ValueError:
                continue
            store.execute("UPDATE predictions SET result=?, outcome=?, score=?, settled_at=? WHERE id=?",
                          (out, outcome_value(out), score, iso(now), p["id"]))
    return msgs


# ---------------------------------------------------------------------------
# Metriky
# ---------------------------------------------------------------------------

def brier_multi(probs: list[list[float]], outcomes: list[int]) -> float:
    """Brier score pro vice vysledku: prumer sumy (p_i - y_i)^2 (0 = dokonale, 2 = nejhur)."""
    tot = 0.0
    for p, o in zip(probs, outcomes):
        tot += sum((pi - (1.0 if i == o else 0.0)) ** 2 for i, pi in enumerate(p))
    return tot / len(probs) if probs else math.nan


def log_loss_multi(probs: list[list[float]], outcomes: list[int], eps: float = 1e-12) -> float:
    if not probs:
        return math.nan
    return float(-np.mean([math.log(max(p[o], eps)) for p, o in zip(probs, outcomes)]))


BOOT_REPS = 2000
MIN_MATCHES_CI = 30  # pod tim se o prukaznosti nerozhoduje vubec (percentilovy bootstrap by byl nespolehlivy)


def cluster_ci(clusters: dict, reps: int = BOOT_REPS, seed: int = 7, level: float = 0.95) -> tuple[float, float, float]:
    """Pomer souctu (Σ citatel / Σ jmenovatel) a 95% interval z bootstrapu po ZAPASECH.

    Vice tipu nebo predikci z jednoho zapasu neni nezavislych: vysledek zapasu je spolecny. Prevzorkovavaji se
    proto cele zapasy (shlukovy bootstrap), ne jednotlive radky. clusters = {fixture_id: (citatel, jmenovatel)}."""
    if not clusters:
        return math.nan, math.nan, math.nan
    num = np.array([v[0] for v in clusters.values()], float)
    den = np.array([v[1] for v in clusters.values()], float)
    point = float(num.sum() / den.sum()) if den.sum() else math.nan
    k = len(num)
    if k < 2:
        return point, math.nan, math.nan
    idx = np.random.default_rng(seed).integers(0, k, (reps, k))
    d = den[idx].sum(1)
    ratios = num[idx].sum(1)[d > 0] / d[d > 0]
    tail = (1 - level) / 2 * 100  # level < 0.95 jen pro Bonferroniho korekci vice srovnani (vyssi level)
    return point, float(np.percentile(ratios, tail)), float(np.percentile(ratios, 100 - tail))


def verdict(lo: float, hi: float, matches: int, pos: str = "průkazně kladné", neg: str = "průkazně záporné") -> str:
    """Stav prukaznosti podle intervalu (ne podle samotneho poctu zapasu)."""
    if matches < MIN_MATCHES_CI or math.isnan(lo):
        return f"málo dat ({matches} záp.)"
    if lo > 0:
        return pos
    if hi < 0:
        return neg
    return "neprůkazné (interval obsahuje 0)"


def _clusters(rows: list[dict], num, den=lambda r: 1.0) -> dict:
    out: dict = defaultdict(lambda: [0.0, 0.0])
    for i, r in enumerate(rows):
        c = out[r.get("fixture_id", f"radek{i}")]
        c[0] += num(r)
        c[1] += den(r)
    return dict(out)


def pre_kickoff(r: dict) -> bool | None:
    """Byl tip ulozen pred vykopem? None = cas nelze overit (stary zaznam)."""
    pt, ko = parse_dt(r.get("predicted_at")), parse_dt(r.get("kickoff"))
    return None if pt is None or ko is None else pt < ko


def unique_bets(rows: list[dict]) -> tuple[list[dict], int]:
    """Jeden vyber jednoho zapasu = jedna sazka. Opakovane ulozeny tip (dalsi sken, jiny kurz) se do ROI
    nepocita znovu: bere se prvni schvaleny, jinak prvni platny zaznam. Tipy ulozene az po vykopu se vyradi.
    Vraci (sazky, pocet vyrazenych)."""
    best: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda r: r.get("id", 0)):
        if pre_kickoff(r) is False:  # tip ulozeny az po vykopu neni platna predpoved (do vysledku se nepocita)
            continue
        k = (r.get("fixture_id"), r["market"], r["selection"])
        cur = best.get(k)
        rank = (r["result"] == mathcore.VOID, r.get("decision") != APPROVED)
        if cur is None or rank < (cur["result"] == mathcore.VOID, cur.get("decision") != APPROVED):
            best[k] = r
    return list(best.values()), len(rows) - len(best)


def bet_stats(rows: list[dict], bankroll_units: float = 100.0) -> dict:
    """Jednotkove vklady (1 j. na tip). yield = ROI na 1 j. = zisk / obrat (sazky vyhra/prohra vc. pul; push a
    neplatne mimo – metrics.py); capital_return = zisk / vychozi kapital (bankroll_units), neni to ROI.

    Intervaly yieldu a CLV jsou ze shlukoveho bootstrapu po zapasech. CLV jen proti skutecnemu zaviracimu kurzu;
    chybejici zaviraci kurz se nepocita (neni to CLV 0). Proti poslednimu pozorovanemu kurzu zvlast (clv_last)."""
    from ..metrics import is_staked

    # jednotna definice (metrics.py, Faze 3.2.2): vraceni (push) a neplatne (void) nejsou v obratu
    push = sum(1 for r in rows if r["result"] == mathcore.PUSH)
    settled = [r for r in rows if is_staked(r["result"])]
    n = len(settled)
    profit = float(sum(r["profit"] for r in settled))
    wins = sum(1 for r in settled if r["result"] in (mathcore.WON, mathcore.HALF_WON))
    clv_rows = [r for r in settled if r.get("clv") is not None]
    last_rows = [r for r in settled if r.get("clv_last") is not None]
    matches = len({r.get("fixture_id", i) for i, r in enumerate(settled)})
    y, ylo, yhi = cluster_ci(_clusters(settled, lambda r: r["profit"]))
    c, clo, chi = cluster_ci(_clusters(clv_rows, lambda r: r["clv"]))
    clv_matches = len({r.get("fixture_id", i) for i, r in enumerate(clv_rows)})
    status = verdict(ylo, yhi, matches)
    return {"n": n, "matches": matches, "profit": profit, "yield": profit / n if n else math.nan,
            "roi": profit / n if n else math.nan, "capital_return": profit / bankroll_units, "push": push,
            "win_rate": wins / n if n else math.nan,
            "avg_clv": float(np.mean([r["clv"] for r in clv_rows])) if clv_rows else math.nan, "clv_n": len(clv_rows),
            "clv_ci95": None if math.isnan(clo) else (clo, chi), "clv_status": verdict(clo, chi, clv_matches),
            "avg_clv_last": float(np.mean([r["clv_last"] for r in last_rows])) if last_rows else math.nan,
            "clv_last_n": len(last_rows),
            "yield_ci95": None if math.isnan(ylo) else (ylo, yhi), "yield_status": status,
            "significant": status.startswith("průkazně")}


def sport_of(league: str | None) -> str:
    return "NHL" if "NHL" in (league or "").upper() else "fotbal"


def evaluate(store: Store) -> dict:
    out: dict = {"models": {}, "groups": {}}
    fc = store.query("SELECT * FROM forecasts WHERE result IN ('1','X','2')")
    per_model: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    for r in fc:
        o = {"1": 0, "X": 1, "2": 2}[r["result"]]
        for name, p in json.loads(r["probs"]).items():
            per_model[name][0].append(p)
            per_model[name][1].append(o)
    for name, (ps, os_) in sorted(per_model.items()):
        out["models"][name] = {"n": len(ps), "brier": brier_multi(ps, os_), "log_loss": log_loss_multi(ps, os_)}
    tips, out["duplicates"] = unique_bets([dict(r) for r in store.query("SELECT * FROM tips")])
    out["all"] = bet_stats(tips)
    keys = {"trh": lambda r: r["market"].split("_")[0], "soutěž": lambda r: r["league"] or "?",
            "kurzové pásmo": lambda r: odds_band(r["odds"]),
            "kategorie": lambda r: (r["category"] or "bez kategorie").split(" · ")[0],
            "kancelář": lambda r: r["bookmaker"], "rozhodnutí": lambda r: r["decision"],
            "měsíc": lambda r: (r["kickoff"] or "")[:7], "verze": lambda r: f"{r['model_version']}/{r['config_hash']}",
            # Faze 3.2.1: fotbal a NHL zvlast, i v kazdem rozhodnuti (celek nesmi zakryt rozdil sportu)
            "sport": lambda r: sport_of(r.get("league")),
            "sport · rozhodnutí": lambda r: f"{sport_of(r.get('league'))} · {r['decision']}"}
    for gname, fn in keys.items():
        groups: dict[str, list] = defaultdict(list)
        for r in tips:
            groups[fn(r)].append(r)
        out["groups"][gname] = {k: bet_stats(v) for k, v in sorted(groups.items())}
    settled = [r for r in tips if r["result"] and r["result"] != mathcore.VOID]
    out["clv_status"] = dict(sorted(_count(r.get("clv_status") or "neurčeno (starší záznam)" for r in settled).items()))
    out["closing_reasons"] = dict(sorted(_count(f"{r['closing_kind']}: {r['closing_reason']}" for r in settled
                                                if r.get("closing_kind") and r.get("closing_reason")
                                                and r["closing_kind"] != CK_CLOSING).items()))
    out["calibration"] = calibration(store)
    out["data_quality"] = data_quality(store)
    return out


def _count(items) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for x in items:
        c[x] += 1
    return c


def prob_scores(p: list[float], y: list[float], eps: float = 1e-12) -> dict:
    """Brier a log loss binarni predikce (y muze byt 0/1)."""
    pa, ya = np.clip(np.asarray(p, float), eps, 1 - eps), np.asarray(y, float)
    return {"n": len(pa), "brier": float(np.mean((pa - ya) ** 2)) if len(pa) else math.nan,
            "log_loss": float(-np.mean(ya * np.log(pa) + (1 - ya) * np.log(1 - pa))) if len(pa) else math.nan,
            "mean_p": float(pa.mean()) if len(pa) else math.nan, "rate": float(ya.mean()) if len(pa) else math.nan}


def calibration(store: Store, bins: int = 10) -> dict:
    """Kalibrace modelu A–D na vyhodnocenych predpovedich vsech trhu (ne jen 1X2).

    Celkove a po ligach, typech trhu a kurzovych pasmech; kalibracni krivka (predpoved vs. skutecnost po decilech).
    Jeden zapas dava mnoho korelovanych predpovedi, proto se uvadi i pocet zapasu – prukaznost se posuzuje podle
    nej (MIN_SAMPLE zapasu), ne podle poctu radku. Model B na trzich bez aktualnich kurzu je jen dopocet; jeho
    hodnoceni 'B (tržní trhy)' bere jen predpovedi, kde trh mel aktualni kurzy."""
    latest: dict[tuple, dict] = {}
    for r in store.query("SELECT * FROM predictions WHERE outcome IS NOT NULL ORDER BY predicted_at"):
        latest[(r["fixture_id"], r["market"], r["selection"], r["model"])] = dict(r)  # jedna predikce i pres verze
    rows = list(latest.values())
    out: dict = {"models": {}, "by": {}, "curves": {}, "matches": len({r["fixture_id"] for r in rows}),
                 "predictions": len(rows)}
    groups = {"liga": lambda r: r["league"] or "?", "typ trhu": lambda r: r["market_type"],
              "kurzové pásmo": lambda r: odds_band(r["odds"])}
    for model in ("A", "B", "C", "D"):
        mine = [r for r in rows if r["model"] == model]
        variants = [(model, mine)]
        if model == "B":
            variants.append(("B (tržní trhy)", [r for r in mine if r["market_covered"]]))
        for name, rs in variants:
            if not rs:
                continue
            sc = prob_scores([r["p"] for r in rs], [r["outcome"] for r in rs])
            sc["matches"] = len({r["fixture_id"] for r in rs})
            sc.update(_score_cis(rs))
            sc["significant"] = sc["vs_market"]["status"].startswith("průkazně") if sc.get("vs_market") else False
            out["models"][name] = sc
            out["curves"][name] = calibration_table([r["p"] for r in rs], [r["outcome"] for r in rs], bins)
            for gname, fn in groups.items():
                g: dict[str, list] = defaultdict(list)
                for r in rs:
                    g[fn(r)].append(r)
                out["by"].setdefault(gname, {})[name] = {
                    k: {**prob_scores([r["p"] for r in v], [r["outcome"] for r in v]),
                        "matches": len({r["fixture_id"] for r in v})} for k, v in sorted(g.items())}
    # trh (ferova pravdepodobnost bez marze) jako meritko pro stejne radky
    mk = [r for r in rows if r["model"] == "C" and r["p_market"] is not None and 0 < r["p_market"] < 1]
    if mk:
        out["models"]["trh (bez marže)"] = {**prob_scores([r["p_market"] for r in mk], [r["outcome"] for r in mk]),
                                            "matches": len({r["fixture_id"] for r in mk})}
        out["models"]["C na stejných řádcích"] = {**prob_scores([r["p"] for r in mk], [r["outcome"] for r in mk]),
                                                  "matches": len({r["fixture_id"] for r in mk})}
    return out


def _ll(p: float, y: float, eps: float = 1e-12) -> float:
    p = min(max(p, eps), 1 - eps)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _score_cis(rs: list[dict]) -> dict:
    """Intervaly Brier a log loss (shlukovy bootstrap po zapasech) a rozdil log loss proti trhu na stejnych radcich.

    Zaporny rozdil = model je lepsi nez trh bez marze. Prukazne je to jen kdyz cely interval lezi pod / nad nulou;
    samotny pocet zapasu (ani 100) prukaznost nezarucuje."""
    _, blo, bhi = cluster_ci(_clusters(rs, lambda r: (r["p"] - r["outcome"]) ** 2))
    _, llo, lhi = cluster_ci(_clusters(rs, lambda r: _ll(r["p"], r["outcome"])))
    out = {"brier_ci95": None if math.isnan(blo) else (blo, bhi), "log_loss_ci95": None if math.isnan(llo) else (llo, lhi)}
    mk = [r for r in rs if r["p_market"] is not None and 0 < r["p_market"] < 1]
    if mk:
        d, dlo, dhi = cluster_ci(_clusters(mk, lambda r: _ll(r["p"], r["outcome"]) - _ll(r["p_market"], r["outcome"])))
        m = len({r["fixture_id"] for r in mk})
        out["vs_market"] = {"diff": d, "ci95": None if math.isnan(dlo) else (dlo, dhi), "n": len(mk), "matches": m,
                            "status": verdict(dlo, dhi, m, pos="průkazně horší než trh", neg="průkazně lepší než trh")}
    return out


def data_quality(store: Store) -> dict:
    """Prehled kvality dat: stari kurzu pri stazeni a pri doporuceni, pokryti trhu aktualnimi kurzy, CLV."""
    q = store.query("SELECT source, fetched_at, source_time FROM odds_quotes")
    lag = []
    for r in q:
        f, s = parse_dt(r["fetched_at"]), parse_dt(r["source_time"])
        if f and s:
            lag.append((f - s).total_seconds() / 3600)
    lag_a = np.asarray(lag) if lag else np.asarray([0.0])
    tips = [dict(r) for r in store.query("SELECT odds_age_min, odds_status, clv_status, result FROM tips")]
    ages = [r["odds_age_min"] for r in tips if r["odds_age_min"] is not None]
    obs = [dict(r) for r in store.query("SELECT * FROM odds_observations")]
    preds = store.query("SELECT market_type, market_covered, result FROM predictions WHERE model='C'")
    cover: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in preds:
        cover[r["market_type"]][0] += 1
        cover[r["market_type"]][1] += int(bool(r["market_covered"]))
    return {
        "quotes": len(q), "books": len({r[0] for r in store.query("SELECT DISTINCT bookmaker FROM odds_quotes")}),
        "lag_median_h": float(np.median(lag_a)) if lag else None,
        "lag_over_6h": float(np.mean(lag_a > 6)) if lag else None,
        "tips": len(tips), "tip_age_median_min": float(np.median(ages)) if ages else None,
        "tip_status": dict(_count(r["odds_status"] or "neuloženo (starší záznam)" for r in tips)),
        "predictions": len(preds), "predictions_settled": sum(1 for r in preds if r["result"]),
        "market_covered": {k: (v[1], v[0]) for k, v in sorted(cover.items())},
        "observations": len(obs), "observations_status": dict(_count(o["status"] for o in obs)),
        "obs_update_age_h": _median([(parse_dt(o["kickoff"]) - parse_dt(o["latest_update"])).total_seconds() / 3600
                                     for o in obs if o["latest_update"] and o["kickoff"] and o["slot_min"] == min(
                                         x["slot_min"] for x in obs)]),
    }


def _median(xs: list[float]) -> float | None:
    return float(np.median(xs)) if xs else None


# ---------------------------------------------------------------------------
# Backtest modelu A
# ---------------------------------------------------------------------------

def backtest(history: list[Fixture], test_start: date, refit_days: int = 7) -> dict:
    """Walk-forward 1X2 modelu A na vysledcich: trenink = vse pred dnem predikce, test = od test_start."""
    from ..backtest import walk_forward

    df = M.matches_frame(history)
    if df.empty:
        return {"n": 0, "note": "žádná data"}
    preds = walk_forward(df, test_start, refit_days=refit_days)
    if preds.empty:
        return {"n": 0, "note": "model nešlo odhadnout (málo zápasů před testovacím obdobím)"}
    train = df[df["date"] < test_start]
    if train.empty:
        return {"n": 0, "note": "chybí tréninkové období"}
    hg, ag = train["home_goals"].to_numpy(), train["away_goals"].to_numpy()
    base = [float((hg > ag).mean()), float((hg == ag).mean()), float((hg < ag).mean())]
    outcomes = [0 if h > a else 1 if h == a else 2 for h, a in zip(preds["home_goals"], preds["away_goals"])]
    model = [[r.p1, r.px, r.p2] for r in preds.itertuples()]
    naive = [base] * len(outcomes)
    return {
        "n": len(outcomes), "train_n": int(len(train)), "test_start": test_start.isoformat(),
        "model": {"brier": brier_multi(model, outcomes), "log_loss": log_loss_multi(model, outcomes)},
        "naive": {"brier": brier_multi(naive, outcomes), "log_loss": log_loss_multi(naive, outcomes)},
        "calibration": calibration_table([p[0] for p in model], [1 if o == 0 else 0 for o in outcomes]),
        "significant": len(outcomes) >= 300,
    }


def calibration_table(p: list[float], y: list[float], bins: int = 5) -> list[dict]:
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, x in enumerate(p) if lo <= x < hi or (hi == 1 and x == 1)]
        if idx:
            out.append({"bin": f"{lo:.0%}–{hi:.0%}", "n": len(idx), "predicted": float(np.mean([p[i] for i in idx])),
                        "actual": float(np.mean([y[i] for i in idx]))})
    return out


def parse_row_time(s: str | None) -> datetime | None:
    return parse_dt(s)
