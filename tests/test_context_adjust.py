from tipsbot import context
from tipsbot.config import FootballModelConfig


def test_scale_neutral_without_context():
    assert context.football_scale(0, 0, 1.0, FootballModelConfig()) == (1.0, 1.0)


def test_scale_absences_hurt_own_attack_and_help_opponent():
    h, a = context.football_scale(3, 0, 1.0, FootballModelConfig())
    assert h < 1.0 < a


def test_scale_is_capped():
    cfg = FootballModelConfig()
    h, a = context.football_scale(50, 50, 1.0, cfg)
    assert h >= (1 - cfg.absence_cap) * 1.0 and h <= (1 + cfg.absence_cap)
    assert 0.9 < h < 1.1 and 0.9 < a < 1.1


def test_weather_scale_heavy_rain_and_wind():
    hourly = {"time": ["t"], "precipitation": [3.0], "wind_speed_10m": [5.0]}
    assert context.weather_scale(hourly, "t", 0.05) == 0.95
    hourly = {"time": ["t"], "precipitation": [0.0], "wind_speed_10m": [40.0]}
    assert context.weather_scale(hourly, "t", 0.05) == 0.95
    hourly = {"time": ["t"], "precipitation": [0.0], "wind_speed_10m": [5.0]}
    assert context.weather_scale(hourly, "t", 0.05) == 1.0
    assert context.weather_scale({}, "t", 0.05) == 1.0


def test_team_out_counts_only_certain_absences():
    ab = [
        context.Absence("A", "x", "Missing Fixture"),
        context.Absence("A", "y", "Questionable"),
        context.Absence("B", "z", "Out"),
    ]
    assert context.team_out_counts(ab) == {"A": 1, "B": 1}
