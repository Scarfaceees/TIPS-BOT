"""Prikazy `tipsbot profil4 ...` (pridane do existujiciho CLI, zadne duplicitni rozhrani)."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import P4, Profil4Config


PERF_SECTIONS = ("performance", "ranking-performance", "model-comparison", "clv", "settlement-integrity")

def add_parser(sub) -> None:
    p = sub.add_parser("profil4", help="Profil 4 – Hybridní expert (API-Football, modely A–D, hledání příležitostí)")
    s = p.add_subparsers(dest="p4cmd")
    s.add_parser("status", help="stav: kvóta API, soutěže, watchlist, historie")
    d = s.add_parser("daily", help="DAILY SCAN – dnešní zápasy")
    u = s.add_parser("upcoming", help="UPCOMING SCAN – zápasy v okně (výchozí 48 h)")
    u.add_argument("--hours", type=int, default=None)
    for x in (d, u):
        x.add_argument("--quick", action="store_true", help="jen QUICK SCAN bez hloubkové analýzy")
        x.add_argument("--sims", type=int, default=None, help="počet Monte Carlo simulací (výchozí 50 000)")
        x.add_argument("--no-news", action="store_true", help="bez titulků zpráv (Google News RSS)")
    a = s.add_parser("analyze", help="DEEP SCAN – hloubková analýza zápasu")
    a.add_argument("fixture_id", type=int)
    a.add_argument("--sims", type=int, default=None)
    a.add_argument("--no-news", action="store_true")
    a.add_argument("--no-save", action="store_true", help="neukládat do historie")
    w = s.add_parser("watchlist", help="sledované zápasy a kurzy")
    w.add_argument("action", nargs="?", choices=("list", "add", "remove", "check"), default="list")
    w.add_argument("id", nargs="?", type=int, help="fixture_id (add) nebo číslo položky (remove)")
    w.add_argument("--market", default="")
    w.add_argument("--selection", default="")
    w.add_argument("--min-odds", type=float, default=None)
    w.add_argument("--lineups", action="store_true", help="čekat na sestavy")
    o = s.add_parser("odds", help="ruční zadání / import kurzů (např. Tipsport)")
    o.add_argument("--fixture", type=int, default=None, help="id zápasu (pro ruční řádky)")
    o.add_argument("--file", default=None, help="textový soubor s řádky 'kancelář | trh | kurzy' (jinak stdin)")
    o.add_argument("--csv", default=None, help="CSV: fixture_id,bookmaker,market,selection,odds[,time]")
    o.add_argument("--json", default=None, help="JSON se seznamem kurzů")
    o.add_argument("--book", default="tipsport", help="kancelář pro řádky bez kanceláře")
    o.add_argument("--list", action="store_true", help="vypíše uložené kurzy zápasu")
    s.add_parser("settle", help="vyhodnotí skončené zápasy (výsledek, settlement, CLV)")
    s.add_parser("evaluate", help="Brier, log loss, ROI, yield, CLV po modelech, trzích, soutěžích…")
    it = s.add_parser("intel", help="Intelligence & Calibration: A–D na společném vzorku, integrita, ranking (bez API)")
    it.add_argument("section", nargs="?", default="all",
                    choices=["all", "integrity", "ranking", "calibration", "data-quality", "portfolio", "errors",
                             "performance", "ranking-performance", "model-comparison", "clv", "settlement-integrity"],
                    help="jen jedna část přehledu (výchozí: vše)")
    it.add_argument("--out", help="uloží analytický report (JSON) do souboru; historii nemění")
    it.add_argument("--audit-history", action="store_true",
                    help="settlement-integrity: znovu nezávisle ověří všechna uložená vyhodnocení (nic nepřepíše)")
    b = s.add_parser("backtest", help="walk-forward backtest modelu A na výsledcích z API")
    b.add_argument("--league", type=int, required=True, help="id soutěže v API-Football (39 = Premier League)")
    b.add_argument("--season", type=int, required=True, help="testovací sezóna (rok začátku, např. 2025)")
    c = s.add_parser("competitions", help="sledované soutěže")
    c.add_argument("--enable", type=int)
    c.add_argument("--disable", type=int)
    c.add_argument("--priority", type=int, nargs=2, metavar=("ID", "PRIORITA"))
    c.add_argument("--add", type=int, help="přidat soutěž podle id")
    c.add_argument("--name", default=None)
    au = s.add_parser("auto", help="plánované skenování s limity kvóty (běží dokola)")
    au.add_argument("--every-hours", type=float, default=6.0)
    au.add_argument("--hours", type=int, default=None, help="okno UPCOMING skenu")
    au.add_argument("--max-runs", type=int, default=0, help="0 = bez omezení")


def _key_from_panel(db: str | None):
    def get() -> str:
        try:
            from ..storage import Ledger, make_session_factory

            with make_session_factory(db)() as s:
                return Ledger(s).api_football_key()
        except Exception:  # noqa: BLE001 - bez hlavni DB jen promenna prostredi
            return ""
    return get


def build(db: str | None = None, cfg: Profil4Config = P4):
    from .apiclient import ApiFootballClient
    from .provider import DataProvider
    from .store import open_store

    store = open_store(cfg)
    client = ApiFootballClient(store, cfg, key=_key_from_panel(db))
    return store, DataProvider(client)


def _news(args):
    if getattr(args, "no_news", False):
        return None
    from ..context import fetch_news

    return lambda team: fetch_news(team, "injury lineup")


def _read(path: str | None) -> str:
    return Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()


def run(args, cfg: Profil4Config = P4) -> int:
    if not cfg.enabled:
        print("Profil 4 je vypnutý (TIPSBOT_PROFIL4=0).", file=sys.stderr)
        return 1
    from . import history, hunter, odds, report, watchlist
    from .store import utcnow

    cmd = args.p4cmd or "status"
    store, prov = build(args.db, cfg)
    now = utcnow()
    try:
        if cmd == "status":
            b = prov.client.budget.status()
            key = "ano" if prov.client.key() else "NE – nastav TIPSBOT_API_FOOTBALL_KEY nebo klíč v Nastavení panelu"
            print(f"Profil 4 – Hybridní expert · klíč API-Football: {key}")
            print(f"Kvóta dnes ({b['day']} UTC): použito {b['used']} z {b['limit']}, zbývá {b['remaining']}"
                  + (f"; minutový limit {b['minute_limit']}" if b["minute_limit"] else ""))
            print("Soutěže: " + ", ".join(f"{c.name} [{c.league_id}, p{c.priority}{'' if c.enabled else ', vypnuto'}]"
                                         for c in prov.competitions()))
            wl = watchlist.items(store)
            print(f"Watchlist: {len(wl)} aktivních položek")
            t = store.query("SELECT COUNT(*) n, SUM(result IS NOT NULL) s FROM tips")[0]
            print(f"Historie: {t['n']} tipů, vyhodnoceno {t['s'] or 0}")
            err = store.query("SELECT ts, endpoint, error FROM api_log WHERE ok=0 ORDER BY id DESC LIMIT 3")
            for e in err:
                print(f"Poslední chyba API: {e['ts']} {e['endpoint']}: {e['error']}")
        elif cmd in ("daily", "upcoming"):
            rep = hunter.run_scan(prov, cmd, now, cfg, getattr(args, "hours", None), _news(args), deep=not args.quick,
                                  simulations=args.sims)
            print(report.format_scan(rep))
        elif cmd == "analyze":
            an = hunter.analyze_fixture(prov, args.fixture_id, now, cfg, _news(args), record=not args.no_save,
                                        simulations=args.sims)
            print(report.format_analysis(an, now))
        elif cmd == "watchlist":
            if args.action == "add":
                if not args.id:
                    raise ValueError("zadej fixture_id")
                got = prov.fixtures_by_ids([args.id])
                fx = got.data[0] if got.data else None
                wid = watchlist.add(store, args.id, fx.name if fx else str(args.id), fx.kickoff if fx else None,
                                    args.market, args.selection, args.min_odds, args.lineups, now)
                print(f"Přidáno do watchlistu (#{wid}).")
            elif args.action == "remove":
                watchlist.remove(store, args.id)
                print("Odebráno.")
            elif args.action == "check":
                alerts = watchlist.check(store, prov, now, cfg)
                print("\n".join(f"#{a.watch_id} {a.match}: {a.text}" for a in alerts) or "Beze změn.")
            else:
                for w in watchlist.items(store):
                    print(f"#{w['id']} {w['match']} ({w['kickoff']}) {w['market']}:{w['selection']} "
                          f"min. kurz {w['min_odds'] or '–'} · poslední {w['last_odds'] or '–'} / {w['last_decision'] or '–'}")
        elif cmd == "odds":
            if args.list:
                for q in odds.latest(store, args.fixture):
                    print(f"{q.bookmaker} {q.market}:{q.selection} {q.odds:.2f} · {q.source_label()} · "
                          f"{q.status(now, cfg.odds)} · {q.stamp}")
                return 0
            if args.csv:
                quotes, errors = odds.parse_csv(_read(args.csv), now)
            elif args.json:
                quotes, errors = odds.parse_json(_read(args.json), now)
            else:
                if not args.fixture:
                    raise ValueError("pro ruční řádky zadej --fixture ID")
                got = prov.fixtures_by_ids([args.fixture])
                fx = got.data[0] if got.data else None
                quotes, errors = odds.parse_manual(_read(args.file), args.fixture, fx.name if fx else str(args.fixture),
                                                   now, fx.kickoff if fx else None, args.book)
            for e in errors:
                print(f"Přeskočeno – {e}", file=sys.stderr)
            print(f"Uloženo kurzů: {odds.save(store, quotes)} (zdroj: ruční/import, čas {now:%H:%M} UTC)")
        elif cmd == "settle":
            msgs = history.settle(store, prov, now)
            print("\n".join(msgs) or "Nic k vyhodnocení.")
        elif cmd == "evaluate":
            print(report.format_evaluation(history.evaluate(store)))
        elif cmd == "intel":
            from . import intelligence

            if args.section in PERF_SECTIONS:  # Faze 3.2: Predictive Performance a Settlement Integrity
                from . import performance, settleaudit

                if args.audit_history:
                    settleaudit.audit_history(store, now)
                out = performance.store_performance(store, now, audit_daily=not args.audit_history)
                print(performance.format_text(out, args.section))
            else:
                out = intelligence.store_intel(store, now)
                print(intelligence.format_text(out, args.section))
            if args.out:
                Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
                print(f"Report uložen: {args.out}")
        elif cmd == "backtest":
            from datetime import date

            info = prov.league_info(args.league)
            prev = prov.season_results(args.league, args.season - 1, past=True, priority="LOW")
            cur = prov.season_results(args.league, args.season, past=True, priority="LOW")
            hist = list(prev.data or []) + list(cur.data or [])
            start = min((f.kickoff.date() for f in (cur.data or [])), default=date(args.season, 8, 1))
            res = history.backtest(hist, start)
            if not res.get("n"):
                print(f"Backtest neproběhl: {res.get('note')}")
                return 1
            print(f"Backtest modelu A · soutěž {info.data['name'] if info.data else args.league} · test od {res['test_start']}")
            print(f"  Trénink {res['train_n']} zápasů, test {res['n']} zápasů (model se učí jen z dřívějších zápasů)")
            print(f"  Model A: Brier {res['model']['brier']:.4f}, log loss {res['model']['log_loss']:.4f}")
            print(f"  Naivní:  Brier {res['naive']['brier']:.4f}, log loss {res['naive']['log_loss']:.4f}")
            for r in res["calibration"]:
                print(f"  kalibrace výhry domácích {r['bin']}: předpověď {r['predicted']:.0%}, skutečnost {r['actual']:.0%} (n={r['n']})")
            if not res["significant"]:
                print("  Pozor: méně než 300 testovacích zápasů – výsledek je orientační.")
        elif cmd == "competitions":
            if args.enable:
                prov.set_competition(args.enable, enabled=True)
            if args.disable:
                prov.set_competition(args.disable, enabled=False)
            if args.priority:
                prov.set_competition(args.priority[0], priority=args.priority[1])
            if args.add:
                prov.set_competition(args.add, enabled=True, priority=3, name=args.name or str(args.add))
            for c in prov.competitions():
                print(f"{c.league_id:>5}  {c.name:<22} priorita {c.priority}  {'zapnuto' if c.enabled else 'vypnuto'}")
        elif cmd == "auto":
            runs = 0
            while True:
                now = utcnow()
                rem = prov.client.budget.remaining()
                if rem <= cfg.budget.reserve_pct["MEDIUM"] * prov.client.budget.limit():
                    print(f"{now:%H:%M} sken vynechán – kvóta šetřena (zbývá {rem})")
                else:
                    rep = hunter.run_scan(prov, "upcoming", now, cfg, args.hours, None)
                    print(report.format_scan(rep))
                    msgs = history.settle(store, prov, now)
                    if msgs:
                        print("\n".join(msgs))
                runs += 1
                if args.max_runs and runs >= args.max_runs:
                    break
                time.sleep(max(600.0, args.every_hours * 3600))
        return 0
    except ValueError as e:
        print(f"Chyba: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
