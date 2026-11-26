import os
import tempfile

# Testy nikdy nesahaji do skutecne slozky data (klice, databaze, stazena data): vychozi datovy adresar
# bota i Profilu 4 se cte z promenne pri importu, proto se nastavuje pred importem tipsbot.
os.environ["TIPSBOT_DATA_DIR"] = tempfile.mkdtemp(prefix="tipsbot-test-data-")
# Databaze hlavniho bota ma vlastni promennou (vychozi je relativni "sqlite:///data/tipsbot.db"): bez tohoto radku
# test spusteny z korene repozitare zapisoval do PRODUKCNI data/tipsbot.db a cetl z ni skutecny API klic
# (zjisteno 26.11.2026 – closing.tick v test_profil4_calibration poslal skutecny pozadavek /events).
os.environ["TIPSBOT_DATABASE_URL"] = "sqlite:///" + os.path.join(os.environ["TIPSBOT_DATA_DIR"], "tipsbot.db").replace("\\", "/")

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


PAID_API_HOSTS = ("the-odds-api.com", "api-sports.io")


@pytest.fixture(autouse=True)
def _no_paid_api(monkeypatch):
    """Zadny test nesmi poslat skutecny pozadavek na placena API (kredity, kvota). Mocky requests.get funguji dal."""
    import requests.adapters

    orig = requests.adapters.HTTPAdapter.send

    def send(self, request, *args, **kwargs):
        if any(h in (request.url or "") for h in PAID_API_HOSTS):
            raise AssertionError(f"test poslal skutečný požadavek na placené API: {request.url.split('?')[0]}")
        return orig(self, request, *args, **kwargs)

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)


@pytest.fixture(autouse=True)
def _follow_redirects(monkeypatch):
    """Panel po odeslani formulare presmerovava (POST -> redirect -> GET); testy ctou vysledne stranky jako prohlizec."""
    from flask.testing import FlaskClient

    orig = FlaskClient.open

    def open_(self, *args, **kwargs):
        kwargs.setdefault("follow_redirects", True)
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(FlaskClient, "open", open_)
