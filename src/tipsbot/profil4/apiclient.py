"""Centralni klient API-Football (v3) pro Profil 4.

- autentizace hlavickou x-apisports-key; klic z promenne prostredi TIPSBOT_API_FOOTBALL_KEY, jinak z Nastaveni panelu
- cache podle endpointu a parametru (TTL podle endpointu), deduplikace v ramci behu
- timeouty, opakovani s exponencialnim backoffem (sit, 429, 5xx)
- respektovani denni kvoty a minutoveho limitu (BudgetManager), priority pozadavku
- evidence kazdeho pozadavku (api_log) bez klice; klic se nikdy neloguje
- strankovani (paging.total) pro endpointy, ktere ho maji (napr. odds)

API vraci chyby i s HTTP 200 v poli "errors" (napr. vycerpana kvota, spatny klic) – ty se rozpoznavaji zvlast.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .budget import BudgetManager
from .config import P4, Profil4Config
from .store import Store, iso, parse_dt, utcnow

log = logging.getLogger(__name__)

# transport(url, params, headers, timeout) -> (http_status, response_headers, json_body)
Transport = Callable[[str, dict, dict, float], tuple[int, dict, dict]]


def requests_transport(url: str, params: dict, headers: dict, timeout: float) -> tuple[int, dict, dict]:
    import requests

    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {}
    return r.status_code, dict(r.headers), body


class ApiError(RuntimeError):
    pass


@dataclass
class ApiResult:
    endpoint: str
    params: dict
    status: str  # ok | cached | empty | budget | error | no_key
    response: list = field(default_factory=list)
    fetched_at: datetime | None = None
    errors: list[str] = field(default_factory=list)
    results: int = 0
    paging: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "cached", "empty")

    @property
    def has_data(self) -> bool:
        return self.ok and bool(self.response)


def mask(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def cache_key(endpoint: str, params: dict) -> str:
    return endpoint + "?" + json.dumps({k: params[k] for k in sorted(params)}, separators=(",", ":"), default=str)


def _body_errors(body: dict) -> list[str]:
    err = body.get("errors") if isinstance(body, dict) else None
    if not err:
        return []
    if isinstance(err, dict):
        return [f"{k}: {v}" for k, v in err.items()]
    if isinstance(err, list):
        return [str(e) for e in err]
    return [str(err)]


class ApiFootballClient:
    def __init__(
        self,
        store: Store,
        cfg: Profil4Config = P4,
        key: str | Callable[[], str] | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.store = store
        self.cfg = cfg
        self._key = key
        self.transport = transport or requests_transport
        self.sleep = sleep
        self.clock = clock
        self.budget = BudgetManager(store, cfg.budget, clock)
        self._memo: dict[str, ApiResult] = {}

    # --- klic -----------------------------------------------------------
    def key(self) -> str:
        import os

        k = self._key() if callable(self._key) else self._key
        return (k or os.environ.get(self.cfg.api.key_env, "")).strip()

    # --- cache ----------------------------------------------------------
    def _cached(self, key: str) -> ApiResult | None:
        rows = self.store.query("SELECT * FROM api_cache WHERE key=?", (key,))
        if not rows:
            return None
        r = rows[0]
        if parse_dt(r["expires_at"]) < self.clock():
            return None
        return ApiResult(r["endpoint"], json.loads(r["params"]), "cached", json.loads(r["payload"]),
                         parse_dt(r["fetched_at"]), results=len(json.loads(r["payload"])))

    def _save_cache(self, key: str, endpoint: str, params: dict, response: list, ttl: int) -> None:
        now = self.clock()
        self.store.execute(
            "INSERT INTO api_cache(key, endpoint, params, payload, fetched_at, expires_at, empty) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at, "
            "expires_at=excluded.expires_at, empty=excluded.empty",
            (key, endpoint, json.dumps(params, default=str), json.dumps(response), iso(now),
             iso(now + timedelta(seconds=ttl)), 0 if response else 1),
        )

    def last_good(self, endpoint: str, params: dict) -> ApiResult | None:
        """Posledni ulozena odpoved i po expiraci (pro zobrazeni starych dat s casem ziskani)."""
        rows = self.store.query("SELECT * FROM api_cache WHERE key=?", (cache_key(endpoint, params),))
        if not rows:
            return None
        r = rows[0]
        return ApiResult(endpoint, params, "cached", json.loads(r["payload"]), parse_dt(r["fetched_at"]))

    def _ttl(self, ttl_key: str, empty: bool) -> int:
        ttl = self.cfg.cache.ttl.get(ttl_key, 3600)
        return min(ttl, self.cfg.cache.empty_ttl) if empty else ttl

    def _log(self, endpoint, params, priority, fixture_id, cached, http, ok, error) -> None:
        st = self.budget.status() if not cached else {"remaining": None, "limit": None}
        now = self.clock()
        self.store.execute(
            "INSERT INTO api_log(ts, day, endpoint, params, priority, fixture_id, cached, http, ok, error, "
            "remaining_day, limit_day) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (iso(now), now.date().isoformat(), endpoint, json.dumps(params, default=str), priority, fixture_id,
             1 if cached else 0, http, 1 if ok else 0, error, st["remaining"], st["limit"]),
        )

    # --- volani ---------------------------------------------------------
    def get(self, endpoint: str, params: dict | None = None, priority: str = "MEDIUM",
            fixture_id: int | None = None, ttl_key: str | None = None, force: bool = False) -> ApiResult:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = cache_key(endpoint, params)
        ttl_key = ttl_key or endpoint
        if not force:
            if key in self._memo:
                return self._memo[key]
            hit = self._cached(key)
            if hit:
                self._memo[key] = hit
                return hit
        secret = self.key()
        if not secret:
            return ApiResult(endpoint, params, "no_key",
                             errors=[f"Chybí klíč API-Football (proměnná {self.cfg.api.key_env} nebo Nastavení panelu)."])
        decision = self.budget.check(priority, fixture_id)
        if not decision.allowed:
            self._log(endpoint, params, priority, fixture_id, False, None, False, "budget: " + decision.reason)
            return ApiResult(endpoint, params, "budget", errors=[decision.reason])
        wait = self.budget.minute_wait()
        if wait > 0:
            self.sleep(wait)

        url = f"{self.cfg.api.base_url}/{endpoint}"
        headers = {"x-apisports-key": secret}
        attempt, last_err, http = 0, "", None
        while attempt <= self.cfg.api.max_retries:
            try:
                http, resp_headers, body = self.transport(url, params, headers, self.cfg.api.timeout)
            except Exception as e:  # noqa: BLE001 - sitova chyba => opakovat
                last_err = mask(f"síťová chyba: {e}", secret)
                http, resp_headers, body = None, {}, {}
            else:
                self.budget.record(resp_headers, counted=True)
                errors = [mask(x, secret) for x in _body_errors(body)]
                quota = [x for x in errors if "request" in x.lower() and ("limit" in x.lower() or "reached" in x.lower())]
                minute = [x for x in errors if "ratelimit" in x.lower() or "too many" in x.lower()]
                if http == 200 and not errors:
                    response = body.get("response", []) if isinstance(body, dict) else []
                    response = response if isinstance(response, list) else [response]
                    self._save_cache(key, endpoint, params, response, self._ttl(ttl_key, not response))
                    res = ApiResult(endpoint, params, "ok" if response else "empty", response, self.clock(),
                                    results=int(body.get("results", len(response)) or 0),
                                    paging=body.get("paging") or {})
                    if res.paging.get("total"):
                        self.store.set("paging:" + key, str(int(res.paging["total"])))
                    self._memo[key] = res
                    self._log(endpoint, params, priority, fixture_id, False, http, True, None)
                    return res
                if quota:
                    self.budget.exhaust()
                    self._log(endpoint, params, priority, fixture_id, False, http, False, "; ".join(quota))
                    return ApiResult(endpoint, params, "budget", errors=quota)
                if http in (401, 403) or any("token" in x.lower() or "key" in x.lower() for x in errors):
                    msg = "; ".join(errors) or f"HTTP {http}"
                    self._log(endpoint, params, priority, fixture_id, False, http, False, msg)
                    return ApiResult(endpoint, params, "error", errors=[f"Neplatný klíč nebo přístup: {msg}"])
                if http == 200 and errors and not minute:  # chybne parametry – opakovani nepomuze
                    self._log(endpoint, params, priority, fixture_id, False, http, False, "; ".join(errors))
                    return ApiResult(endpoint, params, "error", errors=errors)
                last_err = "; ".join(errors) or f"HTTP {http}"
                if http not in (429, 500, 502, 503, 504) and not minute:
                    self._log(endpoint, params, priority, fixture_id, False, http, False, last_err)
                    return ApiResult(endpoint, params, "error", errors=[last_err])
            attempt += 1
            if attempt > self.cfg.api.max_retries:
                break
            delay = min(self.cfg.api.backoff_max, self.cfg.api.backoff_base * (2 ** (attempt - 1)))
            if http == 429:
                delay = max(delay, self.budget.minute_wait() or delay)
            self.sleep(delay)
        self._log(endpoint, params, priority, fixture_id, False, http, False, last_err)
        return ApiResult(endpoint, params, "error", errors=[last_err or "neznámá chyba"])

    def get_all(self, endpoint: str, params: dict | None = None, priority: str = "MEDIUM",
                fixture_id: int | None = None, ttl_key: str | None = None, max_pages: int = 5,
                force: bool = False) -> ApiResult:
        """Vsechny stranky strankovaneho endpointu (kazda stranka = 1 pozadavek a vlastni cache)."""
        params = dict(params or {})
        first = self.get(endpoint, params, priority, fixture_id, ttl_key, force=force)
        if not first.ok:
            return first
        total = int(first.paging.get("total", 1) or 1) if first.status != "cached" else \
            self._cached_total(endpoint, params)
        out = list(first.response)
        status = first.status
        for page in range(2, min(total, max_pages) + 1):
            res = self.get(endpoint, {**params, "page": page}, priority, fixture_id, ttl_key, force=force)
            if not res.ok:
                status = "partial"
                break
            out.extend(res.response)
        merged = ApiResult(endpoint, params, status if out else "empty", out, first.fetched_at, results=len(out))
        if total > max_pages:
            merged.errors.append(f"staženo jen {max_pages} z {total} stránek")
        return merged

    def _cached_total(self, endpoint: str, params: dict) -> int:
        """U odpovedi z cache neni paging v tele; pocet stranek se uklada zvlast pri stazeni."""
        raw = self.store.get("paging:" + cache_key(endpoint, params), "1")
        return max(1, int(raw or 1))

    def reset_run(self, limit: int | None = None) -> None:
        self._memo.clear()
        self.budget.start_run(limit)
