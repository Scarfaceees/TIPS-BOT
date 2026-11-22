"""Vlastni SQLite uloziste Profilu 4 (data/profil4.db).

Oddelene od hlavni databaze bota, takze Profil 4 nemeni schema ostatnich profilu a lze ho vypnout nebo smazat
bez vlivu na zbytek. Obsahuje: cache API, evidenci pozadavku a kvoty, kurzy s casovou znackou, historii predikci
a tipu, watchlist a posledni reporty.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_cache (
    key TEXT PRIMARY KEY, endpoint TEXT NOT NULL, params TEXT NOT NULL, payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL, expires_at TEXT NOT NULL, empty INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS api_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, day TEXT NOT NULL, endpoint TEXT NOT NULL,
    params TEXT NOT NULL, priority TEXT NOT NULL, fixture_id INTEGER, cached INTEGER NOT NULL,
    http INTEGER, ok INTEGER NOT NULL, error TEXT, remaining_day INTEGER, limit_day INTEGER
);
CREATE INDEX IF NOT EXISTS api_log_day ON api_log(day);
CREATE TABLE IF NOT EXISTS budget (
    day TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0, limit_day INTEGER, remaining_day INTEGER,
    minute_limit INTEGER, minute_remaining INTEGER, updated TEXT
);
CREATE TABLE IF NOT EXISTS odds_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER NOT NULL, match TEXT NOT NULL, kickoff TEXT,
    bookmaker TEXT NOT NULL, market TEXT NOT NULL, selection TEXT NOT NULL, odds REAL NOT NULL,
    fetched_at TEXT NOT NULL, source_time TEXT, source TEXT NOT NULL, rules TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS odds_fixture ON odds_quotes(fixture_id);
CREATE TABLE IF NOT EXISTS competitions (
    league_id INTEGER PRIMARY KEY, name TEXT, priority INTEGER, enabled INTEGER
);
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER NOT NULL, match TEXT NOT NULL, league_id INTEGER,
    league TEXT, kickoff TEXT, market TEXT NOT NULL, probs TEXT NOT NULL, predicted_at TEXT NOT NULL,
    model_version TEXT NOT NULL, config_hash TEXT NOT NULL, data_sources TEXT, result TEXT, score TEXT,
    UNIQUE(fixture_id, market, predicted_at)
);
CREATE TABLE IF NOT EXISTS tips (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER NOT NULL, match TEXT NOT NULL, league_id INTEGER,
    league TEXT, kickoff TEXT, market TEXT NOT NULL, selection TEXT NOT NULL, label TEXT,
    probs TEXT NOT NULL, fair_odds REAL, odds REAL NOT NULL, bookmaker TEXT NOT NULL, odds_source TEXT NOT NULL,
    odds_time TEXT, ev REAL, ev_conservative REAL, robustness REAL, decision TEXT NOT NULL, category TEXT,
    predicted_at TEXT NOT NULL, model_version TEXT NOT NULL, config_hash TEXT NOT NULL, data_sources TEXT,
    result TEXT, score TEXT, profit REAL, closing_odds REAL, closing_fair_odds REAL, clv REAL, settled_at TEXT
);
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER NOT NULL, match TEXT, kickoff TEXT,
    market TEXT, selection TEXT, min_odds REAL, wait_lineups INTEGER NOT NULL DEFAULT 0,
    last_odds REAL, last_ev REAL, last_decision TEXT, last_lineups INTEGER, last_check TEXT,
    created TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, note TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL, t TEXT NOT NULL,
    digest TEXT NOT NULL, data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_archive_kind_key ON archive(kind, key);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER NOT NULL, match TEXT, league_id INTEGER, league TEXT,
    kickoff TEXT, market TEXT NOT NULL, selection TEXT NOT NULL, market_type TEXT NOT NULL, model TEXT NOT NULL,
    p REAL NOT NULL, p_market REAL, market_covered INTEGER, odds REAL, bookmaker TEXT, odds_source TEXT, odds_time TEXT,
    odds_status TEXT, predicted_at TEXT NOT NULL, model_version TEXT NOT NULL, config_hash TEXT NOT NULL,
    outcome REAL, result TEXT, score TEXT, settled_at TEXT,
    UNIQUE(fixture_id, market, selection, model, model_version)
);
CREATE INDEX IF NOT EXISTS predictions_fixture ON predictions(fixture_id);
CREATE TABLE IF NOT EXISTS odds_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER NOT NULL, kickoff TEXT, slot_min INTEGER NOT NULL,
    observed_at TEXT NOT NULL, status TEXT NOT NULL, n_quotes INTEGER NOT NULL, latest_update TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS observations_fixture ON odds_observations(fixture_id);
CREATE TABLE IF NOT EXISTS rankings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, computed_at TEXT NOT NULL, version TEXT NOT NULL, fixture_id INTEGER NOT NULL,
    match TEXT, league TEXT, kickoff TEXT, market TEXT NOT NULL, selection TEXT NOT NULL, label TEXT, odds REAL,
    bookmaker TEXT, odds_time TEXT, ev REAL, ev_conservative REAL, robustness REAL, quality REAL, rank REAL,
    decision TEXT, integrity TEXT, valid INTEGER NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS rankings_key ON rankings(fixture_id, market, selection);
"""

# sloupce pridane pozdeji (ALTER TABLE u existujicich databazi)
LATER_COLUMNS = {
    "tips": {"odds_fetched_at": "TEXT", "odds_age_min": "REAL", "odds_status": "TEXT", "p_market": "REAL",
             "clv_status": "TEXT", "closing_time": "TEXT", "closing_kind": "TEXT", "closing_reason": "TEXT",
             "clv_last": "REAL", "quality": "REAL", "rank": "REAL", "intel": "TEXT"},
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Jak dlouho cekat na zamek databaze, nez zapis selze. Do profil4.db zapisuje panel i automat (vic vlaken
# i kontejneru); vychozich 5 s nestacilo a 22.11.2026 kvuli tomu padl ranni sken i stahovani kurzu pred
# vykopem ("database is locked"). Hlavni databaze bota ma stejne 60 s (storage.py).
BUSY_TIMEOUT_S = 60


class Store:
    """Tenka vrstva nad sqlite3. `path=':memory:'` pro testy."""

    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._batch = 0
        self.conn = sqlite3.connect(str(path), check_same_thread=False, timeout=BUSY_TIMEOUT_S)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Jednorazove opravy dat (kazda jen jednou, poznamenana v kv) a doplneni pozdeji pridanych sloupcu."""
        for table, cols in LATER_COLUMNS.items():
            have = {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
            for name, typ in cols.items():
                if name not in have:
                    self.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        if not self.get("fix_ah_away_sign_v1"):
            # Do 20.11.2026 se u asijskeho handicapu z API-Football otacelo znamenko u strany "Away" (API uvadi
            # u obou stran handicap domacich). Kurzy "Away" z API proto popisovaly jiny vysledek nez jejich trh.
            # Kurzy se smazou (pri dalsim cteni se z cache API rozparsuji spravne) a tipy na ne se zneplatni
            # (void = nepocitaji se do vysledku), aby historie nevykazovala zisk ze spatne sparovaneho kurzu.
            self.execute("DELETE FROM odds_quotes WHERE source='api' AND market LIKE 'AH%' AND selection='away'")
            self.execute("UPDATE tips SET result='void', profit=0, settled_at=? WHERE odds_source='api' AND "
                         "market LIKE 'AH%' AND selection='away'", (iso(utcnow()),))
            self.set("fix_ah_away_sign_v1", iso(utcnow()))

    # --- obecne ---------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            if not self._batch:
                self.conn.commit()
            return cur

    @contextmanager
    def batch(self):
        """Vic zapisu v jedne transakci (jeden commit, zamek databaze jen jednou a kratce). Pri chybe se vrati
        cela davka; vnorene davky se commitnou az na konci te vnejsi."""
        with self._lock:
            self._batch += 1
            try:
                yield self
            except BaseException:
                self._batch -= 1
                if not self._batch:
                    self.conn.rollback()
                raise
            self._batch -= 1
            if not self._batch:
                self.conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, tuple(params)))

    # --- trvaly archiv (archive.py) -----------------------------------------
    ARCHIVED = {"web_last_view": "p4_scan", "web_last_nhl_view": "p4_nhl"}  # prehledy, ktere se jinak prepisuji

    def archive(self, kind: str, key: str, text: str, when: datetime | None = None) -> bool:
        from ..archive import pack

        blob, digest = pack(text)
        last = self.query("SELECT digest FROM archive WHERE kind=? AND key=? ORDER BY id DESC LIMIT 1", (kind, key))
        if last and last[0]["digest"] == digest:
            return False
        self.execute("INSERT INTO archive(kind, key, t, digest, data) VALUES(?,?,?,?,?)",
                     (kind, key, iso(when or utcnow()), digest, blob))
        return True

    def archived(self, kind: str, key: str | None = None) -> list[tuple[str, str, str]]:
        from ..archive import unpack

        sql, par = "SELECT key, t, data FROM archive WHERE kind=?", [kind]
        if key is not None:
            sql, par = sql + " AND key=?", par + [key]
        return [(r["key"], r["t"], unpack(r["data"])) for r in self.query(sql + " ORDER BY id", par)]

    def archive_counts(self) -> dict[str, int]:
        return {r["kind"]: r["n"] for r in self.query("SELECT kind, COUNT(*) AS n FROM archive GROUP BY kind")}

    def get(self, key: str, default: str = "") -> str:
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set(self, key: str, value: str) -> None:
        self.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))
        if key in self.ARCHIVED and value and value != "null":
            self.archive(self.ARCHIVED[key], key, value)

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get(key, "")
        return json.loads(raw) if raw else default

    def set_json(self, key: str, value: Any) -> None:
        self.set(key, json.dumps(value, ensure_ascii=False, default=str))

    def close(self) -> None:
        self.conn.close()


def open_store(cfg=None) -> Store:
    from .config import P4

    cfg = cfg or P4
    return Store(cfg.db_path)
