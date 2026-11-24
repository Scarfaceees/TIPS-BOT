"""Profil 2: rozbor jednoho zapasu z rucne zadanych kurzu (postup z rozboru Wales - Portugalsko, 11/2026).

Jak Profil 2 pocita:
1. Tri scenare ocekavanych golu (lambda domacich a hostu):
   A = model (model bota, rucni odhad nebo prumer obou, volitelne uprava z kontextu),
   B = kompromis (napul model, napul trh) - "model se muze mylit",
   C = trh (lambdy dopocitane z kurzu kancelari po odstraneni marze). Jen pro kontrolu: proti ferove
       linii tychz kurzu je EV vzdy zhruba minus marze, kladne vyjde jen pri lepsim kurzu jine kancelare.
2. Nejistota: lambdy nejsou presne zname, proto se kazda nasobi nahodnym faktorem z gama rozdeleni
   (tvar k, prumer 1). Vysledkem je smes Poissonovych rozdeleni s tezsimi chvosty nez cisty Poisson.
3. Kazda nabidka se vyhodnoti presne ze skorove matice: sance na vyhru, vraceni (cela cisla, asijske linie),
   prohru a EV ve vsech scenarich. Robustni je jen sazka s kladnym EV v A i B.
4. Doporuceni: nejrozumnejsi sazka (nizke riziko prohry, i nizsi kurz), sazka 1,5+ s vysokou sanci,
   nejvyssi hodnota, a seznam sazek, kterym se vyhnout.
5. Kontrola korelace vybranych sazek (spolecna vyhra/prohra) - korelovane sazky nepatri na jeden tiket.
6. Sure bety mezi kancelarmi a hranice kurzu, od kterych by sure bet vznikl.
7. Simulace 1000 zapasu (nejcastejsi vysledky, rozlozeni golu, vysledek sazek po 100 Kc).

Profil 2 neni overen zpetnym testem. Doporucene sazky se proto ukladaji do Sledovani (zdroj "profil2"),
aby se dalo po case zmerit, jestli skutecne vydelavaji.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np
from scipy import optimize, stats

from . import devig

MAX_GOALS = 10
STATE_KEY = "profil2_analyses"
PROFILE_KEY = "profil2"  # klic profilu ve Sledovani (paper.PROFILES)
KEEP = 30


@dataclass(frozen=True)
class Profile2Config:
    gamma_shape: float = 16.0  # nejistota lambdy: variacni koeficient 1/sqrt(k) = 25 %
    quad_points: int = 24  # kvantily gama rozdeleni na jednu osu (presny vypocet smesi)
    sims: int = 1000  # pocet simulovanych zapasu pro zobrazeni
    min_odds: float = 1.15
    max_odds: float = 4.20
    robust_ev: float = 0.0  # EV v horsim scenari aspon tolik => robustni sazka
    value_ev: float = 0.02  # "nejvyssi hodnota": EV v horsim scenari aspon tolik
    safe_max_loss: float = 0.25  # nejrozumnejsi sazka: sance na prohru nejvys tolik (horsi scenar)
    high_prob_min_odds: float = 1.50
    high_prob_min_win: float = 0.60
    avoid_ev: float = -0.05  # EV pod touto hranici v obou scenarich => vyhnout se
    safety_margin: float = 0.0  # minimalni kurz = kurz s nulovym EV v horsim scenari x (1 + rezerva)
    blend_weight: float = 0.5  # scenar B: podil modelu ve smesi model/trh (zbytek trh)
    corr_warn: float = 0.30  # korelace vyhry dvou sazek nad touto hranici => nekombinovat
    suspicious_ev: float = 0.25  # EV nad touto hranici => nejspis chyba v zadani kurzu


P2 = Profile2Config()


# ---------------------------------------------------------------------------
# Trhy a vyhodnoceni sazky na skorove matici
# ---------------------------------------------------------------------------

# trh -> vybery v poradi zadani kurzu
STANDARD = {
    "1X2": ("1", "X", "2"),
    "DC": ("1X", "12", "X2"),
    "DNB": ("home", "away"),
    "BTTS": ("yes", "no"),
}
_LINE = re.compile(r"^(OU|TTH|TTA|AH)_([+-]?\d+(?:\.\d+)?)$")
_RANGE = re.compile(r"^G(\d+)-(\d+)$")
_EXACT_TOTAL = re.compile(r"^G(\d+)$")
_SCORE = re.compile(r"^S(\d+):(\d+)$")
_ATOM_LINE = re.compile(r"^(H_|A_)?([OU])(\d+(?:\.\d+)?)$")

LABELS = {
    "1X2:1": "výhra domácích", "1X2:X": "remíza", "1X2:2": "výhra hostů",
    "DC:1X": "domácí neprohrají (1X)", "DC:12": "nebude remíza (12)", "DC:X2": "hosté neprohrají (X2)",
    "DNB:home": "domácí, remíza vrací", "DNB:away": "hosté, remíza vrací",
    "BTTS:yes": "oba dají gól", "BTTS:no": "oba dají gól – ne",
}


def selections(market: str) -> tuple[str, ...] | None:
    if market in STANDARD:
        return STANDARD[market]
    m = _LINE.match(market)
    if m:
        return ("home", "away") if m.group(1) == "AH" else ("over", "under")
    return None


def _grid() -> tuple[np.ndarray, np.ndarray]:
    g = np.arange(MAX_GOALS + 1)
    return np.meshgrid(g, g, indexing="ij")  # H[i,j] = goly domacich, A[i,j] = goly hostu


H, A = _grid()


def _line_returns(margin: np.ndarray, line: float, odds: float) -> np.ndarray:
    """Navrat sazky 'margin > line' (margin = rozdil nebo pocet golu) vcetne celych a ctvrtinovych linii."""
    def single(ln: float) -> np.ndarray:
        d = margin - ln
        return np.where(np.abs(d) < 1e-9, 1.0, np.where(d > 0, odds, 0.0))

    q = line * 4
    if abs(q - round(q)) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9:  # ctvrtinova linie: pul na kazdou
        return 0.5 * single(line - 0.25) + 0.5 * single(line + 0.25)
    return single(line)


def _atom(atom: str) -> np.ndarray:
    """Logicka podminka na skorove matici (pro kombinovane sazky typu '2&U3.5')."""
    a = atom.strip().upper()
    if a in ("1", "X", "2"):
        return {"1": H > A, "X": H == A, "2": H < A}[a]
    if a in ("1X", "X2", "12"):
        return {"1X": H >= A, "X2": H <= A, "12": H != A}[a]
    if a in ("BTTS", "GG"):
        return (H > 0) & (A > 0)
    if a in ("BTTS_NO", "NG"):
        return ~((H > 0) & (A > 0))
    m = _ATOM_LINE.match(a)
    if m:
        side, ou, line = m.group(1), m.group(2), float(m.group(3))
        if abs(line - round(line)) < 1e-9:
            raise ValueError(f"'{atom}': v kombinaci použij půlovou linii (např. U3.5), celé číslo vrací vklad")
        goals = H if side == "H_" else A if side == "A_" else H + A
        return goals > line if ou == "O" else goals < line
    m = _RANGE.match(a)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (H + A >= lo) & (H + A <= hi)
    m = _EXACT_TOTAL.match(a)
    if m:
        return H + A == int(m.group(1))
    m = _SCORE.match(a)
    if m:
        return (H == int(m.group(1))) & (A == int(m.group(2)))
    raise ValueError(f"neznámá podmínka '{atom}'")


def returns(market: str, selection: str, odds: float) -> np.ndarray:
    """Matice navratu (na 1 Kc vkladu) pro kazde skore: 0 = prohra, 1 = vraceni, odds = vyhra."""
    if market == "EXPR":
        mask = np.ones_like(H, dtype=bool)
        for part in selection.split("&"):
            mask &= _atom(part)
        return np.where(mask, odds, 0.0)
    if market in ("1X2", "DC", "BTTS"):
        key = {"yes": "BTTS", "no": "BTTS_NO"}.get(selection, selection)
        return np.where(_atom(key), odds, 0.0)
    if market == "DNB":
        diff = (H - A) if selection == "home" else (A - H)
        return _line_returns(diff, 0.0, odds)
    m = _LINE.match(market)
    if not m:
        raise ValueError(f"neznámý trh '{market}'")
    kind, line = m.group(1), float(m.group(2))
    if kind == "AH":  # linie z pohledu domacich: AH_-1.0 home = domaci -1
        margin = (H - A) if selection == "home" else (A - H)
        return _line_returns(margin, -line if selection == "home" else line, odds)
    goals = H + A if kind == "OU" else H if kind == "TTH" else A
    if selection == "over":
        return _line_returns(goals, line, odds)
    return _line_returns(-goals, -line, odds)  # under L = (-goly) > -L


def _num(x: float) -> str:
    """3.5 -> '3,5', 4 -> '4,0', 2.25 -> '2,25' (ceske desetinne cislo)."""
    txt = f"{x:.2f}".rstrip("0")
    txt = txt + "0" if txt.endswith(".") else txt
    return txt.replace(".", ",")


def label(market: str, selection: str) -> str:
    if market == "EXPR":
        return _expr_label(selection)
    if f"{market}:{selection}" in LABELS:
        return LABELS[f"{market}:{selection}"]
    m = _LINE.match(market)
    kind, line = m.group(1), float(m.group(2))
    if kind == "AH":
        val = (line if selection == "home" else -line) + 0.0  # -0.0 -> 0.0 (jinak "±-0,0")
        return f"{'domácí' if selection == 'home' else 'hosté'} handicap {'+' if val > 0 else '' if val else '±'}{_num(val)}"
    who = {"OU": "", "TTH": "domácí ", "TTA": "hosté "}[kind]
    word = "víc než" if selection == "over" else "méně než"
    return f"{who}{word} {_num(line)} gólu"


def _expr_label(expr: str) -> str:
    names = {"1": "výhra domácích", "X": "remíza", "2": "výhra hostů", "1X": "1X", "X2": "X2", "12": "12",
             "BTTS": "oba dají gól", "BTTS_NO": "nedají oba"}
    out = []
    for part in expr.upper().split("&"):
        m = _ATOM_LINE.match(part)
        if m:
            who = {"H_": "domácí ", "A_": "hosté ", None: ""}[m.group(1)]
            out.append(f"{who}{'víc než' if m.group(2) == 'O' else 'méně než'} {_num(float(m.group(3)))} gólu")
        elif _EXACT_TOTAL.match(part):
            n = int(part[1:])
            out.append(f"přesně {n} {'gól' if n == 1 else 'góly' if 2 <= n <= 4 else 'gólů'}")
        elif _RANGE.match(part):
            lo, hi = _RANGE.match(part).groups()
            out.append(f"{lo}–{hi} gólů")
        elif _SCORE.match(part):
            out.append(f"skóre {part[1:]}")
        else:
            out.append(names.get(part, part))
    return " a ".join(out)


# ---------------------------------------------------------------------------
# Skorova matice se smesi nejistoty
# ---------------------------------------------------------------------------

def _gamma_factors(k: float, n: int) -> np.ndarray:
    """Kvantily gama rozdeleni s prumerem 1 (deterministicka kvadratura)."""
    q = (np.arange(n) + 0.5) / n
    f = stats.gamma.ppf(q, a=k, scale=1.0 / k)
    return f / f.mean()


def _pois(lam: np.ndarray) -> np.ndarray:
    g = np.arange(MAX_GOALS + 1)
    p = stats.poisson.pmf(g[None, :], lam[:, None])
    p[:, -1] += 1.0 - p.sum(axis=1)  # zbytek do posledni bunky
    return p


def score_matrix(lam_h: float, lam_a: float, k: float = P2.gamma_shape, n: int = P2.quad_points) -> np.ndarray:
    """Pravdepodobnost kazdeho skore; nejistota obou lambd je nezavisla. k <= 0 => cisty Poisson."""
    if k <= 0:
        return np.outer(_pois(np.array([lam_h]))[0], _pois(np.array([lam_a]))[0])
    f = _gamma_factors(k, n)
    ph = _pois(lam_h * f).mean(axis=0)
    pa = _pois(lam_a * f).mean(axis=0)
    return np.outer(ph, pa)


# ---------------------------------------------------------------------------
# Zadani kurzu
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OddsEntry:
    book: str
    market: str  # 1X2, DC, DNB, BTTS, OU_x, TTH_x, TTA_x, AH_x, EXPR
    odds: tuple[float, ...]
    selection: str = ""  # jediny vyber (jeden kurz): u EXPR podminka (napr. "2&U3.5"), jinak vyber trhu

    @property
    def pairs(self) -> list[tuple[str, float]]:
        sels = (self.selection,) if self.selection else selections(self.market)
        return list(zip(sels, self.odds))


def _norm_market(raw: str) -> str:
    m = raw.strip().upper().replace(" ", "").replace(",", ".")
    m = {"OBA": "BTTS", "GG": "BTTS", "NEPROHRA": "DC", "DVOJITASANCE": "DC"}.get(m, m)
    lm = _LINE.match(m)
    return _canon(lm.group(1), float(lm.group(2))) if lm else m


def _single_line(market: str) -> tuple[str, str] | None:
    """Jedna strana linie zapsana zkratkou: U4.0 / O2.5 / H_U1.5 / A_O0.5 -> (OU_4.0, under) ..."""
    m = _ATOM_LINE.match(market)
    if not m:
        return None
    kind = {"H_": "TTH", "A_": "TTA", None: "OU"}[m.group(1)]
    return _canon(kind, float(m.group(3))), ("over" if m.group(2) == "O" else "under")


def _canon(kind: str, line: float) -> str:
    """Jednotny zapis linie: OU_4 -> OU_4.0, AH_-1 -> AH_-1.0."""
    txt = f"{line:g}"
    return f"{kind}_{txt if '.' in txt else txt + '.0'}"


def parse_odds(text: str, default_book: str = "tipsport") -> tuple[list[OddsEntry], list[str]]:
    """Radky:  [kancelar |] [Domaci - Hoste |] trh | kurzy

        tipsport | OU_3.5 | 2.34 1.62          (vic, min)
        tipsport | 1X2 | 6.90 4.80 1.45
        fortuna | BTTS | 1.65 2.11             (ano, ne)
        tipsport | TTA_1.5 | 1.38 2.78         (goly hostu: vic, min)
        tipsport | AH_-1.0 | 2.10 1.70         (domaci -1, hoste +1)
        tipsport | U4.0 | 1.33                 (jedna strana linie; H_U1.5 = domaci min nez 1.5)
        tipsport | AH_1.0:away | 1.65          (jedna strana trhu: TRH:vyber | kurz)
        tipsport | 2&U3.5 | 2.41               (kombinace, jeden kurz)
        tipsport | G1-3 | 1.73                 (pocet golu 1 az 3)
    """
    entries, errors = [], []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 4:  # format ceskych kurzu bota (se zapasem) - zapas se ignoruje
            parts = [parts[0], parts[2], parts[3]]
        if len(parts) == 2:
            parts = [default_book, *parts]
        if len(parts) != 3:
            errors.append(f"řádek {n}: čekám 'kancelář | trh | kurzy'")
            continue
        book, market, raw = parts[0].lower(), _norm_market(parts[1]), parts[2]
        try:
            odds = tuple(float(x.replace(",", ".")) for x in raw.split())
        except ValueError:
            errors.append(f"řádek {n}: kurzy '{raw}' nejsou čísla")
            continue
        if not odds or any(o <= 1.0 for o in odds):
            errors.append(f"řádek {n}: kurz musí být větší než 1.00")
            continue
        sels = selections(market)
        try:
            if sels is not None:
                if len(odds) != len(sels):
                    raise ValueError(f"trh {market} čeká {len(sels)} kurzy ({', '.join(sels)})")
                entries.append(OddsEntry(book, market, odds))
            elif len(odds) == 1 and ":" in market and not _SCORE.search(market.split("&")[-1]) and selections(_norm_market(market.split(":")[0])):
                mk, sel = _norm_market(market.split(":")[0]), market.split(":", 1)[1].lower()
                if sel not in selections(mk):
                    raise ValueError(f"trh {mk}: výběr musí být {', '.join(selections(mk))}")
                entries.append(OddsEntry(book, mk, odds, selection=sel))
            elif len(odds) == 1 and _single_line(market):
                mk, sel = _single_line(market)
                entries.append(OddsEntry(book, mk, odds, selection=sel))
            elif len(odds) == 1:
                returns("EXPR", market, odds[0])  # kontrola, ze podmince rozumime
                entries.append(OddsEntry(book, "EXPR", odds, selection=market))
            else:
                raise ValueError(f"neznámý trh '{parts[1]}'")
        except ValueError as e:
            errors.append(f"řádek {n}: {e}")
    return entries, errors


# ---------------------------------------------------------------------------
# Scenar trhu: lambdy z kurzu bez marze
# ---------------------------------------------------------------------------

def _fair_targets(entries: list[OddsEntry]) -> list[tuple[str, str, float]]:
    """Ferove pravdepodobnosti z kompletnich trhu (prumer pres kancelare). Cela cisla a AH se nepouziji."""
    acc: dict[tuple[str, str], list[float]] = {}
    for e in entries:
        if e.selection or e.market in ("EXPR", "DNB", "DC") or e.market.startswith("AH_"):
            continue
        m = _LINE.match(e.market)
        if m and abs(float(m.group(2)) - round(float(m.group(2)))) < 1e-9:
            continue  # cela linie (vraceni) - ferova pravdepodobnost neni prima
        if m and abs(float(m.group(2)) * 2 - round(float(m.group(2)) * 2)) > 1e-9:
            continue  # ctvrtinova linie
        try:
            fair = devig.fair_line(list(e.odds)).probs
        except devig.DevigError:
            continue
        for sel, p in zip(selections(e.market), fair):
            acc.setdefault((e.market, sel), []).append(p)
    return [(mk, s, float(np.mean(ps))) for (mk, s), ps in acc.items()]


def market_lambdas(entries: list[OddsEntry], k: float = P2.gamma_shape) -> tuple[float, float] | None:
    """Lambdy, pri kterych model nejlepe sedi na ferove pravdepodobnosti trhu (stejna nejistota jako scenar A)."""
    targets = _fair_targets(entries)
    if len({mk for mk, _, _ in targets}) < 2 or not any(mk.startswith(("OU_", "TTH_", "TTA_")) or mk == "1X2" for mk, _, _ in targets):
        return None
    masks = [(returns(mk, s, 1.0) > 0.5, p) for mk, s, p in targets]

    def loss(x: np.ndarray) -> float:
        lh, la = np.exp(x)
        mat = score_matrix(lh, la, k, n=12)
        return float(sum((mat[mask].sum() - p) ** 2 for mask, p in masks))

    best = min((optimize.minimize(loss, np.log(x0), method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-9})
                for x0 in ((1.3, 1.1), (0.8, 2.0), (2.0, 0.8))), key=lambda r: r.fun)
    lh, la = np.exp(best.x)
    return float(lh), float(la)


# ---------------------------------------------------------------------------
# Rozbor
# ---------------------------------------------------------------------------

@dataclass
class Scenario:
    name: str
    lam_h: float
    lam_a: float
    source: str
    decides: bool = True  # pocita se do robustnosti (A, B); trh (C) je jen kontrola


@dataclass
class OfferEval:
    key: str  # trh:vyber
    market: str
    selection: str
    label: str
    book: str
    odds: float
    p_win: list[float]  # po scenarich [A, B]
    p_push: list[float]
    p_loss: list[float]
    ev: list[float]
    breakeven: list[float]  # kurz, pri kterem je EV = 0
    min_odds: float  # kurz potrebny v horsim scenari + rezerva
    worst_ev: float
    worst_win: float
    worst_loss: float
    worst_push: float
    sim_profit: float  # vysledek 1000 simulovanych zapasu po 100 Kc (v %)
    verdict: str  # robust | borderline | none | avoid | suspicious
    gradeable: bool  # umi ho vyhodnotit Sledovani (OU, TTH/TTA, BTTS, 1X2, DC, DNB)


@dataclass
class Pick:
    role: str  # safe | high_prob | value
    title: str
    offer: OfferEval
    stake: float = 0.0
    stake_note: str = ""


@dataclass
class Analysis:
    match: str
    league: str
    day: str
    created: str
    scenarios: list[Scenario]
    k: float
    offers: list[OfferEval]
    picks: list[Pick]
    avoid: list[OfferEval]
    correlation: list[dict]
    surebets: list[dict]
    sim: dict
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(d: dict) -> "Analysis":
        d = dict(d)
        d["scenarios"] = [Scenario(**s) for s in d["scenarios"]]
        d["offers"] = [OfferEval(**o) for o in d["offers"]]
        d["avoid"] = [OfferEval(**o) for o in d["avoid"]]
        d["picks"] = [Pick(**{**p, "offer": OfferEval(**p["offer"])}) for p in d["picks"]]
        return Analysis(**d)


def _eval_offer(market: str, sel: str, book: str, odds: float, mats: list[np.ndarray], sim_scores: np.ndarray,
                cfg: Profile2Config, decides: list[bool] | None = None) -> OfferEval:
    r = returns(market, sel, odds)
    p_win, p_push, p_loss, ev, be = [], [], [], [], []
    for mat in mats:
        win = float(mat[r > 1.0 + 1e-9].sum())
        push = float(mat[np.abs(r - 1.0) < 1e-9].sum())
        half = float(mat[(r > 1e-9) & (r < 1.0 - 1e-9)].sum())  # polovicni prohra
        e = float((mat * r).sum() - 1.0)
        p_win.append(win)
        p_push.append(push)
        p_loss.append(1.0 - win - push - half * 0.5)  # polovicni prohra se pocita napul
        ev.append(e)
        be.append(odds / (e + 1.0) if e > -1.0 else float("inf"))
    idx = [i for i, d in enumerate(decides or [True] * len(mats)) if d] or list(range(len(mats)))
    worst = min(idx, key=lambda i: ev[i])
    profit = float(r[sim_scores[:, 0], sim_scores[:, 1]].mean() - 1.0)
    w_ev = ev[worst]
    best_ev = max(ev[i] for i in idx)
    if w_ev > cfg.suspicious_ev:
        verdict = "suspicious"
    elif w_ev >= cfg.robust_ev:
        verdict = "robust"
    elif best_ev > 0:
        verdict = "borderline"
    elif best_ev <= cfg.avoid_ev:
        verdict = "avoid"
    else:
        verdict = "none"
    gradeable = market in ("1X2", "DC", "DNB", "BTTS") or bool(re.match(r"^(OU|TTH|TTA)_", market))
    return OfferEval(
        key=f"{market}:{sel}", market=market, selection=sel, label=label(market, sel), book=book, odds=odds,
        p_win=p_win, p_push=p_push, p_loss=p_loss, ev=ev, breakeven=be,
        min_odds=round(max(be[i] for i in idx) * (1.0 + cfg.safety_margin), 2), worst_ev=w_ev, worst_win=p_win[worst],
        worst_loss=p_loss[worst], worst_push=p_push[worst], sim_profit=profit, verdict=verdict, gradeable=gradeable,
    )


def _best_offers(entries: list[OddsEntry]) -> dict[tuple[str, str], tuple[float, str]]:
    best: dict[tuple[str, str], tuple[float, str]] = {}
    for e in entries:
        for s, o in e.pairs:
            if (e.market, s) not in best or o > best[(e.market, s)][0]:
                best[(e.market, s)] = (o, e.book)
    return best


def from_czech(entries: list, match_name: str) -> list[OddsEntry]:
    """Kurzy ulozene v Nastaveni/u zapasu (czechodds) pro dany zapas -> zaznamy Profilu 2."""
    from .czechodds import _en
    from .names import match_team

    home, _, away = (p.strip() for p in match_name.partition(" - "))
    out = []
    for e in entries:
        if not (match_team(_en(e.home), [_en(home)]) and match_team(_en(e.away), [_en(away)])):
            continue
        mk = _norm_market(e.market)
        if selections(mk) and len(selections(mk)) == len(e.odds):
            out.append(OddsEntry(e.book, mk, tuple(e.odds)))
    return out


FOREIGN_BOOK = "zahraničí"


def _same_match(name: str, home: str, away: str) -> bool:
    from .czechodds import _en
    from .names import match_team

    h, _, a = (p.strip() for p in name.partition(" - "))
    return bool(match_team(_en(h), [_en(home)]) and match_team(_en(a), [_en(away)]))


def foreign_entries(ledger, match_name: str, now: datetime, p4_store=None) -> tuple[list[OddsEntry], str]:
    """Zahranicni kurzy zapasu bez rucniho opisovani: median evropskych kancelari z posledniho stazeni kurzoveho API
    (ulozene v cache, kredity se nespotrebuji) a median kancelari z API-Football (Profil 4: handicapy, dalsi linie,
    goly tymu). Ceske kancelare se od nich lisi obvykle jen v setinach. Vraci (zaznamy, popis zdroje)."""
    from . import oddsapi
    from .config import SETTINGS
    from .pipeline import market_entries

    home, _, away = (p.strip() for p in match_name.partition(" - "))
    out: dict[str, OddsEntry] = {}
    src = []
    for league in SETTINGS.odds_api.sport_keys:
        cached = ledger.odds_cache(league)
        if not cached:
            continue
        for ev in oddsapi.parse_events(cached[1], league):
            if ev.kickoff > now - timedelta(hours=3) and _same_match(ev.name, home, away):
                for e in market_entries(ev, now):
                    mk = _norm_market(e.market)
                    if selections(mk) and len(selections(mk)) == len(e.odds):
                        out[mk] = OddsEntry(f"{FOREIGN_BOOK} (kurzové API)", mk, tuple(e.odds))
                src.append(f"kurzové API {cached[0]:%d.%m. %H:%M} UTC")
                break
    if p4_store is not None:
        n0 = len(out)
        for e in api_football_entries(p4_store, home, away, now):
            out.setdefault(e.market, e)  # kurzove API ma prednost
        if len(out) > n0:
            src.append("API-Football")
    return list(out.values()), " + ".join(src)


def api_football_entries(p4_store, home: str, away: str, now: datetime) -> list[OddsEntry]:
    """Median kancelari z API-Football (stazeno Profilem 4) pro zapas: handicapy, dalsi linie golu, goly tymu,
    dvojita sance... Trh jen s kurzy aspon dvou kancelari."""
    import statistics

    from .profil4.store import iso

    rows = p4_store.query("SELECT DISTINCT fixture_id, match FROM odds_quotes WHERE kickoff > ? AND source='api'",
                          (iso((now - timedelta(hours=3)).replace(tzinfo=now.tzinfo or timezone.utc)),))
    fid = next((r["fixture_id"] for r in rows if r["match"] and _same_match(r["match"], home, away)), None)
    if fid is None:
        return []
    from .profil4 import odds as O

    by: dict[tuple[str, str], dict[str, float]] = {}
    for q in O.latest(p4_store, fid):
        if q.source == "api":
            by.setdefault((q.market, q.bookmaker), {})[q.selection] = q.odds
    per_market: dict[str, list[tuple[float, ...]]] = {}
    for (mk, _), prices in by.items():
        sels = selections(mk)
        if sels and all(s in prices for s in sels):
            per_market.setdefault(mk, []).append(tuple(prices[s] for s in sels))
    return [OddsEntry(f"{FOREIGN_BOOK} (API-Football, {len(r_)} kanc.)", mk,
                      tuple(round(statistics.median(r[i] for r in r_), 2) for i in range(len(r_[0]))))
            for mk, r_ in sorted(per_market.items()) if len(r_) >= 2]


def surebets(entries: list[OddsEntry]) -> list[dict]:
    """Nejlepsi kurz kazdeho vyberu napric kancelarmi; soucet 1/kurz pod 1 = sure bet.

    U dvoucestnych trhu ukaze i hranici: jaky kurz na druhe strane by sure bet vytvoril."""
    best = _best_offers(entries)
    markets = sorted({e.market for e in entries if e.market != "EXPR"})
    out = []
    for mk in markets:
        sels = selections(mk)
        if not all((mk, s) in best for s in sels):
            continue
        odds = [best[(mk, s)][0] for s in sels]
        books = [best[(mk, s)][1] for s in sels]
        total = sum(1.0 / o for o in odds)
        row = {"market": mk, "selections": list(sels), "labels": [label(mk, x) for x in sels], "odds": odds, "books": books, "sum": total,
               "yield": 1.0 / total - 1.0, "sure": total < 1.0, "split": [], "needed": []}
        if row["sure"]:
            row["split"] = [round(100 * (1 / o) / total, 1) for o in odds]  # % celkoveho vkladu
        if len(sels) == 2:  # hranice: kurz na druhe strane, od ktereho vznikne sure bet
            need = [round(1.0 / (1.0 - 1.0 / odds[i]), 2) for i in range(2)]
            row["needed"] = need if max(need) <= 12 else []  # nerealne hranice (kurz 100+) se neukazuji
        out.append(row)
    return out


def _correlation(offers: list[OfferEval], mat: np.ndarray) -> list[dict]:
    out = []
    wins = {o.key: (returns(o.market, o.selection, o.odds) > 1.0 + 1e-9) for o in offers}
    loses = {o.key: (returns(o.market, o.selection, o.odds) < 1e-9) for o in offers}
    for i in range(len(offers)):
        for j in range(i + 1, len(offers)):
            a, b = offers[i], offers[j]
            pa, pb = mat[wins[a.key]].sum(), mat[wins[b.key]].sum()
            pab = mat[wins[a.key] & wins[b.key]].sum()
            denom = np.sqrt(pa * (1 - pa) * pb * (1 - pb))
            phi = float((pab - pa * pb) / denom) if denom > 0 else 0.0
            out.append({"a": a.label, "b": b.label, "both_win": float(pab),
                        "both_lose": float(mat[loses[a.key] & loses[b.key]].sum()), "phi": phi})
    return out


def _simulate(lam_h: float, lam_a: float, k: float, n: int, seed: int) -> tuple[np.ndarray, dict]:
    rng = np.random.default_rng(seed)
    if k > 0:
        lh = rng.gamma(k, lam_h / k, n)
        la = rng.gamma(k, lam_a / k, n)
    else:
        lh, la = np.full(n, lam_h), np.full(n, lam_a)
    h = np.minimum(rng.poisson(lh), MAX_GOALS)
    a = np.minimum(rng.poisson(la), MAX_GOALS)
    scores = np.stack([h, a], axis=1)
    keys, counts = np.unique(h * 100 + a, return_counts=True)
    order = np.argsort(-counts)
    top = [{"score": f"{int(keys[i] // 100)}:{int(keys[i] % 100)}", "pct": float(100.0 * counts[i] / n)} for i in order[:12]]
    tot = h + a
    dist = {str(g): 100.0 * float((tot == g).mean()) for g in range(5)}
    dist["5+"] = 100.0 * float((tot >= 5).mean())
    summary = {
        "n": n, "top": top, "home": 100.0 * float((h > a).mean()), "draw": 100.0 * float((h == a).mean()),
        "away": 100.0 * float((h < a).mean()), "avg_goals": float(tot.mean()), "goals": dist,
    }
    return scores, summary


def _utcnow() -> datetime:
    from .storage import utcnow

    return utcnow()


def _seed(match: str) -> int:
    return zlib.crc32(match.encode())


def analyse(
    match: str,
    entries: list[OddsEntry],
    model_lams: tuple[float, float] | None,
    model_source: str = "ruční odhad",
    league: str = "",
    day: str = "",
    context_scale: tuple[float, float] = (1.0, 1.0),
    bankroll: float = 0.0,
    exposure: float = 0.0,
    now: datetime | None = None,
    cfg: Profile2Config = P2,
) -> Analysis:
    """Kompletni rozbor zapasu podle Profilu 2."""
    from . import risk

    if not entries:
        raise ValueError("Zadej aspoň jeden kurz.")
    notes: list[str] = []
    k = cfg.gamma_shape
    mkt = market_lambdas(entries, k)
    if model_lams is None and mkt is None:
        raise ValueError("Chybí odhad gólů (λ) i dost kurzů na dopočet z trhu (aspoň 1X2 a jedna gólová linie).")
    scenarios = []
    if model_lams is not None:
        lh, la = model_lams[0] * context_scale[0], model_lams[1] * context_scale[1]
        src = model_source + (f", kontext ×{context_scale[0]:.2f}/×{context_scale[1]:.2f}" if context_scale != (1.0, 1.0) else "")
        scenarios.append(Scenario("A – model", lh, la, src))
        if mkt is not None:
            w = cfg.blend_weight
            scenarios.append(Scenario("B – kompromis", w * lh + (1 - w) * mkt[0], w * la + (1 - w) * mkt[1],
                                      f"{w:.0%} model, {1 - w:.0%} trh"))
    if mkt is not None:
        scenarios.append(Scenario("C – trh", mkt[0], mkt[1], "dopočteno z kurzů bez marže (kontrola)",
                                  decides=model_lams is None))
    if model_lams is None:
        notes.append("Chybí odhad modelu – hodnotí se jen proti trhu. Proti férové linii týchž kurzů je EV vždy "
                     "kolem minus marže; kladné vyjde jen u lepšího kurzu jiné kanceláře.")
    elif mkt is None:
        notes.append("Z kurzů nejde dopočítat trh (chybí 1X2 nebo gólová linie) – robustnost vůči trhu nelze ověřit, "
                     "doporučení ber opatrně.")
    mats = [score_matrix(s.lam_h, s.lam_a, k) for s in scenarios]
    base = scenarios[0]
    sim_scores, sim = _simulate(base.lam_h, base.lam_a, k, cfg.sims, _seed(match))

    offers = []
    for e in entries:
        for s, o in e.pairs:
            offers.append(_eval_offer(e.market, s, e.book, o, mats, sim_scores, cfg, [sc.decides for sc in scenarios]))
    # u stejneho vyberu necha nejlepsi kurz
    best: dict[str, OfferEval] = {}
    for o in offers:
        if o.key not in best or o.odds > best[o.key].odds:
            best[o.key] = o
    offers = sorted(best.values(), key=lambda o: -o.worst_ev)
    for o in offers:
        if o.verdict == "suspicious":
            notes.append(f"{o.label} @ {o.odds:.2f}: EV {o.worst_ev:+.0%} je podezřele vysoké – zkontroluj kurz a trh.")

    usable = [o for o in offers if o.verdict == "robust" and cfg.min_odds <= o.odds <= cfg.max_odds]
    picks: list[Pick] = []
    safe = [o for o in usable if o.worst_loss <= cfg.safe_max_loss]
    if safe:
        picks.append(Pick("safe", "Nejrozumnější sázka", max(safe, key=lambda o: o.worst_ev)))
    high = [o for o in usable if o.odds >= cfg.high_prob_min_odds and o.worst_win >= cfg.high_prob_min_win]
    if high:
        picks.append(Pick("high_prob", "Kurz 1,5+ s vysokou šancí", max(high, key=lambda o: o.worst_ev)))
    val = [o for o in usable if o.worst_ev >= cfg.value_ev]
    if val:
        picks.append(Pick("value", "Nejvyšší hodnota", max(val, key=lambda o: o.worst_ev)))
    seen, uniq = set(), []
    for p in picks:  # stejna sazka ve vic kategoriich se ukaze jen jednou (u prvni)
        if p.offer.key in seen:
            continue
        seen.add(p.offer.key)
        uniq.append(p)
    picks = uniq
    for p in picks:
        o = p.offer
        p_eff = o.worst_win / max(1e-9, 1.0 - o.worst_push)  # vraceni = sazka neprobehla
        st = risk.size_stake(p_eff, o.odds, bankroll, exposure)
        p.stake, p.stake_note = st.stake, st.reason
    if not picks:
        notes.append("Žádná sázka nemá kladnou hodnotu v obou scénářích – nejrozumnější je nesázet.")

    avoid = [o for o in offers if o.verdict == "avoid"][-6:][::-1]
    corr = _correlation([p.offer for p in picks], mats[0]) if len(picks) > 1 else []
    if any(c["phi"] >= cfg.corr_warn for c in corr):
        notes.append("Doporučené sázky stojí na stejném průběhu zápasu – nedávej je na jeden tiket (AKO), "
                     "ber je jako jednu sázku rozdělenou na části.")
    if model_lams is not None and mkt is not None:
        tot_a, tot_b = scenarios[0].lam_h + scenarios[0].lam_a, scenarios[-1].lam_h + scenarios[-1].lam_a
        if abs(tot_a - tot_b) > 0.6:
            notes.append(f"Model ({tot_a:.2f} gólu) a trh ({tot_b:.2f}) se rozcházejí o víc než 0,6 gólu – "
                         "ověř vstupy (sestavy, absence), než vsadíš.")
    return Analysis(
        match=match, league=league, day=day, created=(now or _utcnow()).isoformat(timespec="minutes"),
        scenarios=scenarios, k=k, offers=offers, picks=picks, avoid=avoid, correlation=corr,
        surebets=surebets(entries), sim=sim, notes=notes,
    )


def bot_lambdas(league: str, home: str, away: str, today) -> tuple[tuple[float, float] | None, str]:
    """Ocekavane goly z modelu bota (Dixon-Coles) pro fotbalovou soutez; None + duvod, kdyz model neni."""
    from .config import LEAGUES, Sport
    from .names import match_team
    from .pipeline import ModelProvider

    if league not in LEAGUES or LEAGUES[league].sport is not Sport.FOOTBALL:
        return None, "model bota je jen pro fotbalové soutěže"
    prov = ModelProvider(league, today)
    if prov.params is None:
        return None, prov.note or "model bota není k dispozici"
    from .czechodds import _en

    h, a = match_team(_en(home), prov.params.teams), match_team(_en(away), prov.params.teams)
    if not h or not a:
        return None, f"tým není v datech modelu ({home if not h else away})"
    lh, la = prov.params.expected_goals(h, a)
    return (float(lh), float(la)), f"model bota ({LEAGUES[league].name})"


def combine_lambdas(manual: tuple[float, float] | None, league: str, home: str, away: str,
                    today) -> tuple[tuple[float, float] | None, str]:
    """Scenar A: rucni odhad, model bota, nebo prumer obou (kdyz jsou k dispozici oba)."""
    bot, note = (None, "")
    if league:
        try:
            bot, note = bot_lambdas(league, home, away, today)
        except Exception as e:  # noqa: BLE001 - chybejici data modelu nesmi shodit rozbor
            bot, note = None, f"model bota selhal ({e})"
    if manual and bot:
        return ((manual[0] + bot[0]) / 2, (manual[1] + bot[1]) / 2), \
            f"průměr: ruční odhad {manual[0]:.2f}:{manual[1]:.2f} a {note} {bot[0]:.2f}:{bot[1]:.2f}"
    if manual:
        return (float(manual[0]), float(manual[1])), "ruční odhad" + (f" ({note})" if league and not bot else "")
    if bot:
        return bot, note
    return None, note


# ---------------------------------------------------------------------------
# Ulozeni a sledovani
# ---------------------------------------------------------------------------

def load(ledger) -> list[Analysis]:
    raw = ledger._get(STATE_KEY, "")
    return [Analysis.from_json(d) for d in json.loads(raw)] if raw else []


def save(ledger, analysis: Analysis) -> None:
    rest = [a for a in load(ledger) if not (a.match == analysis.match and a.day == analysis.day)]
    items = ([analysis] + rest)[:KEEP]
    ledger._set(STATE_KEY, json.dumps([a.to_json() for a in items], ensure_ascii=False))


def remove(ledger, match: str, day: str) -> None:
    items = [a for a in load(ledger) if not (a.match == match and a.day == day)]
    ledger._set(STATE_KEY, json.dumps([a.to_json() for a in items], ensure_ascii=False))


def track_picks(ledger, analysis: Analysis, now: datetime) -> int:
    """Doporucene sazky do Sledovani (zdroj 'profil2'), jen trhy, ktere umi vyhodnotit."""
    from . import paper

    if not analysis.day:
        raise ValueError("Pro sledování zadej datum zápasu.")
    picks = paper.load(ledger)
    have = {(p.name, p.day, p.market, p.selection) for p in picks if p.profile == PROFILE_KEY}
    n = 0
    for pk in analysis.picks:
        o = pk.offer
        if not o.gradeable or (analysis.match, analysis.day, o.market, o.selection) in have:
            continue
        p_eff = o.worst_win / max(1e-9, 1.0 - o.worst_push)
        picks.append(paper.Pick(
            id=__import__("uuid").uuid4().hex[:8], league=analysis.league, match_id=f"manual:{analysis.day}:{analysis.match}",
            name=analysis.match, day=analysis.day, kickoff=f"{analysis.day}T23:00:00", market=o.market,
            selection=o.selection, odds=o.odds, book=o.book, p_model=round(p_eff, 4), source="rucne", created=now.isoformat(),
            flags=[pk.title], profile=PROFILE_KEY, detail=pick_detail(analysis, pk),
        ))
        n += 1
    if n:
        paper.save(ledger, picks)
    return n


def pick_detail(a: Analysis, pk: Pick) -> str:
    """Kratky rozbor vyberu pro Sledovani."""
    o = pk.offer
    sc = "; ".join(f"{s.name} {s.lam_h:.2f}:{s.lam_a:.2f}" for s in a.scenarios)
    ev = " / ".join(f"{e:+.1%}" for e in o.ev)
    push = f", vrácení {o.worst_push:.0%}" if o.worst_push > 0.005 else ""
    return (f"{pk.title}. Góly {sc}. Výhra {o.worst_win:.0%}{push}, prohra {o.worst_loss:.0%}; EV {ev}; "
            f"min. kurz {o.min_odds:.2f}.")


def outcome_split(market: str, selection: str, odds: float, mat: np.ndarray) -> dict[str, float]:
    """Pet vysledku sazky ze skorove matice (Faze 3.2.1): plna vyhra, pul vyhra, vraceni, pul prohra, plna prohra.
    U ctvrtinovych linii jde polovina vkladu na kazdou sousedni linii (stejne jako `returns`)."""
    r, eps = returns(market, selection, odds), 1e-9
    full_win = r >= odds - eps
    push = np.abs(r - 1.0) < eps
    half_win = (r > 1.0 + eps) & ~full_win
    half_loss = (r > eps) & (r < 1.0 - eps)
    loss = r < eps
    return {k: float(mat[m].sum()) for k, m in (("win", full_win), ("half_win", half_win), ("push", push),
                                                 ("half_loss", half_loss), ("loss", loss))}


def p_interval(market: str, selection: str, odds: float, lam_h: float, lam_a: float, k: float,
               q: tuple[float, float] = (0.05, 0.95)) -> tuple[float, float] | None:
    """Citlivost sance na vyhru na nejistotu ocekavanych golu: sance pri λ na 5. a 95. kvantilu gama(k).
    Neni to konfidencni interval modelu, jen rozsah, ve kterem se sance pohybuje pri chybe odhadu golu."""
    if k <= 0:
        return None
    vals = []
    for qq in q:
        f = float(stats.gamma.ppf(qq, a=k, scale=1.0 / k))
        split = outcome_split(market, selection, odds, score_matrix(lam_h * f, lam_a * f, 0))
        vals.append(split["win"] + split["half_win"])
    return (min(vals), max(vals))


def scenario_data(a: Analysis, pk: Pick) -> dict:
    """Strukturovane scenare vyberu pro Sledovani (od Faze 3.2.1; starsi vybery maji jen textovy rozbor):
    λ, EV, sance (5 vysledku) a citlivost kazdeho scenare, pocet simulaci a seed. Simulace slouzi jen
    k zobrazeni – sance i EV se pocitaji presne ze skorove matice (vyber na seedu nezavisi)."""
    o = pk.offer
    out = {"role": pk.role, "title": pk.title, "k": a.k, "sims": a.sim.get("n"), "seed": _seed(a.match),
           "sim_profit": o.sim_profit, "verdict": o.verdict, "scenarios": []}
    for s, ev, be in zip(a.scenarios, o.ev, o.breakeven):
        mat = score_matrix(s.lam_h, s.lam_a, a.k)
        split = outcome_split(o.market, o.selection, o.odds, mat)
        out["scenarios"].append({
            "name": s.name, "lam_h": round(s.lam_h, 3), "lam_a": round(s.lam_a, 3), "decides": s.decides,
            "source": s.source, "ev": round(ev, 4), "breakeven": round(be, 3) if math.isfinite(be) else None,
            "split": {k_: round(v, 4) for k_, v in split.items()},
            "p_range": [round(x, 4) for x in (p_interval(o.market, o.selection, o.odds, s.lam_h, s.lam_a, a.k) or ())],
        })
    return out


def auto_pick(match: str, model: dict | None, czech_entries: list, cfg: Profile2Config = P2) -> tuple[Pick, Analysis] | None:
    """Automaticky vyber pro Sledovani pri skenu: λ z modelu bota + rucne zadane ceske kurzy zapasu.

    Bere prvni doporuceni v poradi nejrozumnejsi -> 1,5+ s vysokou sanci -> nejvyssi hodnota, jen trhy,
    ktere Sledovani umi vyhodnotit."""
    if not model or not model.get("lambda_home") or not model.get("lambda_away"):
        return None
    entries = []
    for e in czech_entries:
        mk = _norm_market(e.market)
        if selections(mk) and len(selections(mk)) == len(e.odds):
            entries.append(OddsEntry(e.book, mk, tuple(e.odds)))
    if not entries:
        return None
    try:
        a = analyse(match, entries, (float(model["lambda_home"]), float(model["lambda_away"])), "model bota", cfg=cfg)
    except ValueError:
        return None
    if len([s for s in a.scenarios if s.decides]) < 2:
        return None  # bez trhu nejde overit robustnost
    pk = next((p for p in a.picks if p.offer.gradeable), None)
    return (pk, a) if pk else None


def format_text(a: Analysis, cur: str = "Kč") -> str:
    """Rozbor jako prosty text (prikazova radka, kopirovani)."""
    pct = lambda x: f"{100 * x:.0f} %"  # noqa: E731
    out = [f"PROFIL 2 – {a.match}" + (f" ({a.day})" if a.day else ""), ""]
    out.append("Scénáře (očekávané góly domácí : hosté):")
    for s in a.scenarios:
        out.append(f"  {s.name}: {s.lam_h:.2f} : {s.lam_a:.2f} (celkem {s.lam_h + s.lam_a:.2f}) – {s.source}")
    out.append("")
    if a.picks:
        out.append("Doporučení:")
        for p in a.picks:
            o = p.offer
            ev = " / ".join(f"{e:+.1%}" for e in o.ev)
            push = f", vrácení {pct(o.worst_push)}" if o.worst_push > 0.005 else ""
            stake = f", vklad {p.stake:.0f} {cur}" if p.stake else f", vklad: {p.stake_note}"
            out.append(f"  {p.title}: {o.label} @ {o.odds:.2f} ({o.book}) – výhra {pct(o.worst_win)}{push}, "
                       f"prohra {pct(o.worst_loss)}; EV {ev}; min. kurz {o.min_odds:.2f}{stake}")
    for n in a.notes:
        out.append(f"  ! {n}")
    if a.avoid:
        out.append("")
        out.append("Vyhnout se: " + ", ".join(f"{o.label} @ {o.odds:.2f} ({o.worst_ev:+.0%})" for o in a.avoid))
    out.append("")
    out.append(f"Simulace {a.sim['n']} zápasů: domácí {a.sim['home']:.1f} % · remíza {a.sim['draw']:.1f} % · "
               f"hosté {a.sim['away']:.1f} % · průměr {a.sim['avg_goals']:.2f} gólu")
    out.append("  Nejčastější výsledky: " + ", ".join(f"{t['score']} {t['pct']:.1f} %" for t in a.sim["top"][:8]))
    out.append("  Počet gólů: " + ", ".join(f"{g}: {v:.1f} %" for g, v in a.sim["goals"].items()))
    out.append("")
    out.append("Všechny nabídky (EV ve scénářích " + " / ".join(s.name.split(" ")[0] for s in a.scenarios) + "):")
    for o in a.offers:
        mark = {"robust": "✓", "borderline": "~", "avoid": "✗", "suspicious": "?"}.get(o.verdict, " ")
        out.append(f"  {mark} {o.label:<34} {o.odds:>5.2f}  " + " / ".join(f"{e:+6.1%}" for e in o.ev)
                   + f"  min {o.min_odds:.2f}  sim {o.sim_profit:+.0%}")
    sure = [s for s in a.surebets if s["sure"]]
    out.append("")
    if sure:
        for s in sure:
            out.append(f"SURE BET: " + ", ".join(f"{sel} {o:.2f} ({b}) {sp} %" for sel, o, b, sp in
                                                         zip(s["labels"], s["odds"], s["books"], s["split"]))
                       + f" – výnos {s['yield']:+.1%}")
    else:
        out.append("Sure bet: žádný (pro dvoucestné trhy viz hranice v panelu).")
    return "\n".join(out)
