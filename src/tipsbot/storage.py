"""Databaze a evidence: tipy, rucne zapsane tikety, bankroll, CLV, stop-loss."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from . import risk
from .config import SETTINGS

TIP_OPEN, TIP_PLACED, TIP_CANCELLED, TIP_EXPIRED = "open", "placed", "cancelled", "expired"
TICKET_OPEN = "open"
COMBO = "AKU"  # trh tiketu s vic zapasy (akumulator); jednotlive zapasy jsou v ticket_legs
# vysledek tiketu -> nasobek vkladu, ktery se vraci jako zisk/ztrata (pri kurzu o)
RESULTS = ("won", "lost", "void", "half_won", "half_lost")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Tip(Base):
    __tablename__ = "tips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    sport: Mapped[str] = mapped_column(String)
    league: Mapped[str] = mapped_column(String)
    match_name: Mapped[str] = mapped_column(String, default="")
    kickoff: Mapped[datetime] = mapped_column(DateTime)
    market: Mapped[str] = mapped_column(String)
    selection: Mapped[str] = mapped_column(String)
    bookmaker: Mapped[str] = mapped_column(String)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    valid_until: Mapped[datetime] = mapped_column(DateTime)
    raw_odds: Mapped[float] = mapped_column(Float)  # nabizeny kurz ceske kancelare
    min_odds: Mapped[float] = mapped_column(Float)  # pod tento kurz nesazet
    devigged_sharp_odds: Mapped[float] = mapped_column(Float)  # ferovy kurz z konsensu
    p_model: Mapped[float] = mapped_column(Float)
    p_market: Mapped[float] = mapped_column(Float)
    model_probability: Mapped[float] = mapped_column(Float)  # finalni pravdepodobnost
    ev_percentage: Mapped[float] = mapped_column(Float)
    ev_threshold: Mapped[float] = mapped_column(Float)
    kelly_stake: Mapped[float] = mapped_column(Float)
    early: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    model_version: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default=TIP_OPEN, index=True)
    cancel_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tip_id: Mapped[int | None] = mapped_column(ForeignKey("tips.id"), nullable=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    league: Mapped[str] = mapped_column(String, default="")
    match_name: Mapped[str] = mapped_column(String, default="")
    match_day: Mapped[date] = mapped_column(Date, index=True)
    market: Mapped[str] = mapped_column(String)
    selection: Mapped[str] = mapped_column(String)
    bookmaker: Mapped[str] = mapped_column(String)
    odds: Mapped[float] = mapped_column(Float)  # skutecne vsazeny kurz
    stake: Mapped[float] = mapped_column(Float)  # skutecne vsazena castka
    placed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String, default=TICKET_OPEN, index=True)
    closing_odds: Mapped[float | None] = mapped_column(Float, nullable=True)  # ferovy zaviraci kurz
    clv_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TicketLeg(Base):
    """Jeden zapas tiketu AKU. Tiket je prohrany, jakmile prohraje jedna noha; jinak se vyhodnoti po posledni."""

    __tablename__ = "ticket_legs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    league: Mapped[str] = mapped_column(String, default="")
    match_name: Mapped[str] = mapped_column(String, default="")
    match_day: Mapped[date] = mapped_column(Date, index=True)
    market: Mapped[str] = mapped_column(String)
    selection: Mapped[str] = mapped_column(String)
    odds: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, default=TICKET_OPEN, index=True)
    score: Mapped[str] = mapped_column(String, default="")


class BankrollEntry(Base):
    __tablename__ = "bankroll_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    amount: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(String)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)


class State(Base):
    __tablename__ = "state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


class OddsApiCall(Base):
    """Jeden skutecny pozadavek na The Odds API (Phase 3.4 / B1). Jen pribyva, nikdy se neprepisuje.

    Naklad: `cost` + `cost_kind` = "header" (hlavicka x-requests-last, zname), "delta" (rozdil x-requests-used proti
    predchozimu evidovanemu pozadavku v mesici – odhad, mezi tim mohl byt pozadavek mimo evidenci) nebo "unknown".
    `cost_expected` = naklad podle dokumentace a parametru (trhy x regiony), jen pro srovnani."""

    __tablename__ = "odds_api_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    endpoint: Mapped[str] = mapped_column(String)
    purpose: Mapped[str] = mapped_column(String)
    sport: Mapped[str] = mapped_column(String)
    league: Mapped[str | None] = mapped_column(String, nullable=True)
    params: Mapped[str] = mapped_column(String, default="")
    fixture_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    http: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_kind: Mapped[str] = mapped_column(String, default="unknown")
    cost_expected: Mapped[int | None] = mapped_column(Integer, nullable=True)


class P5Candidate(Base):
    """Profil 5 – Market Value, stinovy rezim (profil5.py). Nova tabulka (Faze 3.2.1), vznika sama pri startu;
    stavajici tabulky se nemeni. Kandidat se zapise pri prvnim zjisteni a jeho vstupy se uz nemeni – pozdejsi skeny
    dopisuji jen zaviraci cenu (CLV) a po zapase vysledek. Odmitnute nabidky se zapisuji s duvody (status=rejected)."""

    __tablename__ = "p5_candidates"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created: Mapped[datetime] = mapped_column(DateTime, index=True)
    algo: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, index=True)  # candidate | rejected
    league: Mapped[str] = mapped_column(String, default="")
    sport: Mapped[str] = mapped_column(String, default="")
    match_id: Mapped[str] = mapped_column(String, index=True)
    match: Mapped[str] = mapped_column(String, default="")
    kickoff: Mapped[datetime] = mapped_column(DateTime, index=True)
    market: Mapped[str] = mapped_column(String)
    selection: Mapped[str] = mapped_column(String)
    book: Mapped[str] = mapped_column(String)
    odds: Mapped[float] = mapped_column(Float)
    p_ref: Mapped[float | None] = mapped_column(Float, nullable=True)
    fair_odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge: Mapped[float | None] = mapped_column(Float, nullable=True)  # EV proti trhu = kurz × p_ref − 1
    dev_median: Mapped[float | None] = mapped_column(Float, nullable=True)  # kurz / median ostatnich − 1
    n_ref: Mapped[int] = mapped_column(Integer, default=0)
    age_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    best_odds: Mapped[float | None] = mapped_column(Float, nullable=True)  # line shopping: nejlepsi kurz trhu
    best_book: Mapped[str] = mapped_column(String, default="")
    median_odds: Mapped[float | None] = mapped_column(Float, nullable=True)  # median vsech kancelari
    data: Mapped[str] = mapped_column(String, default="{}")  # JSON: snapshot, zahrnute/vynechane kancelare, integrita
    reasons: Mapped[str] = mapped_column(String, default="[]")
    closing_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    closing_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closing_kind: Mapped[str] = mapped_column(String, default="")
    clv: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[str] = mapped_column(String, default="")
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IntegrityRejection(Base):
    """Faze 3.2.2: nabidky, ktere spolecna Market Integrity tvrde odmitla (PODEZŘELÉ). Puvodni vyber zustava jako
    diagnosticky zaznam u profilu; tady je jednotna stopa pro vsechny profily. Jen pridavani."""

    __tablename__ = "integrity_rejections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    profile: Mapped[str] = mapped_column(String, index=True)  # profil, ktery nabidku vybral
    match_id: Mapped[str] = mapped_column(String, index=True)
    match: Mapped[str] = mapped_column(String, default="")
    kickoff: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    market: Mapped[str] = mapped_column(String)
    selection: Mapped[str] = mapped_column(String)
    book: Mapped[str] = mapped_column(String)
    odds: Mapped[float] = mapped_column(Float)  # puvodni kurz
    ref_fair: Mapped[float | None] = mapped_column(Float, nullable=True)  # referencni (ferovy) kurz bez kancelare
    ref_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    age_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    ref_n: Mapped[int] = mapped_column(Integer, default=0)  # nezavislych skupin kancelari
    reasons: Mapped[str] = mapped_column(String, default="[]")
    check: Mapped[str] = mapped_column(String, default="{}")  # cely vysledek kontroly (JSON)


class ArchiveEntry(Base):
    """Trvaly archiv rozboru a skenu (archive.py): jen pridavani, nic se nemaze."""

    __tablename__ = "archive"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    key: Mapped[str] = mapped_column(String, index=True)
    t: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    digest: Mapped[str] = mapped_column(String)
    data: Mapped[bytes] = mapped_column(LargeBinary)


def make_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    url = database_url or SETTINGS.database_url
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        from pathlib import Path

        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    # panel a automat pisou do stejne SQLite databaze: pri zapisu druheho radeji pockat nez spadnout
    engine = create_engine(url, connect_args={"timeout": 60} if url.startswith("sqlite") else {})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def profit_for(result: str, odds: float, stake: float) -> float:
    if result == "won":
        return stake * (odds - 1.0)
    if result == "lost":
        return -stake
    if result == "void":
        return 0.0
    if result == "half_won":
        return stake * (odds - 1.0) / 2.0
    if result == "half_lost":
        return -stake / 2.0
    raise ValueError(f"Neznamy vysledek: {result} (povolene: {RESULTS})")


class Ledger:
    """Operace nad evidenci v ramci jedne session."""

    def __init__(self, session: Session):
        self.s = session

    # --- stav -----------------------------------------------------------
    def _get(self, key: str, default: str) -> str:
        row = self.s.get(State, key)
        return row.value if row else default

    def _set(self, key: str, value: str) -> None:
        row = self.s.get(State, key)
        if row:
            row.value = value
        else:
            self.s.add(State(key=key, value=value))

    def api_key(self) -> str:
        """Klic kurzoveho API: z nastaveni v panelu, jinak z promenne prostredi."""
        return self._get("odds_api_key", "") or SETTINGS.odds_api.api_key

    def set_api_key(self, key: str) -> None:
        self._set("odds_api_key", key.strip())

    def odds_cache(self, league: str):
        """(cas stazeni, surova data, zbyvajici kredity) nebo None."""
        import json

        raw = self._get(f"odds_cache:{league}", "")
        if not raw:
            return None
        d = json.loads(raw)
        return datetime.fromisoformat(d["t"]), d["payload"], d.get("credits")

    def set_odds_cache(self, league: str, when: datetime, payload: list, credits: int | None) -> None:
        import json

        self._set(f"odds_cache:{league}", json.dumps({"t": when.isoformat(), "payload": payload, "credits": credits}))

    def goalie_overrides(self) -> dict[str, str]:
        """Rucne zadani brankari NHL {zkratka tymu: jmeno} (panel); plati, dokud se nezrusi."""
        import json

        raw = self._get("nhl_goalie_overrides", "")
        return json.loads(raw) if raw else {}

    def set_goalie_override(self, team: str, name: str | None) -> None:
        import json

        cur = self.goalie_overrides()
        if name:
            cur[team] = name
        else:
            cur.pop(team, None)
        self._set("nhl_goalie_overrides", json.dumps(cur, ensure_ascii=False))

    def set_odds_api_quota(self, remaining: int | None, used: int | None, when: datetime) -> None:
        """Stav kreditu kurzoveho API z hlavicek posledni odpovedi (x-requests-remaining / -used, mesicni pocitadla).
        Po dnech se drzi posledni hodnota 'used', aby slo ukazat spotrebu za dnesek."""
        import json

        if remaining is None and used is None:
            return
        q = self.odds_api_quota()
        days = q.get("days", {})
        if used is not None:
            days[when.date().isoformat()] = used
        days = dict(sorted(days.items())[-40:])
        self._set("odds_api_quota", json.dumps({"t": when.isoformat(), "remaining": remaining if remaining is not None else q.get("remaining"),
                                                "used": used if used is not None else q.get("used"), "days": days}))

    def log_odds_api_call(self, rec) -> OddsApiCall:
        """Zapise pozadavek kurzoveho API (oddsapi.CallRecord) do odds_api_calls v ramci teto session.
        Commit dela volajici spolu se stavem kreditu. Nic nestahuje."""
        import json

        from .oddsapi import expected_cost

        cost, kind = None, "unknown"
        if rec.last is not None:
            cost, kind = rec.last, "header"
        elif rec.used is not None:
            month = rec.ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            prev = self.s.scalars(select(OddsApiCall.used).where(OddsApiCall.used.is_not(None), OddsApiCall.ts >= month)
                                  .order_by(OddsApiCall.id.desc()).limit(1)).first()
            if prev is not None and rec.used >= prev:
                cost, kind = rec.used - prev, "delta"
        row = OddsApiCall(ts=rec.ts, endpoint=rec.endpoint, purpose=rec.purpose, sport=rec.sport, league=rec.league,
                          params=json.dumps(rec.params, sort_keys=True, default=str), fixture_id=rec.fixture_id,
                          event_id=rec.event_id, http=rec.http, ok=rec.ok, error=rec.error, remaining=rec.remaining,
                          used=rec.used, cost=cost, cost_kind=kind, cost_expected=expected_cost(rec))
        self.s.add(row)
        self.s.flush()
        return row

    def odds_api_calls(self, since: datetime | None = None) -> list[OddsApiCall]:
        q = select(OddsApiCall).order_by(OddsApiCall.id)
        if since is not None:
            q = q.where(OddsApiCall.ts >= since)
        return list(self.s.scalars(q))

    def odds_api_quota(self) -> dict:
        """{'t', 'remaining', 'used', 'days', 'today'}: today = kredity spotrebovane dnes (UTC), kdyz to jde spocitat."""
        import json

        raw = self._get("odds_api_quota", "")
        q = json.loads(raw) if raw else {}
        days = q.get("days", {})
        if q.get("t") and days:
            today = q["t"][:10]
            before = [v for d, v in sorted(days.items()) if d < today]
            if today in days and before and days[today] >= before[-1]:  # mesicni reset = nelze porovnat
                q["today"] = days[today] - before[-1]
        return q

    def api_football_key(self) -> str:
        return self._get("api_football_key", "")

    def set_api_football_key(self, key: str) -> None:
        self._set("api_football_key", key.strip())

    def football_data_key(self) -> str:
        return self._get("football_data_key", "")

    def set_football_data_key(self, key: str) -> None:
        self._set("football_data_key", key.strip())

    def save_team_report(self, team: str, report: dict, keep: int = 20) -> None:
        """Ulozi analyzu tymu; historie poslednich `keep` analyz (nejnovejsi prvni)."""
        import json

        history = self.team_reports(team)
        history.insert(0, report)
        self._set(f"team_report:{team}", json.dumps(history[:keep], ensure_ascii=False, default=str))

    def team_reports(self, team: str) -> list[dict]:
        import json

        raw = self._get(f"team_report:{team}", "")
        return json.loads(raw) if raw else []

    def highlightly_key(self) -> str:
        return self._get("highlightly_key", "")

    def set_highlightly_key(self, key: str) -> None:
        self._set("highlightly_key", key.strip())

    def next_kickoff(self, league: str) -> datetime | None:
        raw = self._get(f"next_kickoff:{league}", "")
        return datetime.fromisoformat(raw) if raw else None

    def set_next_kickoff(self, league: str, when: datetime) -> None:
        self._set(f"next_kickoff:{league}", when.isoformat())

    def set_analysis(self, league: str, raw: str, when: datetime | None = None) -> None:
        self._set(f"analysis:{league}", raw)
        if raw != "[]":
            self.archive("scan", league, raw, when)

    # --- trvaly archiv (archive.py) ---------------------------------------
    def archive(self, kind: str, key: str, text: str, when: datetime | None = None) -> bool:
        """Ulozi verzi rozboru/skenu, pokud se od posledni verze stejneho klice zmenila. Vraci, zda ulozil."""
        from . import archive

        blob, digest = archive.pack(text)
        last = self.s.scalars(select(ArchiveEntry.digest).where(ArchiveEntry.kind == kind, ArchiveEntry.key == key)
                              .order_by(ArchiveEntry.id.desc()).limit(1)).first()
        if last == digest:
            return False
        self.s.add(ArchiveEntry(kind=kind, key=key, t=when or utcnow(), digest=digest, data=blob))
        return True

    def archived(self, kind: str, key: str | None = None) -> list[tuple[str, datetime, str]]:
        """Vsechny verze (klic, cas, text) od nejstarsi."""
        from . import archive

        q = select(ArchiveEntry).where(ArchiveEntry.kind == kind)
        if key is not None:
            q = q.where(ArchiveEntry.key == key)
        return [(a.key, a.t, archive.unpack(a.data)) for a in self.s.scalars(q.order_by(ArchiveEntry.id))]

    def archive_counts(self) -> dict[str, int]:
        return dict(self.s.execute(select(ArchiveEntry.kind, func.count()).group_by(ArchiveEntry.kind)).all())

    def analysis(self, league: str) -> str:
        return self._get(f"analysis:{league}", "[]")

    def has_open_tips(self, league: str) -> bool:
        return any(t.league == league for t in self.open_tips())

    # --- bankroll -------------------------------------------------------
    def bankroll(self) -> float:
        total = self.s.scalar(select(func.coalesce(func.sum(BankrollEntry.amount), 0.0)))
        return float(total or 0.0)

    def peak(self) -> float:
        return float(self._get("peak", "0"))

    def drawdown(self) -> float:
        return risk.drawdown(self.bankroll(), self.peak())

    def paused(self) -> bool:
        return self._get("paused", "0") == "1"

    def test_mode(self) -> bool:
        return self._get("test_mode", "0") == "1"

    def set_test_mode(self, on: bool) -> None:
        self._set("test_mode", "1" if on else "0")

    def _move(self, amount: float, reason: str, ticket_id: int | None = None) -> None:
        self.s.add(BankrollEntry(amount=amount, reason=reason, ticket_id=ticket_id))
        self.s.flush()
        balance = self.bankroll()
        if reason in ("deposit", "withdrawal"):
            # Vklad/vyber neni vysledek sazeni: vrchol se posune o stejnou castku.
            self._set("peak", str(max(self.peak() + amount, balance, 0.0)))
        elif balance > self.peak():
            self._set("peak", str(balance))
        if risk.stop_loss_hit(balance, self.peak()):
            self._set("paused", "1")

    def deposit(self, amount: float) -> None:
        if amount <= 0:
            raise ValueError("Vklad musi byt kladny.")
        self._move(amount, "deposit")

    def withdraw(self, amount: float) -> None:
        if amount <= 0 or amount > self.bankroll():
            raise ValueError("Neplatna castka vyberu.")
        self._move(-amount, "withdrawal")

    def resume(self) -> None:
        """Obnovi vydavani tipu po re-kalibraci; propad se pocita od aktualniho stavu."""
        self._set("paused", "0")
        self._set("peak", str(self.bankroll()))

    # --- expozice -------------------------------------------------------
    def open_exposure(self, day: date) -> float:
        q = select(func.coalesce(func.sum(Ticket.stake), 0.0)).where(
            Ticket.match_day == day, Ticket.status == TICKET_OPEN
        )
        return float(self.s.scalar(q) or 0.0)

    def bets_on_match(self, match_id: str) -> int:
        q = select(func.count(func.distinct(Ticket.selection + "|" + Ticket.market))).where(
            Ticket.match_id == match_id, Ticket.status != "void"
        )
        return int(self.s.scalar(q) or 0)

    def open_tip_on_match(self, match_id: str) -> Tip | None:
        return self.s.scalar(
            select(Tip).where(Tip.match_id == match_id, Tip.status.in_((TIP_OPEN, TIP_PLACED)))
        )

    # --- tipy -----------------------------------------------------------
    def add_tip(self, tip: Tip) -> Tip:
        self.s.add(tip)
        self.s.flush()
        return tip

    def open_tips(self) -> list[Tip]:
        return list(self.s.scalars(select(Tip).where(Tip.status == TIP_OPEN).order_by(Tip.kickoff)))

    def cancel_tip(self, tip: Tip, reason: str, expired: bool = False) -> None:
        tip.status = TIP_EXPIRED if expired else TIP_CANCELLED
        tip.cancel_reason = reason

    # --- tikety ---------------------------------------------------------
    def add_ticket(
        self,
        *,
        bookmaker: str,
        odds: float,
        stake: float,
        tip_id: int | None = None,
        match_id: str | None = None,
        market: str | None = None,
        selection: str | None = None,
        match_day: date | None = None,
        league: str = "",
        match_name: str = "",
    ) -> Ticket:
        """Zapise rucne podanou sazku. S tip_id se udaje o zapase doplni z tipu."""
        if odds <= 1.0 or stake <= 0:
            raise ValueError("Kurz musi byt > 1 a vklad > 0.")
        tip = self.s.get(Tip, tip_id) if tip_id is not None else None
        if tip_id is not None and tip is None:
            raise ValueError(f"Tip {tip_id} neexistuje.")
        if tip:
            match_id = match_id or tip.match_id
            market = market or tip.market
            selection = selection or tip.selection
            match_day = match_day or tip.kickoff.date()
            league = league or tip.league
            match_name = match_name or tip.match_name
            if tip.status == TIP_OPEN:
                tip.status = TIP_PLACED
        if not (match_id and market and selection and match_day):
            raise ValueError("Bez tipu je potreba zadat match_id, market, selection a match_day.")
        ticket = Ticket(
            tip_id=tip_id,
            match_id=match_id,
            league=league,
            match_name=match_name,
            match_day=match_day,
            market=market,
            selection=selection,
            bookmaker=bookmaker,
            odds=odds,
            stake=stake,
        )
        self.s.add(ticket)
        self.s.flush()
        return ticket

    def add_combo_ticket(self, *, bookmaker: str, odds: float, stake: float, legs: list[dict]) -> Ticket:
        """Tiket AKU (vic zapasu, jeden vklad). `legs`: match_name, match_day, market, selection, odds (+ league).
        `odds` = celkovy kurz z tiketu kancelare (zaokrouhleny soucin kurzu nohou)."""
        if len(legs) < 2:
            raise ValueError("Tiket AKU potřebuje aspoň 2 zápasy.")
        if odds <= 1.0 or stake <= 0:
            raise ValueError("Kurz musi byt > 1 a vklad > 0.")
        for leg in legs:
            if not (leg.get("match_name") and leg.get("match_day") and leg.get("market") and leg.get("selection")):
                raise ValueError("U každého zápasu tiketu vyplň zápas, den, trh a výběr.")
            if not leg.get("odds") or leg["odds"] <= 1.0:
                raise ValueError(f"Kurz zápasu {leg['match_name']} musí být > 1.")
        names = [leg["match_name"] for leg in legs]
        leagues = {leg.get("league", "") for leg in legs}
        ticket = Ticket(match_id=f"aku:{min(leg['match_day'] for leg in legs)}:{' + '.join(names)}",
                        league=leagues.pop() if len(leagues) == 1 else "", match_name=" + ".join(names),
                        match_day=max(leg["match_day"] for leg in legs), market=COMBO, selection=f"{len(legs)} zápasy",
                        bookmaker=bookmaker, odds=odds, stake=stake)
        self.s.add(ticket)
        self.s.flush()
        for leg in legs:
            self.s.add(TicketLeg(ticket_id=ticket.id, league=leg.get("league", ""), match_name=leg["match_name"],
                                 match_day=leg["match_day"], market=leg["market"], selection=leg["selection"],
                                 odds=leg["odds"]))
        self.s.flush()
        return ticket

    def ticket_legs(self, ticket_id: int) -> list[TicketLeg]:
        return list(self.s.scalars(select(TicketLeg).where(TicketLeg.ticket_id == ticket_id).order_by(TicketLeg.id)))

    def open_legs(self) -> list[TicketLeg]:
        """Nevyhodnocene nohy tiketu AKU – i uz prohranych tiketu (23.11.2026: vysledek kazdeho zapasu se propise
        i do prohraneho tiketu; vysledek uzavreneho tiketu se tim nemeni, viz _close_combo)."""
        return list(self.s.scalars(select(TicketLeg).where(TicketLeg.status == TICKET_OPEN).order_by(TicketLeg.match_day)))

    def settle_leg(self, leg_id: int, result: str, score: str = "") -> Ticket | None:
        """Vysledek jedne nohy; kdyz tim je tiket AKU rozhodnuty, vyhodnoti se cely (vraci ho)."""
        if result not in RESULTS:
            raise ValueError(f"Neznámý výsledek {result}.")
        leg = self.s.get(TicketLeg, leg_id)
        if leg is None or leg.status != TICKET_OPEN:
            return None
        leg.status, leg.score = result, score
        self.s.flush()
        return self._close_combo(leg.ticket_id)

    def _close_combo(self, ticket_id: int) -> Ticket | None:
        ticket = self.s.get(Ticket, ticket_id)
        if ticket is None or ticket.status != TICKET_OPEN:
            return None
        legs = self.ticket_legs(ticket_id)
        if any(x.status == "lost" for x in legs):
            return self.settle_ticket(ticket_id, "lost")
        if any(x.status == TICKET_OPEN for x in legs):
            return None
        # jako kancelar: vracena noha ma kurz 1, polovicni vyhra/prohra (asijske linie) puli sazku na te noze
        factor = 1.0
        for x in legs:
            factor *= {"won": 1.0, "void": 1.0 / x.odds, "half_won": (1.0 + x.odds) / (2.0 * x.odds),
                       "half_lost": 0.5 / x.odds}[x.status]
        if all(x.status == "void" for x in legs):
            return self.settle_ticket(ticket_id, "void")
        pnl = ticket.stake * (ticket.odds * factor - 1.0)
        status = "won" if pnl > 1e-9 else "void" if abs(pnl) <= 1e-9 else "half_lost"  # zisk = kurz po prepoctu
        return self.settle_ticket(ticket_id, status, pnl=pnl)

    def placed_for_tip(self, tip_id: int) -> float:
        q = select(func.coalesce(func.sum(Ticket.stake), 0.0)).where(Ticket.tip_id == tip_id)
        return float(self.s.scalar(q) or 0.0)

    def set_closing(self, ticket: Ticket, fair_closing_odds: float) -> None:
        """Ulozi ferovy zaviraci kurz a CLV = vsazeny kurz / zaviraci - 1."""
        ticket.closing_odds = fair_closing_odds
        ticket.clv_pct = (ticket.odds / fair_closing_odds - 1.0) * 100.0

    def settle_ticket(self, ticket_id: int, result: str, fair_closing_odds: float | None = None,
                      pnl: float | None = None) -> Ticket:
        ticket = self.s.get(Ticket, ticket_id)
        if ticket is None:
            raise ValueError(f"Tiket {ticket_id} neexistuje.")
        if ticket.status != TICKET_OPEN:
            raise ValueError(f"Tiket {ticket_id} uz je vyhodnocen ({ticket.status}).")
        if pnl is None:
            pnl = profit_for(result, ticket.odds, ticket.stake)
        ticket.status = result
        ticket.profit_loss = pnl
        ticket.settled_at = utcnow()
        if fair_closing_odds:
            self.set_closing(ticket, fair_closing_odds)
        self._move(pnl, "settlement", ticket.id)
        return ticket

    def delete_open_ticket(self, ticket_id: int) -> Ticket:
        """Smaze omylem zapsany OTEVRENY tiket (i jeho nohy). Vyhodnoceny tiket se nemaze – je v bankrollu."""
        ticket = self.s.get(Ticket, ticket_id)
        if ticket is None:
            raise ValueError(f"Tiket {ticket_id} neexistuje.")
        if ticket.status != TICKET_OPEN:
            raise ValueError(f"Tiket {ticket_id} je už vyhodnocený ({ticket.status}), nejde smazat.")
        if self.s.scalar(select(func.count()).select_from(BankrollEntry).where(BankrollEntry.ticket_id == ticket_id)):
            raise ValueError(f"Tiket {ticket_id} má pohyb v bankrollu, nejde smazat.")
        for leg in self.ticket_legs(ticket_id):
            self.s.delete(leg)
        if ticket.tip_id is not None:
            tip = self.s.get(Tip, ticket.tip_id)
            others = self.s.scalar(select(func.count()).select_from(Ticket).where(Ticket.tip_id == ticket.tip_id,
                                                                                   Ticket.id != ticket_id))
            if tip is not None and tip.status == TIP_PLACED and not others:
                tip.status = TIP_OPEN
        self.s.delete(ticket)
        self.s.flush()
        return ticket

    # --- prehled --------------------------------------------------------
    def summary(self) -> dict[str, float]:
        settled = list(self.s.scalars(select(Ticket).where(Ticket.status != TICKET_OPEN)))
        graded = [t for t in settled if t.status != "void"]
        staked = sum(t.stake for t in graded)
        pnl = sum(t.profit_loss or 0.0 for t in settled)
        wins = sum(1 for t in graded if t.status in ("won", "half_won"))
        clv = [t.clv_pct for t in settled if t.clv_pct is not None]
        open_n = self.s.scalar(select(func.count()).select_from(Ticket).where(Ticket.status == TICKET_OPEN))
        return {
            "bankroll": self.bankroll(),
            "peak": self.peak(),
            "drawdown_pct": self.drawdown() * 100.0,
            "paused": float(self.paused()),
            "settled_bets": float(len(graded)),
            "open_bets": float(open_n or 0),
            "win_rate_pct": 100.0 * wins / len(graded) if graded else 0.0,
            "profit_loss": pnl,
            "yield_pct": 100.0 * pnl / staked if staked else 0.0,
            "avg_clv_pct": sum(clv) / len(clv) if clv else 0.0,
            "clv_tracked": float(len(clv)),
        }
