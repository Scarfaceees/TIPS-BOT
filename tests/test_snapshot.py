"""Datovy snapshot (snapshot.py): konzistence kopie pri soubeznych zapisech, vzajemna konzistence obou databazi,
timeout, preruseni a opakovany snapshot. Kopie musi byt vzdy JEDEN skutecny stav obou databazi – ne smes.

Invarianty zapisovace (simuluje automat):
- v kazde databazi jedna transakce = prevod mezi ucty (soucet zustava 1000) + zapis poradoveho cisla do logu;
- zapisuje se nejdriv A, pak B se stejnym cislem – v kazdem okamziku plati max(A) - max(B) ∈ {0, 1}.
Kopie, ktera by vznikla smichanim stavu z ruznych okamziku, porusi aspon jeden invariant."""

from __future__ import annotations

import hashlib
import json
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


def _failures(root):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((root / snapshot.FAILED_DIR).glob("F*.json"))]


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
        r = real(con, dst, deadline, chunk)
        calls.append(dst.name)
        if len(calls) == 1:  # po prvni kopii (tipsbot) prijde sken: A i B dostanou nove transakce
            for p in (a, b):
                c = sqlite3.connect(p)
                for s in (1, 2):
                    _step(c, s)
                c.close()
        return r

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
        r = real(con, dst, deadline, chunk)
        calls.append(1)
        if len(calls) == 1:
            for p in (a, b):
                c = sqlite3.connect(p)
                for s in (1, 2):
                    _step(c, s)
                c.close()
        return r

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
    assert [x.name for x in (tmp_path / "s4").iterdir()] == ["failed"]  # jen diagnostika, zadny snapshot
    assert snapshot.items(tmp_path / "s4") == []
    (fail,) = _failures(tmp_path / "s4")
    assert fail["status"] == "FAILED" and fail["published"] is False and fail["error"] == "SnapshotBusy"
    assert "stále mění" in fail["reason"] and fail["attempts"] == 1
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
    assert [x.name for x in (tmp_path / "s5").iterdir()] == ["failed"]
    (fail,) = _failures(tmp_path / "s5")
    assert fail["error"] == "SnapshotBusy" and fail["attempt_log"]  # diagnostika s prubehem pokusu


def test_interrupted_snapshot_leaves_nothing_listed_and_next_one_works(dbs, tmp_path, monkeypatch):
    a, b = dbs
    real = snapshot._copy

    def boom(con, dst, deadline, chunk):
        if dst.name == "profil4.db":
            raise KeyboardInterrupt  # preruseni uprostred (napr. Ctrl+C, zastaveni pozadavku)
        return real(con, dst, deadline, chunk)

    monkeypatch.setattr(snapshot, "_copy", boom)
    with pytest.raises(KeyboardInterrupt):
        snapshot.create(a, b, tmp_path / "s6")
    assert [x.name for x in (tmp_path / "s6").iterdir()] == ["failed"]
    assert _failures(tmp_path / "s6")[0]["error"] == "KeyboardInterrupt"
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
    assert [x.name for x in (tmp_path / "s9").iterdir()] == ["failed"] and list(work.iterdir()) == []
    assert "poškodila" in _failures(tmp_path / "s9")[0]["reason"]


# --- auditni metadata meta.json (META_VERSION 2) --------------------------------------------------------------------
DB_KEYS = {"data_version_before", "data_version_read_before", "copy_started", "backup_restarts", "copy_finished",
           "data_version_after", "data_version_read_after", "data_version_unchanged"}


def _saved(root, meta):
    return json.loads((root / meta["id"] / "meta.json").read_text(encoding="utf-8"))


def test_audit_meta_without_writes(dbs, tmp_path):
    a, b = dbs
    m = snapshot.create(a, b, tmp_path / "m1", chunk=8)
    assert _saved(tmp_path / "m1", m) == m  # meta.json = vracena metadata
    assert m["meta_version"] == 2 and m["status"] == "PUBLISHED" and m["attempts"] == 1
    assert m["restarts"] == {"repeated_attempts": 0, "backup_internal": 0}
    assert m["started"] <= m["finished"]
    (rec,) = m["attempt_log"]
    assert rec["result"] == "stable" and rec["reason"] is None
    t, p = m["databases"]["tipsbot.db"], m["databases"]["profil4.db"]
    for d, path in ((t, a), (p, b)):
        assert DB_KEYS <= d.keys()
        assert d["data_version_before"] == d["data_version_after"] and d["data_version_unchanged"]
        assert d["data_version_read_before"] <= d["copy_started"] <= d["copy_finished"] <= d["data_version_read_after"]
        assert d["integrity_check"] == "ok" and d["foreign_key_violations"] == 0
        copy = tmp_path / "m1" / m["id"] / path.name
        assert d["sha256"] == hashlib.sha256(copy.read_bytes()).hexdigest() and d["size"] == copy.stat().st_size
    assert t["copy_finished"] <= p["copy_started"]  # nejdriv tipsbot.db, pak profil4.db
    c = m["consistency"]
    assert {x["status"] for x in c["per_database"].values()} == {"UNCHANGED_DURING_COPY"}
    assert c["snapshot"]["status"] == "CONSISTENT" and all(c["snapshot"]["checks"].values())
    w = c["snapshot"]["common_window"]
    assert m["started"] <= w["from"] <= w["until"] <= m["finished"]
    assert c["snapshot"]["limits"]  # data_version neni prezentovana jako absolutni dukaz
    assert not (tmp_path / "m1" / snapshot.FAILED_DIR).exists()
    assert "CONSISTENT" in snapshot.audit_summary(m)


def test_audit_meta_with_concurrent_writes(dbs, tmp_path):
    a, b = dbs
    w = Writer(a, b, burst=15, pause=0.3)
    w.start()
    try:
        time.sleep(0.2)
        m = snapshot.create(a, b, tmp_path / "m2", deadline_s=60, chunk=8)
    finally:
        w.stop.set()
        w.join()
    assert w.errors == []
    assert len(m["attempt_log"]) == m["attempts"] == m["restarts"]["repeated_attempts"] + 1
    for rec in m["attempt_log"][:-1]:  # kazde opakovani ma duvod a uplny zaznam obou databazi
        assert rec["result"] == "repeat" and "znovu se kopírují obě databáze" in rec["reason"]
        assert set(rec["databases"]) == {"tipsbot.db", "profil4.db"}
    assert m["attempt_log"][-1]["result"] == "stable"
    assert m["consistency"]["snapshot"]["status"] == "CONSISTENT"
    _check_pair(*(_state(p) for p in _copies(tmp_path / "m2", m)))


def test_change_of_one_database_between_copies_repeats_both(dbs, tmp_path, monkeypatch):
    a, b = dbs
    real, calls = snapshot._copy, []

    def copy(con, dst, deadline, chunk):
        r = real(con, dst, deadline, chunk)
        calls.append(dst.name)
        if len(calls) == 1:  # po kopii tipsbot.db zapise automat JEN do tipsbot.db
            c = sqlite3.connect(a)
            _step(c, 1)
            c.close()
        return r

    monkeypatch.setattr(snapshot, "_copy", copy)
    m = snapshot.create(a, b, tmp_path / "m3")
    first, second = m["attempt_log"]
    assert first["databases"]["tipsbot.db"]["data_version_unchanged"] is False
    assert first["databases"]["profil4.db"]["data_version_unchanged"] is True
    assert first["result"] == "repeat" and first["reason"].startswith("data_version se změnila: tipsbot.db –")
    assert calls == ["tipsbot.db", "profil4.db"] * 2  # opakuje se kopie OBOU databazi
    assert second["result"] == "stable"
    assert m["consistency"]["per_database"]["tipsbot.db"]["status"] == "UNCHANGED_DURING_COPY"  # posledni pokus
    assert _state(_copies(tmp_path / "m3", m)[0])["max"] == 1


def test_forced_backup_restart_is_recorded(dbs, tmp_path, monkeypatch):
    """Zapis jineho spojeni uprostred Backup API (mezi kroky): SQLite kopii restartuje, data_version se zmeni,
    pokus se opakuje – vse s duvodem v metadatech."""
    a, b = dbs
    real = snapshot._copy

    class Clock:  # snapshot._copy vola time.monotonic() po kazdem kroku – 5. krok kopie profil4.db = zapis automatu
        sleep = staticmethod(time.sleep)
        active, steps, done = False, 0, False

        def monotonic(self):
            if self.active and not self.done:
                self.steps += 1
                if self.steps == 5:
                    c = sqlite3.connect(b)
                    _step(c, 1)
                    c.close()
                    self.done = True
            return time.monotonic()

    clock = Clock()

    def copy(con, dst, deadline, chunk):
        clock.active = dst.name == "profil4.db"
        try:
            return real(con, dst, deadline, chunk)
        finally:
            clock.active = False

    monkeypatch.setattr(snapshot, "time", clock)
    monkeypatch.setattr(snapshot, "_copy", copy)
    m = snapshot.create(a, b, tmp_path / "m4", chunk=8)
    first = m["attempt_log"][0]
    p4 = first["databases"]["profil4.db"]
    assert p4["backup_restarts"] >= 1 and p4["data_version_unchanged"] is False
    assert "data_version se změnila: profil4.db" in first["reason"]
    assert "vnitřní restart Backup API: profil4.db" in first["reason"]
    assert m["attempts"] == 2 and m["restarts"]["repeated_attempts"] == 1 and m["restarts"]["backup_internal"] >= 1
    assert m["consistency"]["snapshot"]["status"] == "CONSISTENT"
    assert _state(_copies(tmp_path / "m4", m)[1])["max"] == 1  # snapshot obsahuje zapis, ktery restart vyvolal


def test_failed_integrity_is_never_published(dbs, tmp_path, monkeypatch):
    a, b = dbs
    monkeypatch.setattr(snapshot, "_check_copy",
                        lambda p: {"integrity_check": "*** in database main ***", "foreign_key_violations": 0})
    with pytest.raises(RuntimeError, match="integrity_check"):
        snapshot.create(a, b, tmp_path / "m5")
    assert snapshot.items(tmp_path / "m5") == []
    assert [x.name for x in (tmp_path / "m5").iterdir()] == ["failed"]
    (fail,) = _failures(tmp_path / "m5")
    assert fail["published"] is False and "integrity_check" in fail["reason"] and fail["attempt_log"]


def test_old_meta_without_audit_fields_still_works(dbs, tmp_path):
    a, b = dbs
    root = tmp_path / "m6"
    m = snapshot.create(a, b, root, NOW)
    old = {k: m[k] for k in ("id", "created", "copied_until", "files", "attempts", "sizes")}  # format do 25. 11.
    (root / m["id"] / "meta.json").write_text(json.dumps(old), encoding="utf-8")
    assert snapshot.items(root) == [old] and snapshot.get(root, m["id"]) == old
    url, p4, at = snapshot.paths(root, old)
    assert p4.name == "profil4.db" and at == NOW
    assert snapshot.label(old, NOW).startswith(f"Snapshot {m['id']} (data k 2026-11-25 12:00")
    assert snapshot.audit_summary(old) == "starší snapshot bez auditních metadat (pokusů 1)"
    with snapshot.session_factory(url)() as s:
        from sqlalchemy import text
        assert s.execute(text("SELECT SUM(bal) FROM acct")).scalar() == 1000
