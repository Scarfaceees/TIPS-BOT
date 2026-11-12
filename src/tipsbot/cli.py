"""Ovladani z prikazove radky: bankroll, tipy, rucni zapis tiketu, prehled."""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import SETTINGS
from .storage import RESULTS, Ledger, make_session_factory
from .tips import format_tip


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tipsbot", description="Tips Bot - value-betting poradce")
    p.add_argument("--db", default=None, help="URL databaze (vychozi z konfigurace)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="vytvori databazi")

    d = sub.add_parser("deposit", help="vklad do bankrollu")
    d.add_argument("amount", type=float)
    w = sub.add_parser("withdraw", help="vyber z bankrollu")
    w.add_argument("amount", type=float)

    sub.add_parser("status", help="prehled bankrollu a vysledku")
    sub.add_parser("tips", help="otevrene tipy")
    sub.add_parser("resume", help="obnovi vydavani tipu po stop-lossu")

    c = sub.add_parser("tip-cancel", help="zrusi tip")
    c.add_argument("tip_id", type=int)
    c.add_argument("--reason", default="zruseno rucne")

    t = sub.add_parser("ticket-add", help="zapise rucne podanou sazku")
    t.add_argument("--tip", type=int, default=None, help="cislo tipu (udaje o zapase se doplni)")
    t.add_argument("--book", required=True, help="kancelar")
    t.add_argument("--odds", type=float, required=True, help="skutecny kurz")
    t.add_argument("--stake", type=float, required=True, help="skutecny vklad")
    t.add_argument("--match-id", default=None)
    t.add_argument("--match", default="", help="nazev zapasu")
    t.add_argument("--league", default="")
    t.add_argument("--market", default=None)
    t.add_argument("--selection", default=None)
    t.add_argument("--day", default=None, help="den zapasu RRRR-MM-DD")

    s = sub.add_parser("ticket-settle", help="vyhodnoti tiket")
    s.add_argument("ticket_id", type=int)
    s.add_argument("result", choices=RESULTS)
    s.add_argument("--closing", type=float, default=None, help="ferovy zaviraci kurz (pro CLV)")

    sub.add_parser("tickets", help="otevrene tikety")

    tm = sub.add_parser("test-mode", help="zapne/vypne testovaci rezim (virtualni bankroll)")
    tm.add_argument("state", choices=("on", "off"))

    web = sub.add_parser("web", help="spusti webovy panel")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)

    ff = sub.add_parser("fetch-football", help="stahne historicka data fotbalu")
    ff.add_argument("--league", required=True)
    ff.add_argument("--seasons", type=int, default=4)

    sub.add_parser("fetch-cups", help="stahne vysledky pohar (LM, EL, ECL)")

    ak = sub.add_parser("set-key", help="ulozi API klic kurzoveho API")
    ak.add_argument("key")

    sub.add_parser("fetch-nhl", help="stahne data NHL z MoneyPuck")

    sc = sub.add_parser("scan", help="nacte zive kurzy a vyda/zkontroluje tipy")
    sc.add_argument("--league", default="all", help="kod souteze nebo 'all'")

    rn = sub.add_parser("run", help="opakovany sken v intervalu (pozor na kredity API)")
    rn.add_argument("--league", default="all")
    rn.add_argument("--interval", type=int, default=SETTINGS.timing.odds_refresh_seconds, help="sekundy mezi skeny")

    bt = sub.add_parser("backtest", help="backtest fotbaloveho modelu na stazenych datech")
    bt.add_argument("--league", required=True)
    bt.add_argument("--seasons", type=int, default=3, help="pocet testovanych sezon")
    bt.add_argument("--xi", type=float, nargs="*", help="vyzkousi vic hodnot casoveho utlumu, napr. --xi 0.002 0.0038 0.006")
    cal = sub.add_parser("calibrate", help="zkalibruje fotbalovy model a ulozi korekci pro dane ligy")
    cal.add_argument("--league", required=True, help="klic souteze nebo 'all'")
    cal.add_argument("--seasons", type=int, default=3)
    return p


def _calibrate(league: str, seasons: int) -> int:
    from datetime import date as _date

    from . import backtest, calibration
    from .config import LEAGUES, Sport
    from .ingestion import current_season_start_year, load_football
    from .pipeline import calibration_path

    keys = [k for k, lg in LEAGUES.items() if lg.sport is Sport.FOOTBALL] if league == "all" else [league]
    for key in keys:
        try:
            df = load_football(key)
        except (FileNotFoundError, ValueError, KeyError):
            print(f"{key}: preskoceno (chybi data, spust fetch-football)")
            continue
        start = max(_date(current_season_start_year() - seasons + 1, 7, 1), df["date"].min())
        out = backtest.calibrate(backtest.walk_forward(df, start))
        if out is None:
            print(f"{key}: malo zapasu pro kalibraci")
            continue
        scalers, before, after = out
        if after < before - 1e-4:
            calibration.save_scalers(calibration_path(key), scalers, {"brier_before": before, "brier_after": after})
            print(f"{key}: kalibrace ulozena (Brier na nevidenych datech {before:.4f} -> {after:.4f})")
        else:
            print(f"{key}: kalibrace nezlepsila ({before:.4f} -> {after:.4f}), nepouzita")
    return 0


def _backtest(league: str, seasons: int, xis: list[float] | None = None) -> int:
    from datetime import date as _date

    from . import backtest
    from .config import LEAGUES
    from .ingestion import current_season_start_year, load_football

    if league not in LEAGUES:
        print(f"Chyba: neznama liga {league}", file=sys.stderr)
        return 1
    try:
        df = load_football(league)
    except FileNotFoundError as e:
        print(f"Chyba: {e}", file=sys.stderr)
        return 1
    start = _date(current_season_start_year() - seasons + 1, 7, 1)
    start = max(start, df["date"].min())
    if xis:
        from dataclasses import replace

        from .config import SETTINGS

        report = None
        for xi in xis:
            st = replace(SETTINGS, football=replace(SETTINGS.football, xi=xi))
            report = backtest.evaluate(backtest.walk_forward(df, start, settings=st), tier=LEAGUES[league].tier, settings=st)
            print(f"xi={xi}: {report.summary()}")
        return 0
    preds = backtest.walk_forward(df, start)
    report = backtest.evaluate(preds, tier=LEAGUES[league].tier)
    print(f"{LEAGUES[league].name}: {report.summary()}")
    return 0 if report.passed else 2


def _scan(args) -> int:
    import time

    from .pipeline import scan_all

    leagues = None if args.league == "all" else [args.league]
    factory = make_session_factory(args.db)
    while True:
        with factory() as session:
            reports = scan_all(Ledger(session), leagues)
            session.commit()
            for rep in reports:
                print(rep.summary())
                for tip in rep.new_tips:
                    print(format_tip(tip) + "\n")
        if args.cmd == "scan":
            return 0
        time.sleep(max(args.interval, 30))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "web":
        from .web import run

        run(args.db, args.host, args.port)
        return 0
    if args.cmd == "fetch-football":
        from .ingestion import fetch_football

        try:
            paths = fetch_football(args.league, args.seasons)
        except ValueError as e:
            print(f"Chyba: {e}", file=sys.stderr)
            return 1
        print(f"Stazeno souboru: {len(paths)}")
        return 0 if paths else 1
    if args.cmd == "fetch-cups":
        from .cups import fetch_cups

        with make_session_factory(args.db)() as session:
            led = Ledger(session)
            notes = fetch_cups(led.api_football_key(), led.football_data_key())
        print("\n".join(notes))
        return 0
    if args.cmd == "calibrate":
        return _calibrate(args.league, args.seasons)
    if args.cmd == "backtest":
        return _backtest(args.league, args.seasons, args.xi)
    if args.cmd == "fetch-nhl":
        from .nhl_data import download_nhl

        print(f"Stazeno: {download_nhl()}")
        from .nhl_data import download_goalies

        try:
            print(f"Stazeno: {download_goalies()}")
        except Exception as e:  # noqa: BLE001 - brankari jsou doplnek, model bezi i bez nich
            print(f"Brankari se nepodarilo stahnout ({e}); model pobezi bez nich.")
        return 0
    if args.cmd in ("scan", "run"):
        return _scan(args)
    factory = make_session_factory(args.db)
    cur = SETTINGS.currency
    with factory() as session:
        led = Ledger(session)
        try:
            if args.cmd == "init":
                print("Databaze je pripravena.")
            elif args.cmd == "deposit":
                led.deposit(args.amount)
                print(f"Bankroll: {led.bankroll():.2f} {cur}")
            elif args.cmd == "withdraw":
                led.withdraw(args.amount)
                print(f"Bankroll: {led.bankroll():.2f} {cur}")
            elif args.cmd == "resume":
                led.resume()
                print("Vydavani tipu obnoveno.")
            elif args.cmd == "status":
                s = led.summary()
                print(f"Bankroll:      {s['bankroll']:.2f} {cur} (vrchol {s['peak']:.2f})")
                print(f"Propad:        {s['drawdown_pct']:.1f} %" + ("  -> STOP-LOSS AKTIVNI" if s["paused"] else ""))
                print(f"Sazky:         {s['settled_bets']:.0f} vyhodnocenych, {s['open_bets']:.0f} otevrenych")
                print(f"Uspesnost:     {s['win_rate_pct']:.1f} %")
                print(f"Zisk/ztrata:   {s['profit_loss']:+.2f} {cur} (yield {s['yield_pct']:+.2f} %)")
                print(f"Prumerne CLV:  {s['avg_clv_pct']:+.2f} % (z {s['clv_tracked']:.0f} sazek)")
            elif args.cmd == "tips":
                tips = led.open_tips()
                print("\n\n".join(format_tip(t) for t in tips) if tips else "Zadne otevrene tipy.")
            elif args.cmd == "tip-cancel":
                from .storage import Tip

                tip = session.get(Tip, args.tip_id)
                if tip is None:
                    raise ValueError(f"Tip {args.tip_id} neexistuje.")
                led.cancel_tip(tip, args.reason)
                print(f"Tip {tip.id} zrusen.")
            elif args.cmd == "ticket-add":
                ticket = led.add_ticket(
                    bookmaker=args.book,
                    odds=args.odds,
                    stake=args.stake,
                    tip_id=args.tip,
                    match_id=args.match_id,
                    market=args.market,
                    selection=args.selection,
                    match_day=date.fromisoformat(args.day) if args.day else None,
                    league=args.league,
                    match_name=args.match,
                )
                print(f"Tiket #{ticket.id} zapsan: {ticket.match_name or ticket.match_id} | "
                      f"{ticket.market} {ticket.selection} | {ticket.bookmaker} {ticket.odds:.2f} | "
                      f"{ticket.stake:.0f} {cur}")
            elif args.cmd == "ticket-settle":
                ticket = led.settle_ticket(args.ticket_id, args.result, args.closing)
                print(f"Tiket #{ticket.id}: {ticket.status}, {ticket.profit_loss:+.2f} {cur}. "
                      f"Bankroll: {led.bankroll():.2f} {cur}")
                if led.paused():
                    print("STOP-LOSS: propad dosahl limitu, tipy jsou pozastavene do re-kalibrace.")
            elif args.cmd == "set-key":
                led.set_api_key(args.key)
                print("API klic ulozen.")
            elif args.cmd == "test-mode":
                led.set_test_mode(args.state == "on")
                print(f"Testovaci rezim: {args.state}")
            elif args.cmd == "tickets":
                from sqlalchemy import select

                from .storage import TICKET_OPEN, Ticket

                rows = list(session.scalars(select(Ticket).where(Ticket.status == TICKET_OPEN)))
                for t in rows:
                    print(f"#{t.id} {t.match_day} {t.match_name or t.match_id} | {t.market} {t.selection} | "
                          f"{t.bookmaker} {t.odds:.2f} | {t.stake:.0f} {cur}")
                if not rows:
                    print("Zadne otevrene tikety.")
            session.commit()
        except ValueError as e:
            session.rollback()
            print(f"Chyba: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
