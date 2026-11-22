"""Kontrolni vrstva pred APPROVED BET: kurz, trh, pravdepodobnost a EV musi popisovat presne tentyz vysledek.

1. Nezavisly prepocet – vlastni, od Profilu 2 oddelena implementace vyhodnoceni sazky (cela, pulova i ctvrtinova
   linie: vyhra / pulvyhra / vraceni / pulprohra / prohra). Vysledek se porovna s hlavnim enginem (EV, ferovy kurz,
   pravdepodobnost); nesoulad = chyba vypoctu nebo zamena trhu => NO BET.
2. Sparovani kurzu – kurz kancelare musi mit u te same kancelare protistranu tehoz trhu a soucet implikovanych
   pravdepodobnosti musi davat smysl. Zamena stran/znamenka handicapu nebo over/under se projevi souctem pod 100 %
   (napr. 1.67 + 13.00 = 67.6 %) => NO BET.
3. Cena trhu – ferova pravdepodobnost trhu (bez marze) pro presne tento vyber: primo z kompletniho trhu, jinak
   z trzniho modelu B, ale jen kdyz B ma pro tento typ trhu aktualni kurzy. EV proti trhu ukazuje, o kolik je kurz
   nad trhem; extremni hodnota vyzaduje nezavisle potvrzeni (jina kancelar s podobnym kurzem).
"""

from __future__ import annotations

import math

import numpy as np

from .. import profile2
from . import mathcore
from . import odds as O

_EPS = 1e-9


# ---------------------------------------------------------------------------
# 1. Nezavisly prepocet
# ---------------------------------------------------------------------------

def _halves(line: float) -> list[float]:
    """Ctvrtinova linie = dve poloviny vkladu na sousednich linich (-0.75 -> -0.5 a -1.0)."""
    q = round(line * 4)
    if abs(line * 4 - q) > _EPS:
        raise ValueError(f"linie {line} není násobkem 0,25")
    return [line - 0.25, line + 0.25] if q % 2 else [line]


def _result(margin: float) -> str:
    return "win" if margin > _EPS else "push" if margin >= -_EPS else "lose"


def outcome(market: str, selection: str, home: int, away: int) -> list[tuple[float, str]]:
    """Vysledek sazky pri skore home:away jako [(podil vkladu, 'win'|'push'|'lose')]."""
    res = "1" if home > away else "X" if home == away else "2"
    if market == "1X2":
        if selection not in ("1", "X", "2"):
            raise ValueError(selection)
        return [(1.0, "win" if selection == res else "lose")]
    if market == "DC":
        if selection not in ("1X", "12", "X2"):
            raise ValueError(selection)
        return [(1.0, "win" if res in selection else "lose")]
    if market == "DNB":
        if selection not in ("home", "away"):
            raise ValueError(selection)
        return [(1.0, "push" if res == "X" else "win" if (res == "1") == (selection == "home") else "lose")]
    if market == "BTTS":
        if selection not in ("yes", "no"):
            raise ValueError(selection)
        both = home > 0 and away > 0
        return [(1.0, "win" if both == (selection == "yes") else "lose")]
    kind, _, raw = market.partition("_")
    line = float(raw)
    parts = _halves(line)
    if kind in ("OU", "TTH", "TTA"):
        if selection not in ("over", "under"):
            raise ValueError(selection)
        goals = {"OU": home + away, "TTH": home, "TTA": away}[kind]
        margins = [(goals - l) if selection == "over" else (l - goals) for l in parts]
    elif kind == "AH":  # linie z pohledu domacich: domaci +l, hoste -l
        if selection not in ("home", "away"):
            raise ValueError(selection)
        margins = [(home - away + l) if selection == "home" else (away - home - l) for l in parts]
    else:
        raise ValueError(f"trh {market} nelze nezávisle ověřit")
    return [(1.0 / len(parts), _result(m)) for m in margins]


def recompute(market: str, selection: str, mat: np.ndarray, odds: float) -> dict:
    """Nezavisle EV, ferovy kurz a pravdepodobnosti z matice skore."""
    a = b = 0.0
    for h in range(mat.shape[0]):
        for g in range(mat.shape[1]):
            p = float(mat[h, g])
            if p == 0.0:
                continue
            for share, r in outcome(market, selection, h, g):
                if r == "win":
                    a += p * share
                elif r == "push":
                    b += p * share
    fair = (1.0 - b) / a if a > 0 else math.inf
    return {"a": a, "b": b, "ev": a * odds + b - 1.0, "fair": fair, "p_eff": a / (1.0 - b) if b < 1 else 1.0}


def _close(x: float, y: float, tol: float = 1e-6) -> bool:
    if math.isinf(x) or math.isinf(y):
        return x == y
    return abs(x - y) <= tol * max(1.0, abs(x), abs(y))


def validate(c, mat: np.ndarray) -> tuple[list[str], list[str]]:
    """(tvrde chyby, mekke vyhrady) kandidata vuci matici modelu C a jeho kurzu."""
    hard, soft = [], []
    q = c.quote
    if q.fixture_id != c.fixture_id:
        hard.append(f"kurz patří k jinému zápasu (id {q.fixture_id} ≠ {c.fixture_id})")
    if (q.market, q.selection) != (c.market, c.selection):
        hard.append(f"kurz je pro {q.market}:{q.selection}, tip pro {c.market}:{c.selection}")
    if not (isinstance(q.odds, (int, float)) and math.isfinite(q.odds) and q.odds > 1.0):
        hard.append(f"neplatný kurz {q.odds!r}")
        return hard, soft
    for name, p in c.p.items():
        if p is not None and not (isinstance(p, float) and math.isfinite(p) and 0.0 < p < 1.0):
            hard.append(f"pravděpodobnost modelu {name} = {p!r} není v intervalu (0, 1)")
    total = float(mat.sum())
    if not (np.all(mat >= 0) and 0.99 <= total <= 1.0 + 1e-6):
        hard.append(f"matice skóre není rozdělení pravděpodobnosti (součet {total:.4f})")
    try:
        ind = recompute(c.market, c.selection, mat, q.odds)
    except ValueError as e:
        soft.append(f"výpočet nelze nezávisle ověřit ({e})")
        return hard, soft
    for what, mine, theirs in (("EV", ind["ev"], c.ev_c), ("férový kurz", ind["fair"], c.fair_odds_c),
                               ("pravděpodobnost výhry", ind["a"], c.p_win_c), ("pravděpodobnost (C)", ind["p_eff"], c.p.get("C"))):
        if theirs is None or not _close(mine, theirs):
            hard.append(f"{what} nesouhlasí s nezávislým výpočtem ({theirs!r} vs {mine:.6f})")
    return hard, soft


def binary_check(p: float, odds: float, ev: float, fair: float, min_odds: float, ev_min: float) -> list[str]:
    """Nezavisla kontrola binarniho trhu bez vraceni (NHL): EV = p·kurz − 1, ferovy kurz = 1/p,
    minimalni kurz = (1 + prah EV)/p. Pocita se znovu z p a kurzu, ne z hodnot kandidata."""
    out = []
    if not (0.0 < p < 1.0) or not (odds > 1.0):
        return [f"neplatná pravděpodobnost {p!r} nebo kurz {odds!r}"]
    for what, mine, theirs in (("EV", p * odds - 1.0, ev), ("férový kurz", 1.0 / p, fair),
                               ("minimální kurz", (1.0 + ev_min) / p, min_odds)):
        if theirs is None or not _close(mine, theirs):
            out.append(f"{what} nesouhlasí s nezávislým výpočtem ({theirs!r} vs {mine:.6f})")
    return out


# ---------------------------------------------------------------------------
# 2. Sparovani kurzu s trhem kancelare
# ---------------------------------------------------------------------------

def pairing(q: O.OddsQuote, quotes: list[O.OddsQuote], cfg) -> tuple[list[str], list[str]]:
    """Ma kurz u te same kancelare protistranu a davaji kurzy trhu dohromady rozumnou marzi?

    Proc soucet pod 100 % znamena chybu: kancelar, ktera vypisuje vsechny vzajemne vylucne vysledky trhu, musi mit
    sum(1/kurz) >= 1, jinak by sama proti sobe nabizela jistou vyhru (vsadit 1/kurz na kazdou stranu vrati 1
    pri vkladu sum(1/kurz) < 1). Plati i pro asijske linie: u cele linie se pri vraceni vraci obe strany,
    u ctvrtinove linie dava pul-vyhra/pul-prohra zisk 0.5·(1 − sum(1/kurz)). U dvojite sance pokryva kazdy vysledek
    dva vybery, proto se porovnava s 2. Provize burzy kurz jen snizuje (soucet roste), nikdy nevytvori soucet pod
    100 %. Burza muze mit soucet tesne kolem 100 %, proto tolerance (cfg.min_book_overround = −1 %).
    Strany se paruji jen ze STEJNEHO casu zdroje: kurzy z ruznych aktualizaci spolu netvori jeden trh.
    Overeno na 2 443 kompletnich trzich z API-Football (20.11.2026): nejnizsi marze +1.3 %, zadna pod 100 %.
    """
    sels = profile2.selections(q.market) if q.market != "EXPR" else None
    if not sels:
        return [], ([] if q.source != "api" else [f"trh {q.market} nelze spárovat s protistranou"])
    same = [x for x in quotes if x.bookmaker == q.bookmaker and x.market == q.market and x.fixture_id == q.fixture_id]
    per = {x.selection: x.odds for x in same if x.stamp == q.stamp}
    per[q.selection] = q.odds
    missing = [s for s in sels if s not in per]
    if missing:
        if q.source != "api":
            return [], []  # rucne opsany jediny kurz je vedome zadani uzivatele
        other_time = [s for s in missing if any(x.selection == s for x in same)]
        if other_time:
            return [], [f"protistrana {q.bookmaker} ({', '.join(other_time)}) je z jiné aktualizace kurzů – "
                        f"spárování trhu nelze ověřit"]
        return [], [f"{q.bookmaker} nenabízí protistranu ({', '.join(missing)}) – spárování trhu nelze ověřit"]
    expected = 2.0 if q.market == "DC" else 1.0
    over = sum(1.0 / per[s] for s in sels) - expected
    if over < cfg.min_book_overround:
        return [f"kurzy {q.bookmaker} na trhu {q.market} dávají dohromady {(over + expected) * 100:.1f} % "
                f"(pod 100 %) – nejsou jeden trh (záměna strany, znaménka nebo linie)"], []
    if over > cfg.max_book_overround:
        return [], [f"marže {q.bookmaker} na trhu {q.market} je {over:.0%} – kurz je nejspíš chybný nebo neúplný trh"]
    return [], []


# ---------------------------------------------------------------------------
# 3. Cena trhu a nezavisle potvrzeni
# ---------------------------------------------------------------------------

def market_covered(market: str, inputs: list[str] | set[str]) -> bool:
    """Ma trzni model B pro tento typ trhu aktualni kurzy (jinak jsou jeho cisla jen dopocet)?"""
    inputs = set(inputs)
    goal = any(m == "BTTS" or m.startswith(("OU_", "TTH_", "TTA_")) for m in inputs)
    if market in ("1X2", "DC", "DNB"):
        return "1X2" in inputs
    if market.startswith("AH_") or market == "EXPR":
        return "1X2" in inputs and goal
    if market == "BTTS" or market.startswith(("OU_", "TTH_", "TTA_")):
        return goal
    return False


def market_price(b, market: str, selection: str, odds: float) -> tuple[float | None, float | None, str]:
    """(ferova pravdepodobnost trhu, EV kurzu proti trhu, zdroj). None, kdyz trh pro tento vyber neni znam."""
    if b is None or not b.available:
        return None, None, "trh chybí"
    table = b.extra.get("table", {})
    key = f"{market}:{selection}"
    if key in table.get("consensus", {}):
        p = table["consensus"][key]
        src = ", ".join(table.get("sources", {}).get(market, []))
        return p, p * odds - 1.0, f"kurzy bez marže ({src})"
    if not market_covered(market, b.extra.get("inputs", [])):
        return None, None, "pro tento trh nejsou aktuální kurzy"
    try:
        e = mathcore.evaluate(market, selection, b.matrix, odds)
    except ValueError:
        return None, None, "trh nelze dopočítat"
    return e.p_effective, e.ev, "tržní model B"


def confirmations(q: O.OddsQuote, quotes: list[O.OddsQuote], now, odds_cfg, tolerance: float) -> list[str]:
    """Jine kancelare s aktualnim kurzem na presne tentyz vyber, nejvys o `tolerance` nizsim."""
    out = []
    for x in quotes:
        if (x.bookmaker != q.bookmaker and x.fixture_id == q.fixture_id and x.market == q.market
                and x.selection == q.selection and x.odds >= q.odds * (1 - tolerance)
                and x.status(now, odds_cfg) == O.CURRENT):
            out.append(f"{x.bookmaker} {x.odds:.2f}")
    return sorted(set(out))
