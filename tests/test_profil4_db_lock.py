"""Zamykani databaze Profilu 4 (chyba z ostreho provozu 22.11.2026: "database is locked").

Automat (dve vlakna) a panel zapisuji do stejneho data/profil4.db. S vychozim cekanim 5 s padal ranni sken
i stahovani kurzu pred vykopem, a po padu ranniho skenu se dalsi pokus odlozil o 3 hodiny."""

import sqlite3
import threading
import time
from datetime import timedelta

import pytest

from tipsbot.profil4 import store as S
from tipsbot.profil4.store import Store


def test_busy_timeout_is_long(tmp_path):
    st = Store(tmp_path / "p4.db")
    assert st.conn.execute("PRAGMA busy_timeout").fetchone()[0] == S.BUSY_TIMEOUT_S * 1000 >= 60000
    st.close()


def test_writer_waits_for_other_connection_instead_of_failing(tmp_path, monkeypatch):
    path = tmp_path / "p4.db"
    a, b = Store(path), Store(path)
    holder = sqlite3.connect(str(path), check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # jine spojeni (jine vlakno / kontejner) drzi zapisovy zamek
    holder.execute("INSERT INTO kv(key, value) VALUES('x', '1')")

    def release():
        time.sleep(1.5)
        holder.commit()

    t = threading.Thread(target=release)
    t.start()
    t0 = time.monotonic()
    b.set("y", "2")  # puvodne by pri kratsim timeoutu selhalo; ted pocka
    t.join()
    assert time.monotonic() - t0 >= 1.0
    assert a.get("x") == "1" and a.get("y") == "2"
    for c in (a, b):
        c.close()
    holder.close()


def test_short_timeout_reproduces_original_failure(tmp_path):
    """Reprodukce puvodni chyby: spojeni bez dlouheho cekani pri drzenem zamku selze."""
    path = tmp_path / "p4.db"
    Store(path).close()
    holder = sqlite3.connect(str(path))
    holder.execute("BEGIN IMMEDIATE")
    quick = sqlite3.connect(str(path), timeout=0.2)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        quick.execute("INSERT INTO kv(key, value) VALUES('z', '1')")
    holder.rollback()
    holder.close()
    quick.close()


def test_batch_commits_once_and_rolls_back_on_error(tmp_path):
    path = tmp_path / "p4.db"
    st, other = Store(path), Store(path)
    with st.batch():
        st.set("a", "1")
        with st.batch():  # vnorena davka se commitne az s vnejsi
            st.set("b", "2")
        assert other.get("a") == ""  # jine spojeni rozpracovanou davku nevidi
    assert other.get("a") == "1" and other.get("b") == "2"
    with pytest.raises(RuntimeError):
        with st.batch():
            st.set("c", "3")
            raise RuntimeError("chyba uprostred")
    assert other.get("c") == "" and st.get("c") == ""
    st.set("d", "4")  # po chybe normalni zapis zase commituje hned
    assert other.get("d") == "4"
    st.close()
    other.close()


def test_failed_morning_scan_retries_in_minutes_not_hours(tmp_path, monkeypatch):
    import test_profil4_flow as t

    from tipsbot.profil4 import closing, config as c4, hunter

    cfg = c4.Profil4Config(data_dir=tmp_path)
    prov, store, _, _ = t.make()
    store.close = lambda: None
    monkeypatch.setattr(closing, "open_store", lambda cfg=None: store)
    now = t.NOW.replace(hour=7)  # 08:00 v Praze
    real = hunter.run_scan

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(hunter, "run_scan", locked)
    with pytest.raises(sqlite3.OperationalError):
        closing.morning_scan(now, cfg, lambda: (store, prov))
    assert store.get("auto_morning_scan") == ""  # neoznaceno jako hotove
    monkeypatch.setattr(hunter, "run_scan", real)
    assert closing.morning_scan(now + timedelta(minutes=5), cfg, lambda: (store, prov)) == []  # cekani na opakovani
    notes = closing.morning_scan(now + closing.MORNING_RETRY, cfg, lambda: (store, prov))
    assert notes and "ranní sken 48 h" in notes[0]  # za 10 min, ne az za 3 h
    assert store.get("auto_morning_scan") != ""  # a teprve ted oznaceno jako hotove
