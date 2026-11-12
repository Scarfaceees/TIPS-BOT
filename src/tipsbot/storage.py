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


def make_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    url = database_url or SETTINGS.database_url
    if url.startswith("sqlite:///") and not url.endswith(":memory:"):
        from pathlib import Path

        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
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

    def set_analysis(self, league: str, raw: str) -> None:
        self._set(f"analysis:{league}", raw)

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

    def placed_for_tip(self, tip_id: int) -> float:
        q = select(func.coalesce(func.sum(Ticket.stake), 0.0)).where(Ticket.tip_id == tip_id)
        return float(self.s.scalar(q) or 0.0)

    def set_closing(self, ticket: Ticket, fair_closing_odds: float) -> None:
        """Ulozi ferovy zaviraci kurz a CLV = vsazeny kurz / zaviraci - 1."""
        ticket.closing_odds = fair_closing_odds
        ticket.clv_pct = (ticket.odds / fair_closing_odds - 1.0) * 100.0

    def settle_ticket(self, ticket_id: int, result: str, fair_closing_odds: float | None = None) -> Ticket:
        ticket = self.s.get(Ticket, ticket_id)
        if ticket is None:
            raise ValueError(f"Tiket {ticket_id} neexistuje.")
        if ticket.status != TICKET_OPEN:
            raise ValueError(f"Tiket {ticket_id} uz je vyhodnocen ({ticket.status}).")
        pnl = profit_for(result, ticket.odds, ticket.stake)
        ticket.status = result
        ticket.profit_loss = pnl
        ticket.settled_at = utcnow()
        if fair_closing_odds:
            self.set_closing(ticket, fair_closing_odds)
        self._move(pnl, "settlement", ticket.id)
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
