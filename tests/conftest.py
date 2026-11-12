from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from tipsbot.models.football import score_matrix
from tipsbot.storage import Ledger, make_session_factory


def simulate_league(n_teams=10, rounds=6, seed=1, start=date(2024, 8, 1), gamma=0.30, rho=-0.08):
    """Synteticka liga se znamymi silami tymu."""
    rng = np.random.default_rng(seed)
    teams = [f"T{i:02d}" for i in range(n_teams)]
    att = rng.normal(0, 0.30, n_teams)
    att -= att.mean()
    de = rng.normal(0, 0.25, n_teams)
    de -= de.mean()
    mu = np.log(1.25)
    rows, day = [], start
    for _ in range(rounds):
        for i in range(n_teams):
            for j in range(n_teams):
                if i == j:
                    continue
                lam = np.exp(mu + gamma + att[i] - de[j])
                mu_ = np.exp(mu + att[j] - de[i])
                m = score_matrix(lam, mu_, rho)
                k = rng.choice(m.size, p=m.ravel())
                hg, ag = divmod(k, m.shape[1])
                rows.append({"date": day, "home": teams[i], "away": teams[j], "home_goals": hg, "away_goals": ag})
            day += timedelta(days=4)
    truth = {"teams": teams, "att": dict(zip(teams, att)), "def": dict(zip(teams, de)), "gamma": gamma, "rho": rho}
    return pd.DataFrame(rows), truth


@pytest.fixture(scope="session")
def league():
    return simulate_league()


@pytest.fixture
def ledger():
    factory = make_session_factory("sqlite:///:memory:")
    with factory() as session:
        led = Ledger(session)
        led.deposit(10_000)
        yield led
