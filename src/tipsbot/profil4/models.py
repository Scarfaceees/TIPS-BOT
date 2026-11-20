"""Modely A–D Profilu 4. Jedina autorita pro cisla; deterministicke a testovatelne.

A – vlastni statisticky model: Dixon-Coles z bota (`models.football.fit`: utok/obrana, domaci vyhoda po tymech,
    exponencialni utlum stari zapasu, korekce nizkych skore, sila soupere je soucasti odhadu, cold start).
    Data: vysledky souteze z API-Football (aktualni + minula sezona). Zadna nova implementace – jen jiny zdroj dat.
B – trzni model: kurzy bez marze (vychozi multiplikativni p_i / sum p), konsensus kancelari (median), prednost maji
    ostre kancelare (Pinnacle), kdyz maji kompletni trh. Z ferovych pravdepodobnosti 1X2 a golovych linii se
    dopocitaji ocekavane goly a Poissonova matice. Puvodni kurzy i hrube a ocistene pravdepodobnosti se uchovavaji.
C – kombinovany model: smes matic w*A + (1-w)*B (konzistentni pro vsechny trhy). Vaha z konfigurace, nebo ze
    zpetneho testu bota pro soutez. Kdyz jeden zdroj chybi, C = dostupny model a je oznacen jako omezeny.
D – konzervativni kontrola: nejistota parametru (gama smes ocekavanych golu, sirsi pri malo datech) a modelova
    nejistota (A vs. trh). Pro kazdou sazku se bere NEJHORSI EV z {C s nejistotou, A s nejistotou, B}.
    Neni to dalsi predikce, ale spodni odhad.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from scipy import optimize, stats

from .. import profile2
from ..models import football
from . import mathcore
from .config import Profil4Config
from .odds import OddsQuote
from .provider import Fixture

GRID = profile2.MAX_GOALS


@dataclass
class ModelResult:
    name: str
    available: bool
    matrix: np.ndarray | None = None
    lam_home: float | None = None
    lam_away: float | None = None
    rho: float = 0.0
    note: str = ""
    n_matches: int = 0
    degraded: bool = False
    extra: dict = field(default_factory=dict)


def unavailable(name: str, note: str) -> ModelResult:
    return ModelResult(name, False, note=note)


# ---------------------------------------------------------------------------
# Model A
# ---------------------------------------------------------------------------

def matches_frame(history: list[Fixture]):
    import pandas as pd

    rows = [{"date": f.kickoff.date(), "home": f.home, "away": f.away, "home_goals": f.ft_home, "away_goals": f.ft_away}
            for f in history if f.finished]
    return pd.DataFrame(rows, columns=["date", "home", "away", "home_goals", "away_goals"])


def model_a(fixture: Fixture, history: list[Fixture], cfg: Profil4Config) -> ModelResult:
    df = matches_frame([f for f in history if f.kickoff < fixture.kickoff])  # zadne informace z budoucnosti
    if len(df) < cfg.model.min_matches_model_a:
        return unavailable("A – vlastní", f"málo odehraných zápasů v datech ({len(df)} < {cfg.model.min_matches_model_a})")
    counts = {t: int(((df["home"] == t) | (df["away"] == t)).sum()) for t in (fixture.home, fixture.away)}
    if min(counts.values()) < 5:
        return unavailable("A – vlastní", f"tým má v datech málo zápasů ({counts})")
    season = [f.kickoff.date() for f in history if f.season == fixture.season and f.finished
              and f.kickoff < fixture.kickoff]  # i zacatek sezony jen z toho, co bylo pred zapasem znamo
    start = min(season) if season else None
    try:
        params = football.fit(df, fixture.kickoff.date(), season_start=start)
        lam, mu = params.expected_goals(fixture.home, fixture.away)
    except football.ModelError as e:
        return unavailable("A – vlastní", f"model nelze odhadnout: {e}")
    mat = football.score_matrix(lam, mu, params.rho, GRID)
    return ModelResult("A – vlastní", True, mat, lam, mu, params.rho, "Dixon-Coles (výsledky z API-Football)",
                       len(df), extra={"team_matches": counts})


# ---------------------------------------------------------------------------
# Model B
# ---------------------------------------------------------------------------

COMPLETE = ("1X2", "BTTS")  # + OU_/TTH_/TTA_ pulove linie


def _half_line(market: str) -> bool:
    if not market.startswith(("OU_", "TTH_", "TTA_")):
        return False
    line = float(market.split("_", 1)[1])
    return abs(line * 2 - round(line * 2)) < 1e-9 and abs(line - round(line)) > 1e-9


def market_table(quotes: list[OddsQuote], method: str = "multiplicative",
                 sharp: tuple[str, ...] = ("pinnacle",)) -> dict:
    """Ferove pravdepodobnosti kompletnich trhu po kancelarich + konsensus (median).

    Vraci {"books": {trh: {kancelar: {"odds", "raw", "fair", "overround"}}}, "consensus": {trh:vyber: p}, "sources": {trh: [...]}}.
    """
    by: dict[tuple[str, str], dict[str, float]] = {}
    for q in quotes:
        if q.market in COMPLETE or _half_line(q.market):
            by.setdefault((q.market, q.bookmaker), {})[q.selection] = q.odds
    books: dict[str, dict] = {}
    for (mk, bk), sel_odds in by.items():
        sels = profile2.selections(mk)
        if not sels or not all(s in sel_odds for s in sels):
            continue
        odds = [sel_odds[s] for s in sels]
        try:
            raw, fair = mathcore.devig(odds, method)
        except ValueError:
            continue
        books.setdefault(mk, {})[bk] = {"selections": list(sels), "odds": odds, "raw": raw, "fair": fair,
                                         "overround": sum(raw) - 1.0}
    consensus, sources = {}, {}
    for mk, per in books.items():
        use = [b for b in per if b in sharp] or list(per)
        sels = per[use[0]]["selections"]
        med = [float(np.median([per[b]["fair"][i] for b in use])) for i in range(len(sels))]
        s = sum(med)
        for sel, p in zip(sels, med):
            consensus[f"{mk}:{sel}"] = p / s
        sources[mk] = use
    return {"books": books, "consensus": consensus, "sources": sources}


PRIOR_TOTAL = 2.6  # jen pro rozhodnuti remizy fitu bez golove linie; nesmi prebit samotny trh


def fit_market_lambdas(consensus: dict[str, float], prior_total: float | None = None) -> tuple[float, float] | None:
    """Ocekavane goly, ktere nejlepe vysvetli ferove pravdepodobnosti trhu (Poisson).

    Bez golove linie urci obe lambdy samotne 1X2 (dva stupne volnosti, dva parametry). Prior na soucet golu ma jen
    zanedbatelnou vahu: drive (vaha 0.02) prebijel trh a model B pak neodpovidal ani vlastnim kurzum 1X2
    (Schalke–Stuttgart: remiza trh 23 %, model B 28 %)."""
    if not all(f"1X2:{s}" in consensus for s in ("1", "X", "2")):
        return None
    targets = [(k.rsplit(":", 1)[0], k.rsplit(":", 1)[1], p) for k, p in consensus.items()]
    masks = [(mathcore.coefficients(mk, s)[0] > 0.99, p) for mk, s, p in targets]
    has_goals = any(mk.startswith(("OU_", "TTH_", "TTA_")) or mk == "BTTS" for mk, _, _ in targets)

    def loss(x: np.ndarray) -> float:
        lh, la = np.exp(x)
        mat = football.score_matrix(lh, la, 0.0, GRID)
        err = sum((float(mat[m].sum()) - p) ** 2 for m, p in masks)
        if not has_goals and prior_total:
            err += 1e-6 * (lh + la - prior_total) ** 2  # jen rozhodne mezi temer stejne dobrymi fity
        return err

    best = min((optimize.minimize(loss, np.log(x0), method="Nelder-Mead", options={"xatol": 1e-5, "fatol": 1e-11})
                for x0 in ((1.4, 1.1), (0.9, 1.8), (2.0, 0.8))), key=lambda r: r.fun)
    lh, la = np.exp(best.x)
    return float(lh), float(la)


def model_b(quotes: list[OddsQuote], cfg: Profil4Config) -> ModelResult:
    """Ciste trzni model – zavisi jen na kurzech (nikdy na modelu A, jinak by 'shoda s trhem' nic neoverovala)."""
    table = market_table(quotes, cfg.odds.devig_method, cfg.odds.sharp_books)
    lams = fit_market_lambdas(table["consensus"], PRIOR_TOTAL)
    if lams is None:
        return unavailable("B – tržní", "chybí kompletní trh 1X2 s aktuálními kurzy")
    mat = football.score_matrix(lams[0], lams[1], 0.0, GRID)
    goal_lines = [m for m in table["books"] if m != "1X2"]
    note = "kurzy bez marže (" + cfg.odds.devig_method + "), zdroje 1X2: " + ", ".join(table["sources"]["1X2"])
    if not goal_lines:
        note += "; bez aktuální gólové linie – gólové trhy jen dopočteny z 1X2, nejsou tržní"
    resid = max(abs(mathcore.evaluate("1X2", s, mat).p_win - table["consensus"][f"1X2:{s}"]) for s in ("1", "X", "2"))
    if resid > 0.02:
        note += f"; Poissonův fit se od kurzů 1X2 odchyluje až o {resid * 100:.1f} p.b."
    return ModelResult("B – tržní", True, mat, lams[0], lams[1], 0.0, note, degraded=not goal_lines or resid > 0.02,
                       extra={"table": table, "inputs": sorted(table["books"]), "fit_1x2_resid": resid})


# ---------------------------------------------------------------------------
# Model C a D
# ---------------------------------------------------------------------------

def model_weight(cfg: Profil4Config, bot_league: str = "") -> tuple[float, str]:
    if cfg.model.use_backtested_weights and bot_league:
        from ..config import SETTINGS

        w = SETTINGS.value.league_model_weight.get(bot_league)
        if w is not None:
            return float(w), f"váha ze zpětného testu bota pro {bot_league}"
    return cfg.model.model_weight, "výchozí váha z konfigurace"


def model_c(a: ModelResult, b: ModelResult, weight: float, weight_note: str) -> ModelResult:
    if a.available and b.available:
        mat = weight * a.matrix + (1 - weight) * b.matrix
        lh = weight * a.lam_home + (1 - weight) * b.lam_home
        la = weight * a.lam_away + (1 - weight) * b.lam_away
        return ModelResult("C – kombinovaný", True, mat, lh, la, a.rho * weight,
                           f"{weight:.0%} model A, {1 - weight:.0%} trh B ({weight_note})", degraded=b.degraded,
                           extra={"weight": weight})
    one = a if a.available else b if b.available else None
    if one is None:
        return unavailable("C – kombinovaný", "není k dispozici model A ani trh B")
    return ModelResult("C – kombinovaný", True, one.matrix, one.lam_home, one.lam_away, one.rho,
                       f"jen {one.name} – druhý zdroj chybí, shodu modelu s trhem nelze ověřit", degraded=True,
                       extra={"weight": 1.0 if one is a else 0.0})


def _gamma_points(k: float, n: int) -> np.ndarray:
    q = (np.arange(n) + 0.5) / n
    f = stats.gamma.ppf(q, a=k, scale=1.0 / k)
    return f / f.mean()


def uncertain_matrix(lam: float, mu: float, rho: float, k: float, n: int = 12) -> np.ndarray:
    """Skorova matice s nejistotou ocekavanych golu (nezavisle gama faktory, prumer 1, CV = 1/sqrt(k))."""
    if k <= 0:
        return football.score_matrix(lam, mu, rho, GRID)
    f = _gamma_points(k, n)
    acc = np.zeros((GRID + 1, GRID + 1))
    for fh in f:
        for fa in f:
            acc += football.score_matrix(lam * fh, mu * fa, rho, GRID)
    return acc / (n * n)


def gamma_k(cfg: Profil4Config, data_quality: float, n_matches: int) -> float:
    """Malo dat nebo nizka kvalita => mensi k (vetsi nejistota)."""
    good = min(1.0, n_matches / 300.0) * min(1.0, max(0.0, (data_quality - 0.3) / 0.6))
    return cfg.model.gamma_k_poor + good * (cfg.model.gamma_k_good - cfg.model.gamma_k_poor)


@dataclass
class ModelSet:
    a: ModelResult
    b: ModelResult
    c: ModelResult
    k: float
    a_unc: np.ndarray | None
    c_unc: np.ndarray | None
    weight: float

    def conservative_mats(self) -> list[tuple[str, np.ndarray]]:
        mats = []
        if self.c_unc is not None:
            mats.append(("C s nejistotou", self.c_unc))
        if self.a_unc is not None:
            mats.append(("A s nejistotou", self.a_unc))
        if self.b.available:
            mats.append(("B trh", self.b.matrix))
        return mats

    def evaluate_d(self, market: str, selection: str, odds: float) -> tuple[mathcore.BetEval, str] | None:
        """Model D: nejhorsi EV ze spodnich odhadu."""
        evals = [(mathcore.evaluate(market, selection, m, odds), n) for n, m in self.conservative_mats()]
        if not evals:
            return None
        return min(evals, key=lambda x: x[0].ev)

    def d_probability(self, market: str, selection: str) -> float | None:
        evals = [mathcore.evaluate(market, selection, m) for _, m in self.conservative_mats()]
        return min(e.p_effective for e in evals) if evals else None


def build(fixture: Fixture, history: list[Fixture], quotes: list[OddsQuote], cfg: Profil4Config,
          data_quality: float, bot_league: str = "") -> ModelSet:
    a = model_a(fixture, history, cfg)
    b = model_b(quotes, cfg)
    w, note = model_weight(cfg, bot_league)
    c = model_c(a, b, w, note)
    k = gamma_k(cfg, data_quality, a.n_matches)
    a_unc = uncertain_matrix(a.lam_home, a.lam_away, a.rho, k) if a.available else None
    if c.available:
        if a.available and b.available:
            b_unc = uncertain_matrix(b.lam_home, b.lam_away, 0.0, k)
            c_unc = w * a_unc + (1 - w) * b_unc
        else:
            c_unc = uncertain_matrix(c.lam_home, c.lam_away, c.rho, k)
    else:
        c_unc = None
    return ModelSet(a, b, c, k, a_unc, c_unc, c.extra.get("weight", w) if c.available else w)


def expected_score(mat: np.ndarray) -> tuple[float, float]:
    g = np.arange(mat.shape[0])
    return float((mat.sum(axis=1) * g).sum()), float((mat.sum(axis=0) * g).sum())


def cv(k: float) -> float:
    return 1.0 / math.sqrt(k) if k > 0 else 0.0


def today_from(fixture: Fixture) -> date:
    return fixture.kickoff.date()
