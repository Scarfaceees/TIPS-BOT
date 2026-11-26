"""Phase 3.5.1 / M1 – merici evidence NHL modelu (jen zaznam, rozhodovani ji NECTE a nemeni).

Pro kazdy rozhodovaci okamzik (sken x zapas) a trh se ulozi vedle sebe:
- p_raw   – cisty model: stejne ocekavane goly a stejny seed jako v produkci, ale BEZ ukotveni na trzni total;
- p_used  – model skutecne pouzity v rozhodovani (vystup ModelProvider.probabilities, u OU obvykle ukotveny);
- p_mkt_sharp – ferova linie hlavniho bota (ostre kancelare, devig.consensus_line);
- p_mkt_p4    – trh B Profilu 4 (Pinnacle bez marze, jinak median >= 2 kancelari, profil4.nhl.market_b);
- p_blend_main – smes hlavniho bota (logit, devig.blend_probability), p_blend_p4 / p_cons_p4 – C a D Profilu 4.

Pravidla (Phase 3.5.1, docs/phase-3-5-1-nhl-measurement-proposal.md):
- model ani jeho matematika se nemeni; pouzivaji se jen existujici funkce (expected_goals, simulate, _total_anchor,
  consensus_line, blend_probability, market_b);
- dodatecna simulace ma VLASTNI generator `default_rng(seed)` – stav generatoru produkcniho vypoctu se nemeni;
- vsechny kurzy snimku jsou z jednoho stazeni (jedne cache), casy se ukladaji a kontroluji;
- chybejici nebo nesrovnatelna data se vyslovne oznaci (missing / exclusion), nic se nedoplnuje nahradni hodnotou;
- tabulky jen pribyvaji (zadny UPDATE ani DELETE), historie se zpetne nedoplnuje.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

MEASURE_VERSION = "m2-2"
# Pravidla srovnatelnosti (kap. 5 navrhu). Patri mereni, ne rozhodovaci konfiguraci (config_hash se nemeni).
REF_MAX_AGE_MIN = 180.0  # S3: referencni trh nejvys tolik minut stary v okamziku vypoctu
DATA_MAX_LAG_DAYS = 3  # S5: posledni zapas v datech MoneyPuck nejvys tolik dni pred dnem zapasu
P4_MIN_BOOKS = 3
# F1: The Odds API – evidence B1 (odds_api_calls) pro skutecny cas stazeni kurzu NHL
NHL_ODDS_ENDPOINT = "/sports/icehockey_nhl/odds"
FETCH_WINDOW_H = 3  # pozadavek musi lezet mezi zacatkem pruchodu (cas cache) a +3 h
# CallRecord.ts = ZACATEK pozadavku oriznuty na sekundy; data odpovidaji stavu pri odpovedi. Cas poskytovatele smi
# proto byt o dobu pozadavku pozdeji (requests timeout 30 s na spojeni/cteni + oriznuti) – nad tuto mez je to anomalie.
STAMP_TOLERANCE_S = 60  # S2 (varianta Profil 4): median aspon 3 kancelari, jinak jen Pinnacle

FAMILIES = ("MONEYLINE", "PUCKLINE", "OU")


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def family(market: str) -> str:
    return market.split("_")[0]


def seed_of(match_id: str) -> int:
    """Stejny seed jako ModelProvider.probabilities (pipeline.py)."""
    return zlib.crc32(match_id.encode())


def _team_games(ratings, team: str) -> int:
    past = ratings.past
    season = int(past["season"].max())
    return int(((past["team"] == team) & (past["season"] == season)).sum())


def _file_mtimes() -> dict[str, str | None]:
    from .. import nhl_data

    out = {}
    for name, path in (("all_teams", nhl_data.nhl_csv_path()), ("goalies", nhl_data.goalies_csv_path())):
        try:
            out[name] = _iso(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))
        except OSError:
            out[name] = None
    return out


def _p4_reference(quotes, exchanges) -> dict[str, tuple[list[str], datetime | None]]:
    """Kancelare a nejnovejsi cas, ze kterych market_b sklada trh B ({trh: (kancelare, cas)}) – stejne seskupeni."""
    from .nhl import selections

    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for q in sorted((q for q in quotes if q.bookmaker not in exchanges), key=lambda q: q.stamp):
        d = latest.setdefault((q.market, q.bookmaker), {"stamp": q.stamp, "sels": {}})
        if q.stamp > d["stamp"]:
            d.update(stamp=q.stamp, sels={})
        d["sels"][q.selection] = q.odds
    out: dict[str, tuple[list[str], datetime | None]] = {}
    for (mk, book), d in latest.items():
        sels = selections(mk)
        if not sels or not all(s in d["sels"] for s in sels):
            continue
        books, stamp = out.get(mk, ([], None))
        out[mk] = (sorted(books + [book]), max(x for x in (stamp, d["stamp"]) if x is not None))
    return out


def build(fx, ev, quotes, provider, model_used: dict | None, *, now: datetime, odds_fetched_at: datetime | None,
          scan_id: str, weight_p4: float, config_hash: str = "", overrides: dict[str, str] | None = None,
          odds_cost_source: str = "cache", settings=None, odds_pass_started_at: datetime | None = None,
          fetch_evidence: dict | None = None) -> tuple[dict, list[dict]]:
    """Snimek a radky mereni pro jeden zapas. Cista funkce: nic nezapisuje, nemeni `provider` ani `model_used`.

    `ev` = oddsapi.Event ze stejneho stazeni jako `quotes`; `model_used` = presne to, co dostalo rozhodovani."""
    from .. import devig, oddsapi
    from ..config import LEAGUES, SETTINGS
    from ..models import nhl as NM
    from ..names import nhl_abbr
    from ..pipeline import _total_anchor
    from .nhl import EXCHANGES, LEAGUE, market_b, selections

    settings = settings or SETTINGS
    overrides = overrides or {}
    computed_at = _aware(now)
    kickoff = _aware(fx.kickoff)
    missing: list[str] = []
    snap: dict[str, Any] = dict.fromkeys(_SNAP_COLS)  # vse, co se nezjisti, zustane vyslovne prazdne (NULL)
    snap |= {
        "scan_id": scan_id, "fixture_id": fx.id, "match": fx.name, "kickoff": _iso(kickoff),
        "computed_at": _iso(computed_at), "odds_fetched_at": _iso(odds_fetched_at), "odds_cost_source": odds_cost_source,
        "seed": seed_of(ev.match_id), "model_version": settings.model_version, "config_hash": config_hash,
        "code_rev": os.environ.get("TIPSBOT_CODE_REV", "unknown"), "measure_version": MEASURE_VERSION,
        "model_weight_main": settings.value.for_league(LEAGUE).model_weight, "model_weight_p4": weight_p4,
        "data_file_mtime": json.dumps(_file_mtimes()),
        "odds_pass_started_at": _iso(odds_pass_started_at),
        "odds_call_id": (fetch_evidence or {}).get("call_id"),
        "odds_fetch_evidence": json.dumps(_public_evidence(fetch_evidence), ensure_ascii=False) if fetch_evidence else None,
    }

    # --- model: kontext, cisty vypocet, ukotveni --------------------------------------------------------------
    ratings = getattr(provider, "ratings", None)
    home, away = nhl_abbr(ev.home), nhl_abbr(ev.away)
    ctx = ratings.context(home, away, ev.kickoff.date()) if ratings is not None and home and away else None
    raw: dict | None = None
    if ratings is None:
        missing.append("model NHL není k dispozici (chybí data MoneyPuck)")
    elif ctx is None:
        missing.append(f"tým bez ratingu: {', '.join(n for n, t in ((ev.home, home), (ev.away, away)) if not t or t not in ratings.ratings)}")
    if ratings is not None:
        last = ratings.past["date"].max()
        snap["data_last_game"] = str(last)
        if (ev.kickoff.date() - last).days > DATA_MAX_LAG_DAYS:
            missing.append(f"data MoneyPuck končí {last} (víc než {DATA_MAX_LAG_DAYS} dny před zápasem)")
        for side, team in (("home", home), ("away", away)):
            if team and team in ratings.ratings:
                games = _team_games(ratings, team)
                snap[f"{side}_games"] = games
                snap[f"cold_start_{side}"] = int(games < settings.nhl.cold_start_games)
            snap[f"goalie_{side}_src"] = ("manual" if team in overrides else "weighted" if team in ratings.goalies
                                          else "missing")  # missing = Goalie() s GSAx 0 (vychozi hodnota modelu)
    if ctx is not None:
        snap["b2b_home"], snap["b2b_away"] = int(ctx.home_b2b_travel), int(ctx.away_b2b_travel)
        lam_h, lam_a = NM.expected_goals(ctx, settings.nhl)
        snap["lambda_raw_h"], snap["lambda_raw_a"] = lam_h, lam_a
        # vlastni generator se stejnym seedem: produkcni vypocet (a jeho generator) zustava nedotcen
        raw = NM.simulate(lam_h, lam_a, settings.nhl, np.random.default_rng(snap["seed"]))
    anchor = _total_anchor(ev, settings)
    snap["anchored"] = int(bool(model_used and model_used.get("anchored")))
    if anchor is not None:
        snap["anchor_market"], snap["anchor_target"] = anchor
    if model_used is not None:
        snap["lambda_used_h"], snap["lambda_used_a"] = model_used.get("lambda_home"), model_used.get("lambda_away")
        if bool(snap["anchored"]) != (anchor is not None):
            missing.append("nesoulad ukotvení: příznak modelu neodpovídá tržnímu totalu snímku")
        if raw is not None and snap.get("lambda_used_h") and snap.get("lambda_raw_h"):
            snap["anchor_k"] = snap["lambda_used_h"] / snap["lambda_raw_h"] if snap["anchored"] else 1.0
            if not snap["anchored"] and any(abs(raw[k] - model_used[k]) > 1e-12 for k in raw if k in model_used):
                missing.append("čistý model se neshoduje s použitým modelem bez ukotvení (jiný vstup nebo seed)")
    elif ctx is not None:
        missing.append("rozhodování nedostalo model, přestože kontext existuje")

    # --- trh: ostre kancelare (hlavni bot) a trh B Profilu 4, vse z tehoz stazeni ------------------------------
    # F3: casy poskytovatele se sbiraji u VSECH kancelari (zadny predcasny konec); chybejici nebo nemozny cas jedne
    # kancelare se zapise k ni (book_time_anomalies) a vyradi jen radky, jejichz reference tu kancelar pouziva.
    fetched = _aware(odds_fetched_at) if odds_fetched_at is not None else None
    stamps_by_book: dict[tuple[str, str], datetime] = {}
    anomalies: dict[str, dict[str, str]] = {}
    for q in quotes:
        k = (q.bookmaker, q.market)
        if q.source_time is None:  # OddsQuote.stamp by tu vratil cas skenu – to neni cas poskytovatele
            anomalies.setdefault(q.bookmaker, {})[q.market] = "chybí čas poskytovatele"
            continue
        st_q = _aware(q.source_time)
        stamps_by_book[k] = max(stamps_by_book.get(k, st_q), st_q)
        if fetched is not None and (st_q - fetched).total_seconds() > STAMP_TOLERANCE_S:
            anomalies.setdefault(q.bookmaker, {})[q.market] = (
                f"čas poskytovatele {_iso(st_q)} je po stažení {_iso(fetched)} (víc než {STAMP_TOLERANCE_S} s)")
    snap["book_time_anomalies"] = json.dumps(anomalies, ensure_ascii=False, sort_keys=True)
    if odds_fetched_at is None:
        why = (fetch_evidence or {}).get("reason") or "evidence B1 nepředána"
        missing.append(f"S0: čas stažení kurzů neprokázán ({why})")
    elif fetched > computed_at:
        missing.append("kurzy stažené až po výpočtu modelu")
    sharp_keys = settings.odds_api.sharp_keys
    sharp: dict[str, dict] = {}
    for mo in ev.markets.values():
        sels = selections(mo.market)
        if sels is None or tuple(mo.selections) != sels:
            continue
        sh, _offers = oddsapi.sharp_and_offers(mo, settings)
        try:
            line = devig.consensus_line(sh)
        except devig.DevigError:
            continue
        books = [b for b in mo.books if b in sharp_keys]
        st = [stamps_by_book[(b, mo.market)] for b in books if (b, mo.market) in stamps_by_book]
        bad = {b: anomalies[b][mo.market] for b in books if mo.market in anomalies.get(b, {})}
        sharp[mo.market] = {"probs": dict(zip(sels, line.probs)), "sources": dict(zip(line.sources, line.weights)),
                            "books": books, "stamp": max(st) if st and not bad else None, "bad": bad}
    p4 = market_b(quotes)
    p4_ref = _p4_reference(quotes, EXCHANGES)
    all_sharp_st = [s["stamp"] for s in sharp.values() if s["stamp"] is not None]
    snap["ref_sharp_books"] = json.dumps(sorted({b for s in sharp.values() for b in s["books"]}))
    snap["ref_sharp_stamp_min"] = _iso(min(all_sharp_st)) if all_sharp_st else None
    snap["ref_sharp_stamp_max"] = _iso(max(all_sharp_st)) if all_sharp_st else None
    p4_st = [st for _, st in p4_ref.values() if st is not None]
    snap["ref_p4_books"] = json.dumps(sorted({b for books, _ in p4_ref.values() for b in books}))
    snap["ref_p4_stamp_max"] = _iso(max(p4_st)) if p4_st else None
    ref_st = max(all_sharp_st) if all_sharp_st else (max(p4_st) if p4_st else None)
    snap["ref_age_min"] = (computed_at - _aware(ref_st)).total_seconds() / 60 if ref_st else None

    # --- radky trhu --------------------------------------------------------------------------------------------
    allowed = set(LEAGUES[LEAGUE].markets)
    markets = sorted({mo.market for mo in ev.markets.values() if selections(mo.market) == tuple(mo.selections)}
                     | {q.market for q in quotes if selections(q.market)})
    snap_excl = list(missing)
    if computed_at >= kickoff:
        snap_excl.append("S1: výpočet až po začátku zápasu")
    w_main, rows = snap["model_weight_main"], []
    for mk in markets:
        sels = selections(mk) or ()
        for sel in sels:
            key = f"{mk}:{sel}"
            p_raw = raw.get(key) if raw else None
            p_used = model_used.get(key) if model_used else None
            sh = sharp.get(mk)
            p_sh = sh["probs"].get(sel) if sh else None
            pb, src = p4.get(key, (None, ""))
            books_p4, st_p4 = p4_ref.get(mk, ([], None))
            if src.startswith("Pinnacle"):  # market_b bere Pinnacle prednostne – ostatni kancelare trh B netvori
                books_p4 = ["pinnacle"]
                st_p4 = stamps_by_book.get(("pinnacle", mk))
            # hlavni bot: bez ostre linie trh nehodnoti; bez modelu bere p_model = p_market (build_candidates)
            if p_sh is None:
                pbm, kind_m = None, "bez ostré linie – hlavní bot trh nehodnotí"
            elif p_used is None:
                pbm, kind_m = p_sh, "jen trh (model chybí, systém použije p_model = p_market)"
            else:
                pbm, kind_m = devig.blend_probability(p_used, p_sh, w_main), "směs"
            # Profil 4: C a D presne jako profil4.nhl.candidates
            if p_used is not None and pb is not None:
                pc, kind_p = weight_p4 * p_used + (1 - weight_p4) * pb, "směs"
            elif p_used is not None or pb is not None:
                pc = p_used if p_used is not None else pb
                kind_p = "jen model (trh B chybí)" if pb is None else "jen trh (model A chybí)"
            else:
                pc, kind_p = None, "nic"
            pd = min(x for x in (pb, pc) if x is not None) if (pb is not None or pc is not None) else None
            offers = [q for q in quotes if q.market == mk and q.selection == sel and q.bookmaker not in EXCHANGES]
            best = max(offers, key=lambda q: q.odds) if offers else None
            excl_s, excl_p = list(snap_excl), list(snap_excl)
            if p_raw is None:
                excl_s.append("chybí čistý model")
                excl_p.append("chybí čistý model")
            if p_sh is None:
                excl_s.append("S2: chybí ostrá férová linie")
            elif sh["bad"]:
                excl_s.extend(f"S3: ostrá kancelář {b}: {why}" for b, why in sorted(sh["bad"].items()))
            elif sh["stamp"] is None:
                excl_s.append("S3: neznámý čas poskytovatele ostré linie")
            elif (computed_at - _aware(sh["stamp"])).total_seconds() / 60 > REF_MAX_AGE_MIN:
                excl_s.append(f"S3: ostrá linie starší než {REF_MAX_AGE_MIN:.0f} min")
            if pb is None:
                excl_p.append("S2: chybí trh B")
            elif not src.startswith("Pinnacle") and len(books_p4) < P4_MIN_BOOKS:
                excl_p.append(f"S2: trh B jen z {len(books_p4)} kanceláří (min. {P4_MIN_BOOKS})")
            bad_p4 = sorted(b for b in books_p4 if mk in anomalies.get(b, {}))
            if pb is not None and bad_p4:
                excl_p.extend(f"S3: kancelář trhu B {b}: {anomalies[b][mk]}" for b in bad_p4)
            elif pb is not None and st_p4 is not None and (computed_at - _aware(st_p4)).total_seconds() / 60 > REF_MAX_AGE_MIN:
                excl_p.append(f"S3: trh B starší než {REF_MAX_AGE_MIN:.0f} min")
            rows.append({
                "fixture_id": fx.id, "market": mk, "selection": sel, "market_family": family(mk),
                "p_raw": p_raw, "p_used": p_used,
                "independence_used": ("chybí" if p_used is None else "ukotveno na tržní total" if snap["anchored"]
                                      else "nezávislý"),
                "p_mkt_sharp": p_sh, "mkt_sharp_sources": json.dumps(sh["sources"]) if sh else None,
                "mkt_sharp_stamp": _iso(sh["stamp"]) if sh else None,
                "p_mkt_p4": pb, "mkt_p4_source": src or None, "mkt_p4_books": json.dumps(books_p4) if pb is not None else None,
                "mkt_p4_stamp": _iso(st_p4) if pb is not None else None,
                "p_blend_main": pbm, "blend_main_kind": kind_m, "p_blend_p4": pc, "p_cons_p4": pd, "blend_p4_kind": kind_p,
                "best_odds": best.odds if best else None, "best_book": best.bookmaker if best else None,
                "best_stamp": _iso(best.stamp) if best else None,
                "in_main": int(p_sh is not None and mk in allowed), "in_p4": int(best is not None),
                "comparable_sharp": int(not excl_s), "comparable_p4": int(not excl_p),
                "exclusion": json.dumps({"sharp": excl_s, "p4": excl_p}, ensure_ascii=False),
            })
    snap["missing"] = json.dumps(missing, ensure_ascii=False)
    snap["comparable"] = int(not snap_excl)
    snap["exclusion"] = json.dumps(snap_excl, ensure_ascii=False)
    snap["input_hash"] = input_hash(snap, rows)
    return snap, rows


# Vstupy snimku (bez casu vypoctu a z nej odvozeneho stari/srovnatelnosti): stejny otisk = stejne meritelne vstupy.
_HASH_SNAP = ("fixture_id", "kickoff", "odds_fetched_at", "odds_pass_started_at", "odds_call_id", "ref_sharp_books", "ref_sharp_stamp_max", "ref_p4_books",
              "ref_p4_stamp_max", "data_last_game", "data_file_mtime", "goalie_home_src", "goalie_away_src", "seed",
              "lambda_raw_h", "lambda_raw_a", "anchored", "anchor_market", "anchor_target", "lambda_used_h",
              "lambda_used_a", "model_weight_main", "model_weight_p4", "missing", "model_version", "config_hash",
              "measure_version")
_HASH_ROW = ("market", "selection", "p_raw", "p_used", "p_mkt_sharp", "p_mkt_p4", "mkt_p4_books", "p_blend_main",
             "p_blend_p4", "best_odds", "best_book", "best_stamp")


def input_hash(snap: dict, rows: list[dict]) -> str:
    raw = json.dumps([[snap.get(k) for k in _HASH_SNAP], [[r.get(k) for k in _HASH_ROW] for r in rows]],
                     sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


_SNAP_COLS = ("scan_id", "fixture_id", "match", "kickoff", "computed_at", "odds_fetched_at", "odds_cost_source",
              "ref_sharp_books", "ref_sharp_stamp_min", "ref_sharp_stamp_max", "ref_p4_books", "ref_p4_stamp_max",
              "ref_age_min", "data_last_game", "data_file_mtime", "home_games", "away_games", "cold_start_home",
              "cold_start_away", "goalie_home_src", "goalie_away_src", "b2b_home", "b2b_away", "seed", "lambda_raw_h",
              "lambda_raw_a", "anchored", "anchor_market", "anchor_target", "anchor_k", "lambda_used_h", "lambda_used_a",
              "model_weight_main", "model_weight_p4", "missing", "comparable", "exclusion", "model_version",
              "config_hash", "code_rev", "measure_version", "input_hash", "odds_pass_started_at", "odds_call_id",
              "odds_fetch_evidence", "book_time_anomalies")
_ROW_COLS = ("fixture_id", "market", "selection", "market_family", "p_raw", "p_used", "independence_used",
             "p_mkt_sharp", "mkt_sharp_sources", "mkt_sharp_stamp", "p_mkt_p4", "mkt_p4_source", "mkt_p4_books",
             "mkt_p4_stamp", "p_blend_main", "blend_main_kind", "p_blend_p4", "p_cons_p4", "blend_p4_kind", "best_odds",
             "best_book", "best_stamp", "in_main", "in_p4", "comparable_sharp", "comparable_p4", "exclusion")


def record(store, snap: dict, rows: list[dict]) -> int | None:
    """Ulozi snimek a jeho radky v jedne transakci (jen INSERT). Vraci id snimku, nebo None, kdyz se nic neulozilo:
    - opakovany sken se stejnymi vstupy (stejny otisk jako posledni snimek zapasu) – zadne duplicitni mereni;
    - stejny sken (scan_id) uz zapas zapsal."""
    last = store.query("SELECT input_hash FROM nhl_snapshots WHERE fixture_id=? ORDER BY id DESC LIMIT 1",
                       (snap["fixture_id"],))
    if last and snap.get("input_hash") and last[0]["input_hash"] == snap["input_hash"]:
        return None
    if store.query("SELECT 1 FROM nhl_snapshots WHERE scan_id=? AND fixture_id=?", (snap["scan_id"], snap["fixture_id"])):
        return None
    with store.batch():
        cur = store.execute(f"INSERT INTO nhl_snapshots({', '.join(_SNAP_COLS)}) VALUES({', '.join('?' * len(_SNAP_COLS))})",
                            [snap.get(c) for c in _SNAP_COLS])
        sid = int(cur.lastrowid)
        for r in rows:
            store.execute(f"INSERT INTO nhl_measurements(snapshot_id, {', '.join(_ROW_COLS)}) "
                          f"VALUES(?, {', '.join('?' * len(_ROW_COLS))})", [sid] + [r.get(c) for c in _ROW_COLS])
    return sid


CONFLICT, CORRECTION = "konflikt", "oprava"


def _check_result(g: dict) -> list[str]:
    """Potvrzeny konecny vysledek? Vraci duvody, proc ho NEULOZIT (prazdne = v poradku)."""
    final, reg, end = g.get("final"), g.get("reg"), g.get("end")
    if not final or not reg or None in (*final, *reg):
        return ["chybí konečné skóre nebo skóre po 60 min"]
    if end not in ("REG", "OT", "SO"):
        return [f"neznámý konec zápasu {end!r}"]
    if end == "REG" and (tuple(reg) != tuple(final) or final[0] == final[1]):
        return [f"konec v základní době, ale po 60 min {tuple(reg)} a konečné {tuple(final)} nesedí"]
    if end in ("OT", "SO") and (reg[0] != reg[1] or abs(final[0] - final[1]) != 1 or sum(final) != sum(reg) + 1):
        return [f"prodloužení/nájezdy: po 60 min {tuple(reg)}, konečné {tuple(final)} – nesedí"]
    return []


def stored_result(store, fixture_id: int) -> dict | None:
    rows = store.query('SELECT final_h, final_a, reg_h, reg_a, "end", source, fetched_at FROM nhl_results WHERE fixture_id=?',
                       (fixture_id,))
    if not rows:
        return None
    r = rows[0]
    return {"final": [r["final_h"], r["final_a"]], "reg": [r["reg_h"], r["reg_a"]], "end": r["end"],
            "source": r["source"], "fetched_at": r["fetched_at"]}


def _core(res: dict) -> dict:
    return {"final": list(res["final"]), "reg": list(res["reg"]), "end": res["end"]}


def record_result(store, fixture_id: int, g: dict, now: datetime, source: str = "NHL API (nhl_settle)") -> bool:
    """Vysledek zapasu jednou provzdy (INSERT OR IGNORE – prvni zapis plati). `g` = nhl_settle.parse_scores.

    Ulozi jen potvrzeny konecny vysledek (_check_result). Jiny vysledek nez ulozeny se NEPREPISE – zapise se jako
    konflikt do nhl_result_corrections a do logu; opravit lze jen vyslovne (correct_result)."""
    bad = _check_result(g)
    if bad:
        log.warning("měření NHL: výsledek zápasu %s neuložen (%s)", fixture_id, "; ".join(bad))
        return False
    new = _core(g)
    cur = store.execute('INSERT OR IGNORE INTO nhl_results(fixture_id, final_h, final_a, reg_h, reg_a, "end", source, '
                        "fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                        (fixture_id, new["final"][0], new["final"][1], new["reg"][0], new["reg"][1], new["end"], source,
                         _iso(_aware(now))))
    if cur.rowcount == 1:
        return True
    old = _core(stored_result(store, fixture_id))
    if old != new:
        c = store.execute("INSERT OR IGNORE INTO nhl_result_corrections(fixture_id, kind, old, new, reason, source, created_at) "
                          "VALUES(?,?,?,?,?,?,?)", (fixture_id, CONFLICT, json.dumps(old), json.dumps(new),
                                                    "zdroj nyní hlásí jiný výsledek", source, _iso(_aware(now))))
        if c.rowcount == 1:
            log.warning("měření NHL: zápas %s – zdroj hlásí %s, uloženo %s; nepřepsáno (konflikt k ručnímu ověření)",
                        fixture_id, new, old)
    return False


def correct_result(store, fixture_id: int, final: tuple[int, int], reg: tuple[int, int], end: str, reason: str,
                   now: datetime, source: str = "ručně") -> bool:
    """Bezpecna oprava chybneho vysledku: puvodni zaznam v nhl_results zustava, oprava se PRIDA s duvodem, zdrojem
    a casem. Vyhodnoceni pouzije effective_result (posledni oprava, jinak puvodni). Neplatna oprava se odmitne."""
    if not reason.strip():
        raise ValueError("oprava výsledku musí mít důvod")
    old = stored_result(store, fixture_id)
    if old is None:
        raise ValueError(f"zápas {fixture_id} nemá uložený výsledek – opravovat není co")
    new = {"final": list(final), "reg": list(reg), "end": end}
    bad = _check_result(new)
    if bad:
        raise ValueError("; ".join(bad))
    cur = store.execute("INSERT OR IGNORE INTO nhl_result_corrections(fixture_id, kind, old, new, reason, source, created_at) "
                        "VALUES(?,?,?,?,?,?,?)", (fixture_id, CORRECTION, json.dumps(_core(effective_result(store, fixture_id))),
                                                  json.dumps(new), reason, source, _iso(_aware(now))))
    return cur.rowcount == 1


def effective_result(store, fixture_id: int) -> dict | None:
    """Vysledek pro vyhodnoceni: posledni vyslovna oprava, jinak prvni ulozeny (konflikty se nikdy nepouziji)."""
    fix = store.query("SELECT new, reason, created_at FROM nhl_result_corrections WHERE fixture_id=? AND kind=? "
                      "ORDER BY id DESC LIMIT 1", (fixture_id, CORRECTION))
    if fix:
        return {**json.loads(fix[0]["new"]), "source": "oprava: " + fix[0]["reason"], "fetched_at": fix[0]["created_at"]}
    return stored_result(store, fixture_id)


def record_results(store, games: dict, now: datetime) -> int:
    """Vysledky merenych zapasu z UZ STAZENYCH dat vyhodnoceni (nhl.settle: {id zapasu NHL API: zapas}). Zadny dotaz.

    Prirazeni pres id zapasu NHL API + kontrola tymu a casu zacatku; pri neshode se nic neulozi (varovani v logu)."""
    from ..names import nhl_abbr

    n = 0
    for r in store.query("SELECT fixture_id, match, kickoff FROM nhl_snapshots WHERE id IN "
                         "(SELECT MAX(id) FROM nhl_snapshots GROUP BY fixture_id)"):
        g = games.get(r["fixture_id"])
        if g is None:
            continue
        home, _, away = (r["match"] or "").partition(" - ")
        ko = datetime.fromisoformat(r["kickoff"])
        start = g.get("start")
        if (nhl_abbr(home), nhl_abbr(away)) != (g.get("home"), g.get("away")) or (
                start is not None and abs((_aware(start) - ko).total_seconds()) > 6 * 3600):
            log.warning("měření NHL: výsledek %s nepřiřazen – měřený zápas %s (%s) neodpovídá %s-%s (%s)", r["fixture_id"],
                        r["match"], r["kickoff"], g.get("home"), g.get("away"), start)
            continue
        n += int(record_result(store, r["fixture_id"], g, now))
    return n


def measure_scan(store, *, fx, ev, quotes, provider, model_used, now: datetime, odds_fetched_at, scan_id: str,
                 weight_p4: float, config_hash: str, overrides: dict | None, odds_pass_started_at=None,
                 fetch_evidence: dict | None = None) -> int | None:
    """Napojeni na sken (nhl.run): sestavi a ulozi mereni. NIKDY nevyhodi vyjimku – chyba jde jen do logu, sken
    a rozhodovani pokracuji beze zmeny. Vraci id snimku (None = nic neulozeno)."""
    try:
        if ev is None:
            log.warning("měření NHL: zápas %s (%s) bez rozparsované události kurzového API – neměřeno", fx.id, fx.name)
            return None
        snap, rows = build(fx, ev, quotes, provider, model_used, now=now, odds_fetched_at=odds_fetched_at,
                           scan_id=scan_id, weight_p4=weight_p4, config_hash=config_hash, overrides=overrides,
                           odds_pass_started_at=odds_pass_started_at, fetch_evidence=fetch_evidence)
        return record(store, snap, rows)
    except Exception as e:  # noqa: BLE001 - evidence nesmi shodit ani zmenit sken
        log.warning("měření NHL selhalo u zápasu %s (%s): %s: %s", getattr(fx, "id", "?"), getattr(fx, "name", "?"),
                    type(e).__name__, e)
        return None


def measure_results(store, games: dict, now: datetime) -> int:
    """Napojeni na vyhodnoceni (nhl.settle). NIKDY nevyhodi vyjimku – chyba jen do logu."""
    try:
        return record_results(store, games, now)
    except Exception as e:  # noqa: BLE001
        log.warning("měření NHL: ukládání výsledků selhalo: %s: %s", type(e).__name__, e)
        return 0


def _public_evidence(ev: dict | None) -> dict | None:
    if ev is None:
        return None
    return {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in ev.items()}


def _naive_utc(t: datetime) -> datetime:
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def fetch_evidence(ledger, cache_t: datetime | None, cache_credits: int | None) -> dict:
    """F1: skutecny cas stazeni kurzu NHL v cache – prirazeni pozadavku z evidence B1 (odds_api_calls, jen cteni).

    PROKAZANE prirazeni = presne jeden kandidat:
    - purpose refresh_league, liga NHL, endpoint /sports/icehockey_nhl/odds, HTTP 200 a ok;
    - cas pozadavku v intervalu <cas cache (zacatek pruchodu, na sekundy), + FETCH_WINDOW_H>;
    - zbyvajici kredity z hlavicky pozadavku = kredity ulozene s cache (tataz odpoved);
    jinak `fetched_at` = None s duvodem (chybi / nejednoznacne / jen neuspesne pozadavky). Zadny odhad casu."""
    base = {"fetched_at": None, "call_id": None, "pass_started_at": cache_t, "cache_credits": cache_credits}
    if cache_t is None:
        return {**base, "reason": "chybí čas cache kurzů"}
    t0 = _naive_utc(cache_t).replace(microsecond=0)  # CallRecord.ts je na cele sekundy
    calls = [c for c in ledger.odds_api_calls(since=t0)
             if c.purpose == "refresh_league" and c.league == "NHL" and c.endpoint == NHL_ODDS_ENDPOINT
             and t0 <= c.ts <= t0 + timedelta(hours=FETCH_WINDOW_H)]
    good = [c for c in calls if c.ok and c.http == 200]
    info = {**base, "candidates": [c.id for c in good], "failed": len(calls) - len(good)}
    if not good:
        return {**info, "reason": ("evidence B1: jen neúspěšné požadavky na kurzy NHL" if calls else
                                   "evidence B1: žádný požadavek na kurzy NHL od času cache")}
    if cache_credits is None:
        if len(good) != 1:
            return {**info, "reason": f"evidence B1: {len(good)} požadavků a cache nemá kredity – nejednoznačné"}
        match = good
    else:
        match = [c for c in good if c.remaining == cache_credits]
        if not match:
            return {**info, "reason": f"evidence B1: žádný požadavek nemá zbývající kredity {cache_credits} jako cache"}
    if len(match) > 1:
        return {**info, "reason": f"evidence B1: {len(match)} požadavky odpovídají cache – nejednoznačné"}
    c = match[0]
    return {**info, "fetched_at": c.ts.replace(tzinfo=timezone.utc), "call_id": c.id, "remaining": c.remaining,
            "reason": "prokázáno: jediný úspěšný požadavek se stejnými kredity jako cache"}


def fetch_evidence_safe(ledger, cached) -> dict:
    """Napojeni na nhl.run: NIKDY nevyhodi vyjimku (chyba = neprokazany cas a varovani v logu)."""
    t = cached[0] if cached else None
    try:
        return fetch_evidence(ledger, t, cached[2] if cached else None)
    except Exception as e:  # noqa: BLE001 - evidence nesmi shodit sken
        log.warning("měření NHL: evidenci B1 nelze přečíst: %s: %s", type(e).__name__, e)
        return {"fetched_at": None, "call_id": None, "pass_started_at": t,
                "reason": f"evidence B1 nečitelná ({type(e).__name__})"}
