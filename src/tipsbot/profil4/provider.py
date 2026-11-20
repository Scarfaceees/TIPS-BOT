"""Central Data Provider: jedine misto, kudy Profil 4 bere data z API-Football.

Vraci normalizovane datove kontrakty (Fixture, Injury, Lineup ...) spolu se stavem dostupnosti a casem ziskani.
Chybejici data se nikdy nenahrazuji vymyslenymi hodnotami – vraci se prazdny vysledek se stavem.
Endpointy a parametry odpovidaji dokumentaci API-Football v3 (overeno 11/2026):
  fixtures (date | league+season [+from/to] | ids max 20), fixtures/headtohead (h2h=id-id, last),
  fixtures/lineups (fixture), injuries (fixture | ids), predictions (fixture), odds (fixture | league+season
  [+bet] | date; strankovane, kurzy 1–14 dni pred zapasem, historie 7 dni), odds/bookmakers, odds/bets,
  leagues (id, current -> coverage), standings (league+season), teams/statistics (league+season+team).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .apiclient import ApiFootballClient, ApiResult
from .config import Competition
from .odds import parse_api_odds
from .store import parse_dt

AVAILABLE, PARTIAL, NOT_YET, UNAVAILABLE, NOT_COVERED, SKIPPED, ERROR = (
    "AVAILABLE", "PARTIAL", "NOT_YET_AVAILABLE", "UNAVAILABLE", "NOT_COVERED", "SKIPPED_BUDGET", "ERROR")

FINISHED = ("FT", "AET", "PEN")
VOID_STATUSES = ("PST", "CANC", "ABD", "AWD", "WO")
NOT_STARTED = ("TBD", "NS")


@dataclass(frozen=True)
class Fixture:
    id: int
    league_id: int
    league: str
    season: int
    kickoff: datetime
    status: str
    home_id: int
    home: str
    away_id: int
    away: str
    ft_home: int | None = None  # skore po 90 minutach (vc. nastaveni, bez prodlouzeni)
    ft_away: int | None = None
    round: str = ""
    venue: str = ""
    referee: str = ""

    @property
    def name(self) -> str:
        return f"{self.home} - {self.away}"

    @property
    def finished(self) -> bool:
        return self.status in FINISHED and self.ft_home is not None and self.ft_away is not None


@dataclass(frozen=True)
class Injury:
    team_id: int
    team: str
    player: str
    kind: str  # Missing Fixture | Questionable ...
    reason: str


@dataclass(frozen=True)
class TeamLineup:
    team_id: int
    team: str
    formation: str
    start_xi: tuple[str, ...]
    coach: str = ""


@dataclass
class Fetched:
    """Data + stav + cas ziskani."""

    status: str
    data: object = None
    fetched_at: datetime | None = None
    note: str = ""
    errors: list[str] = field(default_factory=list)


def parse_fixture(item: dict) -> Fixture | None:
    fx, lg, tm = item.get("fixture", {}) or {}, item.get("league", {}) or {}, item.get("teams", {}) or {}
    if not fx.get("id") or not fx.get("date"):
        return None
    ft = (item.get("score", {}) or {}).get("fulltime", {}) or {}
    status = ((fx.get("status") or {}).get("short")) or ""
    return Fixture(
        id=int(fx["id"]), league_id=int(lg.get("id") or 0), league=lg.get("name") or "", season=int(lg.get("season") or 0),
        kickoff=parse_dt(fx["date"]), status=status,
        home_id=int((tm.get("home") or {}).get("id") or 0), home=(tm.get("home") or {}).get("name") or "?",
        away_id=int((tm.get("away") or {}).get("id") or 0), away=(tm.get("away") or {}).get("name") or "?",
        ft_home=ft.get("home"), ft_away=ft.get("away"), round=lg.get("round") or "",
        venue=((fx.get("venue") or {}).get("name")) or "", referee=fx.get("referee") or "",
    )


def _status(res: ApiResult, empty_status: str = UNAVAILABLE) -> str:
    if res.status == "budget":
        return SKIPPED
    if res.status in ("error", "no_key"):
        return ERROR
    return AVAILABLE if res.response else empty_status


class DataProvider:
    def __init__(self, client: ApiFootballClient):
        self.client = client
        self.cfg = client.cfg

    # --- souteze --------------------------------------------------------
    def competitions(self) -> list[Competition]:
        """Sledovane souteze: vychozi z konfigurace + upravy ulozene prikazem (zapnout/vypnout, priorita)."""
        over = {r["league_id"]: r for r in self.client.store.query("SELECT * FROM competitions")}
        out = []
        for c in self.cfg.competitions:
            r = over.pop(c.league_id, None)
            if r:
                c = Competition(c.league_id, c.name, int(r["priority"]), bool(r["enabled"]), c.bot_league)
            out.append(c)
        for lid, r in over.items():  # souteze pridane uzivatelem
            out.append(Competition(lid, r["name"] or str(lid), int(r["priority"]), bool(r["enabled"])))
        return sorted(out, key=lambda c: (c.priority, c.name))

    def set_competition(self, league_id: int, enabled: bool | None = None, priority: int | None = None,
                        name: str | None = None) -> Competition:
        cur = next((c for c in self.competitions() if c.league_id == league_id), None)
        if cur is None and name is None:
            name = str(league_id)
        base = cur or Competition(league_id, name or str(league_id), 3, True)
        new = Competition(league_id, name or base.name, priority if priority is not None else base.priority,
                          base.enabled if enabled is None else enabled, base.bot_league)
        self.client.store.execute(
            "INSERT INTO competitions(league_id, name, priority, enabled) VALUES(?,?,?,?) ON CONFLICT(league_id) "
            "DO UPDATE SET name=excluded.name, priority=excluded.priority, enabled=excluded.enabled",
            (new.league_id, new.name, new.priority, 1 if new.enabled else 0))
        return new

    def league_info(self, league_id: int) -> Fetched:
        """Aktualni sezona a pokryti souteze (coverage) – 1 pozadavek tydne na soutez."""
        res = self.client.get("leagues", {"id": league_id, "current": "true"}, priority="LOW", ttl_key="leagues")
        if not res.response:
            return Fetched(_status(res), None, res.fetched_at, errors=res.errors)
        item = res.response[0]
        seasons = [s for s in item.get("seasons", []) if s.get("current")] or item.get("seasons", [])
        if not seasons:
            return Fetched(UNAVAILABLE, None, res.fetched_at, "soutěž nemá sezónu")
        s = seasons[-1]
        return Fetched(AVAILABLE, {"season": int(s.get("year")), "coverage": s.get("coverage", {}) or {},
                                   "name": (item.get("league") or {}).get("name", "")}, res.fetched_at)

    # --- zapasy ---------------------------------------------------------
    def fixtures_on(self, day: date, priority: str = "HIGH") -> Fetched:
        """Vsechny zapasy dne (1 pozadavek pro vsechny souteze), jen sledovane souteze."""
        res = self.client.get("fixtures", {"date": day.isoformat(), "timezone": self.cfg.api.timezone},
                              priority=priority, ttl_key="fixtures:date")
        watched = {c.league_id for c in self.competitions() if c.enabled}
        fixtures = [f for f in (parse_fixture(i) for i in res.response) if f and f.league_id in watched]
        return Fetched(_status(res) if res.status != "ok" or fixtures else UNAVAILABLE, fixtures, res.fetched_at,
                       errors=res.errors)

    def season_results(self, league_id: int, season: int, past: bool = False, priority: str = "MEDIUM") -> Fetched:
        res = self.client.get("fixtures", {"league": league_id, "season": season}, priority=priority,
                              ttl_key="fixtures:season_past" if past else "fixtures:season")
        fixtures = [f for f in (parse_fixture(i) for i in res.response) if f]
        return Fetched(_status(res), fixtures, res.fetched_at, errors=res.errors)

    def fixtures_by_ids(self, ids: list[int], priority: str = "HIGH") -> Fetched:
        out: list[Fixture] = []
        status, errors, when = AVAILABLE, [], None
        for i in range(0, len(ids), 20):  # API: max 20 id v jednom pozadavku
            chunk = ids[i:i + 20]
            res = self.client.get("fixtures", {"ids": "-".join(str(x) for x in chunk)}, priority=priority,
                                  ttl_key="fixtures:ids")
            when = res.fetched_at or when
            if not res.ok:
                status, errors = PARTIAL if out else _status(res), errors + res.errors
                continue
            out.extend(f for f in (parse_fixture(x) for x in res.response) if f)
        return Fetched(status if out or status != AVAILABLE else UNAVAILABLE, out, when, errors=errors)

    # --- kurzy ----------------------------------------------------------
    def odds_fixture(self, fixture: Fixture, priority: str = "HIGH", force: bool = False) -> Fetched:
        """Kurzy zapasu; `force` obejde cache (zaviraci kurz tesne pred vykopem)."""
        res = self.client.get_all("odds", {"fixture": fixture.id}, priority=priority, fixture_id=fixture.id, ttl_key="odds",
                                  force=force)
        quotes = parse_api_odds(res.response, res.fetched_at or self.client.clock(), {fixture.id: fixture.name})
        st = _status(res) if res.status != "partial" else PARTIAL
        if st == UNAVAILABLE and fixture.kickoff and (fixture.kickoff - self.client.clock()).days >= 14:
            st = NOT_YET  # API poskytuje kurzy 1–14 dni pred zapasem
        return Fetched(st, quotes, res.fetched_at, errors=res.errors)

    def odds_league(self, league_id: int, season: int, bet: int | None = None, priority: str = "MEDIUM",
                    names: dict[int, str] | None = None) -> Fetched:
        params = {"league": league_id, "season": season, "bet": bet}
        res = self.client.get_all("odds", params, priority=priority, ttl_key="odds")
        quotes = parse_api_odds(res.response, res.fetched_at or self.client.clock(), names)
        return Fetched(_status(res) if res.status != "partial" else PARTIAL, quotes, res.fetched_at, errors=res.errors)

    def bookmakers(self) -> Fetched:
        res = self.client.get("odds/bookmakers", priority="LOW", ttl_key="odds/bookmakers")
        return Fetched(_status(res), [(b.get("id"), b.get("name")) for b in res.response], res.fetched_at, errors=res.errors)

    def bet_id(self, name: str, fallback: int) -> int:
        """Id predzapasove sazky podle nazvu (odds/bets). Id z odds/live nejsou kompatibilni s /odds."""
        res = self.client.get("odds/bets", priority="LOW", ttl_key="odds/bets")
        for b in res.response:
            if str(b.get("name", "")).strip().lower() == name.lower():
                return int(b["id"])
        return fallback

    # --- kontext --------------------------------------------------------
    def injuries(self, fixture_ids: list[int], priority: str = "MEDIUM") -> Fetched:
        out: dict[int, list[Injury]] = {fid: [] for fid in fixture_ids}
        status, errors, when = AVAILABLE, [], None
        for i in range(0, len(fixture_ids), 20):
            chunk = fixture_ids[i:i + 20]
            res = self.client.get("injuries", {"ids": "-".join(str(x) for x in chunk)}, priority=priority,
                                  ttl_key="injuries")
            when = res.fetched_at or when
            if not res.ok:
                status, errors = _status(res), errors + res.errors
                continue
            for it in res.response:
                fid = (it.get("fixture") or {}).get("id")
                p, t = it.get("player") or {}, it.get("team") or {}
                if fid in out and p.get("name"):
                    out[fid].append(Injury(int(t.get("id") or 0), t.get("name") or "", p["name"], p.get("type") or "",
                                           p.get("reason") or ""))
        return Fetched(status, out, when, errors=errors)

    def lineups(self, fixture: Fixture, priority: str = "HIGH") -> Fetched:
        res = self.client.get("fixtures/lineups", {"fixture": fixture.id}, priority=priority, fixture_id=fixture.id,
                              ttl_key="fixtures/lineups")
        teams = []
        for it in res.response:
            t = it.get("team") or {}
            xi = tuple((p.get("player") or {}).get("name", "") for p in it.get("startXI", []) or [])
            teams.append(TeamLineup(int(t.get("id") or 0), t.get("name") or "", it.get("formation") or "", xi,
                                    (it.get("coach") or {}).get("name", "")))
        st = _status(res, NOT_YET)
        if st == AVAILABLE and (len(teams) < 2 or any(len(t.start_xi) < 11 for t in teams)):
            st = PARTIAL
        return Fetched(st, teams, res.fetched_at, errors=res.errors)

    def predictions(self, fixture: Fixture, priority: str = "MEDIUM") -> Fetched:
        res = self.client.get("predictions", {"fixture": fixture.id}, priority=priority, fixture_id=fixture.id,
                              ttl_key="predictions")
        if not res.response:
            return Fetched(_status(res), None, res.fetched_at, errors=res.errors)
        p = res.response[0].get("predictions") or {}
        pct = p.get("percent") or {}

        def num(x):
            try:
                return float(str(x).rstrip("%")) / 100.0
            except (TypeError, ValueError):
                return None

        data = {"home": num(pct.get("home")), "draw": num(pct.get("draw")), "away": num(pct.get("away")),
                "advice": p.get("advice") or "", "under_over": p.get("under_over"),
                "comparison": res.response[0].get("comparison") or {}}
        return Fetched(AVAILABLE, data, res.fetched_at,
                       note="predikce API-Football nejsou založené na kurzech; jen informativní, do výpočtu nevstupují")

    def head_to_head(self, fixture: Fixture, last: int = 10, priority: str = "LOW") -> Fetched:
        res = self.client.get("fixtures/headtohead", {"h2h": f"{fixture.home_id}-{fixture.away_id}", "last": last},
                              priority=priority, fixture_id=fixture.id, ttl_key="fixtures/headtohead")
        fx = [f for f in (parse_fixture(i) for i in res.response) if f and f.finished]
        return Fetched(_status(res), fx, res.fetched_at, errors=res.errors)

    def team_statistics(self, league_id: int, season: int, team_id: int, priority: str = "LOW") -> Fetched:
        res = self.client.get("teams/statistics", {"league": league_id, "season": season, "team": team_id},
                              priority=priority, ttl_key="teams/statistics")
        data = res.response[0] if res.response else None
        return Fetched(_status(res), data, res.fetched_at, errors=res.errors)

    def standings(self, league_id: int, season: int, priority: str = "MEDIUM") -> Fetched:
        res = self.client.get("standings", {"league": league_id, "season": season}, priority=priority, ttl_key="standings")
        rows = []
        for it in res.response:
            for group in (it.get("league") or {}).get("standings", []) or []:
                for r in group:
                    rows.append({"rank": r.get("rank"), "team_id": (r.get("team") or {}).get("id"),
                                 "team": (r.get("team") or {}).get("name"), "points": r.get("points"),
                                 "played": (r.get("all") or {}).get("played"), "form": r.get("form") or "",
                                 "description": r.get("description") or "", "group": r.get("group") or ""})
        return Fetched(_status(res), rows, res.fetched_at, errors=res.errors)
