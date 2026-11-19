"""API Budget Manager: denni kvota, minutovy limit, priority pozadavku a strop na zapas a sken.

Denni kvota API-Football se nuluje o pulnoci UTC. Skutecny limit a zbytek se berou z hlavicek odpovedi
(x-ratelimit-requests-limit / -remaining, minutove X-RateLimit-Limit / -Remaining); dokud hlavicky nejsou,
plati zalozni hodnoty z konfigurace.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import BudgetConfig
from .store import Store, iso, utcnow

PRIORITIES = ("HIGH", "MEDIUM", "LOW")


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str = ""


def _hdr(headers: dict, *names: str) -> int | None:
    low = {k.lower(): v for k, v in (headers or {}).items()}
    for n in names:
        v = low.get(n.lower())
        if v is not None:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                continue
    return None


class BudgetManager:
    def __init__(self, store: Store, cfg: BudgetConfig, clock=utcnow):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.run_used = 0  # pozadavky v aktualnim skenu (nuluje se start_run)
        self.run_limit = cfg.max_per_run

    # --- stav -----------------------------------------------------------
    def day(self, now: datetime | None = None) -> str:
        return (now or self.clock()).date().isoformat()

    def _row(self):
        rows = self.store.query("SELECT * FROM budget WHERE day=?", (self.day(),))
        return rows[0] if rows else None

    def limit(self) -> int:
        r = self._row()
        if r and r["limit_day"]:
            return int(r["limit_day"])
        last = self.store.query("SELECT limit_day FROM budget WHERE limit_day IS NOT NULL ORDER BY day DESC LIMIT 1")
        return int(last[0]["limit_day"]) if last else self.cfg.daily_limit_fallback

    def used(self) -> int:
        r = self._row()
        return int(r["used"]) if r else 0

    def remaining(self) -> int:
        r = self._row()
        own = self.limit() - self.used()
        if r and r["remaining_day"] is not None:
            return max(0, min(int(r["remaining_day"]), own))
        return max(0, own)

    def fixture_used(self, fixture_id: int) -> int:
        rows = self.store.query("SELECT COUNT(*) AS n FROM api_log WHERE day=? AND fixture_id=? AND cached=0 AND http IS NOT NULL",
                                (self.day(), fixture_id))
        return int(rows[0]["n"])

    # --- rozhodovani ----------------------------------------------------
    def start_run(self, limit: int | None = None) -> None:
        self.run_used = 0
        self.run_limit = self.cfg.max_per_run if limit is None else limit

    def check(self, priority: str = "MEDIUM", fixture_id: int | None = None, cost: int = 1) -> BudgetDecision:
        if priority not in PRIORITIES:
            raise ValueError(f"neznámá priorita {priority}")
        rem = self.remaining()
        if rem < cost:
            return BudgetDecision(False, "denní kvóta API je vyčerpaná")
        reserve = self.cfg.reserve_pct[priority] * self.limit()
        if rem - cost < reserve:
            return BudgetDecision(False, f"kvóta šetřena pro důležitější požadavky (zbývá {rem}, priorita {priority})")
        if self.run_used + cost > self.run_limit:
            return BudgetDecision(False, f"limit jednoho skenu ({self.run_limit} požadavků) dosažen")
        if fixture_id is not None and self.fixture_used(fixture_id) + cost > self.cfg.max_per_fixture_per_day:
            return BudgetDecision(False, f"limit na jeden zápas ({self.cfg.max_per_fixture_per_day}/den) dosažen")
        return BudgetDecision(True)

    def minute_wait(self) -> float:
        """Kolik sekund pockat, kdyz je minutovy limit vycerpany (0 = neni treba)."""
        r = self._row()
        if not r or r["minute_remaining"] is None or int(r["minute_remaining"]) > 0:
            return 0.0
        upd = r["updated"]
        if not upd:
            return 0.0
        from .store import parse_dt

        elapsed = (self.clock() - parse_dt(upd)).total_seconds()
        return max(0.0, min(self.cfg.max_wait_for_minute, 61.0 - elapsed))

    # --- evidence -------------------------------------------------------
    def record(self, headers: dict | None, counted: bool = True) -> None:
        now = self.clock()
        day = self.day(now)
        lim = _hdr(headers or {}, "x-ratelimit-requests-limit")
        rem = _hdr(headers or {}, "x-ratelimit-requests-remaining")
        mlim = _hdr(headers or {}, "X-RateLimit-Limit")
        mrem = _hdr(headers or {}, "X-RateLimit-Remaining")
        self.store.execute("INSERT OR IGNORE INTO budget(day, used) VALUES(?, 0)", (day,))
        self.store.execute(
            "UPDATE budget SET used = used + ?, limit_day = COALESCE(?, limit_day), "
            "remaining_day = COALESCE(?, remaining_day), minute_limit = COALESCE(?, minute_limit), "
            "minute_remaining = COALESCE(?, minute_remaining), updated=? WHERE day=?",
            (1 if counted else 0, lim, rem, mlim, mrem, iso(now), day),
        )
        if counted:
            self.run_used += 1

    def exhaust(self) -> None:
        """API hlasi vycerpanou denni kvotu – do konce dne uz nic nevolat."""
        day = self.day()
        self.store.execute("INSERT OR IGNORE INTO budget(day, used) VALUES(?, 0)", (day,))
        self.store.execute("UPDATE budget SET remaining_day = 0 WHERE day=?", (day,))

    def status(self) -> dict:
        r = self._row()
        return {
            "day": self.day(), "limit": self.limit(), "used": self.used(), "remaining": self.remaining(),
            "minute_limit": r["minute_limit"] if r else None, "minute_remaining": r["minute_remaining"] if r else None,
            "run_used": self.run_used, "run_limit": self.run_limit,
        }

