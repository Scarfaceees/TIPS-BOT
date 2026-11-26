"""3.5.2 oblast 8: rollback – STARY kod (spousti se s PYTHONPATH na worktree e7e2a85) nad kopii profil4.db, kterou uz
nový kod zmigroval a naplnil merenim. Stary kod musi nastartovat, skenovat a vyhodnocovat a tabulek mereni se nedotknout."""
import json, os, socket, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(OSError("sit zakazana"))
os.environ["TIPSBOT_DATABASE_URL"] = "sqlite://"
db, backup = Path(sys.argv[1]), Path(sys.argv[2])
import tipsbot
print("kod:", tipsbot.__file__)
from tipsbot.profil4 import nhl
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store
from tipsbot.storage import Ledger, make_session_factory
from tipsbot.names import nhl_abbr

def counts():
    c = sqlite3.connect(db)
    out = {t: c.execute(f"SELECT COUNT(*), COALESCE(MAX(id), 0) FROM {t}").fetchone() for t in ("nhl_snapshots", "nhl_measurements", "nhl_result_corrections")}
    out["nhl_results"] = c.execute("SELECT COUNT(*) FROM nhl_results").fetchone()
    c.close()
    return out

before = counts()
t = sqlite3.connect(f"file:{(backup / 'tipsbot.db').as_posix()}?mode=ro", uri=True)
cache = json.loads(t.execute("SELECT value FROM state WHERE key='odds_cache:NHL'").fetchone()[0])
fetched = datetime.fromisoformat(cache["t"]).replace(tzinfo=timezone.utc)
games = [{"id": 2026029000 + i, "startTimeUTC": e["commence_time"], "homeTeam": {"abbrev": nhl_abbr(e["home_team"])},
          "awayTeam": {"abbrev": nhl_abbr(e["away_team"])}} for i, e in enumerate(cache["payload"])]

class FakeProvider:
    note = None
    def probabilities(self, ev):
        return {"MONEYLINE:home": 0.55, "MONEYLINE:away": 0.45}

s = Store(db)
led = Ledger(make_session_factory("sqlite://")())
led.set_odds_cache("NHL", fetched.replace(tzinfo=None), cache["payload"], 239)
scan = nhl.run(led, s, fetched + timedelta(hours=6), P4, schedule=lambda d: {"gameWeek": [{"games": games}]},
               provider=FakeProvider(), injuries=True)
print("stary kod: sken", len(scan.analyses), "zapasu bez chyby")
final = {"games": [{"id": g["id"], "gameState": "OFF", "startTimeUTC": g["startTimeUTC"], "gameOutcome": {"lastPeriodType": "REG"},
                    "homeTeam": {"abbrev": g["homeTeam"]["abbrev"], "score": 3}, "awayTeam": {"abbrev": g["awayTeam"]["abbrev"], "score": 1},
                    "goals": [{"period": 1, "homeScore": 1, "awayScore": 0}, {"period": 2, "homeScore": 2, "awayScore": 0},
                              {"period": 2, "homeScore": 2, "awayScore": 1}, {"period": 3, "homeScore": 3, "awayScore": 1}]} for g in games]}
msgs = nhl.settle(s, fetched + timedelta(days=3), fetch=lambda d: final)
print("stary kod: vyhodnoceni", len(msgs), "zprav bez chyby")
s.close(); Store(db).close()
after = counts()
print("tabulky mereni beze zmeny po starem kodu:", before == after, before)
