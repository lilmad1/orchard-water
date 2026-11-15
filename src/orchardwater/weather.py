"""Weather data for the irrigation engine.

This is the only file that talks to a weather service. Keeping it in one
place means the service can be swapped later (see docs/fao56-reference.md,
"Weather data source") without touching the water-balance code.

v0 uses Open-Meteo's free forecast API (no key needed). Its licence is
non-commercial only (checked 2026-09-25) - fine for development, but this
must change before real farmers use the output.

Weather is hourly rather than daily, so the water balance can track soil
depletion through the day instead of once at midnight - see
water_balance.py for why. Uses Open-Meteo's hourly
`et0_fao_evapotranspiration` (the same FAO-56 Penman-Monteith formula as
the daily value - not something computed here) and hourly `precipitation`
(total rain+showers+snow, matching what `precipitation_sum` meant at
daily resolution).

It also fetches hourly `wind_speed_10m` (requested in m/s via
`wind_speed_unit=ms` - Open-Meteo's default is km/h) and
`relative_humidity_2m`, for the dual crop coefficient method's Kc_max
formula (FAO-56 Eq. 72, see fao56.kc_max) AND the Kcb climate adjustment
(Eq. 70, see fao56.climate_adjusted_kcb) - both equations share the same
climate-correction term. Neither is needed for the existing root-zone
balance.
"""

import json
import math
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
HOURS_PER_DAY = 24
PAST_DAYS = 7
FORECAST_DAYS = 7
PAST_HOURS = HOURS_PER_DAY * PAST_DAYS
FORECAST_HOURS = HOURS_PER_DAY * FORECAST_DAYS

# Open-Meteo's own documented limit on past_days (checked 2026-09-28,
# https://open-meteo.com/en/docs). Flood irrigation needs more than the
# fixed 7 days above - a flood-irrigated field's last watering can be
# weeks ago - but still can't ask for more history than Open-Meteo
# actually has.
PLAUSIBLE_MAX_PAST_DAYS = 92


@dataclass(frozen=True)
class HourlyWeather:
    """One hour of weather for one field."""

    timestamp: str  # ISO date + hour, e.g. "2026-09-26T14:00"
    et0_mm: float  # reference evapotranspiration that hour
    rain_mm: float  # precipitation that hour
    wind_speed_ms: float  # wind speed at 10 m above ground, m/s
    relative_humidity_pct: float  # relative humidity at 2 m above ground, %


def fetch_hourly_json(lat: float, lon: float, past_days: int = PAST_DAYS) -> dict[str, Any]:
    """Call Open-Meteo and return the raw parsed JSON for one location.

    past_days plus the next 7 days, hour by hour, in Casablanca time (see
    docs/fao56-reference.md, "Weather data source" for the parameter
    choices). Open-Meteo also has `past_hours`/`forecast_hours` parameters
    for requesting an exact number of hourly steps directly. This code
    uses `past_days`/`forecast_days` instead, deliberately - it keeps the
    requested window aligned to whole calendar days, matching the daily
    version's window exactly, and returns 24 hourly values per day instead
    of 1.

    Defaults to the fixed 7 days every drip call site uses. Flood
    irrigation passes a larger value, since a flood-irrigated field can go
    weeks between waterings - up to PLAUSIBLE_MAX_PAST_DAYS, Open-Meteo's
    own documented limit.
    """
    if not 0 <= past_days <= PLAUSIBLE_MAX_PAST_DAYS:
        raise ValueError(
            f"past_days ({past_days}) is outside Open-Meteo's supported range "
            f"(0-{PLAUSIBLE_MAX_PAST_DAYS})"
        )
    params = (
        f"latitude={lat}&longitude={lon}"
        "&hourly=et0_fao_evapotranspiration,precipitation,wind_speed_10m,relative_humidity_2m"
        f"&past_days={past_days}&forecast_days={FORECAST_DAYS}"
        "&timezone=Africa/Casablanca&wind_speed_unit=ms"
    )
    url = f"{OPEN_METEO_URL}?{params}"
    with urllib.request.urlopen(url, timeout=10) as response:
        data: dict[str, Any] = json.load(response)
        return data


def _is_finite_number(value: object) -> bool:
    """True for a real int or float that is not NaN or infinite.

    Checked before math.isfinite(), which raises TypeError on text such as
    "n/a", and a bool is rejected on purpose (True would otherwise pass as
    1.0). Anything else counts as "not a usable number".
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value)


def response_utc_offset_seconds(data: dict[str, Any]) -> int:
    """The response's fixed UTC offset in seconds, checked before it is used.

    Open-Meteo sends one utc_offset_seconds for the whole response. A
    missing, non-numeric, fractional or absurd value is rejected with a
    plain error. A whole-number float such as 3600.0 is turned into the int
    3600, because the saved-state file only accepts an int and a float
    would make every later run report an unreadable state.
    """
    value = data.get("utc_offset_seconds")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) >= 24 * 3600:
        raise ValueError(f"invalid utc_offset_seconds in weather data: {value!r}")
    return value


def parse_hourly_weather(data: dict[str, Any]) -> list[HourlyWeather]:
    """Turn Open-Meteo's JSON shape into a plain list of HourlyWeather.

    Rejects an hour with a missing, non-finite (NaN or +/-inf), or negative
    ET0 or rain value instead of passing it through - the water balance's
    min/max clipping (water_balance.step_one_period) would otherwise turn a
    NaN into a silent 0.0 mm depletion, printing a plausible-looking but
    meaningless "no water needed" result instead of an error (the same
    silent-failure risk applies at any resolution). ET0 and rain are
    physical depths and can't be negative.

    Wind speed and relative humidity get the same missing/non-finite
    checks, plus their own physical bounds: wind speed can't be negative,
    and relative humidity must be a percentage from 0 to 100.
    """
    hourly = data["hourly"]
    timestamps = hourly["time"]
    et0_values = hourly["et0_fao_evapotranspiration"]
    rain_values = hourly["precipitation"]
    wind_values = hourly["wind_speed_10m"]
    humidity_values = hourly["relative_humidity_2m"]
    result = []
    for timestamp, et0_mm, rain_mm, wind_ms, humidity_pct in zip(
        timestamps, et0_values, rain_values, wind_values, humidity_values, strict=True
    ):
        if not _is_finite_number(et0_mm) or et0_mm < 0:
            raise ValueError(f"{timestamp}: invalid ET0 value from weather data: {et0_mm!r}")
        if not _is_finite_number(rain_mm) or rain_mm < 0:
            raise ValueError(f"{timestamp}: invalid rain value from weather data: {rain_mm!r}")
        if not _is_finite_number(wind_ms) or wind_ms < 0:
            raise ValueError(
                f"{timestamp}: invalid wind speed value from weather data: {wind_ms!r}"
            )
        if not _is_finite_number(humidity_pct) or not 0.0 <= humidity_pct <= 100.0:
            raise ValueError(
                f"{timestamp}: invalid relative humidity value from weather data: {humidity_pct!r}"
            )
        result.append(
            HourlyWeather(
                timestamp=timestamp,
                et0_mm=et0_mm,
                rain_mm=rain_mm,
                wind_speed_ms=wind_ms,
                relative_humidity_pct=humidity_pct,
            )
        )
    return result


def split_past_and_forecast(
    hours: list[HourlyWeather],
    past_hours: int = PAST_HOURS,
    forecast_hours: int = FORECAST_HOURS,
) -> tuple[list[HourlyWeather], list[HourlyWeather]]:
    """Split fetched hours into the past window and the coming forecast week.

    Open-Meteo always returns past_days then forecast_days in order, so we
    split by position instead of parsing timestamps - that avoids adding a
    timezone-handling library just for this. Defaults to the fixed 7-day
    past window every drip call site uses; flood irrigation passes the
    actual past_hours it requested from fetch_hourly_json, whatever that
    was.

    Also requires the hours to be strictly consecutive: each timestamp
    exactly one hour after the one before. A right row COUNT alone is not
    enough - a response with a gap padded by duplicate or reordered rows
    keeps the same count but hides real drying history, which can turn an
    already-dry field into a false "no irrigation is due". Timestamps are
    Open-Meteo's local times at one fixed offset per response, so plain
    hour steps are exact; no timezone handling is needed for the
    comparison.
    """
    expected_total = past_hours + forecast_hours
    if len(hours) != expected_total:
        raise ValueError(f"expected {expected_total} hours, got {len(hours)}")
    _require_consecutive_hours(hours)
    return hours[:past_hours], hours[past_hours:]


def _require_consecutive_hours(hours: list[HourlyWeather]) -> None:
    """Raise ValueError unless every timestamp is exactly 1 hour after the last."""
    one_hour = timedelta(hours=1)
    previous = None
    for hour in hours:
        try:
            current = datetime.fromisoformat(hour.timestamp)
        except (TypeError, ValueError):
            raise ValueError(f"unreadable weather timestamp: {hour.timestamp!r}") from None
        if current.tzinfo is not None:
            # The response's own utc_offset_seconds says what the labels mean,
            # so a label carrying its own offset would be read two ways.
            raise ValueError(f"unreadable weather timestamp: {hour.timestamp!r}")
        if previous is not None and current - previous != one_hour:
            raise ValueError(
                f"weather hours are not consecutive: {hour.timestamp} does not "
                "follow the hour before it by exactly 1 hour"
            )
        previous = current
