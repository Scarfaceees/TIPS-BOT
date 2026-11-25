"""Datovy snapshot (snapshot.py): konzistence kopie pri soubeznych zapisech, vzajemna konzistence obou databazi,
timeout, preruseni a opakovany snapshot. Kopie musi byt vzdy JEDEN skutecny stav obou databazi – ne smes.

Invarianty zapisovace (simuluje automat):
- v kazde databazi jedna transakce = prevod mezi ucty (soucet zustava 1000) + zapis poradoveho cisla do logu;
- zapisuje se nejdriv A, pak B se stejnym cislem – v kazdem okamziku plati max(A) - max(B) ∈ {0, 1}.
Kopie, ktera by vznikla smichanim stavu z ruznych okamziku, porusi aspon jeden invariant."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from tipsbot import snapshot

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def _db(path, filler_rows=6000):
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE acct(id INTEGER PRIMARY KEY, bal INTEGER);"
                      "CREATE TABLE log(seq INTEGER PRIMARY KEY);"
                      "CREATE TABLE filler(id INTEGER PRIMARY KEY, v TEXT);")
    con.executemany("INSERT INTO acct VALUES(?, 100)", [(i,) for i in range(10)])
    con.executemany("INSERT INTO filler(v) VALUES(?)", ((f"{i:08d}" * 60,) for i in range(filler_rows)))
    con.commit()
    con.close()


def _step(con, seq):
    a, b = seq % 10, (seq * 7 + 3) % 10
    con.execute("UPDATE acct SET bal = bal - 1 WHERE id = ?", (a,))
    con.execute("UPDATE acct SET bal = bal + 1 WHERE id = ?", (b,))
    con.execute("INSERT INTO log VALUES(?)", (seq,))
    con.commit()


def _state(path) -> dict:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        seqs = [r[0] for r in con.execute("SELECT seq FROM log ORDER BY seq")]
        return {"integrity": con.execute("PRAGMA integrity_check").fetchone()[0],
                "sum": con.execute("SELECT SUM(bal) FROM acct").fetchone()[0],
                "max": seqs[-1] if seqs else 0, "contiguous": seqs == list(range(1, len(seqs) + 1)),
                "filler": hashlib.sha256("|".join(v for (v,) in con.execute("SELECT v FROM filler ORDER BY id"))
                                         .encode()).hexdigest()}
    finally:
        con.close()


def _check_pair(a: dict, b: dict) -> None:
    assert a["integrity"] == b["integrity"] == "ok"
    assert a["sum"] == b["sum"] == 1000  # kazda transakce cela, nebo vubec
    assert a["contiguous"] and b["contiguous"]  # zadna chybejici ani preskocena transakce
    assert a["max"] - b["max"] in (0, 1), (a["max"], b["max"])  # obe databaze z tehoz okamziku


@pytest.fixture
def dbs(tmp_path):
    a, b = tmp_path / "tipsbot.db", tmp_path / "profil4.db"
    _db(a)
    _db(b)
    return a, b


class Writer(threading.Thread):
    """Davky zapisu jako automat: `burst` transakci (A pak B), pak pauza. Meri chyby zamku a nejdelsi cekani."""

    def __init__(self, a, b, burst=15, pause=0.4, timeout=1.0):
        super().__init__()
        self.a, self.b, self.burst, self.pause, self.timeout = a, b, burst, pause, timeout
        self.stop, self.errors, self.seq, self.max_wait = threading.Event(), [], 0, 0.0

    def run(self):
        ca, cb = sqlite3.connect(self.a, timeout=self.timeout), sqlite3.connect(self.b, timeout=self.timeout)
        try:
            while not self.stop.is_set():
                for _ in range(self.burst):
                    self.seq += 1
                    t = time.monotonic()
                    try:
                        _step(ca, self.seq)
                        _step(cb, self.seq)
                    except sqlite3.OperationalError as e:
                        self.errors.append(str(e))
                        self.seq -= 1
                        ca.rollback()
                        cb.rollback()
                    self.max_wait = max(self.max_wait, time.monotonic() - t)
                self.stop.wait(self.pause)
        finally:
            ca.close()
            cb.close()


def _copies(root, meta):
    d = root / meta["id"]
    return d / "tipsbot.db", d / "profil4.db"


# --- 4 + 6: soubezne zapisy, konzistence a spravnost obsahu, obe databaze navzajem ---------------------------------
def test_snapshot_during_concurrent_writes_is_one_consistent_state_of_both_databases(dbs, tmp_path):
    a, b = dbs
    filler = _state(a)["filler"]
    w = Writer(a, b)
    w.start()
    metas = []
    try:
        time.sleep(0.2)
        for _ in range(3):  # opakovane snapshoty behem zapisu
            metas.append(snapshot.create(a, b, tmp_path / "snaps", deadline_s=60, chunk=8))
            time.sleep(0.1)
    finally:
        w.stop.set()
        w.join()
    assert w.errors == []  # automat nedostal „database is locked“
    assert w.max_wait < 0.5  # a necekal na zamek snapshotu
    assert len({m["id"] for m in metas}) == 3
    for m in metas:
        ca, cb = (_state(p) for p in _copies(tmp_path / "snaps", m))
        _check_pair(ca, cb)
        assert ca["filler"] == cb["filler"] == filler  # obsah nezmenenych tabulek presne odpovida zdroji
        assert ca["max"] > 0  # snapshot opravdu vznikl uprostred zapisu
    assert not list((tmp_path / "snaps").glob(".tmp-*"))


def test_write_between_the_two_copies_forces_a_new_attempt(dbs, tmp_path, monkeypatch):
    """Zapis po zkopirovani prvni databaze a pred druhou: bez kontroly data_version by kopie A byla starsi nez B
    (porusi max(A) - max(B) ∈ {0, 1}); s kontrolou se obe zkopiruji znovu."""
    a, b = dbs
    real, calls = snapshot._copy, []

    def copy(con, dst, deadline, chunk):
        real(con, dst, deadline, chunk)
        calls.append(dst.name)
        if len(calls) == 1:  # po prvni kopii (tipsbot) prijde sken: A i B dostanou nove transakce
            for p in (a, b):
                c = sqlite3.connect(p)
                for s in (1, 2):
                    _step(c, s)
                c.close()

    monkeypatch.setattr(snapshot, "_copy", copy)
    m = snapshot.create(a, b, tmp_path / "s1")
    assert m["attempts"] == 2
    ca, cb = (_state(p) for p in _copies(tmp_path / "s1", m))
    _check_pair(ca, cb)
    assert ca["max"] == cb["max"] == 2


def test_without_data_version_check_the_mixed_copy_would_be_detected(dbs, tmp_path, monkeypatch):
    """Kontrola testu samotneho: kdyz se kontrola data_version vyradi, vznikne smichana kopie a invariant ji odhali."""
    a, b = dbs
    real, calls = snapshot._copy, []

    def copy(con, dst, deadline, chunk):
        real(con, dst, deadline, chunk)
        calls.append(1)
        if len(calls) == 1:
            for p in (a, b):
                c = sqlite3.connect(p)
                for s in (1, 2):
                    _step(c, s)
                c.close()

    monkeypatch.setattr(snapshot, "_copy", copy)
    monkeypatch.setattr(snapshot, "_version", lambda con: 0)
    m = snapshot.create(a, b, tmp_path / "s2")
    ca, cb = (_state(p) for p in _copies(tmp_path / "s2", m))
    with pytest.raises(AssertionError):
        _check_pair(ca, cb)


def test_snapshot_without_writes_is_exact_logical_copy_and_source_is_untouched(dbs, tmp_path):
    a, b = dbs
    for p in (a, b):
        c = sqlite3.connect(p)
        for s in range(1, 6):
            _step(c, s)
        c.close()
    src = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)}
    m = snapshot.create(a, b, tmp_path / "s3", chunk=4)
    for s_, c_ in zip((a, b), _copies(tmp_path / "s3", m)):
        x, y = sqlite3.connect(s_), sqlite3.connect(c_)
        assert list(x.iterdump()) == list(y.iterdump())
        x.close()
        y.close()
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)} == src  # zdroj se nezmenil
    assert m["attempts"] == 1 and m["copied_until"] >= m["created"]


# --- 5: timeout, preruseni, opakovani -------------------------------------------------------------------------------
def test_timeout_gives_up_cleans_its_temp_and_leaves_sources_intact(dbs, tmp_path):
    a, b = dbs
    src = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)}
    with pytest.raises(snapshot.SnapshotBusy):
        snapshot.create(a, b, tmp_path / "s4", deadline_s=-1, chunk=1)
    assert list((tmp_path / "s4").iterdir()) == []
    assert snapshot.items(tmp_path / "s4") == []
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (a, b)} == src


def test_continuous_writes_end_in_busy_not_in_blocked_writer(dbs, tmp_path):
    a, b = dbs
    w = Writer(a, b, burst=1, pause=0.0)  # zapis bez prestavky – snapshot se nema sanci dokoncit
    w.start()
    try:
        time.sleep(0.1)
        t = time.monotonic()
        with pytest.raises(snapshot.SnapshotBusy):
            snapshot.create(a, b, tmp_path / "s5", deadline_s=2, chunk=8)
        assert time.monotonic() - t < 10  # limit se dodrzi, nic se nezasekne
    finally:
        w.stop.set()
        w.join()
    assert w.errors == [] and w.seq > 0
    assert list((tmp_path / "s5").iterdir()) == []


def test_interrupted_snapshot_leaves_nothing_listed_and_next_one_works(dbs, tmp_path, monkeypatch):
    a, b = dbs
    real = snapshot._copy

    def boom(con, dst, deadline, chunk):
        if dst.name == "profil4.db":
            raise KeyboardInterrupt  # preruseni uprostred (napr. Ctrl+C, zastaveni pozadavku)
        real(con, dst, deadline, chunk)

    monkeypatch.setattr(snapshot, "_copy", boom)
    with pytest.raises(KeyboardInterrupt):
        snapshot.create(a, b, tmp_path / "s6")
    assert list((tmp_path / "s6").iterdir()) == []
    monkeypatch.setattr(snapshot, "_copy", real)
    # tvrde zabiti procesu (jako 25. 11. v Dockeru): zustane cizi .tmp slozka – seznam ji ignoruje, nic ji nemaze
    left = tmp_path / "s6" / ".tmp-20261125090042"
    left.mkdir()
    (left / "profil4.db").write_bytes(b"castecna kopie")
    m = snapshot.create(a, b, tmp_path / "s6")
    assert [x["id"] for x in snapshot.items(tmp_path / "s6")] == [m["id"]]
    assert snapshot.get(tmp_path / "s6", ".tmp-20261125090042") is None
    assert (left / "profil4.db").read_bytes() == b"castecna kopie"


def test_repeated_snapshot_in_the_same_second_gets_distinct_ids(dbs, tmp_path):
    a, b = dbs
    m1 = snapshot.create(a, b, tmp_path / "s7", NOW)
    m2 = snapshot.create(a, b, tmp_path / "s7", NOW)
    assert m1["id"] != m2["id"] and m2["id"] == m1["id"] + "-1"  # stejna data = stejny otisk, jine ID
    assert {x["id"] for x in snapshot.items(tmp_path / "s7")} == {m1["id"], m2["id"]}
    for m in (m1, m2):
        _check_pair(*(_state(p) for p in _copies(tmp_path / "s7", m)))


def test_local_work_copy_is_removed_and_damaged_transfer_is_rejected(dbs, tmp_path, monkeypatch):
    a, b = dbs
    work = tmp_path / "work"
    work.mkdir()
    snapshot.create(a, b, tmp_path / "s8", work_dir=work)
    assert list(work.iterdir()) == []  # lokalni pracovni kopie po uspechu smazana
    real = snapshot.shutil.copyfile

    def damaged(src, dst):
        real(src, dst)
        with open(dst, "r+b") as f:  # poskozeni pri presunu do slozky snapshotu
            f.seek(200)
            f.write(b"\x00\xff")

    monkeypatch.setattr(snapshot.shutil, "copyfile", damaged)
    with pytest.raises(RuntimeError):
        snapshot.create(a, b, tmp_path / "s9", work_dir=work)
    assert list((tmp_path / "s9").iterdir()) == [] and list(work.iterdir()) == []
