"""Phase 3.4 / B1 – predprodukcni kontrola (zaloha, obnova, migrace). Produkcni databaze jen cte.

  backup <out_dir>            konzistentni kopie obou DB pres SQLite Backup API; zdroj otevren mode=ro
                              (spoustet v kontejneru: docker exec -i tipsbot python - backup /tmp/x < scripts/b1_preflight.py)
  verify <dir>                integrity_check, pocty a otisky tabulek kopie, test obnovy (kopie -> nova DB) a porovnani
  migrate <tipsbot.db kopie>  na KOPII: otisk tabulek, create_all noveho kodu, kontrola nove tabulky a nezmenenych dat,
                              opakovani (idempotence)
  race <tipsbot.db kopie> N   N-krat dva soubezne procesy create_all na cerstve kopii (jako start obou kontejneru)
  oldcode <db kopie> <src>    create_all kodu puvodni verze nad migrovanou kopii (kompatibilita rollbacku)
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

DBS = ("tipsbot.db", "profil4.db")


def ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def fingerprint(path, skip=()):
    """{tabulka: (pocet, sha1 vsech radku podle rowid, schema)} – jen cteni."""
    con = ro(path)
    out = {}
    for name, sql in con.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if name in skip:
            continue
        h, n = hashlib.sha1(), 0
        for row in con.execute(f'SELECT * FROM "{name}" ORDER BY rowid'):
            h.update(repr(row).encode())
            n += 1
        out[name] = (n, h.hexdigest(), sql)
    con.close()
    return out


def integrity(path):
    con = ro(path)
    r = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    return r


def copy(src_path, dst_path, readonly_src=True):
    src = ro(src_path) if readonly_src else sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    src.backup(dst, pages=4096, sleep=0.02)
    dst.close()
    src.close()


def cmd_backup(out_dir, data="/app/data"):
    os.makedirs(out_dir, exist_ok=True)
    for db in DBS:
        p = os.path.join(data, db)
        try:  # pojistka: zdroj musi byt jen pro cteni
            ro(p).execute("CREATE TABLE _probe(a)")
            sys.exit("CHYBA: zdroj neni jen pro cteni")
        except sqlite3.OperationalError:
            pass
        dst = os.path.join(out_dir, db)
        copy(p, dst)
        print(f"{db}: kopie {os.path.getsize(dst)} B, integrity {integrity(dst)}")


def cmd_verify(d):
    ok = True
    for db in DBS:
        c = os.path.join(d, db)
        ic = integrity(c)
        fp = fingerprint(c)
        with tempfile.TemporaryDirectory() as t:
            restored = os.path.join(t, db)
            copy(c, restored)
            ic2, fp2 = integrity(restored), fingerprint(restored)
        same = fp == fp2
        ok &= ic == "ok" and ic2 == "ok" and same
        rows = sum(v[0] for v in fp.values())
        print(f"{db}: integrity kopie={ic}, po obnove={ic2}; tabulek {len(fp)}, radku {rows}; obnova shodna={same}")
    print("VERIFY", "OK" if ok else "CHYBA")


def cmd_migrate(path):
    before = fingerprint(path)
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from tipsbot.storage import make_session_factory

    make_session_factory(f"sqlite:///{path}")
    after = fingerprint(path)
    new = sorted(set(after) - set(before))
    changed = sorted(k for k in before if before[k] != after.get(k))
    make_session_factory(f"sqlite:///{path}")  # druhy start = idempotence
    again = fingerprint(path)
    print("nove tabulky:", new)
    print("zmenene existujici tabulky:", changed or "zadne")
    print("idempotence (2. start beze zmeny):", again == after)
    print("odds_api_calls schema:", after.get("odds_api_calls", (None, None, None))[2])
    print("MIGRATE", "OK" if new == ["odds_api_calls"] and not changed and again == after else "CHYBA")


def cmd_race(path, n):
    code = ("import sys; sys.path.insert(0, sys.argv[2]); from tipsbot.storage import make_session_factory; "
            "make_session_factory('sqlite:///' + sys.argv[1])")
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
    fails = []
    with tempfile.TemporaryDirectory() as t:
        for i in range(n):
            p = os.path.join(t, f"r{i}.db")
            shutil.copyfile(path, p)
            procs = [subprocess.Popen([sys.executable, "-c", code, p, src], stderr=subprocess.PIPE) for _ in range(2)]
            for pr in procs:
                _, err = pr.communicate()
                if pr.returncode:
                    fails.append(err.decode(errors="replace").strip().splitlines()[-1])
            con = ro(p)
            assert con.execute("SELECT 1 FROM sqlite_master WHERE name='odds_api_calls'").fetchone(), "tabulka chybi"
            con.close()
    print(f"RACE: {n} pokusu x 2 procesy, selhani startu: {len(fails)}", json.dumps(fails[:3], ensure_ascii=False))


def cmd_oldcode(path, old_src):
    before = fingerprint(path)
    code = ("import sys; sys.path.insert(0, sys.argv[2]); from tipsbot.storage import make_session_factory, Ledger; "
            "f = make_session_factory('sqlite:///' + sys.argv[1]); s = f(); led = Ledger(s); "
            "print('stary kod: quota', sorted(led.odds_api_quota())); s.close()")
    r = subprocess.run([sys.executable, "-c", code, path, old_src], capture_output=True, text=True)
    print(r.stdout.strip(), r.stderr.strip()[-300:])
    print("OLDCODE", "OK" if r.returncode == 0 and fingerprint(path) == before else "CHYBA")


if __name__ == "__main__":
    a = sys.argv[1:]
    {"backup": lambda: cmd_backup(a[1]), "verify": lambda: cmd_verify(a[1]), "migrate": lambda: cmd_migrate(a[1]),
     "race": lambda: cmd_race(a[1], int(a[2])), "oldcode": lambda: cmd_oldcode(a[1], a[2])}[a[0]]()
