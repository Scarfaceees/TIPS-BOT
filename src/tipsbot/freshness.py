"""Odds Freshness – diagnostika cerstvosti kurzu (jen ulozena data Profilu 4, zadny API dotaz, zadny kredit).

Zaklad mereni je rozliseni dvou casu:
- cas STAZENI = kdy bot kurz stahl (`odds_observations.observed_at`, `odds_quotes.fetched_at`);
- cas AKTUALIZACE = kdy kurz naposledy obnovil poskytovatel (`odds_observations.latest_update`,
  `odds_quotes.source_time`; API-Football pole `update` za cely zapas, kurzove API `last_update` za kancelar).

Zdroje se NIKDY neslucuji do jedne statistiky: API-Football (fotbal, body 75/30/10) a kurzove API (NHL a overeni
zaviraciho kurzu fotbalu zahranicni kancelari). Nic se tu nemeni – body, pravidla zaviraciho kurzu, CLV ani
historicke zaznamy; modul jen cte.

Sekce reportu (stejna data pro panel i Excel, list "Odds Freshness"):
- competitions: cerstvost podle soutezi – stari posledni aktualizace pri vykopu (median, prumer, min, max, pocet zapasu),
  vedle toho cas posledniho stazeni a zpozdeni stazeni za aktualizaci;
- slots: body 75/30/10 – kolik planovanych stazeni prineslo NOVOU verzi kurzu (podle casu aktualizace, ne stazeni);
- batches: hromadne aktualizace poskytovatele – rozlozeni podle hodiny UTC, pocet davek, interval mezi verzemi;
- clv: CLV coverage vyhodnocenych tipu – overitelny zaviraci kurz vs zastaraly vs chybejici;
- weeks: vyvoj po tydnech (pondeli, UTC) pro porovnani obdobi.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

API, ODDSAPI = "api", "oddsapi"
SOURCES = {API: "API-Football", ODDSAPI: "kurzové API"}
SLOTS_NOTE = "body 75/30/10 min (beze změny)"
BATCH_GAP_MIN = 10  # aktualizace ruznych zapasu do 10 min po sobe = jedna hromadna davka poskytovatele
FRESH_MIN, CLOSE_MIN = 60, 20  # pravidla zaviraciho kurzu Faze 2.1 (aktualizace <= 60 min, pozorovani <= 20 min)
NEW, SAME, CATCHUP, FAIL = "nová verze", "stejná verze", "dohnáno jedním stažením", "bez dat / přeskočeno"
CLV_OK_API, CLV_OK_ODDSAPI = "ověřitelný – API-Football", "ověřitelný – kurzové API"
CLV_STALE, CLV_LAST, CLV_LINE, CLV_MISSING = "zastaralý", "jen poslední pozorovaný", "změna trhu / linie", "chybí"
CLV_CATS = (CLV_OK_API, CLV_OK_ODDSAPI, CLV_STALE, CLV_LAST, CLV_LINE, CLV_MISSING)


def _dt(s) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _min(a: datetime, b: datetime) -> float:
    return (a - b).total_seconds() / 60.0


def _week(d: datetime) -> str:
    return (d - timedelta(days=d.weekday())).date().isoformat()


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "median": None, "mean": None, "min": None, "max": None}
    return {"n": len(xs), "median": statistics.median(xs), "mean": statistics.fmean(xs), "min": min(xs), "max": max(xs)}


def _share(xs: list[float], limit: float) -> float | None:
    return sum(x <= limit for x in xs) / len(xs) if xs else None


def _sport(league: str | None) -> str:
    return "hokej" if (league or "").upper() == "NHL" else "fotbal"


# --- nacteni ----------------------------------------------------------------------------------------------------
def load(con: sqlite3.Connection) -> dict:
    """Surova data z databaze Profilu 4 (jen SELECT)."""
    q = con.execute
    leagues: dict[int, str] = {}
    for fid, lg in q("SELECT fixture_id, league FROM predictions UNION ALL SELECT fixture_id, league FROM tips"):
        if lg:
            leagues[fid] = lg
    obs = [dict(zip(("fixture_id", "kickoff", "slot", "observed_at", "status", "n_quotes", "latest_update"), r))
           for r in q("SELECT fixture_id, kickoff, slot_min, observed_at, status, n_quotes, latest_update "
                      "FROM odds_observations ORDER BY fixture_id, observed_at, slot_min DESC")]
    # verze kurzu: jedno stazeni (fetched_at) jednoho zdroje = jedna verze s casem posledni aktualizace u zdroje
    vers = [dict(zip(("fixture_id", "source", "fetched_at", "update", "kickoff"), r))
            for r in q("SELECT fixture_id, source, fetched_at, MAX(source_time), MAX(kickoff) FROM odds_quotes "
                       "WHERE source IN (?, ?) AND source_time IS NOT NULL GROUP BY fixture_id, source, fetched_at",
                       (API, ODDSAPI))]
    tips = [dict(zip(("fixture_id", "league", "kickoff", "decision", "closing_kind"), r))
            for r in q("SELECT fixture_id, league, kickoff, decision, closing_kind FROM tips WHERE result IS NOT NULL")]
    return {"leagues": leagues, "obs": obs, "versions": vers, "tips": tips}


EMPTY = {"leagues": {}, "obs": [], "versions": [], "tips": []}


def report(p4_path: Path | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    path = Path(p4_path) if p4_path else None  # None = bez databaze Profilu 4 (testy, export bez dat)
    if path is None or not path.exists():
        return build(EMPTY, now) | {"missing": True}
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)  # jen cteni
    try:
        return build(load(con), now)
    except sqlite3.DatabaseError as e:
        return build(EMPTY, now) | {"error": str(e)}
    finally:
        con.close()


# --- vypocet ----------------------------------------------------------------------------------------------------
def build(raw: dict, now: datetime) -> dict:
    """Report ze surovych dat; cerstvost pri vykopu jen u zapasu, ktere uz zacaly (ko <= now)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    leagues, obs, vers = raw["leagues"], raw["obs"], raw["versions"]

    def obs_source(fid: int) -> str:  # NHL bere kurzy z kurzoveho API, fotbal v bodech z API-Football
        return ODDSAPI if _sport(leagues.get(fid)) == "hokej" else API

    for o in obs:
        o["src"] = obs_source(o["fixture_id"])
        o["ko"], o["at"], o["upd"] = _dt(o["kickoff"]), _dt(o["observed_at"]), _dt(o["latest_update"])
    for v in vers:
        v["at"], v["upd"], v["ko"] = _dt(v["fetched_at"]), _dt(v["update"]), _dt(v["kickoff"])

    # 1) cerstvost pri vykopu: zapasy sledovane v bodech pred vykopem, kazdy zdroj zvlast
    tracked = {o["fixture_id"]: o["ko"] for o in obs if o["ko"] and o["ko"] <= now}
    per_fix: dict[tuple[int, str], dict] = {}
    for v in vers:
        ko = tracked.get(v["fixture_id"])
        if ko is None or v["upd"] is None or v["upd"] >= ko or (v["at"] and v["at"] >= ko):
            continue
        d = per_fix.setdefault((v["fixture_id"], v["source"]), {"upd": v["upd"], "at": v["at"], "ko": ko})
        d["upd"] = max(d["upd"], v["upd"])
        d["at"] = max(x for x in (d["at"], v["at"]) if x) if (d["at"] or v["at"]) else None
    for o in obs:  # pozorovani nese cas kazdeho stazeni (i kdyz neprineslo nic noveho)
        if o["fixture_id"] in tracked and o["at"] and o["at"] < o["ko"] and o["n_quotes"]:
            d = per_fix.setdefault((o["fixture_id"], o["src"]), {"upd": o["upd"], "at": o["at"], "ko": o["ko"]})
            d["at"] = max(d["at"] or o["at"], o["at"])
            if o["upd"] and o["upd"] < o["ko"]:
                d["upd"] = max(d["upd"] or o["upd"], o["upd"])
    fixrows = []
    for (fid, src), d in per_fix.items():
        if d["upd"] is None:
            continue
        fixrows.append({"fixture_id": fid, "source": src, "league": leagues.get(fid) or "neznámá soutěž",
                        "week": _week(d["ko"]), "update_age": _min(d["ko"], d["upd"]),
                        "fetch_before": _min(d["ko"], d["at"]) if d["at"] else None,
                        "lag": _min(d["at"], d["upd"]) if d["at"] else None})
    comps = []
    for src in SOURCES:
        groups = defaultdict(list)
        for r in fixrows:
            if r["source"] == src:
                groups[r["league"]].append(r)
        for lg, rs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            ages = [r["update_age"] for r in rs]
            fb = [r["fetch_before"] for r in rs if r["fetch_before"] is not None]
            lag = [r["lag"] for r in rs if r["lag"] is not None]
            comps.append({"source": SOURCES[src], "league": lg, **_stats(ages),
                          "fresh60": _share(ages, FRESH_MIN), "fresh20": _share(ages, CLOSE_MIN),
                          "fetch_median": statistics.median(fb) if fb else None,
                          "lag_median": statistics.median(lag) if lag else None})

    # 2) body 75/30/10: prineslo stazeni novou verzi? (podle casu aktualizace u zdroje, ne casu stazeni)
    known: dict[tuple[int, str], list[datetime]] = defaultdict(list)
    for v in vers:
        if v["at"] and v["upd"]:
            known[(v["fixture_id"], v["source"])].append((v["at"], v["upd"]))
    slot_rows, prev_obs = [], defaultdict(list)
    by_fetch = defaultdict(list)
    for o in obs:
        by_fetch[(o["fixture_id"], o["observed_at"])].append(o)
    for o in obs:
        key = (o["fixture_id"], o["src"])
        same_fetch = by_fetch[(o["fixture_id"], o["observed_at"])]
        if not o["n_quotes"] or o["upd"] is None:
            cat = FAIL
        elif min(x["slot"] for x in same_fetch) != o["slot"]:
            cat = CATCHUP  # zmeskany bod dohnan stazenim pro nizsi bod – neni samostatny pozadavek
        else:
            before = [u for at, u in known[key] if o["at"] and at < o["at"]] + prev_obs[key]
            cat = NEW if not before or o["upd"] > max(before) else SAME
        if o["upd"] is not None and cat != CATCHUP:  # dohnany bod je totez stazeni jako nizsi bod
            prev_obs[key].append(o["upd"])
        o["cat"] = cat
        slot_rows.append(o)
    slots = []
    for src in SOURCES:
        for s in sorted({o["slot"] for o in slot_rows}, reverse=True):
            rs = [o for o in slot_rows if o["src"] == src and o["slot"] == s]
            if not rs:
                continue
            c = {k: sum(o["cat"] == k for o in rs) for k in (NEW, SAME, CATCHUP, FAIL)}
            done = c[NEW] + c[SAME]
            ages = [_min(o["at"], o["upd"]) for o in rs if o["cat"] in (NEW, SAME)]
            slots.append({"source": SOURCES[src], "slot": s, "planned": len(rs), "new": c[NEW], "same": c[SAME],
                          "catchup": c[CATCHUP], "failed": c[FAIL], "new_share": c[NEW] / done if done else None,
                          "age_at_fetch_median": statistics.median(ages) if ages else None})

    # 3) hromadne aktualizace: verze = (zapas, cas aktualizace u zdroje); vsechna ulozena stazeni
    batches = []
    for src in SOURCES:
        stamps = sorted({(v["upd"], v["fixture_id"]) for v in vers if v["source"] == src and v["upd"]})
        if not stamps:
            continue
        hours = [0] * 24
        for u, _ in stamps:
            hours[u.hour] += 1
        clusters: list[list] = []
        for u, f in stamps:
            if clusters and _min(u, clusters[-1][-1][0]) <= BATCH_GAP_MIN:
                clusters[-1].append((u, f))
            else:
                clusters.append([(u, f)])
        sizes = [len({f for _, f in c}) for c in clusters]
        byf = defaultdict(list)
        for u, f in stamps:
            byf[f].append(u)
        gaps = [_min(b, a) for us in byf.values() for a, b in zip(us, us[1:])]
        days = len({u.date() for u, _ in stamps})
        batches.append({"source": SOURCES[src], "versions": len(stamps), "fixtures": len(byf), "days": days,
                        "batches": len(clusters), "batch_median": statistics.median(sizes), "batch_max": max(sizes),
                        "interval_median": statistics.median(gaps) if gaps else None,
                        "interval_min": min(gaps) if gaps else None, "hours": hours,
                        "top_hours": [h for h, n in sorted(enumerate(hours), key=lambda x: -x[1]) if n][:5]})

    # 4) CLV coverage vyhodnocenych tipu
    clv_rows = []
    tips = raw["tips"]
    for t in tips:
        t["cat"] = clv_category(t.get("closing_kind"), _sport(t.get("league")))
    for scope, flt in (("všechny vyhodnocené tipy", lambda t: True),
                       ("jen APPROVED BET", lambda t: t.get("decision") == "APPROVED BET")):
        for sp in ("fotbal", "hokej"):
            rs = [t for t in tips if flt(t) and _sport(t.get("league")) == sp]
            if not rs:
                continue
            c = {k: sum(t["cat"] == k for t in rs) for k in CLV_CATS}
            ok = c[CLV_OK_API] + c[CLV_OK_ODDSAPI]
            clv_rows.append({"scope": scope, "sport": sp, "tips": len(rs), **{k: c[k] for k in CLV_CATS},
                             "verified": ok, "coverage": ok / len(rs)})

    # 5) vyvoj po tydnech
    weeks = []
    for wk in sorted({r["week"] for r in fixrows} | {_week(o["ko"]) for o in slot_rows if o["ko"]}):
        for src in SOURCES:
            rs = [r for r in fixrows if r["week"] == wk and r["source"] == src]
            ob = [o for o in slot_rows if o["src"] == src and o["ko"] and _week(o["ko"]) == wk and o["cat"] in (NEW, SAME)]
            if not rs and not ob:
                continue
            ages = [r["update_age"] for r in rs]
            nw = sum(o["cat"] == NEW for o in ob)
            weeks.append({"week": wk, "source": SOURCES[src], "matches": len(rs),
                          "median": statistics.median(ages) if ages else None,
                          "fresh60": _share(ages, FRESH_MIN), "fresh20": _share(ages, CLOSE_MIN),
                          "fetches": len(ob), "new": nw, "new_share": nw / len(ob) if ob else None})
    clv_weeks = []
    for wk in sorted({_week(_dt(t["kickoff"])) for t in tips if _dt(t.get("kickoff"))}):
        for sp in ("fotbal", "hokej"):
            rs = [t for t in tips if _dt(t.get("kickoff")) and _week(_dt(t["kickoff"])) == wk
                  and _sport(t.get("league")) == sp]
            if not rs:
                continue
            ok = sum(t["cat"] in (CLV_OK_API, CLV_OK_ODDSAPI) for t in rs)
            clv_weeks.append({"week": wk, "sport": sp, "tips": len(rs), "verified": ok,
                              "stale": sum(t["cat"] == CLV_STALE for t in rs),
                              "missing": sum(t["cat"] == CLV_MISSING for t in rs), "coverage": ok / len(rs)})

    return {"competitions": comps, "slots": slots, "batches": batches, "clv": clv_rows, "weeks": weeks,
            "clv_weeks": clv_weeks, "fixtures": len(tracked), "observations": len(obs), "now": now}


def clv_category(kind: str | None, sport: str) -> str:
    """Druh zaviraciho kurzu ulozeny u tipu (history.CK_*) -> kategorie pokryti CLV. Nic se neprepocitava."""
    from .profil4 import history as H

    if kind == H.CK_CLOSING:
        return CLV_OK_ODDSAPI if sport == "hokej" else CLV_OK_API  # NHL ma kurzy jen z kurzoveho API
    if kind == H.CK_FOREIGN:
        return CLV_OK_ODDSAPI
    return {H.CK_STALE: CLV_STALE, H.CK_LAST: CLV_LAST, H.CK_LINE: CLV_LINE}.get(kind, CLV_MISSING)
