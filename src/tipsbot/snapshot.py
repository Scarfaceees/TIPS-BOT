"""Datovy snapshot pro Excel, panel a CLI (Faze 3.2.3 – doplneni).

Automat muze mezi dvema vystupy udelat novy sken (napr. 24. 11. 2026: report z 18:11 UTC, Excel z 18:53 UTC, mezi
nimi sken 18:44–18:53 pridal 24 vyberu). Snapshot = konzistentni kopie obou databazi (panel + Profil 4) v jednom
okamziku; Excel, panel (Audit mereni) i CLI z nej ctou stejna data a uvadeji jeho ID a cas.

- kopie pres SQLite backup API ze zdroje otevreneho JEN PRO CTENI; zdrojove databaze se nemeni;
- konzistence: obe kopie se udelaji znovu, kdyz se behem kopirovani zmenila kterakoli databaze (PRAGMA data_version);
- ID = S<cas UTC>-<6 znaku SHA-256 obsahu obou kopii> – stejna data = stejny otisk;
- snapshot se vytvari jen na vyzadani (tlacitko v panelu / `tipsbot snapshot create`), nic se automaticky nemaze.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
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


def create(tipsbot_db: Path, p4_db: Path | None, root: Path, now: datetime | None = None, tries: int = 3) -> dict:
    """Konzistentni kopie obou databazi do `root/<ID>/`. Vraci metadata (ulozena i jako meta.json)."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    tmp = Path(root) / f".tmp-{now:%Y%m%d%H%M%S}"
    tmp.mkdir(parents=True, exist_ok=False)
    sources = [Path(tipsbot_db)] + ([Path(p4_db)] if p4_db and Path(p4_db).exists() else [])
    for attempt in range(1, tries + 1):
        cons = [_ro(p) for p in sources]
        try:
            before = [_version(c) for c in cons]
            for c, name in zip(cons, FILES):
                dst = sqlite3.connect(tmp / name)
                try:
                    c.backup(dst)
                finally:
                    dst.close()
            stable = [_version(c) for c in cons] == before
        finally:
            for c in cons:
                c.close()
        if stable:
            break
    else:
        raise RuntimeError("databáze se během kopírování opakovaně měnila (běží sken) – zkus snapshot za chvíli")
    copies = [tmp / n for n in FILES[:len(sources)]]
    sid = f"S{now:%Y%m%d-%H%M%S}-{_sha(copies)[:6]}"
    meta = {"id": sid, "created": now.isoformat(), "files": [p.name for p in copies], "attempts": attempt,
            "sizes": {p.name: p.stat().st_size for p in copies}}
    (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    final = Path(root) / sid
    tmp.rename(final)
    return meta


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
