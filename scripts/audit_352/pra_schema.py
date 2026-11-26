"""3.5.2 oblast 1: schema produkcni profil4.db (jen cteni) proti schematu M1/M2 (nova prazdna DB v tmp)."""
import sqlite3, sys, tempfile
from pathlib import Path
from tipsbot.profil4.store import Store

prod = sys.argv[1]
c = sqlite3.connect(f"file:{prod}?mode=ro", uri=True, timeout=30)
c.execute("PRAGMA query_only=ON")
def cols(conn):
    out = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        out[t] = [(r[1], r[2].upper(), r[3], r[5]) for r in conn.execute(f'PRAGMA table_info("{t}")')]
    idx = sorted(r for r in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"))
    return out, idx
p_cols, p_idx = cols(c)
print("produkce: journal_mode", c.execute("PRAGMA journal_mode").fetchone()[0], "| page_size", c.execute("PRAGMA page_size").fetchone()[0],
      "| tabulek", len(p_cols))
tmp = Path(tempfile.mkdtemp()) / "new.db"
Store(tmp).close()
n = sqlite3.connect(tmp)
n_cols, n_idx = cols(n)
missing_t = sorted(set(n_cols) - set(p_cols))
extra_t = sorted(set(p_cols) - set(n_cols))
print("tabulky, ktere M1/M2 prida:", missing_t)
print("tabulky jen v produkci:", extra_t)
diff = {}
for t in set(n_cols) & set(p_cols):
    a, b = {x[0]: x for x in p_cols[t]}, {x[0]: x for x in n_cols[t]}
    d = {"chybi_v_produkci": sorted(set(b) - set(a)), "jen_v_produkci": sorted(set(a) - set(b)),
         "jiny_typ": sorted(k for k in set(a) & set(b) if a[k][1] != b[k][1])}
    if any(d.values()):
        diff[t] = d
print("rozdily ve spolecnych tabulkach:", diff or "zadne")
print("indexy, ktere M1/M2 prida:", sorted(set(n_idx) - set(p_idx)))
print("indexy jen v produkci:", sorted(set(p_idx) - set(n_idx)))
print("hotove migrace (kv fix_*):", [r[0] for r in c.execute("SELECT key FROM kv WHERE key LIKE 'fix_%'")])
