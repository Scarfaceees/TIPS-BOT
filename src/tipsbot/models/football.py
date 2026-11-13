"""Dixon-Coles model pro fotbal.

log(lambda_home) = mu + gamma + hta[home] + att[home] - def[away]
log(lambda_away) = mu + att[away] - def[home]

- casovy utlum exp(-xi * dny), zapasy starsi nez max_history_seasons maji vahu 0
- korekce nizkych skore tau(rho)
- domaci vyhoda po tymech (hta), pritahovana k ligove hodnote gamma
- cold-start: tymy s malo zapasy v sezone se michaji s parametry z minule sezony
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.stats import poisson

from ..config import SETTINGS, FootballModelConfig

REQUIRED_COLUMNS = ("date", "home", "away", "home_goals", "away_goals")
# neutral = zapas na neutralni pude (bez domaci vyhody), match_weight = vaha zapasu (dulezitost souteze)
OPTIONAL_COLUMNS = ("neutral", "match_weight")


class ModelError(ValueError):
    pass


@dataclass(frozen=True)
class DCParams:
    teams: tuple[str, ...]
    attack: dict[str, float]
    defence: dict[str, float]
    home_adv: dict[str, float]  # celkova domaci vyhoda tymu (gamma + odchylka)
    mu: float
    gamma: float
    rho: float

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        for t in (home, away):
            if t not in self.attack:
                raise ModelError(f"Neznamy tym: {t}")
        lam = np.exp(self.mu + self.home_adv[home] + self.attack[home] - self.defence[away])
        mu_ = np.exp(self.mu + self.attack[away] - self.defence[home])
        return float(lam), float(mu_)


def _tau(x: np.ndarray, y: np.ndarray, lam: np.ndarray, mu_: np.ndarray, rho: float) -> np.ndarray:
    t = np.ones_like(lam, dtype=float)
    t = np.where((x == 0) & (y == 0), 1.0 - lam * mu_ * rho, t)
    t = np.where((x == 0) & (y == 1), 1.0 + lam * rho, t)
    t = np.where((x == 1) & (y == 0), 1.0 + mu_ * rho, t)
    t = np.where((x == 1) & (y == 1), 1.0 - rho, t)
    return np.clip(t, 1e-10, None)


def _to_date(d) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return pd.Timestamp(d).date()


def _prepare(matches: pd.DataFrame, as_of: date, cfg: FootballModelConfig) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in matches.columns]
    if missing:
        raise ModelError(f"Chybi sloupce: {missing}")
    optional = [c for c in OPTIONAL_COLUMNS if c in matches.columns]
    df = matches.loc[:, [*REQUIRED_COLUMNS, *optional]].dropna(subset=list(REQUIRED_COLUMNS)).copy()  # ostatni sloupce se zahodi
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df[df["date"] < as_of]
    days = np.array([(as_of - d).days for d in df["date"]], dtype=float)
    df["weight"] = np.exp(-cfg.xi * days) * (df["match_weight"] if "match_weight" in df else 1.0)
    df = df[days <= cfg.max_history_seasons * 365]
    if len(df) == 0:
        raise ModelError("Zadne zapasy pro odhad modelu.")
    return df.reset_index(drop=True)


def _fit_raw(df: pd.DataFrame, cfg: FootballModelConfig, team_group: dict[str, int] | None = None) -> DCParams:
    """`team_group` (tym -> cislo skupiny/ligy) zapne spolecny odhad: kazda skupina dostane offset sily,
    ktery se uplatni jen v zapasech mezi skupinami (poharech); v ligovych zapasech se vykrati."""
    teams = tuple(sorted(set(df["home"]) | set(df["away"])))
    n = len(teams)
    if len(df) < n:
        raise ModelError("Prilis malo zapasu pro odhad modelu.")
    idx = {t: i for i, t in enumerate(teams)}
    n_groups = (max(team_group.values()) + 1) if team_group else 0
    grp = np.array([team_group.get(t, 0) for t in teams], dtype=int) if team_group else np.zeros(n, dtype=int)
    h = df["home"].map(idx).to_numpy()
    a = df["away"].map(idx).to_numpy()
    x = df["home_goals"].to_numpy(dtype=float)
    y = df["away_goals"].to_numpy(dtype=float)
    w = df["weight"].to_numpy(dtype=float)
    home_on = (~df["neutral"].astype(bool)).to_numpy(dtype=float) if "neutral" in df else np.ones(len(df))
    use_hta = cfg.team_home_advantage
    shrink = cfg.home_advantage_shrinkage_matches

    base = 3 + (3 * n if use_hta else 2 * n)

    def unpack(p: np.ndarray):
        mu, gamma, rho = p[0], p[1], p[2]
        att = p[3 : 3 + n]
        de = p[3 + n : 3 + 2 * n]
        att = att - att.mean()
        de = de - de.mean()
        hta = p[3 + 2 * n : 3 + 3 * n] if use_hta else np.zeros(n)
        if n_groups:
            q = p[base : base + n_groups][grp]
            att, de = att + q, de + q
        return mu, gamma, rho, att, de, hta

    def nll(p: np.ndarray) -> float:
        mu, gamma, rho, att, de, hta = unpack(p)
        log_lam = mu + home_on * (gamma + hta[h]) + att[h] - de[a]
        log_mu = mu + att[a] - de[h]
        lam, mu_ = np.exp(log_lam), np.exp(log_mu)
        ll = np.log(_tau(x, y, lam, mu_, rho)) + x * log_lam - lam + y * log_mu - mu_
        penalty = 0.5 * shrink * float(np.sum(hta**2)) + cfg.ridge * float(np.sum(p[3:] ** 2))
        return float(-np.sum(w * ll) + penalty)

    mean_goals = max((x.mean() + y.mean()) / 2.0, 0.1)
    size = base + n_groups
    p0 = np.zeros(size)
    p0[0], p0[1], p0[2] = np.log(mean_goals), 0.25, -0.05
    bounds = [(-3, 3), (-1, 1.5), (-0.3, 0.3)] + [(-3, 3)] * (size - 3)
    res = minimize(nll, p0, method="L-BFGS-B", bounds=bounds, options={"maxiter": 500})
    mu, gamma, rho, att, de, hta = unpack(res.x)
    return DCParams(
        teams=teams,
        attack={t: float(att[i]) for t, i in idx.items()},
        defence={t: float(de[i]) for t, i in idx.items()},
        home_adv={t: float(gamma + hta[i]) for t, i in idx.items()},
        mu=float(mu),
        gamma=float(gamma),
        rho=float(rho),
    )


def fit(
    matches: pd.DataFrame,
    as_of,
    season_start=None,
    cfg: FootballModelConfig | None = None,
) -> DCParams:
    """Odhadne parametry jedne ligy k datu `as_of` (zapasy z toho dne se nepouziji).

    Pokud je zadan `season_start`, tymy s mene nez cold_start_matches zapasy
    v aktualni sezone dostanou smes: prior (minula sezona) a aktualni odhad.
    """
    cfg = cfg or SETTINGS.football
    as_of = _to_date(as_of)
    df = _prepare(matches, as_of, cfg)
    full = _fit_raw(df, cfg)
    if season_start is None:
        return full

    season_start = _to_date(season_start)
    current = df[df["date"] >= season_start]
    played = pd.concat([current["home"], current["away"]]).value_counts().to_dict()
    cold = [t for t in full.teams if played.get(t, 0) < cfg.cold_start_matches]
    before = df[df["date"] < season_start]
    if not cold or len(before) == 0:
        return full
    try:
        prior = _fit_raw(before, cfg)
    except ModelError:
        return full

    w = cfg.cold_start_prior_weight
    # Novacek bez historie: prior = prumer tri nejslabsich tymu minule sezony.
    weakest_att = float(np.mean(sorted(prior.attack.values())[:3]))
    weakest_def = float(np.mean(sorted(prior.defence.values())[:3]))
    attack, defence, home_adv = dict(full.attack), dict(full.defence), dict(full.home_adv)
    for t in cold:
        p_att = prior.attack.get(t, weakest_att)
        p_def = prior.defence.get(t, weakest_def)
        p_hta = prior.home_adv.get(t, prior.gamma)
        attack[t] = w * p_att + (1 - w) * full.attack[t]
        defence[t] = w * p_def + (1 - w) * full.defence[t]
        home_adv[t] = w * p_hta + (1 - w) * full.home_adv[t]
    return DCParams(full.teams, attack, defence, home_adv, full.mu, full.gamma, full.rho)


def fit_joint(
    domestic: dict[str, pd.DataFrame],
    cups: pd.DataFrame,
    as_of,
    cfg: FootballModelConfig | None = None,
    cup_name_map=None,
) -> tuple[DCParams, dict[str, float]]:
    """Spolecny odhad ligovych a pohatovych zapasu. Vraci parametry a offsety sily lig.

    `domestic`: liga -> zapasy (jmena tymu z football-data.co.uk). `cups`: pohary (jmena z API).
    `cup_name_map(name, known_teams)` prevede nazev z poharu na ligovy; tymy bez shody patri do skupiny OTHER.
    """
    cfg = cfg or SETTINGS.football
    as_of = _to_date(as_of)
    leagues = sorted(domestic)
    group_of = {lg: i for i, lg in enumerate(leagues)}
    other = len(leagues)
    team_group: dict[str, int] = {}
    frames = []
    for lg in leagues:
        d = domestic[lg]
        frames.append(d.loc[:, list(REQUIRED_COLUMNS)])
        for t in set(d["home"]) | set(d["away"]):
            team_group[t] = group_of[lg]
    known = list(team_group)
    c = cups.loc[:, list(REQUIRED_COLUMNS)].copy()
    if cup_name_map is not None:
        for col in ("home", "away"):
            c[col] = c[col].map(lambda n: cup_name_map(n, known) or n)
    for t in set(c["home"]) | set(c["away"]):
        team_group.setdefault(t, other)
    frames.append(c)
    df = _prepare(pd.concat(frames, ignore_index=True), as_of, cfg)
    params = _fit_raw(df, cfg, team_group={t: g for t, g in team_group.items() if t in set(df["home"]) | set(df["away"])})
    # offset ligy = prumerna sila (utok+obrana) jejich tymu proti prumeru vsech tymu
    strength = {t: params.attack[t] + params.defence[t] for t in params.teams}
    overall = float(np.mean(list(strength.values())))
    names = leagues + ["OTHER"]
    offsets = {}
    for g, name in enumerate(names):
        vals = [strength[t] for t in params.teams if team_group.get(t) == g]
        if vals:
            offsets[name] = float(np.mean(vals) - overall)
    return params, offsets


def score_matrix(lam: float, mu_: float, rho: float, max_goals: int | None = None) -> np.ndarray:
    """Matice P(domaci = i, hoste = j) s korekci nizkych skore."""
    k = (max_goals or SETTINGS.football.max_goals) + 1
    goals = np.arange(k)
    m = np.outer(poisson.pmf(goals, lam), poisson.pmf(goals, mu_))
    m[0, 0] *= max(1.0 - lam * mu_ * rho, 1e-10)
    m[0, 1] *= max(1.0 + lam * rho, 1e-10)
    m[1, 0] *= max(1.0 + mu_ * rho, 1e-10)
    m[1, 1] *= max(1.0 - rho, 1e-10)
    return m / m.sum()


def match_matrix(params: DCParams, home: str, away: str) -> np.ndarray:
    lam, mu_ = params.expected_goals(home, away)
    return score_matrix(lam, mu_, params.rho)


def probs_1x2(m: np.ndarray) -> tuple[float, float, float]:
    home = float(np.tril(m, -1).sum())
    draw = float(np.trace(m))
    away = float(np.triu(m, 1).sum())
    return home, draw, away


def prob_over(m: np.ndarray, line: float = 2.5) -> float:
    i, j = np.indices(m.shape)
    return float(m[(i + j) > line].sum())


def _ah_single(m: np.ndarray, line: float, side: str) -> tuple[float, float, float]:
    """(vyhra, vraceni, prohra) pro celou nebo pulovou linii z pohledu `side`."""
    i, j = np.indices(m.shape)
    diff = (i - j) if side == "home" else (j - i)
    adj = diff + line
    return float(m[adj > 1e-9].sum()), float(m[np.abs(adj) <= 1e-9].sum()), float(m[adj < -1e-9].sum())


def asian_handicap_ev(m: np.ndarray, line: float, odds: float, side: str = "home") -> float:
    """EV sazky na asijsky handicap (vcetne ctvrtinovych linii) na jednotku vkladu."""
    if side not in ("home", "away"):
        raise ModelError("side musi byt 'home' nebo 'away'")
    quarter = abs(line * 4 - round(line * 4)) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9
    lines = (line - 0.25, line + 0.25) if quarter else (line,)
    ev = 0.0
    for ln in lines:
        win, _, lose = _ah_single(m, ln, side)
        ev += (win * (odds - 1.0) - lose) / len(lines)
    return ev


def equivalent_probability(ev: float, odds: float) -> float:
    """Pravdepodobnost, ktera by pri danem kurzu dala stejne EV (pro trhy s vracenim)."""
    return (ev + 1.0) / odds


def market_probabilities(
    params: DCParams, home: str, away: str, scale: tuple[float, float] = (1.0, 1.0)
) -> dict[str, float]:
    """Pravdepodobnosti hlavnich trhu pro jeden zapas. `scale` = nasobky golu (domaci, hoste) z kontextu."""
    lam, mu_ = params.expected_goals(home, away)
    lam, mu_ = lam * scale[0], mu_ * scale[1]
    m = score_matrix(lam, mu_, params.rho)
    p1, px, p2 = probs_1x2(m)
    i, j = np.indices(m.shape)
    out = {
        "lambda_home": lam,
        "lambda_away": mu_,
        "1X2:1": p1,
        "1X2:X": px,
        "1X2:2": p2,
        "DC:1X": p1 + px,
        "DC:X2": px + p2,
        "DC:12": p1 + p2,
        "DNB:home": p1 / (p1 + p2),
        "DNB:away": p2 / (p1 + p2),
    }
    for line in (1.5, 2.5, 3.5):
        over = prob_over(m, line)
        out[f"OU_{line:g}:over"], out[f"OU_{line:g}:under"] = over, 1.0 - over
    btts = float(m[1:, 1:].sum())
    out["BTTS:yes"], out["BTTS:no"] = btts, 1.0 - btts
    for line in (0.5, 1.5, 2.5):
        th = float(m[i > line].sum())
        ta = float(m[j > line].sum())
        out[f"TTH_{line:g}:over"], out[f"TTH_{line:g}:under"] = th, 1.0 - th
        out[f"TTA_{line:g}:over"], out[f"TTA_{line:g}:under"] = ta, 1.0 - ta
    out["AH_-1.5:home"] = float(m[(i - j) >= 2].sum())
    out["AH_+1.5:away"] = 1.0 - out["AH_-1.5:home"]
    return out
