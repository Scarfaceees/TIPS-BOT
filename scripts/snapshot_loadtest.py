"""Zatezovy test snapshotu (snapshot.py) nad KOPIEMI produkcnich databazi – nikdy ne nad data/.

Spousti se v Dockeru (stejny obraz a stejny typ pripojeni slozky jako produkce):

  docker run --rm -v <slozka s kopiemi>:/work -v <repo>/scripts:/scripts tipsbot-loadtest \
      python /scripts/snapshot_loadtest.py --dir /work --minutes 6

Zapisovac jako automat (vlastni proces, timeout 60 s jako aplikace): v davkach pise do OBOU kopii – prevody mezi ucty
(soucet 1000) a poradove cislo do logu, nejdriv tipsbot.db, pak profil4.db; kazda davka navic vlozi blok radku
(delsi transakce). Mezi davkami pauza. Do produkcnich tabulek se nesaha, testovaci tabulky maji predponu _lt_.

Behem zapisu se opakovane vytvari snapshot. Kazdy snapshot se overi:
- PRAGMA integrity_check obou kopii;
- invarianty: soucet uctu 1000, log bez mezer, max(tipsbot) - max(profil4) ∈ {0, 1} (oba z tehoz okamziku);
- produkcni tabulky (tips, odds_quotes, state…) maji stejny pocet radku a stejny otisk jako zdroj (zapisovac na ne
  nesaha, takze musi sedet presne).
Vysledek: doba snapshotu, pocet pokusu, SnapshotBusy, nejdelsi cekani zapisovace a chyby zamku.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import multiprocessing as mp
import sqlite3
import statistics
import sys
import time
from pathlib import Path

from tipsbot import snapshot

DBS = ("tipsbot.db", "profil4.db")


def prepare(d: Path) -> None:
    for n in DBS:
        con = sqlite3.connect(d / n)
        con.executescript("DROP TABLE IF EXISTS _lt_acct; DROP TABLE IF EXISTS _lt_log; DROP TABLE IF EXISTS _lt_bulk;"
                          "CREATE TABLE _lt_acct(id INTEGER PRIMARY KEY, bal INTEGER);"
                          "CREATE TABLE _lt_log(seq INTEGER PRIMARY KEY);"
                          "CREATE TABLE _lt_bulk(id INTEGER PRIMARY KEY, v TEXT);")
        con.executemany("INSERT INTO _lt_acct VALUES(?, 100)", [(i,) for i in range(10)])
        con.commit()
        con.close()


def _txn(con, seq, bulk):
    a, b = seq % 10, (seq * 7 + 3) % 10
    con.execute("UPDATE _lt_acct SET bal = bal - 1 WHERE id = ?", (a,))
    con.execute("UPDATE _lt_acct SET bal = bal + 1 WHERE id = ?", (b,))
    con.execute("INSERT INTO _lt_log VALUES(?)", (seq,))
    if bulk:
        con.executemany("INSERT INTO _lt_bulk(v) VALUES(?)", (("x" * 300,) for _ in range(bulk)))
    con.commit()


def writer(d: str, stop, out, burst: int, pause: float, bulk: int) -> None:
    cons = [sqlite3.connect(Path(d) / n, timeout=60) for n in DBS]  # jako aplikace
    seq, waits, errors = 0, [], []
    while not stop.is_set():
        for i in range(burst):
            seq += 1
            t = time.monotonic()
            try:
                for c in cons:
                    _txn(c, seq, bulk if i == 0 else 0)
            except sqlite3.OperationalError as e:
                errors.append(str(e))
                seq -= 1
                for c in cons:
                    c.rollback()
            waits.append(time.monotonic() - t)
        stop.wait(pause)
    out.put({"seq": seq, "errors": errors, "max_wait": max(waits) if waits else 0,
             "p99_wait": sorted(waits)[int(len(waits) * 0.99)] if waits else 0, "txns": len(waits)})


def _fingerprint(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        out = {}
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '_lt_%' "
                                "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            h = hashlib.sha256()
            n = 0
            for row in con.execute(f'SELECT * FROM "{t}" ORDER BY rowid'):
                h.update(repr(row).encode())
                n += 1
            out[t] = (n, h.hexdigest()[:16])
        return out
    finally:
        con.close()


def _state(path: Path) -> dict:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        seqs = [r[0] for r in con.execute("SELECT seq FROM _lt_log ORDER BY seq")]
        return {"integrity": con.execute("PRAGMA integrity_check").fetchone()[0],
                "sum": con.execute("SELECT SUM(bal) FROM _lt_acct").fetchone()[0],
                "max": seqs[-1] if seqs else 0, "contiguous": seqs == list(range(1, len(seqs) + 1))}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--minutes", type=float, default=6)
    ap.add_argument("--burst", type=int, default=40)
    ap.add_argument("--pause", type=float, default=3.0)
    ap.add_argument("--bulk", type=int, default=2000)
    ap.add_argument("--deadline", type=float, default=snapshot.DEADLINE_S)
    a = ap.parse_args()
    d = Path(a.dir)
    prepare(d)
    src_fp = {n: _fingerprint(d / n) for n in DBS}
    root = d / "snaps"
    stop, out = mp.Event(), mp.Queue()
    w = mp.Process(target=writer, args=(str(d), stop, out, a.burst, a.pause, a.bulk))
    w.start()
    res, end = [], time.monotonic() + a.minutes * 60
    time.sleep(2)
    while time.monotonic() < end:
        t = time.monotonic()
        try:
            m = snapshot.create(d / DBS[0], d / DBS[1], root, deadline_s=a.deadline)
            res.append({"id": m["id"], "s": round(time.monotonic() - t, 1), "attempts": m["attempts"]})
        except snapshot.SnapshotBusy:
            res.append({"id": None, "s": round(time.monotonic() - t, 1), "busy": True})
        print(json.dumps(res[-1]), flush=True)
        time.sleep(5)
    stop.set()
    wr = out.get(timeout=120)
    w.join()
    bad = []
    for r in res:
        if not r["id"]:
            continue
        sd = root / r["id"]
        st = {n: _state(sd / n) for n in DBS}
        ok = (all(s["integrity"] == "ok" and s["sum"] == 1000 and s["contiguous"] for s in st.values())
              and st[DBS[0]]["max"] - st[DBS[1]]["max"] in (0, 1) and st[DBS[0]]["max"] > 0)
        same = all(_fingerprint(sd / n) == src_fp[n] for n in DBS)
        r.update(consistent=ok, prod_tables_equal=same, seq=(st[DBS[0]]["max"], st[DBS[1]]["max"]))
        if not (ok and same):
            bad.append(r)
    done = [r for r in res if r["id"]]
    summary = {
        "sizes_mb": {n: round((d / n).stat().st_size / 2**20, 1) for n in DBS},
        "snapshots": len(done), "busy": sum(1 for r in res if r.get("busy")),
        "duration_s": {"median": statistics.median(r["s"] for r in done) if done else None,
                       "max": max(r["s"] for r in done) if done else None},
        "attempts_max": max((r["attempts"] for r in done), default=None),
        "consistent": sum(1 for r in done if r.get("consistent")),
        "prod_tables_equal": sum(1 for r in done if r.get("prod_tables_equal")),
        "writer": {"txns": wr["txns"], "lock_errors": len(wr["errors"]), "max_wait_s": round(wr["max_wait"], 2),
                   "p99_wait_s": round(wr["p99_wait"], 3)},
        "failures": bad,
    }
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False))
    return 0 if not bad and not wr["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
