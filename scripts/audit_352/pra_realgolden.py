"""Regrese na SKUTECNYCH datech: NHL sken Profilu 4 + kandidati hlavniho bota z realne cache a realnych dat MoneyPuck
(kopie), nad kopii produkcni profil4.db. Vystup JSON – spousti se nad e7e2a85 i nad HEAD."""
import json, os, shutil, socket, sqlite3, sys, tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(OSError("sit zakazana"))
os.environ["TIPSBOT_DATABASE_URL"] = "sqlite://"
backup, nhl_dir = Path(sys.argv[1]), Path(sys.argv[2])
work = Path(tempfile.mkdtemp()); (work / "nhl").mkdir()
for f in ("all_teams.csv", "goalies.csv"):
    shutil.copy(nhl_dir / f, work / "nhl" / f)
shutil.copy(backup / "profil4.db", work / "p4.db")
from tipsbot import devig, nhl_data, oddsapi, pipeline
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
import copy
pls = {"real": cache["payload"]}
pin = copy.deepcopy(cache["payload"])
for e in pin:
    wh = next((b for b in e["bookmakers"] if b["key"] == "williamhill"), e["bookmakers"][0])
    e["bookmakers"].append({**copy.deepcopy(wh), "key": "pinnacle"})
pls["pinnacle"] = pin
out = {}
for name, pl in pls.items():
    games = [{"id": 2026029000 + i, "startTimeUTC": e["commence_time"], "homeTeam": {"abbrev": nhl_abbr(e["home_team"])},
              "awayTeam": {"abbrev": nhl_abbr(e["away_team"])}} for i, e in enumerate(pl)]
    shutil.copy(backup / "profil4.db", work / f"{name}.db")
    s = Store(work / f"{name}.db")
    led = Ledger(make_session_factory("sqlite://")())
    led.set_odds_cache("NHL", fetched.replace(tzinfo=None), pl, 239)
    prov = pipeline.ModelProvider("NHL", NOW.date(), st)
    scan = nhl.run(led, s, NOW, P4, schedule=lambda d: {"gameWeek": [{"games": games}]}, provider=prov, injuries=True)
    out[name + ":cands"] = [(c.key, c.p, c.ev_c, c.ev_d, c.robustness, c.decision, c.category if hasattr(c, "category") else None)
                            for an in scan.analyses for c in an.candidates]
    out[name + ":tips"] = [tuple(r)[1:] for r in s.query("SELECT * FROM tips WHERE id > 552 ORDER BY fixture_id, market, selection")]
    out[name + ":pred"] = [tuple(r)[1:] for r in s.query("SELECT * FROM predictions WHERE fixture_id >= 2026029000 ORDER BY fixture_id, market, selection, model")]
    evs = oddsapi.parse_events(pl, "NHL", st)
    main = []
    for ev in evs:
        m = prov.probabilities(ev)
        cands, line = pipeline.build_candidates(ev, m, st)
        main += [(c.match_id, c.market, c.selection, c.p_model, c.p_market,
                  devig.assess_value(c.p_model, c.p_market, max(o.odds for o in c.offers), 0.025, st.value.for_league("NHL")).prob)
                 for c in cands]
    out[name + ":main"] = main
    s.close()
print(json.dumps(out, sort_keys=True, default=str))
