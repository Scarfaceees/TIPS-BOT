"""Centralni konfigurace Tips Bota.

Hodnoty odpovidaji dokumentu "Konfigurace bota - odpovedi na 25 otazek".
Bot je poradce: sazku nepodava, tikety zapisuje uzivatel rucne.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Sport(str, Enum):
    FOOTBALL = "football"
    NHL = "nhl"


class Tier(str, Enum):
    """Likvidita trhu - urcuje prah EV."""

    MAJOR = "major"  # top 5 lig + NHL
    MINOR = "minor"  # Liga Portugal, Chance Liga


@dataclass(frozen=True)
class League:
    key: str
    name: str
    sport: Sport
    tier: Tier
    markets: tuple[str, ...]


FOOTBALL_MARKETS = ("1X2", "ASIAN_HANDICAP", "OU_2.5")
NHL_MARKETS = ("MONEYLINE", "PUCKLINE_1.5", "PUCKLINE_1.5R", "OU_5.5", "OU_6.5")

LEAGUES: dict[str, League] = {
    lg.key: lg
    for lg in (
        League("ENG1", "Premier League", Sport.FOOTBALL, Tier.MAJOR, FOOTBALL_MARKETS),
        League("ESP1", "La Liga", Sport.FOOTBALL, Tier.MAJOR, FOOTBALL_MARKETS),
        League("GER1", "Bundesliga", Sport.FOOTBALL, Tier.MAJOR, FOOTBALL_MARKETS),
        League("FRA1", "Ligue 1", Sport.FOOTBALL, Tier.MAJOR, FOOTBALL_MARKETS),
        League("ITA1", "Serie A", Sport.FOOTBALL, Tier.MAJOR, FOOTBALL_MARKETS),
        League("POR1", "Liga Portugal", Sport.FOOTBALL, Tier.MINOR, FOOTBALL_MARKETS),
        League("CZE1", "Chance Liga", Sport.FOOTBALL, Tier.MINOR, FOOTBALL_MARKETS),
        League("UCL", "Liga mistrů", Sport.FOOTBALL, Tier.MAJOR, FOOTBALL_MARKETS),
        League("UEL", "Evropská liga", Sport.FOOTBALL, Tier.MAJOR, FOOTBALL_MARKETS),
        League("UECL", "Konferenční liga", Sport.FOOTBALL, Tier.MINOR, FOOTBALL_MARKETS),
        League("UNL", "Liga národů UEFA", Sport.FOOTBALL, Tier.MINOR, FOOTBALL_MARKETS),
        League("NHL", "NHL", Sport.NHL, Tier.MAJOR, NHL_MARKETS),
    )
}


@dataclass(frozen=True)
class BookmakerConfig:
    # Ceske kancelare: zde se sazi, do ferove linie nevstupuji.
    betting_books: tuple[str, ...] = ("betano", "fortuna", "tipsport", "chance", "betarena")
    # Ostre zdroje pro ferovou linii a jejich vahy (bod 6).
    sharp_weights: dict[str, float] = field(
        default_factory=lambda: {"pinnacle": 0.50, "betfair_exchange": 0.30, "sbobet": 0.20}
    )
    min_sharp_sources: int = 1
    # Provize burzy - kurzy Betfairu se pred deviggingem prepocitaji na ciste.
    exchange_commission: dict[str, float] = field(
        default_factory=lambda: {"betfair_exchange": 0.05}
    )
    # Zdroj, vuci kteremu se meri CLV.
    clv_reference: str = "pinnacle"
    # Dokud nejsou automaticky dostupne kurzy ceskych kancelari, berou se jako
    # nabidky kurzy ostatnich (neostrych) kancelari z kurzoveho API. Tip pak slouzi
    # jako cil: u ceske kancelare vsadit jen pri kurzu >= minimalni kurz tipu.
    proxy_offers: bool = True
    proxy_exclude: tuple[str, ...] = ("betfair_ex_eu", "betfair_ex_uk", "matchbook", "smarkets")


@dataclass(frozen=True)
class TicketConfig:
    """Kombinovane tikety: max. 3 prilezitosti, standardne 2; kazda z jineho zapasu."""

    max_legs: int = 3
    preferred_legs: int = 2
    max_combined_odds: float = 9.0
    min_leg_prob: float = 0.50  # kazda prilezitost v tiketu
    min_combined_prob: float = 0.40  # sance na vyhru celeho tiketu
    top_n: int = 5
    # Cilovy tip (kdyz neni overena hodnota): sance modelu v tomto pasmu => realny ferovy kurz cca 1.4-1.7
    target_prob_min: float = 0.58
    target_prob_max: float = 0.72


@dataclass(frozen=True)
class DevigConfig:
    # Outsider = implikovana pravdepodobnost pod touto hranici (bod 3).
    longshot_prob: float = 0.20
    # Pokud se Shin a Power u outsidera lisi o vic nez tolik (relativne), bere se nizsi.
    longshot_tolerance: float = 0.03


@dataclass(frozen=True)
class ValueConfig:
    ev_min: dict[Tier, float] = field(
        default_factory=lambda: {Tier.MAJOR: 0.025, Tier.MINOR: 0.040}
    )
    # Casny tip (12-24 h pred zapasem) = dvojnasobek prahu (bod 7).
    early_ev_multiplier: float = 2.0
    min_odds: float = 1.45
    max_odds: float = 4.20
    # Vaha modelu pri michani s trzni ferovou linii; zbytek je trh.
    model_weight: float = 0.50
    # EV nad touto hranici je podezrele (chyba dat/kurzu) - tip se jen oznaci k rucni kontrole.
    ev_suspicious: float = 0.15
    # Model a trh se nesmi rozchazet o vic nez tolik (absolutne v pravdepodobnosti).
    max_model_market_gap: float = 0.08
    # Hlavni tip musi mit rozumnou sanci na vyhru (po smichani modelu a trhu).
    min_probability: float = 0.45
    # Arbitraz/hedging se navrhne jen nad timto cistym vynosem (bod 24).
    arb_min_yield: float = 0.03

    def ev_threshold(self, tier: Tier, early: bool = False) -> float:
        base = self.ev_min[tier]
        return base * self.early_ev_multiplier if early else base


@dataclass(frozen=True)
class RiskConfig:
    kelly_fraction: float = 0.125
    max_stake_pct: float = 0.02
    max_daily_exposure_pct: float = 0.15
    max_bets_per_match: int = 1
    stop_loss_drawdown: float = 0.15
    # Nejmensi vklad, ktery ma smysl doporucit (v mene bankrollu).
    min_stake: float = 20.0
    stake_rounding: float = 10.0


@dataclass(frozen=True)
class TimingConfig:
    early_window_hours: tuple[int, int] = (12, 24)
    # Bez prubezne obnovy: bezny tip lze vydat kdykoli od 12 h do 15 min pred vykopem.
    main_window_minutes: tuple[int, int] = (15, 720)
    # Bez prubezne obnovy: sken se spousti rucne nebo par krat za hraci den.
    odds_refresh_seconds: int = 6 * 3600
    # Tip plati do vykopu (nejdele tuto dobu); hlida ho minimalni kurz.
    tip_validity_seconds: int = 24 * 3600


@dataclass(frozen=True)
class FootballModelConfig:
    xi: float = 0.0038  # polocas ~180 dni
    max_history_seasons: int = 2
    max_goals: int = 10  # rozmer skorove matice
    # Domaci vyhoda po tymech (bod 9), pritahovana k ligovemu prumeru.
    team_home_advantage: bool = True
    home_advantage_shrinkage_matches: float = 20.0
    # Cold-start (bod 15).
    cold_start_matches: int = 8
    cold_start_prior_weight: float = 0.60
    # Kontext zapasu (zraneni, pocasi). Konzervativni odhady bez historickeho overeni
    # (neexistuji data o zranenich k backtestu) - proto male a se stropem.
    context_adjust: bool = True
    absence_attack_per_player: float = 0.015  # kazdy jisty vypadek snizi utok tymu o 1,5 %
    absence_defence_per_player: float = 0.010  # a zvysi golove ocekavani souperu o 1 %
    absence_cap: float = 0.08  # max. celkova uprava na jednu stranu
    weather_goal_factor: float = 0.05  # tezky dest nebo silny vitr: -5 % golu obou tymu


@dataclass(frozen=True)
class NHLModelConfig:
    mc_iterations: int = 5000
    mc_seed: int | None = None
    b2b_xg_penalty: float = 0.05  # jen B2B + cesta pres casova pasma
    cold_start_games: int = 8
    cold_start_prior_weight: float = 0.60
    ot_length_minutes: float = 5.0
    # Domaci led: nasobek ocekavanych golu domacich / hostu.
    home_factor: float = 1.035
    away_factor: float = 0.965
    # Vaha strelby (Fenwick) jako pomocneho signalu vedle xG (bod 19).
    shot_volume_weight: float = 0.20
    # GSAx brankare se stahuje k nule podle poctu celenych strel.
    goalie_regression_shots: float = 1500.0
    shots_against_per_60: float = 28.0
    # Spolecna slozka bivariatniho Poissona (korelace skore).
    regulation_covariance: float = 0.15
    # Hra 3 na 3: nasobek gólove intenzity proti hre 5 na 5.
    ot_scoring_multiplier: float = 1.9
    # Silnejsi tym ma v prodlouzeni mensi vyhodu nez v zakladni dobe.
    ot_strength_shrink: float = 0.6
    min_expected_goals: float = 0.8


@dataclass(frozen=True)
class ValidationConfig:
    backtest_seasons: int = 3
    max_brier: float = 0.20
    min_sharpe: float = 1.2
    calibration: str = "platt"
    min_bets_for_evaluation: int = 500


@dataclass(frozen=True)
class OddsApiConfig:
    """The Odds API (the-odds-api.com). Klic se cte z promenne TIPSBOT_ODDS_API_KEY."""

    base_url: str = "https://api.the-odds-api.com/v4"
    api_key: str = os.environ.get("TIPSBOT_ODDS_API_KEY", "")
    regions: str = "eu"
    # Kurzy se mezi skeny znovu pouziji (setri kredity); tlacitko v panelu umi vynutit nova data.
    cache_seconds: int = 600
    # 1 volani stoji (pocet trhu x pocet regionu) kreditu.
    markets: tuple[str, ...] = ("h2h", "totals")
    sport_keys: dict[str, str] = field(
        default_factory=lambda: {
            "ENG1": "soccer_epl",
            "ESP1": "soccer_spain_la_liga",
            "GER1": "soccer_germany_bundesliga",
            "ITA1": "soccer_italy_serie_a",
            "FRA1": "soccer_france_ligue_one",
            "POR1": "soccer_portugal_primeira_liga",
            "UCL": "soccer_uefa_champs_league",
            "UEL": "soccer_uefa_europa_league",
            "UECL": "soccer_uefa_europa_conference_league",
            "UNL": "soccer_uefa_nations_league",
            "NHL": "icehockey_nhl",
        }
    )
    # klic kancelare v API -> nazev ostreho zdroje v konfiguraci
    sharp_keys: dict[str, str] = field(
        default_factory=lambda: {
            "pinnacle": "pinnacle",
            "betfair_ex_eu": "betfair_exchange",
            "betfair_ex_uk": "betfair_exchange",
            "sbobet": "sbobet",
        }
    )


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.environ.get("TIPSBOT_DATA_DIR", "data"))
    database_url: str = os.environ.get("TIPSBOT_DATABASE_URL", "sqlite:///data/tipsbot.db")
    currency: str = "CZK"
    model_version: str = "0.1.0"
    bookmakers: BookmakerConfig = field(default_factory=BookmakerConfig)
    devig: DevigConfig = field(default_factory=DevigConfig)
    value: ValueConfig = field(default_factory=ValueConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    timing: TimingConfig = field(default_factory=TimingConfig)
    football: FootballModelConfig = field(default_factory=FootballModelConfig)
    nhl: NHLModelConfig = field(default_factory=NHLModelConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    odds_api: OddsApiConfig = field(default_factory=OddsApiConfig)
    tickets: TicketConfig = field(default_factory=TicketConfig)


SETTINGS = Settings()
