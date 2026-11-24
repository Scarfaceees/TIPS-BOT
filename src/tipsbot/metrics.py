"""Metriky vysledkovosti z posloupnosti sazek (od nejstarsi): yield, maximalni propad, serie proher.

JEDNOTNA DEFINICE (Faze 3.2.2) – plati pro panel, Excel, CLI i agregace v databazi (Sledovani, Profil 4, Profil 5):
- sazka s vkladem (staked) = vysledek vyhra, pul vyhra, prohra nebo pul prohra; kazda = 1 jednotka obratu
  (i pul vyhra / pul prohra – vsazena byla cela jednotka);
- zisk: vyhra = kurz − 1, pul vyhra = (kurz − 1) / 2, pul prohra = −0,5, prohra = −1;
- vraceni vkladu (PUSH, DNB remiza, cela asijska linie, NHL vraceni) a NEPLATNA sazka (INVALID, zruseny zapas,
  chybne sparovany kurz) nejsou v obratu ani v poctu sazek – zisk 0, vykazuji se zvlast;
- YIELD = ROI na 1 j. = zisk / pocet sazek s vkladem. U skutecnych tiketu ROI = zisk v Kc / vsazene Kc
  (stejny vzorec, jen v korunach; jednotky a Kc se nikdy nescitaji);
- win rate = (vyhry + pul vyhry) / sazky s vkladem;
- Profil 4 navic ukazuje „zhodnocení kapitálu“ = zisk / 100 j. (ne ROI).
"""

from __future__ import annotations

STAKED = ("won", "half_won", "lost", "half_lost")
WINS = ("won", "half_won")


def is_staked(result: str | None) -> bool:
    """Jde vysledek do obratu (jmenovatele yieldu)? Push, vraceni, neplatne a cekajici ne."""
    return result in STAKED


def unit_yield(rows: list[tuple[str | None, float | None]]) -> dict:
    """(vysledek, zisk v j.) -> sazky s vkladem, vyhry, prohry, zisk, yield, win rate. Jedina definice yieldu."""
    st = [(r, p or 0.0) for r, p in rows if is_staked(r)]
    profit = sum(p for _, p in st)
    wins = sum(1 for r, _ in st if r in WINS)
    return {"staked": len(st), "wins": wins, "losses": len(st) - wins, "profit": profit,
            "yield": profit / len(st) if st else None, "win_rate": wins / len(st) if st else None}


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
