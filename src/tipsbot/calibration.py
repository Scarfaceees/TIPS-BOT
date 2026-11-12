"""Kalibrace pravdepodobnosti (Plattova skalace) a metriky kvality modelu."""

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
from scipy.optimize import minimize

_EPS = 1e-9


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, _EPS, 1 - _EPS)
    return np.log(p / (1 - p))


@dataclass(frozen=True)
class PlattScaler:
    """p_kalibrovane = sigmoid(a * logit(p) + b). a=1, b=0 je identita."""

    a: float = 1.0
    b: float = 0.0

    def apply(self, p):
        z = self.a * _logit(np.asarray(p, dtype=float)) + self.b
        out = 1.0 / (1.0 + np.exp(-z))
        return float(out) if np.ndim(p) == 0 else out


def fit_platt(probs: Sequence[float], outcomes: Sequence[int], min_samples: int = 200) -> PlattScaler:
    """Odhadne Plattovu skalaci. Pri malem vzorku vraci identitu."""
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if len(p) != len(y):
        raise ValueError("probs a outcomes musi mit stejnou delku")
    if len(p) < min_samples or y.min() == y.max():
        return PlattScaler()
    x = _logit(p)

    def nll(theta: np.ndarray) -> float:
        z = theta[0] * x + theta[1]
        return float(np.sum(np.logaddexp(0.0, z) - y * z))

    res = minimize(nll, np.array([1.0, 0.0]), method="L-BFGS-B", bounds=[(0.2, 3.0), (-2.0, 2.0)])
    return PlattScaler(float(res.x[0]), float(res.x[1]))


def normalize(probs: Sequence[float]) -> list[float]:
    s = float(sum(probs))
    return [float(x) / s for x in probs]


def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    """Binarni Brier score: prumer (p - y)^2."""
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    return float(np.mean((p - y) ** 2))


def log_loss(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    p = np.clip(np.asarray(probs, dtype=float), _EPS, 1 - _EPS)
    y = np.asarray(outcomes, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def sharpe_ratio(period_returns: Sequence[float], periods_per_year: float = 300.0) -> float:
    """Anualizovany Sharpe z vynosu za obdobi (typicky hraci dny, v podilu bankrollu)."""
    r = np.asarray(period_returns, dtype=float)
    if len(r) < 2:
        return 0.0
    sd = float(r.std(ddof=1))
    if sd == 0:
        return 0.0
    return float(r.mean() / sd * math.sqrt(periods_per_year))


def max_drawdown(equity: Sequence[float]) -> float:
    """Nejvetsi propad od vrcholu (kladne cislo, podil)."""
    e = np.asarray(equity, dtype=float)
    if len(e) == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    return float(np.max((peak - e) / peak))


# --- kalibrace 1X2 (po jednotlivych vysledcich) -------------------------------

OUTCOMES = ("1", "x", "2")


def fit_1x2_scalers(p1, px, p2, results, min_samples: int = 300) -> dict[str, PlattScaler]:
    """Plattova skalace zvlast pro vyhru domacich, remizu a vyhru hostu. results: 0/1/2."""
    res = np.asarray(results)
    cols = (p1, px, p2)
    return {o: fit_platt(cols[k], (res == k).astype(int), min_samples) for k, o in enumerate(OUTCOMES)}


def apply_1x2(scalers: dict[str, PlattScaler], p1: float, px: float, p2: float) -> tuple[float, float, float]:
    """Kalibrovane pravdepodobnosti 1X2 (znovu normalizovane na soucet 1)."""
    out = [scalers[o].apply(p) for o, p in zip(OUTCOMES, (p1, px, p2))]
    s = sum(out)
    return out[0] / s, out[1] / s, out[2] / s


def save_scalers(path: Path | str, scalers: dict[str, PlattScaler], meta: dict | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"scalers": {k: {"a": v.a, "b": v.b} for k, v in scalers.items()}, "meta": meta or {}}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_scalers(path: Path | str) -> dict[str, PlattScaler] | None:
    path = Path(path)
    if not path.exists():
        return None
    d = json.loads(path.read_text(encoding="utf-8"))["scalers"]
    return {k: PlattScaler(v["a"], v["b"]) for k, v in d.items()}
