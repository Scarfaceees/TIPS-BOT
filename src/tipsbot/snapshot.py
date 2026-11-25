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
- snapshot se vytvari jen na vyzadani (tlacitko v panelu / `tipsbot snapshot create`), nic se automaticky nemaze;
- meta.json (verze 2) je auditni zaznam: casy a data_version kazde databaze, pokusy a duvody opakovani, kontroly kopii
  (integrity_check, foreign_key_check, SHA-256) a zvlast spolecna konzistence; neuspech -> diagnostika ve failed/.
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
META_VERSION = 2  # 1 = jen id/created/copied_until/files/attempts/sizes (snapshoty do 25. 11. 2026); 2 = auditni metadata
FAILED_DIR = "failed"  # diagnostika neuspesnych pokusu (F*.json) – nikdy se nevystavuje jako snapshot
RETRY_PAUSE_S = 0.5  # pauza mezi pokusy, at automat dokonci davku zapisu

CONSISTENCY_LIMITS = (
    "PRAGMA data_version téhož spojení zůstane stejná, jen když do databáze mezi dvěma čteními nezapsalo žádné jiné "
    "spojení. Nezachytí změnu souboru mimo SQLite (např. jeho nahrazení nebo obnovu ze zálohy) a sama o sobě není "
    "absolutní důkaz; společný okamžik je doložen kombinací všech kontrol v `checks`.")


class SnapshotBusy(RuntimeError):
    """Snapshot se nepodaril, protoze se databaze behem kopirovani menila (bezi sken)."""


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _copy(con: sqlite3.Connection, dst_path: Path, deadline: float, chunk: int) -> int:
    """Backup API po `chunk` strankach. Vraci pocet vnitrnich restartu Backup API (pocet zbyvajicich stranek po kroku
    narostl = SQLite zacal znovu, protoze do zdroje zapsalo jine spojeni). Orientacni udaj: restart hned po prvnim
    kroku se takto nepozna – rozhoduje kontrola data_version."""
    seen = {"last": None, "restarts": 0}

    def progress(status, remaining, total):
        if seen["last"] is not None and remaining > seen["last"]:
            seen["restarts"] += 1
        seen["last"] = remaining
        if time.monotonic() > deadline:
            raise SnapshotBusy("databáze se během kopírování stále mění (běží sken) – zkus snapshot za chvíli")

    dst = sqlite3.connect(dst_path)
    try:
        con.backup(dst, pages=chunk, progress=progress, sleep=0.05)
    finally:
        dst.close()
    return seen["restarts"]


def _check_copy(path: Path) -> dict:
    """integrity_check a foreign_key_check hotove lokalni kopie (zdroj uz neni zamceny)."""
    con = _ro(path)
    try:
        integrity = [r[0] for r in con.execute("PRAGMA integrity_check")]
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()
    return {"integrity_check": "ok" if integrity == ["ok"] else "; ".join(integrity[:20]),
            "foreign_key_violations": len(fk)}


def _rmtree_own(d: Path) -> None:
    """Smaze jen vlastni docasnou slozku tohoto snapshotu (soubory primo v ni)."""
    if d.exists():
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def _record_failure(root: Path, started: str, attempts: list, err: BaseException) -> None:
    """Diagnostika neuspesneho snapshotu do root/failed/F*.json. Nikdy nesmi zakryt puvodni chybu."""
    try:
        d = Path(root) / FAILED_DIR
        d.mkdir(parents=True, exist_ok=True)
        stamp = started[:19].replace("-", "").replace(":", "")
        fd, _ = tempfile.mkstemp(prefix=f"F{stamp}-", suffix=".json", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"meta_version": META_VERSION, "status": "FAILED", "published": False, "started": started,
                       "failed_at": _ts(), "error": type(err).__name__, "reason": str(err) or type(err).__name__,
                       "attempts": len(attempts), "attempt_log": attempts}, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001 – diagnostika je navic, puvodni chyba ma prednost
        pass


def _attempt(sources: list[Path], names: tuple, work: Path, deadline: float, chunk: int, rec: dict) -> None:
    """Jeden pokus: data_version obou -> Backup API obou -> data_version obou. Prubeh i vysledek zapise do `rec`."""
    cons = [_ro(p) for p in sources]
    dbs = rec["databases"]
    try:
        for c, n in zip(cons, names):
            dbs[n] = {"data_version_before": _version(c), "data_version_read_before": _ts()}
        for c, n in zip(cons, names):
            (work / n).unlink(missing_ok=True)
            dbs[n]["copy_started"] = _ts()
            dbs[n]["backup_restarts"] = _copy(c, work / n, deadline, chunk) or 0
            dbs[n]["copy_finished"] = _ts()
        for c, n in zip(cons, names):
            dbs[n]["data_version_after"] = _version(c)
            dbs[n]["data_version_read_after"] = _ts()
            dbs[n]["data_version_unchanged"] = dbs[n]["data_version_after"] == dbs[n]["data_version_before"]
    finally:
        for c in cons:
            c.close()
    changed = [n for n in names if not dbs[n]["data_version_unchanged"]]
    restarted = [n for n in names if dbs[n]["backup_restarts"]]
    rec["finished"] = _ts()
    reasons = ([f"data_version se změnila: {', '.join(changed)}"] if changed else []) + \
              ([f"vnitřní restart Backup API: {', '.join(restarted)}"] if restarted else [])
    rec["result"] = "repeat" if reasons else "stable"
    rec["reason"] = ("; ".join(reasons) + " – znovu se kopírují obě databáze") if reasons else None


def _consistency(dbs: dict) -> dict:
    """Vysledek kontroly kazde databaze zvlast a z nich odvozena spolecna konzistence snapshotu."""
    per = {n: {"data_version_unchanged": d["data_version_unchanged"], "backup_restarts": d["backup_restarts"],
               "integrity_check": d["integrity_check"],
               "status": "UNCHANGED_DURING_COPY" if d["data_version_unchanged"] and not d["backup_restarts"]
               else "CHANGED_DURING_COPY"} for n, d in dbs.items()}
    w_from = max(d["data_version_read_before"] for d in dbs.values())
    w_until = min(d["data_version_read_after"] for d in dbs.values())
    checks = {"all_databases_unchanged": all(p["status"] == "UNCHANGED_DURING_COPY" for p in per.values()),
              "common_window_not_empty": w_from <= w_until,
              "all_integrity_ok": all(p["integrity_check"] == "ok" for p in per.values())}
    return {"per_database": per,
            "snapshot": {"status": "CONSISTENT" if all(checks.values()) else "NOT_CONSISTENT", "checks": checks,
                         "common_window": {"from": w_from, "until": w_until},
                         "meaning": "obě kopie odpovídají stavu databází v kterémkoli okamžiku společného okna – "
                                    "v tomto intervalu nebyl do žádné z nich zapsán commit",
                         "limits": CONSISTENCY_LIMITS}}


def create(tipsbot_db: Path, p4_db: Path | None, root: Path, now: datetime | None = None,
           deadline_s: float = DEADLINE_S, chunk: int = CHUNK_PAGES, work_dir: Path | None = None) -> dict:
    """Konzistentni kopie obou databazi do `root/<ID>/`. Vraci metadata (ulozena i jako meta.json).

    Kopiruje se po malych krocich (`chunk` stranek): zdroj je zamceny jen po dobu jednoho kroku, takze automat muze
    mezi kroky zapisovat (databaze jsou v rezimu journal_mode=delete – dlouhy zamek pro cteni by zapis zablokoval).
    Zapis jineho spojeni kopii restartuje; po `deadline_s` sekundach se snapshot vzda (SnapshotBusy) a rozpracovanou
    vlastni docasnou kopii smaze. Konzistence obou databazi: PRAGMA data_version pred a po kopirovani.

    Backup jde nejdriv do LOKALNI docasne slozky (`work_dir`, vychozi systemovy temp – v Dockeru /tmp kontejneru):
    zapis do pripojene slozky data/ je v Dockeru Desktop ~4× pomalejsi (profil4.db 67 s proti 15 s) a automat zapisuje
    do profil4.db kazdou minutu (auto_tick_at), takze by kopii stale restartoval. Hotova kopie se zkontroluje
    (integrity_check, foreign_key_check, SHA-256), bez jakehokoli zamku na zdroji se zkopiruje do `root` a otisk se
    overi znovu. Auditni metadata (META_VERSION 2): casy a data_version kazdeho pokusu a kazde databaze, duvod
    opakovani, kontroly kopii a zvlast spolecna konzistence. Neuspech: diagnostika v root/failed/, nic se nevystavi."""
    started = _ts()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    Path(root).mkdir(parents=True, exist_ok=True)
    sources = [Path(tipsbot_db)] + ([Path(p4_db)] if p4_db and Path(p4_db).exists() else [])
    names = FILES[:len(sources)]
    attempts: list[dict] = []
    tmp = work = None
    try:
        tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{now:%Y%m%d%H%M%S}-", dir=root))  # unikatni i pri soubehu
        work = Path(tempfile.mkdtemp(prefix="tipsbot-snapshot-", dir=work_dir))
        deadline = time.monotonic() + deadline_s
        while True:  # pokusy az do casoveho limitu; kazdy pokus kopiruje obe databaze znovu
            rec = {"attempt": len(attempts) + 1, "started": _ts(), "databases": {}}
            attempts.append(rec)
            _attempt(sources, names, work, deadline, chunk, rec)
            if rec["result"] == "stable":
                break
            if time.monotonic() + RETRY_PAUSE_S > deadline:
                raise SnapshotBusy("databáze se během kopírování opakovaně měnila (běží sken) – zkus snapshot za chvíli")
            time.sleep(RETRY_PAUSE_S)
        dbs = {n: dict(rec["databases"][n]) for n in names}
        for n in names:
            dbs[n].update(_check_copy(work / n), sha256=_sha([work / n]), size=(work / n).stat().st_size)
        bad = [f"{n}: {dbs[n]['integrity_check']}" for n in names if dbs[n]["integrity_check"] != "ok"]
        if bad:
            raise RuntimeError("kopie neprošla integrity_check – snapshot zahozen: " + "; ".join(bad))
        consistency = _consistency(dbs)
        if consistency["snapshot"]["status"] != "CONSISTENT":
            raise RuntimeError(f"snapshot není konzistentní – zahozen: {consistency['snapshot']['checks']}")
        digest = _sha([work / n for n in names])
        for n in names:  # hotova soukroma kopie -> snapshot (zdroj uz neni potreba, zadny zamek)
            shutil.copyfile(work / n, tmp / n)
        if [_sha([tmp / n]) for n in names] != [dbs[n]["sha256"] for n in names]:
            raise RuntimeError("kopie snapshotu se při přesunu poškodila – snapshot zahozen")
    except BaseException as e:
        if tmp:
            _rmtree_own(tmp)  # jen vlastni rozpracovana kopie tohoto snapshotu
        _record_failure(root, started, attempts, e)
        raise
    finally:
        if work:
            _rmtree_own(work)
    finished = datetime.now(timezone.utc)
    base = f"S{now:%Y%m%d-%H%M%S}-{digest[:6]}"
    meta = {"id": base, "created": now.isoformat(),
            "copied_until": finished.replace(microsecond=0).isoformat(),
            "files": list(names), "attempts": len(attempts),
            "sizes": {n: dbs[n]["size"] for n in names},
            # --- META_VERSION 2: auditni metadata ---
            "meta_version": META_VERSION, "status": "PUBLISHED",
            "started": started, "finished": finished.isoformat(timespec="milliseconds"),
            "restarts": {"repeated_attempts": len(attempts) - 1,
                         "backup_internal": sum(d["backup_restarts"] for r in attempts
                                                for d in r["databases"].values())},
            "databases": dbs, "consistency": consistency, "attempt_log": attempts}
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


def audit_summary(meta: dict) -> str:
    """Kratky auditni popis snapshotu; starsi snapshoty (META_VERSION 1) auditni metadata nemaji."""
    c = meta.get("consistency")
    if not c:
        return f"starší snapshot bez auditních metadat (pokusů {meta.get('attempts', '?')})"
    s, w = c["snapshot"], c["snapshot"]["common_window"]
    return (f"{s['status']}, společné okno {w['from'][11:23]}–{w['until'][11:23]} UTC, pokusů {meta['attempts']}, "
            f"integrity {'ok' if s['checks']['all_integrity_ok'] else 'CHYBA'}")


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
