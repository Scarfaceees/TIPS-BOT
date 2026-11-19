"""Coverage Report: jaka data jsou pro konkretni zapas skutecne k dispozici a jak kvalitni je rozbor.

Kvalita dat (0–1) je vazeny prumer stavu jednotlivych zdroju. Chybejici data se nenahrazuji – jen snizuji
kvalitu, a tim robustnost a rozhodnuti (WATCH / NO BET). Pokryti souteze z API (`coverage`) se pouziva k tomu,
aby se nevolaly endpointy, ktere soutez vubec nema (setri kvotu); coverage=true ale nezarucuje data pro zapas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .provider import AVAILABLE, ERROR, NOT_COVERED, NOT_YET, PARTIAL, SKIPPED, UNAVAILABLE

LABELS = {
    "fixture": "Zápas", "model_data": "Výsledky pro model (sezóna)", "odds": "Kurzy",
    "injuries": "Absence", "lineups": "Sestavy", "h2h": "Vzájemné zápasy", "predictions": "Predikce API",
    "team_stats": "Statistiky týmů", "standings": "Tabulka",
}
WEIGHTS = {"fixture": 0.10, "model_data": 0.25, "odds": 0.30, "injuries": 0.10, "lineups": 0.10,
           "h2h": 0.03, "predictions": 0.02, "team_stats": 0.05, "standings": 0.05}
SCORE = {AVAILABLE: 1.0, PARTIAL: 0.5, NOT_YET: 0.6, NOT_COVERED: 0.3, UNAVAILABLE: 0.0, SKIPPED: 0.0, ERROR: 0.0}
CZ = {AVAILABLE: "DOSTUPNÉ", PARTIAL: "ČÁSTEČNÉ", NOT_YET: "ZATÍM NEDOSTUPNÉ", NOT_COVERED: "SOUTĚŽ NEPOKRÝVÁ",
      UNAVAILABLE: "NEDOSTUPNÉ", SKIPPED: "PŘESKOČENO (kvóta)", ERROR: "CHYBA"}


@dataclass
class CoverageItem:
    status: str
    fetched_at: datetime | None = None
    note: str = ""


@dataclass
class CoverageReport:
    fixture_id: int
    items: dict[str, CoverageItem] = field(default_factory=dict)

    def set(self, name: str, status: str, fetched_at: datetime | None = None, note: str = "") -> None:
        self.items[name] = CoverageItem(status, fetched_at, note)

    def status(self, name: str) -> str:
        return self.items[name].status if name in self.items else UNAVAILABLE

    def quality(self) -> float:
        tot = sum(WEIGHTS.values())
        return round(sum(w * SCORE.get(self.status(n), 0.0) for n, w in WEIGHTS.items()) / tot, 3)

    def missing(self) -> list[str]:
        return [LABELS.get(n, n) for n in WEIGHTS if self.status(n) not in (AVAILABLE,)]

    def oldest(self, now: datetime) -> float | None:
        ages = [(now - i.fetched_at).total_seconds() / 60 for i in self.items.values() if i.fetched_at]
        return max(ages) if ages else None

    def lines(self) -> list[str]:
        out = []
        for n in WEIGHTS:
            it = self.items.get(n)
            st = it.status if it else UNAVAILABLE
            extra = f" – {it.note}" if it and it.note else ""
            out.append(f"{LABELS[n]}: {CZ.get(st, st)}{extra}")
        return out


def covered(coverage: dict | None, name: str) -> bool | None:
    """True/False podle pokryti souteze v API, None = nevime."""
    if not coverage:
        return None
    if name == "lineups":
        return bool((coverage.get("fixtures") or {}).get("lineups", True))
    if name in ("injuries", "predictions", "odds", "standings"):
        v = coverage.get(name)
        return None if v is None else bool(v)
    return None
