"""3.5.2 oblast 2: migrace M1/M2 na KOPII produkcni profil4.db – zachovani dat, idempotence, soubeh procesu, zamek."""
import hashlib, multiprocessing as mp, shutil, socket, sqlite3, sys, tempfile, threading, time
from pathlib import Path

socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(OSError("sit zakazana"))


def fingerprint(p, skip_prefix="nhl_"):
    c = sqlite3.connect(f"file:{Path(p).as_posix()}?mode=ro", uri=True)
    out = {}
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if t.startswith(skip_prefix):
            continue
        h = hashlib.sha1()
        n = 0
        for r in c.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
            h.update(repr(r).encode()); n += 1
        out[t] = (n, h.hexdigest()[:12])
    schema = hashlib.sha1(repr(sorted(c.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name NOT LIKE 'nhl_%'").fetchall(), key=str)).encode()).hexdigest()[:12]
    nhl = sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'nhl_%'"))
    c.close()
    return out, schema, nhl


def opener(p, q):
    from tipsbot.profil4.store import Store
    try:
        Store(p).close(); q.put("ok")
    except Exception as e:  # noqa: BLE001
        q.put(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    src = Path(sys.argv[1])
    work = Path(tempfile.mkdtemp())
    a = work / "a.db"
    shutil.copy(src, a)
    before = fingerprint(a)
    print("pred: tabulek", len(before[0]), "radku", sum(n for n, _ in before[0].values()), "nhl tabulky", before[2])
    from tipsbot.profil4.store import Store
    t = time.perf_counter(); Store(a).close(); dt = time.perf_counter() - t
    after = fingerprint(a)
    print(f"migrace: {dt:.2f} s; data a schema puvodnich tabulek shodne: {after[:2] == before[:2]}; nhl tabulky po: {after[2]}")
    Store(a).close(); Store(a).close()
    again = fingerprint(a)
    print("idempotence (2 dalsi starty):", again == after)
    # soubeh: 8 procesu nad stejnou kopii PRED migraci, 10 kol
    fails = []
    for k in range(10):
        b = work / f"race{k}.db"
        shutil.copy(src, b)
        q = mp.Queue()
        ps = [mp.Process(target=opener, args=(str(b), q)) for _ in range(8)]
        [x.start() for x in ps]; [x.join() for x in ps]
        res = [q.get() for _ in ps]
        bad = [r for r in res if r != "ok"]
        fp = fingerprint(b)
        if bad or fp[:2] != before[:2] or len(fp[2]) != 4:
            fails.append((k, bad[:2], fp[2]))
        b.unlink()
    print("soubeh 8 procesu x 10 kol na kopii produkce: selhani", len(fails), fails[:2])
    # migrace behem drzeneho zapisoveho zamku (automat zapisuje): ceka, nepada
    c = work / "lock.db"
    shutil.copy(src, c)
    hold = sqlite3.connect(c, timeout=1, isolation_level=None, check_same_thread=False)
    hold.execute("BEGIN IMMEDIATE"); hold.execute("INSERT INTO kv(key, value) VALUES('lock_test','x')")
    threading.Timer(5.0, lambda: (hold.execute("ROLLBACK"))).start()
    t = time.perf_counter(); Store(c).close(); dt = time.perf_counter() - t
    print(f"migrace pri drzenem zamku 5 s: dokoncena po {dt:.1f} s, nhl tabulky {fingerprint(c)[2]}")
    print("WORK", work)
