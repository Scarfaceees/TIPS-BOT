"""3.5.2 F1 na skutecnych datech: skutecna cache NHL (cas 06:00:37 = zacatek pruchodu, kurzy kancelari az do 06:11:34)
+ SIMULOVANY zaznam evidence B1 (pozadavek 06:11:40, kredity jako cache). Skutecny pozadavek NHL v produkcni
evidenci zatim neni (B1 nasazen 26. 11. 17:20, posledni stazeni NHL 06:00). KOPIE DB, sit zablokovana."""
import copy, json, os, shutil, socket, sqlite3, sys, tempfile
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(OSError("sit zakazana"))
os.environ["TIPSBOT_DATABASE_URL"] = "sqlite://"
backup, nhl_dir, req_at = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
work = Path(tempfile.mkdtemp()); (work / "nhl").mkdir()
for f in ("all_teams.csv", "goalies.csv"):
    shutil.copy(nhl_dir / f, work / "nhl" / f)
from tipsbot import nhl_data, pipeline
from tipsbot.names import nhl_abbr
from tipsbot.oddsapi import CallRecord
from tipsbot.profil4 import nhl
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store
from tipsbot.storage import Ledger, make_session_factory

st = replace(pipeline.SETTINGS, data_dir=work); nhl_data.SETTINGS = st
t = sqlite3.connect(f"file:{(backup / 'tipsbot.db').as_posix()}?mode=ro", uri=True)
cache = json.loads(t.execute("SELECT value FROM state WHERE key='odds_cache:NHL'").fetchone()[0])
cache_t = datetime.fromisoformat(cache["t"])
NOW = datetime.fromisoformat(req_at) + timedelta(minutes=1)
for name, pl in (("skutecna cache", cache["payload"]), ("s Pinnacle", None)):
    if pl is None:
        pl = copy.deepcopy(cache["payload"])
        for e in pl:
            wh = next((b for b in e["bookmakers"] if b["key"] == "williamhill"), e["bookmakers"][0])
            e["bookmakers"].append({**copy.deepcopy(wh), "key": "pinnacle"})
    games = [{"id": 2026029000 + i, "startTimeUTC": e["commence_time"], "homeTeam": {"abbrev": nhl_abbr(e["home_team"])},
              "awayTeam": {"abbrev": nhl_abbr(e["away_team"])}} for i, e in enumerate(pl)]
    for with_ev in (True, False):
        led = Ledger(make_session_factory("sqlite://")())
        led.set_odds_cache("NHL", cache_t, pl, cache["credits"])
        if with_ev:
            led.log_odds_api_call(CallRecord(datetime.fromisoformat(req_at), "/sports/icehockey_nhl/odds", "refresh_league",
                                             "icehockey_nhl", "NHL", {"regions": "eu"}, http=200, ok=True,
                                             remaining=cache["credits"], used=500 - cache["credits"], last=3))
            led.s.commit()
        db = work / f"{name[:3]}{with_ev}.db"; shutil.copy(backup / "profil4.db", db)
        s = Store(db)
        nhl.run(led, s, NOW.replace(tzinfo=timezone.utc), P4, schedule=lambda d: {"gameWeek": [{"games": games}]},
                provider=pipeline.ModelProvider("NHL", NOW.date(), st), injuries=True)
        snaps = [dict(r) for r in s.query("SELECT * FROM nhl_snapshots")]
        rows = [dict(r) for r in s.query("SELECT * FROM nhl_measurements")]
        ex = Counter(e.split(":")[0] for r in rows for e in json.loads(r["exclusion"])["sharp"])
        an = Counter(b for sn in snaps for b in json.loads(sn["book_time_anomalies"]))
        print(f"[{name}, evidence {'ANO' if with_ev else 'NE '}] snimku {len(snaps)}, odds_fetched_at {snaps[0]['odds_fetched_at']}, "
              f"pruchod {snaps[0]['odds_pass_started_at']}, srovnatelne snimky {sum(x['comparable'] for x in snaps)}, "
              f"radky srovnatelne ostra/P4 {sum(r['comparable_sharp'] for r in rows)}/{sum(r['comparable_p4'] for r in rows)} z {len(rows)}")
        print(f"   duvody vyrazeni (ostra, podle pravidla): {dict(ex)}; anomalie kancelari: {dict(an)}")
        if with_ev and name == "s Pinnacle":
            print("   evidence:", json.loads(snaps[0]["odds_fetch_evidence"]))
        s.close()
