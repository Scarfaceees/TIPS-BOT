"""Phase 3.3 – Odds Freshness & Cache audit (jen cteni, zadny API dotaz, nic se nezapisuje do databazi).

Dva kroky:

1) extract – cte produkcni databaze JEN PRO CTENI (sqlite URI mode=ro; skript overi, ze zapis selze)
   a vypise JSON se surovymi pozorovanimi. Spusteni uvnitr kontejneru (Docker Desktop, Git Bash):

       docker exec -i tipsbot python - extract < scripts/phase33_odds_audit.py > audit33.json

2) simulate – offline model nakladu a cerstvosti variant A–E a rozpoctovych scenaru nad audit33.json;
   zapise CSV do docs/ a vypise souhrn:

       python scripts/phase33_odds_audit.py simulate audit33.json docs

Vsechny vysledky simulace jsou MODELOVE ODHADY (predpoklady jsou v ASSUMPTIONS a v CSV), ne namerene hodnoty.
Skript neni soucasti balicku tipsbot (Dockerfile instaluje jen src/), do produkcniho provozu se nedostane.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path("/app/data")


def _dt(s):
    if not s:
        return None
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _min(a, b):
    return round((a - b).total_seconds() / 60.0, 1)


def _ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        con.execute("CREATE TABLE _phase33_write_probe(a)")
    except sqlite3.OperationalError as e:
        if "readonly" not in str(e):
            raise
    else:  # pragma: no cover - nesmi nastat
        raise SystemExit(f"{path}: spojeni NENI jen pro cteni – konec")
    return con


def _stats(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "median": round(statistics.median(xs), 1), "mean": round(statistics.fmean(xs), 1),
            "min": round(min(xs), 1), "max": round(max(xs), 1)}


# ---------------------------------------------------------------------------------------------------------------
# 1) extract
# ---------------------------------------------------------------------------------------------------------------

def extract(data: Path = DATA) -> dict:
    p4, tb = _ro(data / "profil4.db"), _ro(data / "tipsbot.db")
    q = lambda c, s, a=(): c.execute(s, a).fetchall()  # noqa: E731
    league = {}
    for fid, lg in q(p4, "SELECT fixture_id, league FROM predictions UNION ALL SELECT fixture_id, league FROM tips"):
        if lg:
            league[fid] = lg
    names = dict(q(p4, "SELECT fixture_id, match FROM tips UNION SELECT fixture_id, match FROM predictions"))

    # --- predvykopova pozorovani + nove/zmenene kurzy ulozene pri kazdem pozorovani ---------------------------
    obs = q(p4, "SELECT id, fixture_id, kickoff, slot_min, observed_at, status, n_quotes, latest_update, note "
                "FROM odds_observations ORDER BY fixture_id, observed_at, slot_min DESC")
    quotes = defaultdict(list)  # fixture -> [(id, book, market, sel, odds, fetched, source_time, source)]
    fids = sorted({r[1] for r in obs})
    for fid in fids:
        for r in q(p4, "SELECT id, bookmaker, market, selection, odds, fetched_at, source_time, source FROM odds_quotes "
                       "WHERE fixture_id=? ORDER BY id", (fid,)):
            quotes[fid].append(r)
    reqs = defaultdict(int)
    for fid, n in q(p4, "SELECT fixture_id, COUNT(*) FROM api_log WHERE endpoint='odds' AND cached=0 AND http IS NOT NULL "
                        "AND fixture_id IS NOT NULL GROUP BY fixture_id"):
        reqs[fid] = n
    observations = []
    seen_obs = set()
    for oid, fid, ko, slot, at, status, nq, upd, note in obs:
        key = (fid, at)
        ko_d, at_d, up_d = _dt(ko), _dt(at), _dt(upd)
        lg = league.get(fid, "")
        src = "oddsapi" if lg == "NHL" else "api"
        new = changed = 0
        books = set()
        if key not in seen_obs:  # jedno stazeni muze zapsat vic bodu (dohnani); nove kurzy pocitame jednou
            prev = {}
            for qid, bk, mk, sel, odd, fet, stime, s in quotes[fid]:
                if s != src:  # zahranicni overeni (kurzove API) u fotbalu se pocita zvlast (foreign_rows)
                    continue
                f = _dt(fet)
                if f < at_d - timedelta(seconds=5):
                    prev[(bk, mk, sel)] = odd
                    continue
                if f > at_d + timedelta(minutes=5):
                    break
                new += 1
                books.add(bk)
                k = (bk, mk, sel)
                if k in prev and abs(prev[k] - odd) > 1e-9:
                    changed += 1
                prev[k] = odd
        foreign = sum(1 for r in quotes[fid] if src == "api" and r[7] == "oddsapi"
                      and at_d - timedelta(seconds=5) <= _dt(r[5]) <= at_d + timedelta(minutes=5)) if key not in seen_obs else None
        observations.append({
            "fixture_id": fid, "league": lg, "match": names.get(fid, str(fid)), "source": src, "kickoff": ko,
            "slot": slot, "observed_at": at, "status": status, "n_quotes": nq, "provider_update": upd,
            "new_rows": new if key not in seen_obs else None, "changed_values": changed if key not in seen_obs else None,
            "new_books": len(books), "foreign_rows": foreign, "first_of_fetch": key not in seen_obs,
            "provider_age_at_kickoff_min": _min(ko_d, up_d) if up_d else None,
            "fetch_before_kickoff_min": _min(ko_d, at_d), "note": note or "",
            "odds_requests_total": reqs.get(fid, 0)})
        seen_obs.add(key)

    # --- verze kurzu u poskytovatele (API-Football: 'update' za zapas; kurzove API: last_update za kancelar) ---
    ver = defaultdict(set)
    per_fetch_stamps = Counter()
    for fid, src, fet, n_st in q(p4, "SELECT fixture_id, source, fetched_at, COUNT(DISTINCT source_time) FROM odds_quotes "
                                     "WHERE source IN ('api','oddsapi') GROUP BY fixture_id, source, fetched_at"):
        per_fetch_stamps[(src, n_st if n_st < 5 else "5+")] += 1
    for fid, src, st in q(p4, "SELECT DISTINCT fixture_id, source, source_time FROM odds_quotes WHERE source IN ('api','oddsapi')"):
        ver[(fid, src)].add(st)
    gaps = defaultdict(list)
    for (fid, src), sts in ver.items():
        ts = sorted(_dt(s) for s in sts if s)
        if src == "api":  # u kurzoveho API ma kazda kancelar vlastni cas – interval verzi zapasu nedava smysl
            gaps[src] += [_min(b, a) for a, b in zip(ts, ts[1:])]

    # --- API-Football: pozadavky a kvota po dnech; chyby --------------------------------------------------------
    api_days = [dict(zip(("day", "requests", "odds_requests", "errors"), r)) for r in q(
        p4, "SELECT day, SUM(cached=0 AND http IS NOT NULL), SUM(endpoint='odds' AND cached=0 AND http IS NOT NULL), "
            "SUM(ok=0) FROM api_log GROUP BY day ORDER BY day")]
    budget = [dict(zip(("day", "used", "limit_day", "remaining_day"), r))
              for r in q(p4, "SELECT day, used, limit_day, remaining_day FROM budget ORDER BY day")]
    api_errors = [dict(zip(("day", "endpoint", "http", "error", "n"), r)) for r in q(
        p4, "SELECT day, endpoint, http, substr(error,1,60), COUNT(*) FROM api_log WHERE ok=0 GROUP BY 1,2,3,4")]
    skipped_budget = q(p4, "SELECT COUNT(*) FROM odds_observations WHERE status='SKIPPED_BUDGET'")[0][0]

    # --- hlavni bot: cache kurzoveho API a kredity -------------------------------------------------------------
    state = dict(q(tb, "SELECT key, value FROM state WHERE key LIKE 'odds_cache:%' OR key='odds_api_quota' "
                       "OR key LIKE 'next_kickoff:%' OR key IN ('auto:last_refresh','auto:last_morning')"))
    caches = {}
    for k, v in state.items():
        if k.startswith("odds_cache:"):
            d = json.loads(v)
            ev_stamps = [bm.get("last_update") for e in d["payload"] for bm in e.get("bookmakers", [])]
            caches[k.split(":", 1)[1]] = {"fetched_at": d["t"], "credits_after": d.get("credits"),
                                         "events": len(d["payload"]), "book_entries": len(ev_stamps),
                                         "newest_book_update": max(filter(None, ev_stamps), default=None),
                                         "oldest_book_update": min(filter(None, ev_stamps), default=None)}
    quota = json.loads(state.get("odds_api_quota", "{}"))

    # --- NHL: kdy hlavni bot skutecne obnovil kurzy (nova verze last_update v kurzech Profilu 4) --------------
    nhl_refresh = [r[0] for r in q(p4, "SELECT DISTINCT substr(computed_at,1,16) FROM rankings WHERE league='NHL' ORDER BY 1")]
    nhl_games = [dict(zip(("fixture_id", "match", "kickoff"), r)) for r in q(
        p4, "SELECT fixture_id, MAX(match), MAX(kickoff) FROM predictions WHERE league='NHL' GROUP BY fixture_id ORDER BY 3")]

    # --- Profil 4: rozhodnuti podle behu (rankings = kazdy kandidat kazdeho behu) a ulozene tipy -------------
    runs = [dict(zip(("sport", "computed_at", "decision", "n", "age_median", "age_max", "ev_ge_3", "rob_ge_60"), r)) for r in q(
        p4, "SELECT CASE WHEN league='NHL' THEN 'NHL' ELSE 'fotbal' END, substr(computed_at,1,13), decision, COUNT(*), "
            "NULL, MAX((julianday(computed_at)-julianday(odds_time))*1440), SUM(ev>=0.03), SUM(robustness>=60) "
            "FROM rankings WHERE valid=1 GROUP BY 1,2,3 ORDER BY 2")]
    ages = defaultdict(list)
    for sport, hour, age in q(p4, "SELECT CASE WHEN league='NHL' THEN 'NHL' ELSE 'fotbal' END, substr(computed_at,1,13), "
                                  "(julianday(computed_at)-julianday(odds_time))*1440 FROM rankings WHERE valid=1"):
        ages[(sport, hour)].append(age)
    for r in runs:
        r["age_median"] = round(statistics.median(ages[(r["sport"], r["computed_at"])]), 0)
        r["age_max"] = round(r["age_max"] or 0, 0)
    tips = [dict(zip(("sport", "decision", "odds_status", "n", "age_median", "ev_ok", "evd_ok", "rob_ok"), r)) for r in q(
        p4, "SELECT CASE WHEN league='NHL' THEN 'NHL' ELSE 'fotbal' END, decision, COALESCE(odds_status,'(bez stavu)'), "
            "COUNT(*), ROUND(AVG(odds_age_min)), SUM(ev>=0.03), SUM(ev_conservative>=0), SUM(robustness>=60) "
            "FROM tips GROUP BY 1,2,3 ORDER BY 1,2,3")]
    # zastarale WATCH NHL: splnil by kandidat ostatni ciselne prahy? (aproximace – duvody se neukladaji)
    stale_nhl = [dict(zip(("fixture_id", "match", "kickoff", "market", "selection", "odds", "ev", "ev_cons", "rob",
                           "age", "predicted_at", "approved_earlier", "result"), r)) for r in q(
        p4, "SELECT t.fixture_id, t.match, t.kickoff, t.market, t.selection, t.odds, t.ev, t.ev_conservative, t.robustness, "
            "t.odds_age_min, t.predicted_at, EXISTS(SELECT 1 FROM tips a WHERE a.fixture_id=t.fixture_id AND a.market=t.market "
            "AND a.selection=t.selection AND a.decision='APPROVED BET' AND a.predicted_at<t.predicted_at), t.result "
            "FROM tips t WHERE t.league='NHL' AND t.odds_status='ZASTARALÝ'")]
    p4.close()
    tb.close()
    return {"extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "observations": observations,
            "provider_version_gaps_min": {k: _stats(v) for k, v in gaps.items()},
            "provider_gap_hist": Counter(min(int(g // 60), 12) for g in gaps["api"]),
            "versions_per_fixture": {s: _stats([len(v) for (f, src), v in ver.items() if src == s]) for s in ("api", "oddsapi")},
            "stamps_per_fetch": {f"{s}:{n}": c for (s, n), c in per_fetch_stamps.items()},
            "api_football_days": api_days, "budget": budget, "api_errors": api_errors,
            "skipped_budget": skipped_budget, "oddsapi_caches": caches, "oddsapi_quota": quota,
            "next_kickoff": {k.split(":", 1)[1]: v for k, v in state.items() if k.startswith("next_kickoff:")},
            "nhl_rank_runs": nhl_refresh, "nhl_games": nhl_games, "p4_runs": runs, "p4_tips": tips,
            "nhl_stale_tips": stale_nhl}


# ---------------------------------------------------------------------------------------------------------------
# 2) simulate (offline, jen nad JSON z extract)
# ---------------------------------------------------------------------------------------------------------------

ASSUMPTIONS = {
    "monthly_credits": 500,  # remaining + used z hlavicek kurzoveho API (239 + 261 k 26.11.2026)
    "credit_reserve": 50,  # SETTINGS.timing.auto_credit_reserve
    "nhl_league_cost": 3,  # h2h + totals + spreads, 1 region (oddsapi.fetch_raw); overeno 26.11. 242 -> 239
    "football_league_cost": 2,  # h2h + totals, 1 region
    "event_cost_per_market": 1,  # /events/{id}/odds: 1 kredit za trh a region
    "nhl_markets_per_event": 3,  # varianta D: vsechny trhy, ze kterych NHL rozhoduje
    "passes_utc": [6, 12, 18, 0],  # ranni pruchod 07:00 Praha (06:00 UTC v zime) + kazdych 6 h
    "stale_limit_min": 360,  # OddsConfig.max_age_api_min
    "days_per_month": 30,
}


def _nhl_days(games):
    by_day = defaultdict(list)
    for g in games:
        ko = _dt(g["kickoff"])
        by_day[(ko - timedelta(hours=10)).date()].append(ko)  # herni den NHL (vecer v USA)
    return by_day


def _refresh_times(variant, day0, kos, a=ASSUMPTIONS):
    """Casy obnovy NHL cache v jednom hernim dni (UTC) podle varianty; (casy, kredity)."""
    base = datetime(day0.year, day0.month, day0.day, tzinfo=timezone.utc)
    passes = [base + timedelta(hours=h + (24 if h < 6 else 0)) for h in a["passes_utc"]]  # 06,12,18,00(+1)
    c = a["nhl_league_cost"]
    if variant == "A":  # soucasny stav: ranni pruchod obnovi kurzy starsi 6 h, jinak az po 24 h => 1x denne
        t = [passes[0]]
        return t, c * len(t), t
    if variant == "B":  # obnova pri kazdem pruchodu (cache starsi 6 h; pruchody jsou >= 6 h od sebe)
        return passes, c * len(passes), passes
    if variant == "C":  # obnova po 3 h: vyzaduje samostatny 3h spoustec, jen NHL
        t = [base + timedelta(hours=6 + 3 * i) for i in range(8)]
        return t, c * len(t), t
    if variant == "D":  # soucasny stav + kurzy udalosti 75/30/10 min pred kazdym zapasem (vsechny 3 trhy)
        t = [passes[0]] + [k - timedelta(minutes=m) for k in kos for m in (75, 30, 10)]
        cost = c + len(kos) * 3 * a["nhl_markets_per_event"] * a["event_cost_per_market"]
        return sorted(t), cost, [passes[0]]  # rozhodovani (pruchody) se nemeni, novy rozbor by byl potreba navic
    if variant == "E":  # adaptivni: + 1 obnova ligy ~90 min pred prvnim zapasem herniho dne + novy rozbor NHL
        first = min(kos)
        t = [passes[0], first - timedelta(minutes=90)]
        return t, c * 2, t
    raise ValueError(variant)


def simulate(raw: dict, out_dir: Path) -> dict:
    a = dict(ASSUMPTIONS)
    days = _nhl_days(raw["nhl_games"])
    rows, summary = [], {}
    for v in "ABCDE":
        credits, ages_eval, ages_ko, fresh_eval, fresh_6h, n_games = [], [], [], 0, 0, 0
        for d, kos in sorted(days.items()):
            ref, cost, decide_at = _refresh_times(v, d, kos, a)
            credits.append(cost)
            # rozhoduje se v kazdem pruchodu automatu (nhl.run_and_store) a u variant B/C/E i pri kazde obnove;
            # varianta D obnovuje jen kurzy pro zaviraci kurz / CLV, rozbor se nespousti
            base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            passes = [base + timedelta(hours=h + (24 if h < 6 else 0)) for h in a["passes_utc"]]
            decisions = sorted(set(passes) | (set(decide_at) if v in ("B", "C", "E") else set()))
            for ko in kos:
                n_games += 1
                before = [t for t in ref if t <= ko - timedelta(minutes=10)]
                ages_ko.append(_min(ko, max(before)) if before else None)
                dec = [t for t in decisions if t < ko - timedelta(minutes=10)]
                near = [t for t in dec if t >= ko - timedelta(hours=6)]
                if any((src_n := max((r for r in ref if r <= t), default=None)) and _min(t, src_n) <= a["stale_limit_min"]
                       for t in near):
                    fresh_6h += 1
                if dec:
                    last = max(dec)
                    src = max((t for t in ref if t <= last), default=None)
                    age = _min(last, src) if src else None
                    ages_eval.append(age)
                    fresh_eval += age is not None and age <= a["stale_limit_min"]
        per_day = statistics.fmean(credits) if credits else 0
        month = per_day * a["days_per_month"]
        s = {"variant": v, "game_days": len(days), "games": n_games,
             "credits_per_game_day": round(per_day, 1), "credits_month_nhl_est": round(month),
             "requests_per_game_day": round(per_day / a["nhl_league_cost"], 1) if v != "D" else
             round(statistics.fmean([1 + 3 * len(k) for k in days.values()]), 1),
             "age_at_kickoff_median_min": _stats(ages_ko).get("median"),
             "age_at_last_decision_median_min": _stats(ages_eval).get("median"),
             "games_decided_on_current_odds_pct": round(100 * fresh_eval / n_games, 0) if n_games else None,
             "games_with_current_decision_within_6h_pct": round(100 * fresh_6h / n_games, 0) if n_games else None}
        rows.append(s)
        summary[v] = s
    # rozpocet: skutecna denni spotreba z mesicniho pocitadla 'used'
    used = sorted(raw["oddsapi_quota"].get("days", {}).items())
    daily = [b - a_ for (_, a_), (_, b) in zip(used, used[1:]) if b >= a_]
    base_day = statistics.fmean(daily) if daily else 0
    nhl_now = summary["A"]["credits_per_game_day"]
    other_day = max(0.0, base_day - nhl_now)
    budget_rows = []
    for name, v, note in (("konzervativní", "A", "současný stav, NHL 1× denně"),
                          ("vyvážený", "E", "A + 1 obnova NHL před prvním zápasem dne"),
                          ("agresivní", "B", "NHL při každém průchodu (4× denně), strop = měsíční limit − rezerva")):
        nhl = summary[v]["credits_per_game_day"]
        day = other_day + nhl
        month = day * a["days_per_month"]
        budget_rows.append({"scenario": name, "variant": v, "note": note,
                            "credits_day_est": round(day, 1), "credits_month_est": round(month),
                            "reserve_left_month_est": round(a["monthly_credits"] - month),
                            "fits_with_reserve": month <= a["monthly_credits"] - a["credit_reserve"]})
    out_dir.mkdir(parents=True, exist_ok=True)
    obs_cols = ("source", "league", "match", "kickoff", "slot", "observed_at", "fetch_before_kickoff_min", "status",
                "provider_update", "provider_age_at_kickoff_min", "n_quotes", "new_rows", "changed_values", "foreign_rows",
                "odds_requests_total", "note")
    with (out_dir / "phase-3-3-prematch-observations.csv").open("w", newline="", encoding="utf-8") as f:
        f.write("# NAMERENO: odds_observations + odds_quotes (profil4.db, jen cteni); new_rows/changed_values jen u prvniho "
                "bodu jednoho stazeni, u dohnanych bodu prazdne" + chr(10))
        w = csv.DictWriter(f, fieldnames=obs_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(raw["observations"], key=lambda o: (o["source"], o["kickoff"], o["match"], -o["slot"])))
    _csv(out_dir / "phase-3-3-cache-simulation.csv", rows, a)
    _csv(out_dir / "phase-3-3-api-budget-simulation.csv", budget_rows,
         {**a, "measured_daily_credits": [round(x, 1) for x in daily], "measured_daily_mean": round(base_day, 1),
          "other_than_nhl_daily_est": round(other_day, 1)})
    return {"cache": rows, "budget": budget_rows, "daily_measured": daily, "base_day": base_day}


def _csv(path: Path, rows: list[dict], assumptions: dict) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write("# MODELOVY ODHAD (scripts/phase33_odds_audit.py simulate), ne namerene hodnoty\n")
        for k, v in assumptions.items():
            f.write(f"# predpoklad {k} = {v}\n")
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "extract"
    if cmd == "extract":
        json.dump(extract(), sys.stdout, ensure_ascii=False, default=str, indent=1)
    elif cmd == "simulate":
        res = simulate(json.loads(Path(sys.argv[2]).read_text(encoding="utf-8")), Path(sys.argv[3]))
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    else:
        raise SystemExit(__doc__)
