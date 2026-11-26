"""3.5.2 oblast 4/5/7/9: skutecny NHL sken (skutecna cache kurzu z overene zalohy, skutecna data MoneyPuck – kopie)
nad KOPII produkcni profil4.db. Sit zablokovana a pocitana. Mereni zapnute vs. vypnute."""
import copy, json, os, shutil, socket, sqlite3, statistics, sys, tempfile, time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

NET = []
_orig = socket.socket.connect
def _blocked(self, addr, *a, **k):
    NET.append(addr)
    raise OSError("sit zakazana")
socket.socket.connect = _blocked
socket.getaddrinfo = lambda *a, **k: (NET.append(a[0]), (_ for _ in ()).throw(OSError("sit zakazana")))[1]
os.environ["TIPSBOT_DATABASE_URL"] = "sqlite://"

backup, nhl_dir = Path(sys.argv[1]), Path(sys.argv[2])
work = Path(tempfile.mkdtemp())
(work / "nhl").mkdir()
for f in ("all_teams.csv", "goalies.csv"):
    shutil.copy(nhl_dir / f, work / "nhl" / f)
db = work / "profil4.db"
shutil.copy(backup / "profil4.db", db)

from tipsbot import nhl_data, pipeline
from tipsbot.names import nhl_abbr
from tipsbot.profil4 import nhl, nhl_measure as M
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store
from tipsbot.storage import Ledger, make_session_factory

st = replace(pipeline.SETTINGS, data_dir=work)
nhl_data.SETTINGS = st
t = sqlite3.connect(f"file:{(backup / 'tipsbot.db').as_posix()}?mode=ro", uri=True)
cache = json.loads(t.execute("SELECT value FROM state WHERE key='odds_cache:NHL'").fetchone()[0])
payload = cache["payload"]
fetched = datetime.fromisoformat(cache["t"]).replace(tzinfo=timezone.utc)
NOW = fetched + timedelta(hours=6)
games = [{"id": 2026029000 + i, "startTimeUTC": e["commence_time"], "homeTeam": {"abbrev": nhl_abbr(e["home_team"])},
          "awayTeam": {"abbrev": nhl_abbr(e["away_team"])}} for i, e in enumerate(payload)]
SCHED = {"gameWeek": [{"games": games}]}


def with_pinnacle(pl):
    """Varianta s ostrou kanceláří (Pinnacle = kopie williamhill), aby se proslo i ukotveni OU."""
    out = copy.deepcopy(pl)
    for e in out:
        wh = next((b for b in e["bookmakers"] if b["key"] == "williamhill"), e["bookmakers"][0])
        e["bookmakers"].append({**copy.deepcopy(wh), "key": "pinnacle"})
    return out


def run_once(store, pl, now, fetched_at, measure=True):
    led = Ledger(make_session_factory("sqlite://")())
    led.set_odds_cache("NHL", fetched_at.replace(tzinfo=None), pl, 239)
    if not measure:
        orig = nhl.nhl_measure.measure_scan
        nhl.nhl_measure.measure_scan = lambda *a, **k: None
    try:
        t0 = time.perf_counter()
        scan = nhl.run(led, store, now, P4, schedule=lambda d: SCHED, provider=pipeline.ModelProvider("NHL", now.date(), st),
                       injuries=True)
        return time.perf_counter() - t0, scan
    finally:
        if not measure:
            nhl.nhl_measure.measure_scan = orig


def decisions(store):
    return ([tuple(r)[1:] for r in store.query("SELECT * FROM tips ORDER BY fixture_id, market, selection")],
            [tuple(r)[1:] for r in store.query("SELECT * FROM predictions WHERE league='NHL' ORDER BY fixture_id, market, selection, model")])


for name, pl in (("skutecna cache (bez Pinnacle)", payload), ("s Pinnacle (ukotveni)", with_pinnacle(payload))):
    a, b = work / f"on_{len(name)}.db", work / f"off_{len(name)}.db"
    shutil.copy(db, a); shutil.copy(db, b)
    sa, sb = Store(a), Store(b)
    t_on, scan_on = run_once(sa, pl, NOW, fetched)
    t_off, scan_off = run_once(sb, pl, NOW, fetched, measure=False)
    same = decisions(sa)[0][-200:] == decisions(sb)[0][-200:] and decisions(sa)[1] == decisions(sb)[1]
    cands = lambda s: [(c.key, c.p, c.ev_c, c.decision) for an in s.analyses for c in an.candidates]
    snaps = sa.query("SELECT COUNT(*) n, SUM(comparable) c, SUM(anchored) a FROM nhl_snapshots")[0]
    rows = sa.query("SELECT COUNT(*) n, SUM(comparable_sharp) cs, SUM(comparable_p4) cp, SUM(p_raw IS NULL) nr FROM nhl_measurements")[0]
    ex = {}
    for r in sa.query("SELECT exclusion FROM nhl_measurements"):
        for why in json.loads(r["exclusion"])["sharp"]:
            ex[why] = ex.get(why, 0) + 1
    print(f"[{name}] zapasu {len(scan_on.analyses)}; sken s merenim {t_on:.2f} s, bez {t_off:.2f} s; rozhodnuti shodna: {same}, "
          f"kandidati shodni: {cands(scan_on) == cands(scan_off)}")
    print(f"   snimku {snaps['n']} (srovnatelnych {snaps['c']}, ukotvenych {snaps['a']}); radku {rows['n']} "
          f"(srovn. ostra {rows['cs']}, P4 {rows['cp']}, bez cisteho modelu {rows['nr']}); duvody (ostra): {ex}")
    sa.close(); sb.close()

# opakovane meritelne skeny: cas a rust DB
c = work / "grow.db"; shutil.copy(db, c)
s = Store(c)
size0 = c.stat().st_size
times = []
pl = with_pinnacle(payload)
for k in range(20):
    for e in pl:
        for bm in e["bookmakers"]:
            bm["last_update"] = (fetched + timedelta(minutes=k)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dt, _ = run_once(s, pl, NOW + timedelta(minutes=10 * k), fetched + timedelta(minutes=k))
    times.append(dt)
s.close()
n_s = sqlite3.connect(c).execute("SELECT COUNT(*) FROM nhl_snapshots").fetchone()[0]
n_r = sqlite3.connect(c).execute("SELECT COUNT(*) FROM nhl_measurements").fetchone()[0]
growth = c.stat().st_size - size0
print(f"20 skenu se zmenou kurzu: snimku {n_s}, radku {n_r}, rust DB {growth / 1024:.0f} KiB "
      f"= {growth / max(n_s, 1):.0f} B na snimek; cas skenu median {statistics.median(times):.2f} s")
per_season = 1312 * 8
print(f"odhad sezona (1312 zapasu x ~8 snimku): {per_season} snimku, ~{per_season * growth / max(n_s, 1) / 2**20:.1f} MiB")

# vyhodnoceni nad velkou tabulkou: 12 000 snimku
s = Store(c)
with s.batch():
    for i in range(12000):
        s.execute("INSERT INTO nhl_snapshots(scan_id, fixture_id, match, kickoff, computed_at, missing, comparable, exclusion, measure_version, input_hash) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (f"x{i}", 3000000 + i // 8, "Boston Bruins - Toronto Maple Leafs",
                                                 "2026-11-27T00:00:00+00:00", "2026-11-26T00:00:00+00:00", "[]", 1, "[]", "m2-1", f"h{i}"))
t0 = time.perf_counter()
M.record_results(s, {3000005: {"id": 3000005, "home": "BOS", "away": "TOR", "start": datetime(2026, 11, 27, tzinfo=timezone.utc),
                               "final": (3, 2), "reg": (3, 2), "end": "REG"}}, NOW)
print(f"record_results nad {n_s + 12000} snimky: {time.perf_counter() - t0:.3f} s")
s.close()
print("sitove pokusy:", NET)
