"""M1 regrese: rozhodovani hlavniho bota a Profilu 4 (NHL) na fixnich datech -> JSON. Spousti se nad vychozi i novou verzi."""
import json, os, socket, sys, tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

def _blocked(*a, **k):
    raise OSError("sit zakazana")
socket.getaddrinfo = _blocked

tests = Path(sys.argv[1]) / "tests"
sys.path.insert(0, str(tests))
os.environ.setdefault("TIPSBOT_DATABASE_URL", "sqlite://")
import pandas as pd
from tipsbot import nhl_data, oddsapi, pipeline
from tipsbot.profil4 import nhl
from tipsbot.profil4.config import P4
from tipsbot.profil4.store import Store
from tipsbot.storage import Ledger, make_session_factory

import test_nhl_measure as T

tmp = Path(tempfile.mkdtemp())
(tmp / "nhl").mkdir()
T._moneypuck().to_csv(tmp / "nhl" / "all_teams.csv", index=False)
pd.DataFrame([{"name": "G", "team": "TOR", "situation": "all", "games_played": 20, "xGoals": 55.0, "goals": 48.0, "ongoal": 600}]
             ).to_csv(tmp / "nhl" / "goalies.csv", index=False)
st = replace(pipeline.SETTINGS, data_dir=tmp)
nhl_data.SETTINGS = st
out = {}
for name, pl in (("anchored", T.payload()), ("no_total", T.payload(sharp_totals=False)),
                 ("soft", T.payload(soft_ml=(2.30, 1.70)))):
    prov = pipeline.ModelProvider("NHL", T.NOW.date(), st)
    led = T.ledger_with([pl])
    s = Store()
    scan = nhl.run(led, s, T.NOW, P4, schedule=lambda d: T.SCHEDULE, provider=prov, injuries=True)
    out[name + ":p4_tips"] = [dict(r) for r in s.query("SELECT * FROM tips ORDER BY market, selection")]
    out[name + ":p4_pred"] = [dict(r) for r in s.query("SELECT * FROM predictions ORDER BY market, selection, model")]
    out[name + ":p4_rank"] = [{k: v for k, v in dict(r).items() if k != "computed_at"} for r in s.query("SELECT * FROM rankings ORDER BY market, selection")]
    ev = oddsapi.parse_events([pl], "NHL", st)[0]
    model = prov.probabilities(ev)
    out[name + ":model"] = model
    cands, line = pipeline.build_candidates(ev, model, st)
    out[name + ":main_cands"] = [(c.market, c.selection, c.p_model, c.p_market) for c in cands]
    from tipsbot import devig
    out[name + ":main_blend"] = [devig.assess_value(c.p_model, c.p_market, 2.0, 0.025, st.value.for_league("NHL")).prob for c in cands]
import test_automation as TA
for name, pl in (("settle", T.payload()),):
    prov = pipeline.ModelProvider("NHL", T.NOW.date(), st)
    s = Store(); nhl.run(T.ledger_with([pl]), s, T.NOW, P4, schedule=lambda d: T.SCHEDULE, provider=prov, injuries=True)
    so = {"games": [{**TA.SCORES["games"][0], "id": T.GID, "startTimeUTC": "2026-11-22T00:00:00Z"}]}
    msgs = nhl.settle(s, T.KO + __import__("datetime").timedelta(hours=5), fetch=lambda d: so)
    out["settle:msgs"] = msgs
    out["settle:p4_tips"] = [dict(r) for r in s.query("SELECT * FROM tips ORDER BY market, selection")]
    out["settle:p4_pred"] = [dict(r) for r in s.query("SELECT * FROM predictions ORDER BY market, selection, model")]
for k in [k for k in out if k.endswith(":p4_tips") or k.endswith(":p4_pred")]:
    for r in out[k]:
        r.pop("id", None)
print(json.dumps(out, sort_keys=True, default=str))
