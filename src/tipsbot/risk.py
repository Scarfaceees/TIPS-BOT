"""Rizeni kapitalu: frakcni Kelly, stropy, denni expozice, stop-loss."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import SETTINGS, RiskConfig


def kelly_fraction(prob: float, odds: float) -> float:
    """Plny Kelly: podil bankrollu. Zaporna hodnota = nesazet."""
    if odds <= 1.0:
        return 0.0
    return (prob * odds - 1.0) / (odds - 1.0)


@dataclass(frozen=True)
class StakeDecision:
    stake: float
    full_kelly: float
    reason: str  # "ok" nebo duvod, proc je vklad 0 / snizeny

    @property
    def allowed(self) -> bool:
        return self.stake > 0


def _round_down(amount: float, step: float) -> float:
    if step <= 0:
        return amount
    return math.floor(amount / step + 1e-9) * step


def size_stake(
    prob: float,
    odds: float,
    bankroll: float,
    open_exposure_today: float = 0.0,
    bets_on_match: int = 0,
    paused: bool = False,
    cfg: RiskConfig | None = None,
) -> StakeDecision:
    """Doporuceny vklad v mene bankrollu."""
    cfg = cfg or SETTINGS.risk
    fk = kelly_fraction(prob, odds)
    if paused:
        return StakeDecision(0.0, fk, "stop-loss aktivni")
    if bankroll <= 0:
        return StakeDecision(0.0, fk, "nulovy bankroll")
    if fk <= 0:
        return StakeDecision(0.0, fk, "zaporne EV")
    if bets_on_match >= cfg.max_bets_per_match:
        return StakeDecision(0.0, fk, "na utkani uz je sazka")

    reason = "ok"
    stake = bankroll * cfg.kelly_fraction * fk
    cap = bankroll * cfg.max_stake_pct
    if stake > cap:
        stake, reason = cap, "strop na sazku"
    room = bankroll * cfg.max_daily_exposure_pct - open_exposure_today
    if room <= 0:
        return StakeDecision(0.0, fk, "denni expozice vycerpana")
    if stake > room:
        stake, reason = room, "omezeno denni expozici"
    stake = _round_down(stake, cfg.stake_rounding)
    if stake < cfg.min_stake:
        return StakeDecision(0.0, fk, "vklad pod minimem")
    return StakeDecision(stake, fk, reason)


def drawdown(balance: float, peak: float) -> float:
    if peak <= 0:
        return 0.0
    return max(0.0, 1.0 - balance / peak)


def stop_loss_hit(balance: float, peak: float, cfg: RiskConfig | None = None) -> bool:
    cfg = cfg or SETTINGS.risk
    return drawdown(balance, peak) >= cfg.stop_loss_drawdown - 1e-12


def arbitrage_stakes(odds: list[float], total: float) -> list[float]:
    """Rozdeleni castky mezi vysledky tak, aby vyplata byla vzdy stejna."""
    inv = [1.0 / o for o in odds]
    s = sum(inv)
    return [total * x / s for x in inv]
