"""3.5.2 doplnek: dopad cekani na zamek profil4.db pri mereni vice zapasu (skutecna cache a MoneyPuck, KOPIE DB).

Scenare (cizi spojeni drzi zapisovy zamek, jako panel nebo vlakno Profilu 4):
- none: bez zamku;
- burst: zamek 2 s kazde 3 s po celou dobu skenu (rusny panel);
- hold70: jeden zamek 70 s od zacatku (dele nez BUSY_TIMEOUT_S = 60 s) – jen 3 zapasy.
Vzdy s merenim a bez nej (measure_scan vypnute). Timeout ani chovani se nemeni. Sit zablokovana."""
import json, os, shutil, socket, sqlite3, sys, tempfile, threading, time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(OSError("sit zakazana"))
os.environ["TIPSBOT_DATABASE_URL"] = "sqlite://"
backup, nhl_dir = Path(sys.argv[1]), Path(sys.argv[2])
work = Path(tempfile.mkdtemp()); (work / "nhl").mkdir()
for f in ("all_teams.csv", "goalies.csv"):
    shutil.copy(nhl_dir / f, work / "nhl" / f)
from tipsbot import nhl_data, pipeline
from tipsbot.names import nhl_abbr
from tipsbot.profil4 import nhl
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store
from tipsbot.storage import Ledger, make_session_factory

st = replace(pipeline.SETTINGS, data_dir=work); nhl_data.SETTINGS = st
t = sqlite3.connect(f"file:{(backup / 'tipsbot.db').as_posix()}?mode=ro", uri=True)
cache = json.loads(t.execute("SELECT value FROM state WHERE key='odds_cache:NHL'").fetchone()[0])
fetched = datetime.fromisoformat(cache["t"]).replace(tzinfo=timezone.utc)
NOW = fetched + timedelta(hours=6)
prov = pipeline.ModelProvider("NHL", NOW.date(), st)


def holder(path, mode, stop):
    c = sqlite3.connect(path, timeout=1, isolation_level=None, check_same_thread=False)
    if mode == "burst":
        while not stop.is_set():
            c.execute("BEGIN IMMEDIATE"); c.execute("INSERT INTO kv(key,value) VALUES('lock','x') ON CONFLICT(key) DO UPDATE SET value='y'")
            time.sleep(2.0); c.execute("COMMIT"); time.sleep(1.0)
    elif mode == "hold70":
        c.execute("BEGIN IMMEDIATE"); c.execute("INSERT INTO kv(key,value) VALUES('lock','x') ON CONFLICT(key) DO UPDATE SET value='y'")
        stop.wait(70); c.execute("COMMIT")
    c.close()


def run(mode, measure, n_games):
    pl = cache["payload"][:n_games]
    games = [{"id": 2026029000 + i, "startTimeUTC": e["commence_time"], "homeTeam": {"abbrev": nhl_abbr(e["home_team"])},
              "awayTeam": {"abbrev": nhl_abbr(e["away_team"])}} for i, e in enumerate(pl)]
    db = work / f"{mode}_{measure}.db"; shutil.copy(backup / "profil4.db", db)
    Store(db).close()  # migrace predem (meri se sken, ne prvni vytvoreni tabulek)
    s = Store(db)
    led = Ledger(make_session_factory("sqlite://")())
    led.set_odds_cache("NHL", fetched.replace(tzinfo=None), pl, 239)
    orig = nhl.nhl_measure.measure_scan
    if not measure:
        nhl.nhl_measure.measure_scan = lambda *a, **k: None
    stop = threading.Event()
    th = threading.Thread(target=holder, args=(db, mode, stop)) if mode != "none" else None
    if th:
        th.start(); time.sleep(0.3)
    t0 = time.perf_counter(); err = None
    try:
        scan = nhl.run(led, s, NOW, P4, schedule=lambda d: {"gameWeek": [{"games": games}]}, provider=prov, injuries=True)
        n = len(scan.analyses)
    except Exception as e:  # noqa: BLE001
        err, n = f"{type(e).__name__}: {e}", 0
    dt = time.perf_counter() - t0
    stop.set(); th and th.join()
    nhl.nhl_measure.measure_scan = orig
    snaps = sqlite3.connect(db).execute("SELECT COUNT(*) FROM nhl_snapshots").fetchone()[0]
    s.close()
    return dt, n, snaps, err


for mode, n_games in (("none", 15), ("burst", 15), ("hold70", 3)):
    for measure in (False, True):
        dt, n, snaps, err = run(mode, measure, n_games)
        print(f"{mode:7} {'s merenim ' if measure else 'bez mereni'} zapasu {n_games}: sken {dt:6.1f} s, analyzovano {n}, "
              f"snimku {snaps}" + (f", CHYBA: {err[:90]}" if err else ""))
