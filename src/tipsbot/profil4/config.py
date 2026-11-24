"""Konfigurace Profilu 4 – Hybridni expert.

Vsechny prahy a vahy jsou vychozi nastaveni, ne univerzalni pravda: meni se zde (nebo promennymi prostredi)
a kazda ulozena predikce si pamatuje otisk konfigurace (`config_hash`), aby slo vysledky porovnat podle verzi.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

MODEL_VERSION = "p4-1.0.0"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "ne")


@dataclass(frozen=True)
class Competition:
    league_id: int  # id souteze v API-Football
    name: str
    priority: int = 2  # 1 = nejvyssi
    enabled: bool = True
    bot_league: str = ""  # klic souteze ve zbytku bota (vaha modelu ze zpetneho testu)


DEFAULT_COMPETITIONS: tuple[Competition, ...] = (
    Competition(39, "Premier League", 1, True, "ENG1"),
    Competition(140, "La Liga", 1, True, "ESP1"),
    Competition(78, "Bundesliga", 1, True, "GER1"),
    Competition(135, "Serie A", 1, True, "ITA1"),
    Competition(61, "Ligue 1", 1, True, "FRA1"),
    Competition(94, "Liga Portugal", 2, True, "POR1"),
    Competition(345, "Chance Liga", 2, True, "CZE1"),
    Competition(2, "Liga mistrů", 1, True, "UCL"),
    Competition(3, "Evropská liga", 2, True, "UEL"),
    Competition(848, "Konferenční liga", 3, True, "UECL"),
)


@dataclass(frozen=True)
class ApiConfig:
    base_url: str = "https://v3.football.api-sports.io"
    key_env: str = "TIPSBOT_API_FOOTBALL_KEY"
    timeout: float = 20.0
    max_retries: int = 3
    backoff_base: float = 1.5  # cekani 1.5 s, 3 s, 6 s ...
    backoff_max: float = 30.0
    timezone: str = "Europe/Prague"


@dataclass(frozen=True)
class BudgetConfig:
    # Denni limit: kdyz API posle hlavicku x-ratelimit-requests-limit, plati ta. Jinak tato hodnota.
    daily_limit_fallback: int = 100
    # Kolik procent denniho limitu musi zbyt, aby smel projit pozadavek dane priority.
    reserve_pct: dict[str, float] = field(default_factory=lambda: {"HIGH": 0.02, "MEDIUM": 0.15, "LOW": 0.40})
    max_per_fixture_per_day: int = 12  # jeden zapas nesmi spotrebovat vic
    max_per_run: int = 60  # jeden sken (denni/nadchazejici) nejvys tolik pozadavku
    per_minute_fallback: int = 10
    max_wait_for_minute: float = 65.0  # pri vycerpanem minutovem limitu se ceka nejvys tolik sekund


@dataclass(frozen=True)
class CacheConfig:
    # TTL v sekundach podle endpointu (orientacne podle doporucene frekvence z dokumentace API-Football).
    ttl: dict[str, int] = field(default_factory=lambda: {
        "leagues": 7 * 86400,
        "odds/bookmakers": 7 * 86400,
        "odds/bets": 7 * 86400,
        "fixtures:season": 12 * 3600,  # cela sezona souteze (vysledky)
        "fixtures:season_past": 7 * 86400,
        "fixtures:date": 3 * 3600,
        "fixtures:ids": 15 * 60,
        "standings": 12 * 3600,
        "teams/statistics": 24 * 3600,
        "fixtures/headtohead": 7 * 86400,
        "injuries": 4 * 3600,  # API aktualizuje zhruba po 4 h
        "fixtures/lineups": 10 * 60,
        "predictions": 12 * 3600,
        "odds": 3 * 3600,  # predzapasove kurzy se v API obnovuji zhruba po 3 h
        "sidelined": 24 * 3600,
    })
    empty_ttl: int = 15 * 60  # prazdna odpoved (napr. sestavy jeste nejsou) se drzi kratce


@dataclass(frozen=True)
class OddsConfig:
    # Kurz starsi nez tolik minut se nesmi prezentovat jako aktualni.
    max_age_api_min: int = 6 * 60  # API obnovuje po ~3 h; dvojnasobek = zastarale
    max_age_manual_min: int = 120  # rucne opsany kurz plati 2 h
    # kurzy pred vykopem pro CLV: u zapasu s tipy/predikcemi se stahnou v techto bodech (minut pred vykopem),
    # kazdy bod 1 pozadavek; automat bota to dela sam, bez skenu (closing.py)
    # Faze 3.2.2 (jen doporuceni, neprovedeno): bod 75 min nikdy nedava zaviraci kurz (plati pozorovani <= 20 min);
    # posun na 5 min by drzel 3 pozadavky na zapas, ale meni logiku posledniho bodu (NHL, zahranicni overeni).
    closing_slots_min: tuple[int, ...] = (75, 30, 10)
    # skutecny zaviraci kurz: pozorovan nejvys 20 min pred vykopem A posledni aktualizace u poskytovatele nejvys
    # 60 min pred vykopem. Jinak jde jen o posledni pozorovany kurz, pripadne zastaraly (poskytovatel neaktualizoval).
    closing_observed_within_min: int = 20
    closing_max_source_age_min: int = 60
    # Ostre kancelare pro trzni model (kdyz jsou v datech, maji prednost pred konsensem vsech).
    sharp_books: tuple[str, ...] = ("pinnacle",)
    # Ceske kancelare: kurzy jen z API (kdyz tam jsou), rucne nebo importem. Nikdy se nenahrazuji jinou kanceleri.
    czech_books: tuple[str, ...] = ("tipsport", "fortuna", "betano", "chance", "betarena")
    devig_method: str = "multiplicative"  # multiplicative | shin | power (puvodni i ocistene se uchovavaji vzdy)


@dataclass(frozen=True)
class ModelConfig:
    model_weight: float = 0.5  # vaha vlastniho modelu v kombinovanem modelu C (zbytek trh)
    # Zpetny test bota (11/2026) ukazal, ze u lig je lepsi mala vaha modelu (0,10). Kdyz je True, pouzije se
    # vaha ze zpetneho testu pro soutez (config.ValueConfig.league_model_weight), jinak model_weight.
    use_backtested_weights: bool = True
    min_matches_model_a: int = 60  # mene odehranych zapasu v datech => model A nedostupny
    # Model D: nejistota parametru (gama smes). Tvar k: variacni koeficient = 1/sqrt(k).
    gamma_k_good: float = 25.0  # dost dat: +-20 %
    gamma_k_poor: float = 9.0  # malo dat: +-33 %
    lineup_shock: float = 0.07  # nepotvrzena sestava: test s -7 % golu favorita


@dataclass(frozen=True)
class MonteCarloConfig:
    simulations: int = 50_000
    seed: int = 20261119
    ci_z: float = 1.96


@dataclass(frozen=True)
class DecisionConfig:
    ev_min: float = 0.03  # minimalni EV (kombinovany model C) pro APPROVED BET
    ev_min_conservative: float = 0.0  # EV v konzervativnim modelu D nesmi byt zaporne
    robustness_min: float = 60.0
    data_quality_min: float = 0.60
    max_model_gap: float = 0.10  # model A a trh B se nesmi rozchazet vic (absolutne)
    min_odds: float = 1.20
    max_odds: float = 5.00
    value_min_odds: float = 1.50  # kategorie B – Value tip
    sensible_min_odds: float = 1.25  # kategorie A – nejrozumnejsi tip
    # kategorie A musi byt opravdu rozumna sazka: aspon 45% sance na vyhru (vraceni vkladu napul) a kurz do 2,30;
    # jinak A neni (driv se A stal i handicap @ 4.96 s 17 % jen proto, ze nic jineho schvalene nebylo)
    sensible_min_p: float = 0.45
    sensible_max_odds: float = 2.30
    suspicious_ev: float = 0.25  # vys = nejspis chyba kurzu/trhu => jen WATCH s upozornenim
    watch_ev: float = 0.0  # WATCH, kdyz EV(C) > watch_ev ale neco chybi
    # kontrolni vrstva (integrity.py)
    max_market_edge: float = 0.10  # kurz o vic nez 10 % nad ferovou cenou trhu = extremni, nutne nezavisle potvrzeni
    confirm_tolerance: float = 0.05  # potvrzeni = jina kancelar s kurzem nejvys o 5 % nizsim
    max_logit_gap: float = 0.5  # model A vs trh B v log-sancich (pomer sanci ~1.65) – hlavne u outsideru
    min_book_overround: float = -0.01  # soucet 1/kurz trhu jedne kancelare pod 99 % = kurzy nejsou jeden trh
    max_book_overround: float = 0.25  # marze nad 25 % = chybny nebo neuplny trh
    # Faze 3.2.2 – pojistka Modelu A (guards.py, zduvodneni z dat tamtez): pri spusteni NO BET pro cely zapas
    model_a_guard_logit: float = 3.0  # rozdil log-sanci A vs trh na 1X2 (pomer sanci 20×)
    model_a_guard_p: float = 0.99  # bez ceny trhu: Model A dava vysledku 1X2 aspon tolik


@dataclass(frozen=True)
class HunterConfig:
    quick_ev_min: float = 0.02  # kandidat na hlubokou analyzu
    quick_gap_min: float = 0.06  # nebo kdyz se model a trh rozchazeji aspon o tolik
    deep_max: int = 5  # kolik zapasu nejvys projde hlubokou analyzou v jednom skenu
    upcoming_hours: int = 48
    lineup_window_min: int = 75  # sestavy se stahuji jen tolik minut pred vykopem
    fetch_predictions: bool = True
    fetch_h2h: bool = True
    fetch_team_stats: bool = True


@dataclass(frozen=True)
class RobustnessWeights:
    """Slozky skore robustnosti (soucet 100). Skore NENI pravdepodobnost vyhry."""

    model_agreement: float = 20.0
    ev_stability: float = 25.0
    data_quality: float = 15.0
    odds_freshness: float = 10.0
    lineup_certainty: float = 10.0
    parameter_sensitivity: float = 10.0
    stress_worst: float = 10.0


@dataclass(frozen=True)
class IntelligenceConfig:
    """Faze 3 – Intelligence & Calibration. Jen diagnostika a razeni: NEMENI pravdepodobnosti, EV ani rozhodnuti.

    Vahy kvality dat a Opportunity Rankingu jsou EXPERIMENTALNI vychozi hodnoty (soucet 100) – zatim nejsou
    overene na historickych datech; meni se jen rucne a po overeni (viz docs/profil4-faze3-intelligence-calibration.md)."""

    # Kvalita dat (Quality of Data Score 0–100)
    q_odds_fresh: float = 20.0  # stari kurzu vuci limitu
    q_provider_lag: float = 10.0  # zpozdeni stazeni za aktualizaci u poskytovatele
    q_counterside: float = 15.0  # protistrana trhu ze stejne aktualizace
    q_books: float = 15.0  # pocet kancelari s aktualnim kurzem na tentyz vyber
    q_consistency: float = 10.0  # rozptyl kurzu mezi kancelarmi
    q_stats: float = 15.0  # vysledky a statistiky pro model
    q_lineups: float = 5.0
    q_injuries: float = 5.0
    q_completeness: float = 5.0  # ostatni zdroje (tabulka, zapas)
    # Opportunity Ranking 0–100 (NENI pravdepodobnost vyhry)
    r_conservative_ev: float = 25.0
    r_data_quality: float = 20.0
    r_robustness: float = 15.0
    r_model_market: float = 15.0
    r_freshness: float = 10.0
    r_confirmation: float = 5.0
    r_risks: float = 5.0
    r_calibration: float = 5.0
    r_ev_floor: float = -0.05  # konzervativni EV, ktere dava 0 bodu
    r_ev_cap: float = 0.10  # konzervativni EV, ktere dava plny pocet bodu
    # kalibrace
    min_matches_verdict: int = 100  # pod tim se o kalibraci a srovnani s trhem nerozhoduje (31 zapasu x 100 vyberu
    # neni 3 100 nezavislych vzorku – vysledky jednoho zapasu jsou silne korelovane)
    min_bin_matches: int = 15  # kalibracni pasmo s mene zapasy se slouci se sousednim
    well_calibrated_tol: float = 0.03  # |skutecnost − odhad| do 3 p.b. (cely interval) = prukazne dobre kalibrovany
    outlier_pct: float = 0.10  # kurz o vic nez 10 % nad medianem kancelari = extremne odlisny
    model_market_gap: float = 0.10  # model vs trh o vic nez 10 p.b. = rozchod s trhem


@dataclass(frozen=True)
class Profil4Config:
    enabled: bool = _env_bool("TIPSBOT_PROFIL4", True)
    data_dir: Path = Path(os.environ.get("TIPSBOT_DATA_DIR", "data"))
    db_name: str = "profil4.db"
    competitions: tuple[Competition, ...] = DEFAULT_COMPETITIONS
    api: ApiConfig = field(default_factory=ApiConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    odds: OddsConfig = field(default_factory=OddsConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    mc: MonteCarloConfig = field(default_factory=MonteCarloConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    hunter: HunterConfig = field(default_factory=HunterConfig)
    robustness: RobustnessWeights = field(default_factory=RobustnessWeights)
    intel: IntelligenceConfig = field(default_factory=IntelligenceConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_name

    def config_hash(self) -> str:
        """Kratky otisk nastaveni (bez cest) – uklada se ke kazde predikci."""
        d = asdict(self)
        d.pop("data_dir", None)
        d.pop("intel", None)  # diagnostika (Faze 3) nemeni predikce – otisk modelu zustava stejny
        raw = json.dumps(d, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode()).hexdigest()[:10]

    def with_(self, **kw) -> "Profil4Config":
        return replace(self, **kw)


P4 = Profil4Config()
