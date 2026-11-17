"""Rohy a karty: kolik jich v zapase padne.

Kazdy tym ma dva nasobky prumeru ligy: kolik rohu (karet) sam ma a kolik jich ma souper proti nemu. Uci se
z odehranych zapasu s casovym utlumem a stazenim k prumeru ligy (malo zapasu = blizko prumeru). U karet se
navic pocita s rozhodcim: nekdo piska prisneji, nekdo mirneji (taky stazeno k prumeru, dokud nema dost zapasu).
Pocet za zapas ma negativni binomicke rozdeleni (rohy i karty kolisaji vic, nez by odpovidalo Poissonovi).

Karty = zlute + cervene, kazda za 1 (tak je pocitaji data football-data.co.uk).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
from scipy.stats import nbinom

CORNER_LINES = (7.5, 8.5, 9.5, 10.5, 11.5, 12.5)
CARD_LINES = (2.5, 3.5, 4.5, 5.5, 6.5)
KINDS = {"ROHY": ("home_corners", "away_corners", CORNER_LINES), "KARTY": ("home_cards", "away_cards", CARD_LINES)}


@dataclass(frozen=True)
class PropsConfig:
    xi: float = 0.0038  # casovy utlum (polocas ~180 dni), jako u golu
    # kolik "prumernych zapasu" se primicha k datum tymu; zpetny test 2025/26 (2 656 zapasu): 20 lepsi nez 5 i 10
    team_prior: float = 20.0
    ref_prior: float = 15.0  # totez pro rozhodciho
    use_referee: bool = True


DEFAULT = PropsConfig()


@dataclass
class CountModel:
    kind: str
    home_mean: float
    away_mean: float
    own: dict[str, float]  # tym -> nasobek vlastnich rohu/karet
    opp: dict[str, float]  # tym -> nasobek rohu/karet soupere proti nemu
    ref: dict[str, float] = field(default_factory=dict)  # rozhodci -> nasobek celkoveho poctu
    r: float = 50.0  # rozptyl (mensi = vic kolisa)
    n: int = 0

    def means(self, home: str, away: str, referee: str | None = None) -> tuple[float, float]:
        rf = self.ref.get(referee_key(referee), 1.0) if referee else 1.0
        mh = self.home_mean * self.own.get(home, 1.0) * self.opp.get(away, 1.0) * rf
        ma = self.away_mean * self.own.get(away, 1.0) * self.opp.get(home, 1.0) * rf
        return mh, ma

    def p_over(self, total_mean: float, line: float) -> float:
        p = self.r / (self.r + total_mean)
        return float(nbinom.sf(int(np.floor(line)), self.r, p))


_LETTERS = str.maketrans({"ı": "i", "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "ß": "ss"})


def referee_key(name) -> str:
    """'Michael Oliver, England' i 'M Oliver' -> 'm oliver' (zdroje pisi jmena ruzne)."""
    if not isinstance(name, str) or not name.strip():
        return ""
    s = unicodedata.normalize("NFKD", name.split(",")[0].translate(_LETTERS)).encode("ascii", "ignore").decode().lower()
    parts = [p for p in s.replace(".", " ").split() if p]
    if not parts:
        return ""
    return parts[-1] if len(parts) == 1 else f"{parts[0][0]} {parts[-1]}"


def fit(df: pd.DataFrame, kind: str, today: date, cfg: PropsConfig = DEFAULT) -> CountModel | None:
    """Model pro 'ROHY' nebo 'KARTY' ze zapasu pred `today`. None, kdyz data chybi."""
    hcol, acol, _ = KINDS[kind]
    if hcol not in df or acol not in df:
        return None
    d = df[(df["date"] < today)].dropna(subset=[hcol, acol])
    if len(d) < 100:
        return None
    age = np.array([(today - x).days for x in d["date"]], dtype=float)
    w = np.exp(-cfg.xi * age)
    h, a = d[hcol].to_numpy(float), d[acol].to_numpy(float)
    home, away = d["home"].to_numpy(), d["away"].to_numpy()
    refs = (np.array([referee_key(x) for x in d["referee"]]) if cfg.use_referee and kind == "KARTY" and "referee" in d
            else np.full(len(d), ""))
    mh, ma = float(np.sum(w * h) / np.sum(w)), float(np.sum(w * a) / np.sum(w))
    teams = sorted(set(home) | set(away))
    own = dict.fromkeys(teams, 1.0)
    opp = dict.fromkeys(teams, 1.0)
    ref: dict[str, float] = {}
    k = cfg.team_prior
    for _ in range(6):
        rf = np.array([ref.get(x, 1.0) for x in refs])
        oa = np.array([opp[t] for t in away])
        oh = np.array([opp[t] for t in home])
        exp_h, exp_a = mh * oa * rf, ma * oh * rf  # bez vlastniho nasobku tymu
        own = _shrunk(teams, _by_team(home, w * h, w * exp_h), _by_team(away, w * a, w * exp_a), k * (mh + ma) / 2)
        sh = np.array([own[t] for t in home])
        sa = np.array([own[t] for t in away])
        # souper: rohy/karty, ktere proti tymu mel souper
        opp = _shrunk(teams, _by_team(away, w * h, w * mh * sh * rf), _by_team(home, w * a, w * ma * sa * rf), k * (mh + ma) / 2)
        if refs.any():
            oa = np.array([opp[t] for t in away])
            oh = np.array([opp[t] for t in home])
            base = mh * sh * oa + ma * sa * oh
            kr = cfg.ref_prior * (mh + ma)
            obs = pd.Series(w * (h + a)).groupby(refs).sum()
            exp = pd.Series(w * base).groupby(refs).sum()
            ref = {r: float((obs[r] + kr) / (exp[r] + kr)) for r in obs.index if r}
    model = CountModel(kind, mh, ma, own, opp, ref, n=len(d))
    mu = np.array([sum(model.means(x, y)) * ref.get(z, 1.0) for x, y, z in zip(home, away, refs)])
    tot = h + a
    excess = float(np.sum(w * ((tot - mu) ** 2 - mu)))
    model.r = float(np.clip(np.sum(w * mu ** 2) / excess, 2.0, 200.0)) if excess > 0 else 200.0
    return model


def _shrunk(teams, first: tuple[dict, dict], second: tuple[dict, dict], prior: float) -> dict[str, float]:
    """(skutecnost + prior) / (ocekavani + prior) ze zapasu doma i venku."""
    return {t: (first[0].get(t, 0) + second[0].get(t, 0) + prior) / (first[1].get(t, 0) + second[1].get(t, 0) + prior)
            for t in teams}


def _by_team(teams: np.ndarray, obs: np.ndarray, exp: np.ndarray) -> tuple[dict, dict]:
    s = pd.DataFrame({"t": teams, "o": obs, "e": exp}).groupby("t").sum()
    return s["o"].to_dict(), s["e"].to_dict()


def probabilities(models: dict[str, CountModel], home: str, away: str, referee: str | None = None) -> dict[str, float]:
    """{"ROHY_9.5:over": p, ...} pro vsechny linie; prumery jsou v klicich 'ROHY_mean', 'KARTY_mean'."""
    out: dict[str, float] = {}
    for kind, m in models.items():
        mh, ma = m.means(home, away, referee if kind == "KARTY" else None)
        out[f"{kind}_mean"] = mh + ma
        for line in KINDS[kind][2]:
            p = m.p_over(mh + ma, line)
            out[f"{kind}_{line:g}:over"], out[f"{kind}_{line:g}:under"] = p, 1.0 - p
    return out


def fit_all(df: pd.DataFrame, today: date, cfg: PropsConfig = DEFAULT) -> dict[str, CountModel]:
    return {k: m for k in KINDS if (m := fit(df, k, today, cfg)) is not None}


def walk_forward(df: pd.DataFrame, start: date, cfg: PropsConfig = DEFAULT, refit_days: int = 14) -> pd.DataFrame:
    """Zpetny test: model se uci jen ze zapasu pred kazdym blokem `refit_days`, predpovida dalsi zapasy."""
    test = df[df["date"] >= start].sort_values("date")
    rows = []
    day = start
    while day <= (test["date"].max() if len(test) else start):
        block = test[(test["date"] >= day) & (test["date"] < date.fromordinal(day.toordinal() + refit_days))]
        if len(block):
            models = fit_all(df, day, cfg)
            for _, r in block.iterrows():
                if not models:
                    break
                ref = r.get("referee") if "referee" in r else None
                p = probabilities(models, r["home"], r["away"], ref if isinstance(ref, str) else None)
                p.update(date=r["date"], home=r["home"], away=r["away"])
                for kind, (hc, ac, _) in KINDS.items():
                    p[f"{kind}_total"] = r[hc] + r[ac] if hc in r and pd.notna(r[hc]) and pd.notna(r[ac]) else np.nan
                rows.append(p)
        day = date.fromordinal(day.toordinal() + refit_days)
    return pd.DataFrame(rows)
