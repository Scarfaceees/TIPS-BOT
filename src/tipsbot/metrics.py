"""Metriky vysledkovosti z posloupnosti sazek (od nejstarsi): yield, maximalni propad, serie proher."""

from __future__ import annotations


def track_record(profits: list[float], stakes: list[float]) -> dict:
    """`profits` a `stakes` ve stejnych jednotkach, chronologicky. Propad = nejvetsi pokles od maxima kumulativniho zisku."""
    peak = cum = max_dd = 0.0
    streak = longest = 0
    for p in profits:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        streak = streak + 1 if p < 0 else 0
        longest = max(longest, streak)
    turnover = sum(stakes)
    return {
        "n": len(profits),
        "profit": cum,
        "yield_pct": 100.0 * cum / turnover if turnover else None,
        "max_drawdown": max_dd,
        "longest_losing": longest,
        "current_losing": streak,
    }
