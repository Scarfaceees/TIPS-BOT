"""Phase 3.3 – kontrola měsíčního resetu kreditů The Odds API (jen čtení, žádný API dotaz).

Čte jen to, co bot ukládá z hlaviček běžných odpovědí (x-requests-remaining / x-requests-used):
stav `odds_api_quota` a `odds_cache:<liga>` v tipsbot.db. DB otevírá v režimu mode=ro.
Doplňkově: `docker logs -t tipsbot-auto | grep "zbyva kreditu"` ukáže zbytek po každém volání.

Spuštění (nic nezapisuje):
    docker exec -i tipsbot python - < scripts/phase33_quota_check.py
"""
import json
import sqlite3

PLAN = 500           # plánovaný měsíční limit (Phase 3.3)
RESET_DAY = "2026-12-01"  # očekávaný reset: 1. den kalendářního měsíce (FAQ The Odds API), hodina/pásmo neuvedeny

db = sqlite3.connect("file:/app/data/tipsbot.db?mode=ro", uri=True)
try:
    db.execute("CREATE TABLE _probe(a)")
    raise SystemExit("CHYBA: databáze není jen pro čtení – kontrola přerušena")
except sqlite3.OperationalError:
    pass

state = dict(db.execute("SELECT key, value FROM state WHERE key='odds_api_quota' OR key LIKE 'odds_cache:%'").fetchall())
q = json.loads(state.get("odds_api_quota") or "{}")
rem, used, t = q.get("remaining"), q.get("used"), q.get("t", "")
print(f"poslední hlavička: t={t} remaining={rem} used={used} součet={None if rem is None or used is None else rem + used}")
print("used po dnech (poslední hodnota dne):", q.get("days", {}))
print("cache lig (čas stažení, remaining v tu chvíli):")
for k, v in sorted(state.items()):
    if k.startswith("odds_cache:"):
        d = json.loads(v)
        print(f"  {k[11:]:<6} {d.get('t')} remaining={d.get('credits')}")

days = q.get("days", {})
before = [u for d, u in days.items() if d < RESET_DAY]
after = {d: u for d, u in days.items() if d >= RESET_DAY}
problems = []
if t[:10] < RESET_DAY:
    print("VÝSLEDEK: od", RESET_DAY, "zatím žádná hlavička – reset nelze ověřit, počkej na první běžné volání.")
else:
    if rem is not None and used is not None and rem + used != PLAN:
        problems.append(f"remaining + used = {rem + used}, čekáno {PLAN} (jiný tarif nebo jiný klíč?)")
    if before and after and min(after.values()) >= max(before):
        problems.append(f"used po {RESET_DAY} ({min(after.values())}) neklesl pod stav před resetem ({max(before)}) – reset neproběhl nebo proběhl později (časové pásmo)")
    if used is not None and used > 40 * max(1, len(after)):
        problems.append(f"used={used} je na {len(after)} dní po resetu nečekaně vysoko (~22/den)")
    print("VÝSLEDEK:", "OK – počítadlo obnoveno na plán" if not problems else "NESOUHLASÍ – nic neopravovat, diagnostika:")
    for p in problems:
        print("  -", p)
