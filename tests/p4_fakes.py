"""Mockovane odpovedi API-Football pro testy Profilu 4 (zadna realna kvota se nespotrebuje)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now = self.now + timedelta(**kw)


class FakeTransport:
    """Odpovedi podle endpointu; zaznamenava volani. `routes[endpoint]` = funkce(params) -> (status, headers, body)."""

    def __init__(self, routes: dict | None = None, limit: int = 100):
        self.routes = routes or {}
        self.calls: list[tuple[str, dict]] = []
        self.limit = limit
        self.used = 0

    def __call__(self, url, params, headers, timeout):
        assert headers.get("x-apisports-key"), "klic musi byt v hlavicce"
        endpoint = url.split("api-sports.io/", 1)[1]
        self.calls.append((endpoint, dict(params)))
        self.used += 1
        hdr = {"x-ratelimit-requests-limit": str(self.limit),
               "x-ratelimit-requests-remaining": str(max(0, self.limit - self.used)),
               "X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "9"}
        route = self.routes.get(endpoint)
        if route is None:
            return 200, hdr, {"errors": [], "results": 0, "response": [], "paging": {"current": 1, "total": 1}}
        out = route(params)
        if isinstance(out, Exception):
            raise out
        status, body = out
        return status, hdr, body


def ok(response, total_pages: int = 1, page: int = 1):
    return 200, {"errors": [], "results": len(response), "response": response,
                 "paging": {"current": page, "total": total_pages}}


def fixture_item(fid, home, away, kickoff, league_id=39, season=2026, status="NS", ft=(None, None),
                 home_id=None, away_id=None):
    return {
        "fixture": {"id": fid, "date": kickoff.isoformat(), "status": {"short": status}, "venue": {"name": "X"},
                    "referee": ""},
        "league": {"id": league_id, "name": "Premier League", "season": season, "round": "R1"},
        "teams": {"home": {"id": home_id or abs(hash(home)) % 10000, "name": home},
                  "away": {"id": away_id or abs(hash(away)) % 10000, "name": away}},
        "goals": {"home": ft[0], "away": ft[1]},
        "score": {"fulltime": {"home": ft[0], "away": ft[1]}},
    }


def odds_item(fid, kickoff, books: dict, update: datetime = NOW):
    """books: {"Pinnacle": {"Match Winner": {"Home": 2.0, ...}, "Goals Over/Under": {"Over 2.5": 1.9, ...}}}"""
    return {
        "fixture": {"id": fid, "date": kickoff.isoformat()},
        "update": update.isoformat(),
        "bookmakers": [
            {"id": i, "name": name,
             "bets": [{"id": j, "name": bet, "values": [{"value": v, "odd": str(o)} for v, o in vals.items()]}
                      for j, (bet, vals) in enumerate(bets.items())]}
            for i, (name, bets) in enumerate(books.items())
        ],
    }


def season_items(teams: list[str], start: datetime, rounds: int = 2, league_id=39, season=2026, seed: int = 1):
    """Kompletni dvoukolova sezona s nahodnymi vysledky (deterministicky)."""
    import numpy as np

    rng = np.random.default_rng(seed)
    strength = {t: 1.0 + 0.6 * (len(teams) - i) / len(teams) for i, t in enumerate(teams)}
    items, fid, day = [], 1000, 0
    for _ in range(rounds):
        for h in teams:
            for a in teams:
                if h == a:
                    continue
                lam = 1.25 * strength[h] / strength[a] ** 0.5
                mu = 1.0 * strength[a] / strength[h] ** 0.5
                hg, ag = int(rng.poisson(lam)), int(rng.poisson(mu))
                items.append(fixture_item(fid, h, a, start + timedelta(days=day // 4), league_id, season, "FT", (hg, ag),
                                          home_id=teams.index(h) + 1, away_id=teams.index(a) + 1))
                fid += 1
                day += 1
    return items
