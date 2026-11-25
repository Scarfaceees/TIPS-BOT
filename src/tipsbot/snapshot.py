"""Datovy snapshot pro Excel, panel a CLI (Faze 3.2.3 – doplneni).

Automat muze mezi dvema vystupy udelat novy sken (napr. 24. 11. 2026: report z 18:11 UTC, Excel z 18:53 UTC, mezi
nimi sken 18:44–18:53 pridal 24 vyberu). Snapshot = konzistentni kopie obou databazi (panel + Profil 4) v jednom
okamziku; Excel, panel (Audit mereni) i CLI z nej ctou stejna data a uvadeji jeho ID a cas.

- kopie pres SQLite Online Backup API (sqlite3.Connection.backup = sqlite3_backup_init/step/finish), NE kopirovani
  souboru: zdroj otevreny JEN PRO CTENI, po krocich `CHUNK_PAGES` stranek, zamek pro cteni jen po dobu kroku;
  zapis jineho spojeni do zdroje SQLite sam pozna a kopii restartuje od zacatku (kopie je vzdy jeden stav databaze);
- konzistence obou databazi navzajem: PRAGMA data_version obou zdroju pred a po kopirovani; kdyz se kterakoli
  zmenila, kopiruji se obe znovu – vysledek odpovida jednomu okamziku, kdy se nemenila ani jedna;
- casovy limit: pri neustalych zapisech se snapshot vzda (SnapshotBusy), zapisy automatu maji prednost;
- rozpracovana kopie je v unikatni skryte slozce `.tmp-*`, do seznamu se nedostane; po chybe ji snapshot smaze
  (po tvrdem zabiti procesu zustane – nic ji automaticky nemaze);
- ID = S<cas UTC>-<6 znaku SHA-256 obsahu obou kopii> – stejna data = stejny otisk;
- snapshot se vytvari jen na vyzadani (tlacitko v panelu / `tipsbot snapshot create`), nic se automaticky nemaze.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

DIRNAME = "snapshots"
FILES = ("tipsbot.db", "profil4.db")


def root() -> Path:
    """Slozka snapshotu: data/snapshots (TIPSBOT_SNAPSHOT_DIR jen pro testy)."""
    from .config import SETTINGS

    return Path(os.environ.get("TIPSBOT_SNAPSHOT_DIR") or Path(SETTINGS.data_dir) / DIRNAME)


def db_file(url: str | None) -> Path:
    """Cesta k SQLite databazi panelu z URL (sqlite:///…)."""
    from .config import SETTINGS

    url = url or SETTINGS.database_url
    if not url.startswith("sqlite:///"):
        raise ValueError("snapshot umí jen SQLite databázi")
    return Path(url[len("sqlite:///"):])


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def _version(con: sqlite3.Connection) -> int:
    return con.execute("PRAGMA data_version").fetchone()[0]


def _sha(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


CHUNK_PAGES = 256  # ~1 MB na krok: zamek pro cteni se drzi jen milisekundy, automat mezi kroky zapisuje
DEADLINE_S = 180  # kdyz se kopie nestihne (bezi sken a databaze se stale meni), snapshot se vzda – zapisy maji prednost


class SnapshotBusy(RuntimeError):
    """Snapshot se nepodaril, protoze se databaze behem kopirovani menila (bezi sken)."""


def _copy(con: sqlite3.Connection, dst_path: Path, deadline: float, chunk: int) -> None:
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise SnapshotBusy("databáze se během kopírování stále mění (běží sken) – zkus snapshot za chvíli")

    dst = sqlite3.connect(dst_path)
    try:
        con.backup(dst, pages=chunk, progress=progress, sleep=0.05)
    finally:
        dst.close()


RETRY_PAUSE_S = 0.5  # pauza mezi pokusy, at automat dokonci davku zapisu


def _rmtree_own(d: Path) -> None:
    """Smaze jen vlastni docasnou slozku tohoto snapshotu (soubory primo v ni)."""
    if d.exists():
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def create(tipsbot_db: Path, p4_db: Path | None, root: Path, now: datetime | None = None,
           deadline_s: float = DEADLINE_S, chunk: int = CHUNK_PAGES, work_dir: Path | None = None) -> dict:
    """Konzistentni kopie obou databazi do `root/<ID>/`. Vraci metadata (ulozena i jako meta.json).

    Kopiruje se po malych krocich (`chunk` stranek): zdroj je zamceny jen po dobu jednoho kroku, takze automat muze
    mezi kroky zapisovat (databaze jsou v rezimu journal_mode=delete – dlouhy zamek pro cteni by zapis zablokoval).
    Zapis jineho spojeni kopii restartuje; po `deadline_s` sekundach se snapshot vzda (SnapshotBusy) a rozpracovanou
    vlastni docasnou kopii smaze. Konzistence obou databazi: PRAGMA data_version pred a po kopirovani.

    Backup jde nejdriv do LOKALNI docasne slozky (`work_dir`, vychozi systemovy temp – v Dockeru /tmp kontejneru):
    zapis do pripojene slozky data/ je v Dockeru Desktop ~4× pomalejsi (profil4.db 67 s proti 15 s) a automat zapisuje
    do profil4.db kazdou minutu (auto_tick_at), takze by kopii stale restartoval. Hotova a overena soukroma kopie se
    pak bez jakehokoli zamku na zdroji zkopiruje do `root` a otisk se zkontroluje."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    Path(root).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{now:%Y%m%d%H%M%S}-", dir=root))  # unikatni i pri soubehu
    work = Path(tempfile.mkdtemp(prefix="tipsbot-snapshot-", dir=work_dir))
    sources = [Path(tipsbot_db)] + ([Path(p4_db)] if p4_db and Path(p4_db).exists() else [])
    deadline = time.monotonic() + deadline_s
    try:
        attempt = 0
        while True:  # pokusy az do casoveho limitu; kazdy pokus kopiruje obe databaze znovu
            attempt += 1
            cons = [_ro(p) for p in sources]
            try:
                before = [_version(c) for c in cons]
                for c, name in zip(cons, FILES):
                    (work / name).unlink(missing_ok=True)
                    _copy(c, work / name, deadline, chunk)
                stable = [_version(c) for c in cons] == before
            finally:
                for c in cons:
                    c.close()
            if stable:
                break
            if time.monotonic() + RETRY_PAUSE_S > deadline:
                raise SnapshotBusy("databáze se během kopírování opakovaně měnila (běží sken) – zkus snapshot za chvíli")
            time.sleep(RETRY_PAUSE_S)
        names = FILES[:len(sources)]
        digest = _sha([work / n for n in names])
        for n in names:  # hotova soukroma kopie -> snapshot (zdroj uz neni potreba, zadny zamek)
            shutil.copyfile(work / n, tmp / n)
        if _sha([tmp / n for n in names]) != digest:
            raise RuntimeError("kopie snapshotu se při přesunu poškodila – snapshot zahozen")
    except BaseException:
        _rmtree_own(tmp)  # jen vlastni rozpracovana kopie tohoto snapshotu
        raise
    finally:
        _rmtree_own(work)
    copies = [tmp / n for n in names]
    base = f"S{now:%Y%m%d-%H%M%S}-{digest[:6]}"
    meta = {"id": base, "created": now.isoformat(),
            "copied_until": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "files": [p.name for p in copies], "attempts": attempt,
            "sizes": {p.name: p.stat().st_size for p in copies}}
    for n in range(100):  # opakovany snapshot ve stejne sekunde se stejnymi daty: -1, -2…
        meta["id"] = base if n == 0 else f"{base}-{n}"
        (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            tmp.rename(Path(root) / meta["id"])
            return meta
        except OSError:
            if not (Path(root) / meta["id"]).exists():
                raise
    raise RuntimeError("nepodařilo se přidělit ID snapshotu")


def items(root: Path) -> list[dict]:
    """Ulozene snapshoty, nejnovejsi prvni."""
    out = []
    for m in Path(root).glob("S*/meta.json"):
        try:
            out.append(json.loads(m.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda m: m["created"], reverse=True)


def get(root: Path, sid: str) -> dict | None:
    """Metadata snapshotu podle ID (jen ID ze seznamu – zadne cesty z parametru)."""
    return next((m for m in items(root) if m["id"] == sid), None)


def paths(root: Path, meta: dict) -> tuple[str, Path | None, datetime]:
    """(URL databaze panelu, cesta k databazi Profilu 4 nebo None, cas snapshotu)."""
    d = Path(root) / meta["id"]
    p4 = d / "profil4.db"
    return f"sqlite:///{(d / 'tipsbot.db').as_posix()}", (p4 if p4.exists() else None), \
        datetime.fromisoformat(meta["created"])


def session_factory(url: str):
    """Session nad snapshotem JEN PRO CTENI (SQLite mode=ro, bez create_all) – snapshot se nikdy nezmeni."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    path = url[len("sqlite:///"):]
    engine = create_engine(f"sqlite:///file:{path}?mode=ro&uri=true")
    return sessionmaker(engine, expire_on_commit=False)


def label(meta: dict | None, read_at: datetime) -> str:
    if meta:
        return f"Snapshot {meta['id']} (data k {meta['created'][:16].replace('T', ' ')} UTC)"
    return f"Živá databáze bez snapshotu (čteno {read_at:%Y-%m-%d %H:%M} UTC) – další sken může čísla změnit"
