import datetime

from services.timezone import ASTANA_TZ
from services.weather import format_forecast, CONSENSUS_MODELS

NOW = datetime.datetime(2026, 1, 5, 8, 0, tzinfo=ASTANA_TZ)
DATE = "2026-01-05"
HOURS = [9, 12, 15, 18, 21]


def _hourly(codes, rain=70, temp=0.0, feels=None):
    """codes: список WMO-кодов на каждый час из HOURS (той же длины)."""
    times = [f"{DATE}T{h:02d}:00" for h in HOURS]
    feels_list = [feels if feels is not None else temp] * len(HOURS)
    return {
        "time": times,
        "temperature_2m": [temp] * len(HOURS),
        "apparent_temperature": feels_list,
        "precipitation_probability": [rain] * len(HOURS),
        "weather_code": codes,
        "wind_speed_10m": [10] * len(HOURS),
    }


def _data(codes, rain=70, temp=0.0, feels=None, current_code=0):
    return {
        "current": {"temperature_2m": temp, "apparent_temperature": temp, "weather_code": current_code},
        "hourly": _hourly(codes, rain=rain, temp=temp, feels=feels),
    }


def test_snow_only_day_does_not_suggest_umbrella():
    # 73 = снег
    data = _data([73, 73, 73, 73, 73])
    text = format_forecast(data, "today", NOW)
    assert "Зонт:" not in text
    assert "снег" in text.lower()
    assert "❄️" in text


def test_rain_only_day_suggests_umbrella():
    # 63 = дождь
    data = _data([63, 63, 63, 63, 63])
    text = format_forecast(data, "today", NOW)
    assert "Зонт:" in text
    assert "☂️" in text


def test_mixed_rain_and_snow_day_mentions_both():
    # утром снег, днём дождь
    data = _data([73, 73, 63, 63, 63])
    text = format_forecast(data, "today", NOW)
    assert "дождь" in text.lower() and "снег" in text.lower()
    assert "Зонт:" not in text  # это не простой rain-only случай
    assert "☂️" in text and "❄️" in text


def test_clear_day_has_no_precipitation_line():
    # 0 = ясно, дождя/снега нет вообще
    data = _data([0, 0, 1, 1, 0], rain=5)
    text = format_forecast(data, "today", NOW)
    assert "Зонт" not in text
    assert "снег" not in text.lower()


def test_fog_high_probability_does_not_falsely_claim_rain():
    # 45 = туман — это не дождь и не снег, вероятность может быть высокой,
    # но советовать зонт от тумана не нужно.
    data = _data([45, 45, 45, 45, 45], rain=80)
    text = format_forecast(data, "today", NOW)
    assert "Зонт:" not in text
    assert "❄️" not in text


def test_compact_format_has_no_old_verbose_labels():
    data = _data([0, 0, 0, 0, 0], rain=5, temp=13.0, feels=11.0)
    text = format_forecast(data, "today", NOW)
    assert "Динамика дня" not in text
    assert "Погода в Астане на" not in text
    assert "ощущается как" not in text  # заменено на компактное "ощущ."
    assert "Что надеть:" not in text
    assert "ощущ." in text


# --- Консенсус 3 моделей (ECMWF/GFS/ICON) ---------------------------------


def _multimodel_hourly(per_model_temps, rain=10, codes_per_model=None, wind=10):
    """per_model_temps: список из 3 температур (по числу CONSENSUS_MODELS),
    одинаковых на все HOURS. codes_per_model: аналогично, список из 3
    WMO-кодов; если не задан — все модели говорят "ясно" (0)."""
    assert len(per_model_temps) == len(CONSENSUS_MODELS)
    codes_per_model = codes_per_model or [0] * len(CONSENSUS_MODELS)
    times = [f"{DATE}T{h:02d}:00" for h in HOURS]
    hourly = {"time": times}
    for model, temp, code in zip(CONSENSUS_MODELS, per_model_temps, codes_per_model):
        hourly[f"temperature_2m_{model}"] = [temp] * len(HOURS)
        hourly[f"apparent_temperature_{model}"] = [temp] * len(HOURS)
        hourly[f"precipitation_probability_{model}"] = [rain] * len(HOURS)
        hourly[f"weather_code_{model}"] = [code] * len(HOURS)
        hourly[f"wind_speed_10m_{model}"] = [wind] * len(HOURS)
    return hourly


def _multimodel_data(per_model_temps, rain=10, codes_per_model=None, current_temp=0.0):
    return {
        "current": {"temperature_2m": current_temp, "apparent_temperature": current_temp, "weather_code": 0},
        "hourly": _multimodel_hourly(per_model_temps, rain=rain, codes_per_model=codes_per_model),
    }


def test_consensus_uses_median_of_three_models():
    # ECMWF=10, GFS=12, ICON=20 -> медиана 12, а не среднее (14) и не крайние
    data = _multimodel_data([10.0, 12.0, 20.0])
    text = format_forecast(data, "today", NOW)
    assert "12°C" in text
    assert "20°C" not in text and "10°C" not in text and "14°C" not in text


def test_large_model_disagreement_is_flagged():
    # разброс температур 10° между моделями - явно стоит предупредить
    data = _multimodel_data([5.0, 10.0, 15.0])
    text = format_forecast(data, "today", NOW)
    assert "🔀" in text and "расходятся" in text


def test_small_model_disagreement_is_not_flagged():
    # разброс всего 1° - несущественно, предупреждать не нужно
    data = _multimodel_data([12.0, 12.5, 13.0])
    text = format_forecast(data, "today", NOW)
    assert "🔀" not in text


def test_consensus_code_majority_vote_wins():
    # 2 из 3 моделей видят дождь (63), 1 - ясно (0) -> побеждает дождь
    data = _multimodel_data([12.0, 12.0, 12.0], rain=70, codes_per_model=[63, 63, 0])
    text = format_forecast(data, "today", NOW)
    assert "Зонт:" in text


def test_backward_compatible_without_model_suffixes():
    # Старый формат без суффиксов моделей (как раньше, best_match) должен
    # продолжать работать как один "источник" - без сюрпризов и падений.
    data = {
        "current": {"temperature_2m": 12, "apparent_temperature": 9, "weather_code": 0},
        "hourly": {
            "time": [f"{DATE}T{h:02d}:00" for h in HOURS],
            "temperature_2m": [13, 18, 19, 17, 15],
            "apparent_temperature": [11, 16, 16, 15, 15],
            "precipitation_probability": [10] * 5,
            "weather_code": [0, 2, 3, 3, 3],
            "wind_speed_10m": [10] * 5,
        },
    }
    text = format_forecast(data, "today", NOW)
    assert "13°C" in text
    assert "🔀" not in text  # один источник -> расхождений быть не может
