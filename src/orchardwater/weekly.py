"""Command-line entry point: weekly irrigation need for one field.

Run it like this (drip, the default):
    python -m orchardwater.weekly --lat 30.47 --lon -8.88 --ha 12 --pump-m3h 25 \\
        --efficiency 0.9 --ec-water 1.2

Add --reset right after actually irrigating the field - this tool
remembers depletion between runs (see state.py) and has no other way to
know water was applied.

Flood irrigation - fields watered weeks apart, not
days - uses --irrigation-method flood and --last-watering-date instead:
    python -m orchardwater.weekly --lat 30.47 --lon -8.88 --ha 12 --pump-m3h 25 \\
        --efficiency 0.9 --ec-water 1.2 --irrigation-method flood \\
        --last-watering-date 2026-09-01 --watering-time 18:00

`argparse` (Python's built-in tool for reading command-line flags like
--lat 30.47) turns those flags into a plain object with one attribute per
flag - see main() below.
"""

import argparse
import dataclasses
import math
import sys
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from orchardwater.card import CardTemplate

from orchardwater.fao56 import (
    CITRUS_CANOPY_FRACTION,
    CITRUS_DEPLETION_FRACTION_P,
    CITRUS_ECE_THRESHOLD_DS_M,
    CITRUS_HEIGHT_M,
    CITRUS_KCB,
    CITRUS_ROOT_DEPTH_M,
    DEFAULT_SOIL,
    DRIP_FW_TRICKLE,
    FLOOD_FW_TABLE20,
    SOIL_TYPES,
    TOPSOIL_EVAP_DEPTH_ZE_M,
    drip_wetted_fraction,
    exposed_wetted_fraction,
    leaching_requirement,
    readily_available_water_mm,
    total_available_water_mm,
    total_evaporable_water_mm,
)
from orchardwater.kc import MonthlyKc
from orchardwater.result import (
    KIND_NO_IRRIGATION_DUE,
    KIND_SOIL_AT_LIMIT,
    KIND_SOIL_LIMIT_AHEAD,
    KIND_UNAVAILABLE,
    KIND_WEEKLY_AMOUNT,
    WeeklyResult,
    summary_kind,
)
from orchardwater.state import (
    STATE_PATH,
    FieldState,
    field_key,
    load_state,
    parse_state_file_any_field,
    save_state,
)
from orchardwater.state import anchor_instant as state_anchor_instant
from orchardwater.units import (
    PLAUSIBLE_MAX_HA,
    PLAUSIBLE_MAX_PUMP_M3H,
    PLAUSIBLE_MIN_EFFICIENCY,
    PLAUSIBLE_MIN_HA,
    PLAUSIBLE_MIN_PUMP_M3H,
    gross_mm,
    m3_to_pump_hours,
    mm_to_m3,
)
from orchardwater.water_balance import (
    DualKcParams,
    IrrigationDeadline,
    PeriodResult,
    irrigation_deadline,
    run_forecast_period,
    weekly_net_need_mm,
)
from orchardwater.weather import (
    FORECAST_HOURS,
    PAST_DAYS,
    PLAUSIBLE_MAX_PAST_DAYS,
    HourlyWeather,
    fetch_hourly_json,
    parse_hourly_weather,
    response_utc_offset_seconds,
    split_past_and_forecast,
)

# Same timezone Open-Meteo is asked for in weather.py, so a deadline
# timestamp means the same hour in both places.
CASABLANCA_TZ = ZoneInfo("Africa/Casablanca")

# Shared between both irrigation methods - the parts of the assumptions
# text that don't depend on how the field is watered (drip's own
# saved-state/--reset assumptions must not be printed under flood
# output, since they include a direct instruction to use a flag flood's
# own CLI rejects).
_COMMON_ASSUMPTIONS_TEXT = """Assumptions:
- If the model's known current depletion has already reached the
  no-stress limit, this tool cannot size a "day before" fix for a later
  crossing using an even-earlier hour that's already in the past - the
  earliest point it has to work with is now. See the "already at the
  no-stress limit" warning, when it appears, for what this tool can and
  can't tell you in that case.
- No runoff modelled: all rain at or above the 0.2 x ET0 evaporation
  threshold is assumed to soak in. FAO-56 states this 0.2x rule for daily
  rain totals; applying the same fraction per hour is not separately
  verified (TODO(verify), see fao56.effective_rain_mm).
- Kc, root depth, depletion fraction and soil water values are provisional
  values chosen from FAO-56 tables, marked TODO(verify) in fao56.py - not
  yet confirmed for this farm.
- Open-Meteo's free weather API is for non-commercial use only.
- Depletion is tracked once per hour (end-of-hour totals), not
  continuously. If water use happens before rain within the same hour,
  this tool cannot see any stress that occurred before the rain brought
  that hour's total back down - only the net result at hour's end.
- Crop water use is calculated with FAO-56's dual crop
  coefficient method (separate transpiration and soil-evaporation terms),
  instead of one flat coefficient. Kc_max (FAO-56 Eq. 72) and Kcb (Eq. 70)
  both use the hour's instantaneous wind and humidity readings as a
  stand-in for FAO-56's own growth-STAGE averages - a real extrapolation,
  not something FAO-56 states (TODO(verify), see fao56.kc_max); both are
  also clamped to FAO-56's own stated validity range for this correction
  (1-6 m/s wind, 20-80% humidity) rather than extrapolated further past
  it. Canopy fraction, crop height, and the topsoil evaporation layer
  depth are provisional FAO-56 table values, marked TODO(verify) in
  fao56.py - not yet confirmed for this farm, same status as the
  existing Kc/root depth/soil values above."""

# Only true when a summary actually prints a gross water figure -
# build_summary()'s normal case, for both methods. build_immediate_
# warning_summary() and build_no_irrigation_due_summary() print neither a
# gross water figure nor (always) a pump-capacity comparison (the footer
# of build_no_irrigation_due_summary and build_immediate_warning_summary
# must not claim a pump-feasibility check and a gross water figure appear
# "below" it, when those functions deliberately print neither).
#
# The pump-capacity bullet below is worded to stay true even inside
# build_summary() itself: not every one of its own branches shows a
# capacity comparison either (the "deadline already passed" branch never
# does, and the no-deadline branch only does when pumping would actually
# take longer than the time left), so claiming one appears "below"
# unconditionally would be false in exactly those branches.
_RECOMMENDATION_ASSUMPTIONS_TEXT = """
- When a pump-capacity comparison appears above (checking whether there's
  enough time to pump the stated amount), it assumes the pump runs
  continuously, without interruption, at the stated flow rate once
  started, and does not check anything finer than one hour.
- The gross water figure includes a leaching correction
  based on the irrigation water's salinity and citrus's salt-tolerance
  threshold (FAO-56 Table 23, TODO(verify) which citrus variety this
  should use). This assumes FAO-56's standard 40-30-20-10 percent
  root-zone water extraction pattern, not separately verified for citrus.
  The formula predicts a STEADY-STATE salt balance (reached over repeated
  irrigations, not guaranteed from any single week's water) and does not
  account for existing soil salinity, rainfall, or how much of this water
  actually drains past the root zone - a planning estimate, not a
  guarantee that soil salinity will actually stay below the threshold. Soil or water
  testing, not this tool, is how you'd confirm the real situation."""

_DRIP_ONLY_ASSUMPTIONS_TEXT = """
- Soil is assumed full on the first run for a field, after --reset, or
  when the saved state belongs to a different field or soil choice.
  Otherwise, it remembers modeled
  depletion from the last run (saved in .orchardwater_state.json) instead
  of starting over - this is the top-up method (not valid for flood
  irrigation, where fields dry out for longer than a week - see
  --irrigation-method flood instead).
- The saved depletion assumes no irrigation happened between runs. If you
  actually watered the field, re-run with --reset - this tool has no
  other way to know it happened, and will otherwise keep counting
  depletion as if that water was never applied. --reset assumes a FULL
  root-zone refill; if you applied only part of what was needed, the
  saved depletion after --reset will understate what the soil actually
  still needs.
- --reset takes effect from the moment you run it, but this tool only has
  whole-hour weather data - the hour label covering that moment still
  applies its FULL hour's water use and rain on top of the reset, even
  the part from before the reset actually happened. This can overstate
  depletion (or credit rain that fell before watering) for that one hour -
  a sharper version of the finer-than-one-hour limit already noted above.
- Saved state for a different field or soil choice is not used, and the
  soil is assumed full instead. Saved state older than 7 days is still
  used: the tool fetches extra weather history to continue from it, up
  to about 90 days. If the state is older than that, or the file cannot
  be read, the tool gives no recommendation. Check the field's actual
  soil moisture, then re-run with --reset.
- The soil-evaporation layer is remembered between runs the same
  way root-zone depletion already is - it starts fully wet
  only on the very first run for a field, after --reset, or when the
  saved state belongs to a different field or soil choice, and otherwise
  dries out continuously, assuming
  no irrigation happens until you follow the recommendation. A
  significant rain (more than about 3-4 mm in one hour) wets the whole
  surface, not just the small drip patch, and that stays true (even
  between rain) until the next significant rain - it does not shrink
  back on its own."""

_FLOOD_ONLY_ASSUMPTIONS_TEXT = """
- This is the "wait-until-low" method for flood/basin/border irrigation,
  not drip's top-up method: soil is assumed full at
  the --watering-time on the given --last-watering-date (both are
  required, Casablanca time), not remembered between runs - update both
  once you've actually re-watered (there is no --reset for this method;
  the date and time you give ARE the reset). Without the real time, rain
  and drying earlier that day cannot be told apart from what happened
  after the watering, and a wrong guess can hide a needed irrigation.
- Updating --last-watering-date and --watering-time
  assumes a full root-zone refill on that date UNLESS you also give
  --post-watering-fullness. If you're not sure the watering was full,
  use that flag. Guessing the fullness too LOW is the safer mistake (it
  makes the next recommendation come earlier/bigger than strictly
  needed); guessing too HIGH, or leaving the full-refill default when it
  wasn't full, can understate what the soil still needs and recommend
  too little.
- The soil-evaporation layer (the dual coefficient method) also starts
  fresh at that same date, fully wet, and is not remembered between runs
  either. Unlike drip, it starts at its OWN maximum wetted value already
  (flood wets the whole surface directly, FAO-56 Table 20), so a
  significant rain during the forecast has nothing higher to switch it
  to."""

# Added to the end of a drip report when the block has a Kc fitted for
# its farm, because the assumptions text describes the textbook dual
# Kc method instead.
MONTHLY_KC_NOTE = """
Crop coefficient: this block uses a monthly Kc fitted for this farm from
measured water use (see kc.py), not the textbook dual Kc (Kcb + Ke)
described in the assumptions above. Crop water use = Kc for the month x ET0.
"""

# Full footer for build_summary()'s normal case (both methods always
# print a pump-feasibility check and a gross water figure there).
ASSUMPTIONS_TEXT = (
    _COMMON_ASSUMPTIONS_TEXT + _RECOMMENDATION_ASSUMPTIONS_TEXT + _DRIP_ONLY_ASSUMPTIONS_TEXT
)
FLOOD_ASSUMPTIONS_TEXT = (
    _COMMON_ASSUMPTIONS_TEXT + _RECOMMENDATION_ASSUMPTIONS_TEXT + _FLOOD_ONLY_ASSUMPTIONS_TEXT
)

# Trimmed footer for build_immediate_warning_summary() and
# build_no_irrigation_due_summary() - neither ever prints a pump-
# feasibility check or a gross water figure, so the recommendation-only
# bullets above would describe something the farmer never sees.
NO_RECOMMENDATION_ASSUMPTIONS_TEXT = _COMMON_ASSUMPTIONS_TEXT + _DRIP_ONLY_ASSUMPTIONS_TEXT
FLOOD_NO_RECOMMENDATION_ASSUMPTIONS_TEXT = _COMMON_ASSUMPTIONS_TEXT + _FLOOD_ONLY_ASSUMPTIONS_TEXT


def hours_between_utc(earlier: datetime, later: datetime) -> float:
    """Elapsed hours from `earlier` to `later`, computed via UTC.

    The ONE place in this file that subtracts two datetimes - every
    feasibility check below calls this instead of subtracting inline.
    Python's datetime subtraction has a surprising rule: if both sides
    share the exact same tzinfo object, it subtracts wall-clock time and
    ignores any UTC-offset change between them - Morocco shifts to UTC+0
    for part of the year around Ramadan, so that shortcut is silently
    wrong across that change. The bug is easy to reintroduce by
    computing elapsed time a second way. Converting both sides to UTC
    first avoids the wall-clock shortcut, and having exactly one
    function that does this means there's nothing left to reintroduce
    the bug in.
    """
    return (later.astimezone(UTC) - earlier.astimezone(UTC)).total_seconds() / 3600.0


def forecast_ends_as_requested(
    last_timestamp: str, response_offset: timezone, now: datetime
) -> bool:
    """True if the weather response ends exactly at the end of the requested forecast week.

    Open-Meteo returns whole calendar days, so a request made on a given
    day ends at 23:00 on the last forecast day: today + (FORECAST_HOURS / 24
    - 1) days. A response can have the right row count and reach back to
    the watering date, and still be shifted. Ending early (a stale copy)
    makes the missing forecast days read as "no irrigation is due". Ending late makes the
    tool calculate for a day outside the requested week, so a deadline can
    land after the week it is meant to cover (for flood, a one-day forward
    shift reproduces this). The match is therefore exact.

    last_timestamp is the label of the final hour (local time at
    response_offset), and now is a real moment. Both are compared as plain
    local clock labels, so no timezone arithmetic is needed.
    """
    today = now.astimezone(response_offset).date()
    last_day = today + timedelta(days=FORECAST_HOURS // 24 - 1)
    expected_last_label = datetime(last_day.year, last_day.month, last_day.day, 23, 0)
    return datetime.fromisoformat(last_timestamp) == expected_last_label


def daily_net_use(
    results: list[PeriodResult], response_offset: timezone, until: datetime | None
) -> tuple[tuple[str, float], ...]:
    """Crop water use minus soaked-in rain per Casablanca calendar day (mm).

    Each hourly result is labelled with the END of its hour (see
    deadline_instant), so the hour belongs to the day one minute before
    that label. Hours after `until` (the deadline) are left out. A rainy day
    can come out negative: the caller treats that as "no watering that day".
    Used only to split the engine's amount over the days, never to change it.
    """
    per_day: dict[str, float] = {}
    for hour in results:
        end = deadline_instant(hour.timestamp, response_offset)
        if until is not None and end > until:
            break
        day = (end - timedelta(minutes=1)).astimezone(CASABLANCA_TZ).date().isoformat()
        per_day[day] = per_day.get(day, 0.0) + hour.etc_mm - hour.eff_rain_mm
    return tuple((day, round(mm, 4)) for day, mm in per_day.items())


def deadline_instant(timestamp: str, response_offset: timezone) -> datetime:
    """The real instant a deadline timestamp refers to.

    Open-Meteo's hourly et0_fao_evapotranspiration and precipitation are
    PRECEDING-hour sums: the value labelled "14:00" covers 13:00-14:00, not
    14:00-15:00. So the timestamp already marks the end of the period its
    depletion was computed over - no "+1 hour" adjustment (adding an hour
    here would give every deadline an extra, incorrect hour of slack).

    Interprets the label using response_offset - the fixed offset
    Open-Meteo actually used for this response, from its own
    utc_offset_seconds field - not CASABLANCA_TZ. Open-Meteo returns ONE
    fixed offset for a whole response and does not shift it mid-response
    across a real Casablanca offset change (a documented Open-Meteo
    limitation, open-meteo/open-meteo#1764), so reinterpreting the label
    with Python's real, date-varying Casablanca rules could silently
    disagree with what Open-Meteo intended for that specific response.

    Pulled out as its own function so it can be tested in isolation.
    """
    return datetime.fromisoformat(timestamp).replace(tzinfo=response_offset)


def _exclude_pre_watering_rain(
    hours: list[HourlyWeather], anchor_instant: datetime, response_offset: timezone
) -> list[HourlyWeather]:
    """Zero the rain in the one hour bucket that straddles anchor_instant.

    Flood-only. Open-Meteo's hourly rain is a PRECEDING-hour sum (see
    deadline_instant's own docstring): the bucket labelled "19:00" covers
    18:00-19:00. --watering-time need not fall exactly on an hour boundary
    (e.g. "18:30"), so the first hour after anchor_instant can still START
    before it - that bucket's rain total mixes rain that fell BEFORE the
    real watering (already reflected in whatever post_watering_fullness the
    farmer gave) with rain that fell after it (which should count).
    Requiring --watering-time does not close this gap by itself, since an
    accepted HH:MM value can still land inside an hour rather than on its
    edge (for example watering at 18:30 with rain in the 18:00-19:00
    bucket).

    Without sub-hour data there is no way to split that one bucket's rain
    into a before-watering part and an after-watering part, so the whole
    bucket's rain is dropped rather than risk double-crediting
    pre-watering rain a second time. Its ET0 is kept: losing at most one
    hour of real drying credit is a small, bounded error, next to a rain
    total that can be large enough to flip the recommendation outright
    (for example 30 mm in one hour).

    hours is expected to already be filtered to instants after
    anchor_instant (see hours_after_anchor above) - only its first element
    can possibly straddle the anchor, since every hour after it starts no
    earlier than that first hour's own end.
    """
    if _first_hour_straddles_anchor(hours, anchor_instant, response_offset):
        return [dataclasses.replace(hours[0], rain_mm=0.0), *hours[1:]]
    return hours


def _first_hour_straddles_anchor(
    hours: list[HourlyWeather], anchor_instant: datetime, response_offset: timezone
) -> bool:
    """True if the first weather hour starts before anchor_instant and ends after it."""
    if not hours:
        return False
    interval_end = deadline_instant(hours[0].timestamp, response_offset)
    interval_start = interval_end - timedelta(hours=1)
    return interval_start < anchor_instant < interval_end


def rain_left_out_note(rain_mm: float) -> str:
    """Footer line telling the farmer that one hour of rain was left out.

    Empty when there was no rain to leave out. The flood method drops the
    rain of the weather hour that contains the watering time, because hourly
    data cannot show whether it fell before or after the watering (see
    _exclude_pre_watering_rain). Dropping it errs on the side of watering
    sooner or more, never less, and the footer says so.
    """
    if rain_mm <= 0.0:
        return ""
    return (
        f"\n- The weather hour that contains the watering time had {rain_mm:.1f} mm of "
        "rain. The weather data cannot show whether it fell before or after the "
        "watering, so it was left out. If it fell after the watering, the soil needs "
        "a little less water than shown here."
    )


def format_casablanca(instant: datetime) -> str:
    """Format an instant as a farmer-readable Casablanca clock time.

    Includes the UTC offset (e.g. "2026-09-20 01:00 (UTC+1)") because
    Morocco's offset changes create a genuinely repeated wall-clock hour:
    on the 2026-09-19/20 change, both 2026-09-20 01:00+01:00 and
    2026-09-20 01:00+00:00 are real, different instants an hour apart,
    and would print identically without the offset.
    """
    local = instant.astimezone(CASABLANCA_TZ)
    offset = local.utcoffset()
    assert offset is not None  # CASABLANCA_TZ always has a defined offset
    offset_hours = int(offset.total_seconds() // 3600)
    return f"{local.strftime('%Y-%m-%d %H:%M')} (UTC{offset_hours:+d})"


def format_amount(value: float, decimals: int = 1) -> str:
    """Format a printed number without ever showing a real amount as "0.0".

    Rounding at a fixed number of decimal places can turn a small but real
    amount (e.g. 0.04 mm of water) into "0.0", which reads as "nothing
    needed" - a farmer acting on that would skip real water. If the normal
    rounding would hide a nonzero value this way, add more decimal places
    until a real digit shows. A true zero still prints as "0.0".

    Fixed-point notation itself runs out for an extremely small value
    (e.g. `1e-16`, which the water-balance math can produce when two
    nearly-equal terms are subtracted): 15 decimal places still shows
    "0.000000000000000". Past that point, fall back to scientific
    notation, which can always show a nonzero value regardless of how
    small it is.

    No caller currently passes NaN, infinity or negative zero (every
    number reaching this function has already passed a finiteness check
    upstream in fao56.py/units.py), but this function would print them
    badly if one ever did - "-0.0", or "nan"/"inf" unchanged. Guarded
    here too, so this display helper cannot misrepresent a value on its
    own even if a future caller skips validation.
    """
    if not math.isfinite(value):
        return "n/a"
    if value == 0.0:
        value = 0.0  # collapses -0.0 to a plain "0.0"
    text = f"{value:.{decimals}f}"
    if value != 0.0 and float(text) == 0.0:
        for extra_decimals in range(decimals + 1, 16):
            text = f"{value:.{extra_decimals}f}"
            if float(text) != 0.0:
                return text
        return f"{value:.2e}"
    return text


def format_below_limit(value_mm: float, limit_mm: float, decimals: int = 1) -> str:
    """Format value_mm with enough decimals to visibly stay below limit_mm.

    Same problem as format_amount()'s own zero-guard, but for "distinct
    from another displayed number" instead of "distinct from zero":
    callers only reach this when value_mm is mathematically strictly below
    limit_mm, but at a fixed number of decimal places the two can round to
    the SAME displayed figure near the boundary (e.g. 44.96 and 45.0 both
    show "45.0" at one decimal place) - printed next to words like "stays
    below," that reads as a contradiction (as in
    build_no_irrigation_due_summary()). Adds decimal places until the two
    display differently, the same escalating approach format_amount()
    uses, with the same eventual give-up point.
    """
    for extra_decimals in range(decimals, 16):
        if round(value_mm, extra_decimals) < round(limit_mm, extra_decimals):
            return format_amount(value_mm, extra_decimals)
    return format_amount(value_mm, 15)


DRIP_REAPPLY_NOTE = (
    " Once you have applied it, re-run this tool for the rest of the week - "
    "running it again before applying would still use the saved depletion "
    "from before watering."
)
FLOOD_REAPPLY_NOTE = (
    " Once you have applied it, re-run this tool for the rest of the week "
    "with an updated --last-watering-date, the actual --watering-time when "
    "the watering finished, and --post-watering-fullness if it was not a "
    "full refill - running it again with the OLD values would still assume "
    "the soil is exactly as dry as this estimate, not accounting for the "
    "water you just applied."
)


def build_summary(
    net_mm: float,
    gross_water_mm: float,
    volume_m3: float,
    pump_hours: float,
    irrigate_by: str | None = None,
    hours_available: float | None = None,
    assumptions_text: str = ASSUMPTIONS_TEXT,
    reapply_note: str = DRIP_REAPPLY_NOTE,
) -> str:
    """Build the printed weekly summary text from already-computed numbers.

    assumptions_text defaults to drip's own (ASSUMPTIONS_TEXT) - flood
    (compute_flood_weekly_summary) passes FLOOD_ASSUMPTIONS_TEXT
    instead, so the printed footer never describes the other method's
    assumptions (drip's own text includes a direct instruction to use
    --reset, a flag flood's own CLI rejects).

    reapply_note defaults to drip's own (DRIP_REAPPLY_NOTE) - the
    feasible-deadline case's own re-run advice, in the SUMMARY BODY (not
    the footer), also mentions "saved depletion," which only means
    anything for drip; flood passes FLOOD_REAPPLY_NOTE instead, so no
    drip-specific text appears under flood output.

    irrigate_by is the deadline, already formatted as a "YYYY-MM-DD HH:MM"
    string in the farm's real Casablanca clock time (not Open-Meteo's raw
    response label, which can differ from real local time across an offset
    change), if the modeled soil would otherwise reach the no-stress limit
    (RAW) before the week is over. When set, the net/gross/volume/hours
    numbers only cover water up to that deadline, not the rest of the
    week.

    hours_available is how many hours remain, from now, before the
    deadline or the end of the forecast (see hours_between_utc). Three
    mutually exclusive deadline outcomes follow (printing more than
    one of them together would contradict itself):
    - hours_available < 0: the deadline has already passed.
    - pump_hours > hours_available: not enough time left to pump the
      amount - stated plainly, with no suggested action ("start now"
      or "use a faster pump" would be unverifiable advice).
    - otherwise: states the hours available and the hours needed as a
      capacity check only ("enough total hours exist"), not a full safety
      claim - it does not simulate pumping alongside hourly depletion, so
      it cannot verify that starting anywhere in that window is safe from
      overwatering. No exact start-clock-time either way (deliberately
      out of scope).
    """
    lines = [
        # Not "Weekly soil refill estimate" for the no-deadline case: after
        # a midday run, the remaining forecast can be less than a full week
        # (only hours still ahead of now are reported), so claiming
        # "weekly" here would overstate the window - the line below already
        # says precisely what the amount covers.
        "Soil refill estimate",
        f"  Modeled net soil refill: {format_amount(net_mm)} mm",
    ]
    if irrigate_by is not None:
        deadline_text = irrigate_by
        deadline_passed = hours_available is not None and hours_available < 0
        infeasible = (
            hours_available is not None and not deadline_passed and pump_hours > hours_available
        )
        if deadline_passed:
            # Re-running mid-day after a deadline has passed would just
            # repeat the same problem if the tool always assumed the soil
            # was full at the calendar-day start. Remembering real
            # depletion between runs instead (see state.py) means a rerun
            # normally reflects hours already spent today. This "deadline
            # already passed" message can still happen (e.g. the very
            # first run for a field, right after --reset, or if saved
            # state was discarded as stale) - it means today's answer is
            # out of date, not that re-running is futile.
            lines.append(
                f"  Warning: the deadline to apply this water, {deadline_text}, "
                "has already passed. This recommendation is out of date - "
                "re-run this tool for a fresh forecast."
            )
        elif infeasible:
            assert hours_available is not None  # guaranteed by `infeasible`'s own check
            # Same near-tie rounding risk as the no-deadline warning below:
            # the two headline hour figures can round to the same display
            # value even though one is really bigger, so state the
            # shortfall too.
            deadline_shortfall_hours = pump_hours - hours_available
            lines.append(
                f"  Warning: this only covers water needed up to {deadline_text}, "
                f"but pumping it needs {format_amount(pump_hours)} hours and only "
                f"{format_amount(hours_available)} hours remain before that deadline - "
                f"short by {format_amount(deadline_shortfall_hours, decimals=2)} hours. "
                "This pump cannot deliver the full amount in time as stated."
            )
        else:
            # This only checks that total pump_hours fits in
            # hours_available - it does not simulate pumping alongside
            # hourly depletion, so it cannot rule out overwatering from
            # starting too early within that window. Worded as a capacity
            # claim only, not a full safety guarantee.
            hours_note = (
                f" You have {format_amount(hours_available)} hours before the deadline, "
                f"enough total time for the {format_amount(pump_hours)} hours this pump "
                "needs - though this only checks the total hours fit, not "
                "when within that window it's safe to start."
                if hours_available is not None
                else ""
            )
            lines.append(
                f"  Warning: this amount is calculated from soil depletion at "
                f"{deadline_text}; it does not cover later hours. Applying it "
                "earlier could overwater, since the soil wasn't that depleted "
                f"yet.{hours_note}{reapply_note}"
            )
    else:
        lines.append(
            "  This amount is calculated from soil depletion at the end of "
            "the forecast week, not any single hour within it."
        )
        if hours_available is not None and pump_hours > hours_available:
            # Compare and warn using the real (unrounded) hours, but the two
            # headline numbers can still round to the same ".1f" display
            # (e.g. 1.04 vs 1.01 both show as "1.0") and look
            # self-contradictory. Stating the shortfall directly keeps the
            # warning consistent regardless of how close the two numbers
            # are.
            shortfall_hours = pump_hours - hours_available
            lines.append(
                f"  Warning: pumping this amount needs {format_amount(pump_hours)} hours, but "
                f"only {format_amount(hours_available)} hours remain in the forecast - short "
                f"by {format_amount(shortfall_hours, decimals=2)} hours. This pump cannot "
                "deliver the full amount within that window."
            )
    lines += [
        "  Gross water to pump (after efficiency and leaching): "
        f"{format_amount(gross_water_mm)} mm",
        f"  Volume: {format_amount(volume_m3)} m3",
        f"  Pump time needed: {format_amount(pump_hours)} hours",
        "",
        assumptions_text,
    ]
    return "\n".join(lines)


def _validate_ha_pump_efficiency(ha: float, pump_m3h: float, efficiency: float) -> None:
    """Shared input checks for both compute_weekly_summary() and
    compute_flood_weekly_summary() - method-agnostic: field size, pump
    flow, and conveyance efficiency don't depend on which irrigation
    method is used.
    """
    if not (math.isfinite(ha) and ha > 0):
        raise ValueError("ha must be a positive, finite number")
    if not PLAUSIBLE_MIN_HA <= ha <= PLAUSIBLE_MAX_HA:
        # Checked here too (mm_to_m3() checks it again downstream) so a
        # plainly wrong field size fails before wasting a weather API call,
        # same as the finite/positive check just above.
        raise ValueError(
            f"ha ({ha}) is outside a realistic single field size "
            f"({PLAUSIBLE_MIN_HA}-{PLAUSIBLE_MAX_HA} ha) - check the units rather "
            "than trust this as a real field area"
        )
    if not (math.isfinite(pump_m3h) and pump_m3h > 0):
        raise ValueError("pump_m3h must be a positive, finite number")
    if not PLAUSIBLE_MIN_PUMP_M3H <= pump_m3h <= PLAUSIBLE_MAX_PUMP_M3H:
        raise ValueError(
            f"pump_m3h ({pump_m3h}) is outside a realistic irrigation pump flow "
            f"({PLAUSIBLE_MIN_PUMP_M3H}-{PLAUSIBLE_MAX_PUMP_M3H} m3/h) - check the "
            "units rather than trust this as a real pump"
        )
    if not 0.0 < efficiency <= 1.0:
        raise ValueError("efficiency must be greater than 0 and at most 1")
    if efficiency < PLAUSIBLE_MIN_EFFICIENCY:
        raise ValueError(
            f"efficiency ({efficiency}) is below a realistic irrigation efficiency "
            f"floor ({PLAUSIBLE_MIN_EFFICIENCY}) - check the units rather than "
            "trust this as a real measured efficiency"
        )


def build_immediate_warning_summary(
    reason: str, assumptions_text: str = NO_RECOMMENDATION_ASSUMPTIONS_TEXT
) -> str:
    """Text to print when this week's model cannot schedule a safe amount at all.

    See water_balance.IrrigationDeadline.unschedulable_reason: this covers
    two cases (RAW reached within the very first forecast hour, or a later
    hour's own water use alone already reaches or exceeds RAW so no
    earlier-hour refill can prevent it) - reason explains which one
    applies, in plain English.

    assumptions_text defaults to drip's own, without the pump-feasibility/
    gross-water bullets - this function never prints either figure, so
    those bullets would describe something the farmer never sees here.
    Flood passes FLOOD_NO_RECOMMENDATION_ASSUMPTIONS_TEXT instead - see
    build_summary's own docstring for the same split on its full-footer
    counterpart.
    """
    lines = [
        "Soil refill estimate unavailable",
        f"  Warning: {reason}. This hourly model cannot calculate a safe "
        "amount for this case. Check the field's actual soil moisture "
        "directly before deciding how much to pump.",
        "",
        assumptions_text,
    ]
    return "\n".join(lines)


def build_no_irrigation_due_summary(
    depletion_mm: float,
    raw_mm: float,
    assumptions_text: str = FLOOD_NO_RECOMMENDATION_ASSUMPTIONS_TEXT,
) -> str:
    """Text for flood's "wait-until-low" method when the forecast week never
    reaches the no-stress limit (RAW) - no deadline, so no irrigation is due.

    compute_flood_weekly_summary() must not fall through to
    build_summary()'s own no-deadline branch in this case, which prints a
    gross-water/pump-time recommendation anyway - correct for drip (applied
    on a regular weekly schedule as a preventative top-up, regardless of
    whether RAW is reached) but wrong for flood, whose whole method is "wait
    until low": nothing is due until RAW is actually reached. Printing a
    pump amount (and sometimes a pump-capacity warning) here would tell the
    farmer to irrigate water they do not need yet.

    assumptions_text defaults to FLOOD_NO_RECOMMENDATION_ASSUMPTIONS_TEXT
    (flood's own footer, without the pump-feasibility/gross-water bullets
    that describe figures this function never prints). This case is
    flood-only - compute_weekly_summary (drip) never calls this function.
    """
    lines = [
        "Soil refill estimate",
        "  No irrigation is due within the forecast week - modeled soil "
        f"depletion stays below the {raw_mm:.1f} mm no-stress limit the "
        f"whole time (reaching {format_below_limit(depletion_mm, raw_mm)} mm "
        "by the end of the forecast). Re-run this tool nearer the time, or "
        "once the field has actually gone longer without water.",
        "",
        assumptions_text,
    ]
    return "\n".join(lines)


def unavailable_result(
    reason: str,
    assumptions_text: str = NO_RECOMMENDATION_ASSUMPTIONS_TEXT,
    kind: str = KIND_UNAVAILABLE,
    deadline: datetime | None = None,
) -> WeeklyResult:
    """The "no estimate possible" outcome: the usual warning text, and no numbers.

    kind is KIND_UNAVAILABLE unless the reason is that the modeled soil has
    reached the stress limit, or reaches it within hours and no safe amount can
    be sized (KIND_SOIL_AT_LIMIT): a card must tell the farmer to check the
    field promptly then, not just say "no estimate".
    """
    return WeeklyResult(
        kind=kind,
        text=build_immediate_warning_summary(reason, assumptions_text),
        deadline=deadline,
    )


def unschedulable_result(
    deadline: IrrigationDeadline,
    response_offset: timezone,
    assumptions_text: str = NO_RECOMMENDATION_ASSUMPTIONS_TEXT,
) -> WeeklyResult:
    """The result when the model reaches the stress limit and no safe amount can be sized.

    water_balance.irrigation_deadline() returns this in two situations. When the limit is
    reached within the very first forecast hour the reason has no time in it, and the card
    must say to check the field now (KIND_SOIL_AT_LIMIT). When a LATER hour's own water use
    alone reaches the limit, the reason names that hour, which can be days away; that gets its
    own card (KIND_SOIL_LIMIT_AHEAD) that says when, since "check the field now" would
    overstate the urgency.
    """
    assert deadline.unschedulable_reason is not None
    reason = deadline.unschedulable_reason
    if "{timestamp}" not in reason:
        return unavailable_result(reason, assumptions_text, kind=KIND_SOIL_AT_LIMIT)
    # One unschedulable case has a literal "{timestamp}" placeholder instead of a raw hour label:
    # water_balance.py has no timezone information to fill it in correctly itself, so the real
    # Casablanca time goes in here.
    moment = deadline_instant(deadline.timestamp, response_offset)
    reason = reason.format(timestamp=format_casablanca(moment))
    return unavailable_result(reason, assumptions_text, kind=KIND_SOIL_LIMIT_AHEAD, deadline=moment)


def amount_result(
    text: str,
    net_mm: float,
    gross_water_mm: float,
    volume_m3: float,
    pump_hours: float,
    deadline: datetime | None,
    hours_available: float | None,
    forecast_end: datetime | None = None,
) -> WeeklyResult:
    """Wrap an amount-giving report (text from build_summary) with its numbers.

    The kind (deadline fine, pump too slow, deadline passed, ...) comes from
    summary_kind(), which uses the same comparisons build_summary() does.
    """
    kind = summary_kind(deadline is not None, hours_available, pump_hours)
    if kind == KIND_WEEKLY_AMOUNT and volume_m3 <= 0.0:
        # A drip week that needs no water (rain covers it) has an amount of exactly 0. A card
        # must not tell a farmer to "water 0.0 m3"; it is the same situation as flood's
        # "nothing needed". A tiny real amount is not 0 and stays an amount.
        kind = KIND_NO_IRRIGATION_DUE
    return WeeklyResult(
        kind=kind,
        text=text,
        net_mm=net_mm,
        gross_water_mm=gross_water_mm,
        volume_m3=volume_m3,
        pump_hours=pump_hours,
        deadline=deadline,
        hours_available=hours_available,
        forecast_end=forecast_end,
    )


def _looks_like_a_real_different_field(other_key: str) -> bool:
    """True if other_key looks like a plausible, genuinely different field.

    state._looks_like_a_field_key() only checks a saved field_key's
    STRUCTURE (three comma-separated parts, coordinates formatted to 6
    decimals) - it can't check the soil segment against
    fao56.SOIL_TYPES without state.py importing fao56.py, crossing this
    codebase's one-file-per-concern boundary (see state.py's own
    docstring), and doesn't check whether the coordinates are even
    geographically real. That leaves a gap: an unrecognized soil name
    (e.g. "san", a typo of "sand") or an out-of-range/non-finite
    coordinate still passes the structural check and reads as "a
    different, legitimate field" - for example a saved 60mm sand-field
    depletion whose field_key's soil segment was corrupted to "san"
    would be treated as some other field entirely, silently discarding
    the real, same-field reading. This is the crop-aware half of that
    validation, done here (not in state.py) since it needs SOIL_TYPES,
    already imported in this module.

    Latitude/longitude bounds are plain geographic reality (-90 to 90,
    -180 to 180), not an FAO-56 constant - there is nothing to cite.
    """
    parts = other_key.split(",")
    if len(parts) != 3:
        return False
    lat_str, lon_str, soil_name = parts
    try:
        lat = float(lat_str)
        lon = float(lon_str)
    except ValueError:
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return False
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    return soil_name in SOIL_TYPES


def _compute_weekly_result(
    lat: float,
    lon: float,
    ha: float,
    pump_m3h: float,
    efficiency: float,
    soil_name: str,
    ec_water_ds_m: float,
    now: datetime | None = None,
    reset: bool = False,
    state_path: Path = STATE_PATH,
    monthly_kc: MonthlyKc | None = None,
) -> WeeklyResult:
    """Fetch weather, run the water balance, and build the summary text.

    now is when the tool is being run - used to check whether the
    recommended pump time still fits before the deadline (see
    hours_between_utc), and to split this run's fetched hours into
    "already happened" (catch up the real depletion) and "still ahead"
    (the actual forecast) instead of always assuming the soil starts full
    at the fetched window's calendar-day start. Not a CLI flag: farmers
    don't need to think about it, so it defaults to the real current
    time; tests pass a fixed value instead.

    ec_water_ds_m is the irrigation water's electrical conductivity
    (salinity), dS/m - a farm-specific measured value, like efficiency,
    with no default (see fao56.leaching_requirement).

    reset discards any saved depletion state and starts fresh (full soil,
    now, at hourly resolution) - use this right after actually irrigating, since the model has
    no other way to know that happened (see state.py). state_path is only
    a parameter so tests can point it at a temporary file instead of the
    real one in the working directory.
    """
    # Check inputs before the unschedulable path can return without converting
    # units. math.isfinite() catches NaN and +/-inf, which "> 0" alone would
    # not.
    _validate_ha_pump_efficiency(ha, pump_m3h, efficiency)
    # leaching_requirement() itself validates ec_water_ds_m is non-negative
    # and not so saline the formula breaks down - computed here, before the
    # unschedulable path can return, so a bad value is always reported
    # clearly rather than only on the code path that happens to use it.
    lr = leaching_requirement(ec_water_ds_m, CITRUS_ECE_THRESHOLD_DS_M)

    if now is None:
        now = datetime.now(CASABLANCA_TZ)

    key = field_key(lat, lon, soil_name)
    # Computed early, purely so the peek step below can validate a saved
    # depletion_mm against THIS crop's TAW before deciding whether to
    # fetch anything - state.py itself has no crop context to do this
    # check (see parse_state_file_any_field()'s own docstring), so it
    # was previously done only after fetching, too late to protect it
    # from the same silent-discard bug.
    soil = SOIL_TYPES[soil_name]
    taw_mm = total_available_water_mm(soil, CITRUS_ROOT_DEPTH_M)
    # Peeked BEFORE fetching so a saved anchor older than the default
    # PAST_DAYS window can still be reached by widening the fetch,
    # instead of load_state() silently rejecting it afterwards as if
    # there were no saved state at all (fetching the fixed window first
    # would discard a real depletion more than PAST_DAYS days old for an
    # assumed-full-soil guess, and the unconditional save further below
    # would then overwrite the file with that guess - silently losing a
    # real, possibly already-over-the-limit depletion for good).
    #
    # parse_state_file_any_field() (not parse_state_file()) here, so a
    # file that exists but could not be parsed/validated at all can be
    # told apart from a file that parses fine but is for a DIFFERENT
    # field - the latter is expected, ordinary use (switching which
    # field this single-file tool tracks) and must proceed normally;
    # the former might be real, corrupted recovery evidence for THIS
    # field and must not be silently discarded (parse_state_file(path,
    # key) would fold "no file", "wrong field", and "unreadable" into
    # the same None, so all three would fall through to the same
    # assumed-full-soil guess - for example an otherwise-valid
    # sand-field 60mm depletion whose saved timestamp was corrupted).
    unreadable_state_warning = unavailable_result(
        "the saved soil-depletion state file exists but could not be read or "
        "validated - it may be corrupted. This tool will not guess a starting soil "
        "condition when real recovery evidence might exist. Check the field's "
        "actual soil moisture directly, then re-run with --reset once you've "
        "confirmed or restored a full soil state"
    )
    peeked_any_field = None if reset else parse_state_file_any_field(state_path)
    if peeked_any_field is not None and peeked_any_field.field_key != key:
        # A field_key that parses as STRUCTURALLY valid (state.py's own
        # _looks_like_a_field_key()) still isn't necessarily a real,
        # different field - it could be corrupted in a way that keeps
        # the shape but breaks the content (an unrecognized soil name,
        # an impossible coordinate). state.py can't check that itself
        # (no fao56.SOIL_TYPES import, one-file-per-concern - see its
        # own docstring), so it's done here instead (for example a saved
        # 60mm sand-field depletion whose field_key's soil segment was
        # corrupted to "san" must not be treated as a different,
        # legitimate field and silently discarded).
        if _looks_like_a_real_different_field(peeked_any_field.field_key):
            peeked = None
        else:
            return unreadable_state_warning
    elif not reset and peeked_any_field is None and state_path.exists():
        # A state file IS there, but nothing about it could be trusted -
        # not even which field it was for. Preserve it (no fetch, no
        # save) rather than guess full soil over what might be real,
        # unreadable evidence.
        return unreadable_state_warning
    else:
        peeked = peeked_any_field
    if peeked is not None and peeked.depletion_mm > taw_mm:
        # depletion_mm (Dr) can't physically exceed this crop's TAW - an
        # implausible VALUE for a state that otherwise parsed fine and
        # matched this exact field, not evidence the soil is full. The
        # check runs BEFORE fetching: running it afterwards would
        # silently discard the state and let the unconditional save
        # overwrite it with an assumed-full-soil guess (for example a
        # saved 91mm depletion against sand's 90mm TAW). Same
        # preserve-and-warn treatment as every other "state exists for
        # this field but can't be trusted" case above - no fetch, no
        # save.
        return unavailable_result(
            f"the saved soil-depletion state from {peeked.anchor_timestamp} "
            f"({peeked.depletion_mm:.1f} mm) is more than this field's total available "
            f"water ({taw_mm:.1f} mm) - that isn't physically possible, so this reading "
            "can't be trusted. Check the field's actual soil moisture directly, then "
            "re-run with --reset once you've confirmed or restored a full soil state"
        )
    past_days = PAST_DAYS
    if peeked is not None:
        peeked_anchor = state_anchor_instant(peeked)
        if peeked_anchor <= now:
            # Same whole-calendar-day convention compute_flood_weekly_
            # summary() uses for its own farmer-given date, plus the same
            # +2 day buffer against timezone/DST boundary ambiguity - see
            # its own comment.
            days_since_anchor = (
                now.astimezone(CASABLANCA_TZ).date()
                - peeked_anchor.astimezone(CASABLANCA_TZ).date()
            ).days
            if days_since_anchor > PAST_DAYS - 2:
                wanted_past_days = days_since_anchor + 2
                if wanted_past_days > PLAUSIBLE_MAX_PAST_DAYS:
                    # Beyond even Open-Meteo's own history limit - there
                    # is no fetch wide enough to recover this anchor.
                    # Return WITHOUT fetching or saving anything, so the
                    # real (if stale) saved depletion survives on disk
                    # for a later, better-informed run instead of being
                    # silently discarded.
                    return unavailable_result(
                        f"the saved soil-depletion state from {peeked.anchor_timestamp} is "
                        f"more than {PLAUSIBLE_MAX_PAST_DAYS - 2} days old - too old for this "
                        "tool to fetch enough weather history to safely continue from it. "
                        "Check the field's actual soil moisture directly, then re-run with "
                        "--reset once you've confirmed or restored a full soil state"
                    )
                past_days = wanted_past_days
        else:
            # peeked_anchor > now: the saved anchor is AFTER the current
            # time - most likely the system clock moved backward since it
            # was saved (less likely, a corrupted timestamp). Widening the
            # fetch cannot help here - the problem isn't reach, it's `now`
            # being earlier than the anchor - and falling through to the
            # normal no-saved-state path would hit exactly the same
            # data-loss shape the stale-anchor case above avoids, just
            # triggered from the other direction: load_state() rejects
            # this anchor too (it's outside [oldest_usable, now]), the
            # caller can't tell that apart from "no state at all", and the
            # unconditional save further below would overwrite a real,
            # possibly dry depletion reading with an assumed-full-soil
            # guess (for example a saved 60mm depletion timestamped one
            # hour ahead of `now`). Preserve the file and say why instead.
            return unavailable_result(
                f"the saved soil-depletion state from {peeked.anchor_timestamp} is AFTER "
                "the current time - the system clock may have moved backward since it was "
                "saved. This tool cannot safely continue from a future-dated reading. Check "
                "the field's actual soil moisture directly, then re-run with --reset once "
                "you've confirmed or restored a full soil state"
            )

    data = fetch_hourly_json(lat, lon, past_days=past_days)
    # Open-Meteo returns ONE fixed utc_offset_seconds for the whole
    # response, based on the offset in effect when the request was made -
    # it does not shift mid-response across a real Morocco clock change
    # (a documented Open-Meteo limitation, open-meteo/open-meteo#1764).
    # So every timestamp in `data["hourly"]["time"]` must be interpreted
    # with THIS SAME fixed offset, not with CASABLANCA_TZ's real,
    # date-varying rules - using ZoneInfo here would silently disagree
    # with what Open-Meteo actually meant for any hour on the other side
    # of a transition within the fetched window. `now`, by contrast, is a
    # real live moment and correctly uses CASABLANCA_TZ's accurate rules.
    response_offset_seconds = response_utc_offset_seconds(data)
    response_offset = timezone(timedelta(seconds=response_offset_seconds))
    all_hours = parse_hourly_weather(data)
    # `forecast` itself is not used directly - the hours to run are
    # derived from `all_hours` and the anchor timestamp instead (see
    # below), so it can reach back into `past` too when a saved anchor
    # is older than today. `past` is still needed for the
    # no-saved-state fallback and the staleness check. past_hours must
    # match whatever past_days the fetch above actually used - still
    # PAST_DAYS*24 normally, but wider whenever a peeked saved anchor
    # needed more history than that to reach.
    past, _forecast = split_past_and_forecast(all_hours, past_hours=past_days * 24)

    # soil/taw_mm/key were already computed above, before the peek step -
    # unchanged here, just no longer recomputed.
    raw_mm = readily_available_water_mm(taw_mm, CITRUS_DEPLETION_FRACTION_P)
    dual_kc = DualKcParams(
        kcb=CITRUS_KCB,
        canopy_fraction=CITRUS_CANOPY_FRACTION,
        wetted_fraction_table20=DRIP_FW_TRICKLE,
        apply_drip_shading=True,
        crop_height_m=CITRUS_HEIGHT_M,
        ze_m=TOPSOIL_EVAP_DEPTH_ZE_M,
    )

    # Pick up real depletion from a previous run instead of always
    # re-assuming full soil - otherwise re-running mid-day after a
    # deadline had passed would just repeat the same problem, since the
    # model would start over from midnight every time.
    #
    # No depletion_mm > taw_mm check here anymore - the peek step above
    # already returns before this point whenever a same-field peeked
    # state fails that check, so load_state() below can no longer return
    # one that would. Removing this made-unreachable check rather than
    # leaving it as dead code.
    first_fetched = deadline_instant(all_hours[0].timestamp, response_offset)
    saved = None if reset else load_state(state_path, key, first_fetched, now)
    if peeked is not None and saved is None:
        # The peek step already fully validated this state (right field,
        # plausible depletion, unambiguous, within reach) and sized
        # past_days to cover its anchor - so the ONLY way load_state()
        # can still reject it here is if the actual fetched weather
        # didn't really cover as far back as requested, despite passing
        # whatever row-count check weather.py does (a
        # correctly-sized-count but time-shifted response - matching row
        # count alone doesn't guarantee matching coverage). Falling
        # through to the ordinary no-saved-state path here would hit the
        # same data-loss shape as the cases above: assumed-full-soil,
        # then the unconditional save overwriting a real, possibly
        # already-over-the-limit depletion. This is a weather-fetch
        # problem, not a farmer-input problem, so no --reset suggestion.
        return unavailable_result(
            f"the saved soil-depletion state from {peeked.anchor_timestamp} could not be "
            "verified against the weather just fetched - the response didn't cover as far "
            "back as expected. This may be a temporary problem with the weather service; "
            "try again shortly. Check the field's actual soil moisture directly if this "
            "keeps happening"
        )
    # tew_mm/drip_few/max_few duplicate run_forecast_period's own
    # calculations - needed here too, purely to validate a saved
    # de_mm/few. Unlike depletion_mm above, an invalid de_mm/few does NOT
    # discard the whole saved anchor (doing so would silently throw away a
    # perfectly valid depletion_mm too, which could hide an imminent RAW
    # crossing behind a false "full soil" restart) - only de_mm/few
    # themselves reset to fresh, same as a normal no-saved-state run
    # already assumes. A saved few must match one of the only two values
    # this model can actually produce (Eq. 75 at the drip fraction, or at
    # fw=1.0 for significant rain) - checking only for a value ABOVE the
    # maximum would miss an impossibly small one below the drip fraction
    # (state.py's own generic 0-1 bound lets e.g. 0.01 through,
    # understating evaporation).
    tew_mm = total_evaporable_water_mm(soil, TOPSOIL_EVAP_DEPTH_ZE_M)
    drip_few = exposed_wetted_fraction(
        CITRUS_CANOPY_FRACTION, drip_wetted_fraction(DRIP_FW_TRICKLE, CITRUS_CANOPY_FRACTION)
    )
    max_few = exposed_wetted_fraction(CITRUS_CANOPY_FRACTION, fw=1.0)
    if saved is not None and (
        saved.de_mm > tew_mm
        or (
            saved.few is not None
            and not math.isclose(saved.few, drip_few, abs_tol=1e-9)
            and not math.isclose(saved.few, max_few, abs_tol=1e-9)
        )
    ):
        saved = FieldState(
            anchor_timestamp=saved.anchor_timestamp,
            anchor_utc_offset_seconds=saved.anchor_utc_offset_seconds,
            depletion_mm=saved.depletion_mm,
            field_key=saved.field_key,
            de_mm=0.0,
            few=None,
        )
    if saved is not None:
        anchor_timestamp = saved.anchor_timestamp
        anchor_offset = timezone(timedelta(seconds=saved.anchor_utc_offset_seconds))
        anchor_depletion_mm = saved.depletion_mm
        anchor_de_mm = saved.de_mm
        anchor_few = saved.few
    elif reset:
        # A reset anchors full soil at now, which skips the other coverage
        # checks. The response still has to reach back to that anchor: a
        # response starting after now leaves the hours in between with no
        # weather at all, so the tool would print a deadline that ignores
        # them (for example a reset on 2026-09-20 12:00 and 336 hours
        # starting 2026-09-28). Checked before anything is saved.
        if first_fetched - timedelta(hours=1) > now:
            return unavailable_result(
                "the weather just fetched for this field starts after the moment of the "
                "reset, so there is no weather for the hours in between - no "
                "recommendation is given and nothing was saved. This may be a temporary "
                "problem with the weather service; try again shortly"
            )
        # Irrigation happened now: a fresh full-soil anchor must not replay
        # the already elapsed hours of this forecast day.
        anchor_timestamp = (
            now.astimezone(response_offset).replace(tzinfo=None).isoformat(timespec="seconds")
        )
        anchor_offset = response_offset
        anchor_depletion_mm = 0.0
        anchor_de_mm = 0.0
        anchor_few = None
    else:
        # No usable saved state (none exists, it's for
        # a different field/soil, or it's older than this run's earliest
        # fetched hour - see load_state). Fall back to today's behavior:
        # soil starts full at the beginning of the forecast week. Anchoring
        # on the LAST hour of `past` (rather than forecast[0] itself) means
        # "every hour after the anchor" is exactly `forecast`, unifying
        # this branch with the saved-state one below instead of needing
        # separate logic.
        anchor_timestamp = past[-1].timestamp
        # That position-based anchor is only meaningful if it is not in the
        # future. A response shifted forward in time (right row count,
        # consecutive hours, covers the forecast end) puts the split after
        # now: the tool would skip the drying of the hours between, print a
        # normal-looking recommendation, and save a future-dated state that
        # the next run rejects as its own future (for example a response
        # starting 2026-09-14 on 2026-09-20 12:00). Checked before anything
        # is calculated or saved.
        #
        # A shifted response is caught here too. Open-Meteo always starts
        # at midnight of the day it was asked about, so the last hour of
        # the past half is labelled 23:00 on the day BEFORE today. Any
        # other label means part of a day, or a whole day, is missing,
        # and the tool would skip that much drying: measured at 38.2 mm
        # with full coverage against 27.0 mm with a 12-hour shift, and a
        # one-day shift at 23:30 assumed full soil at 23:00 and skipped
        # nearly the whole day.
        anchor_is_in_the_future = deadline_instant(anchor_timestamp, response_offset) > now
        expected_anchor_day = now.astimezone(response_offset).date() - timedelta(days=1)
        anchor_label = datetime.fromisoformat(anchor_timestamp)
        anchor_is_not_end_of_yesterday = (
            anchor_label.date() != expected_anchor_day or anchor_label.hour != 23
        )
        if anchor_is_in_the_future or anchor_is_not_end_of_yesterday:
            return unavailable_result(
                "the weather just fetched for this field does not start at the requested "
                "time, so the starting point for the estimate would be wrong - no "
                "recommendation is given and nothing was saved. This may be a temporary "
                "problem with the weather service; try again shortly"
            )
        anchor_offset = response_offset
        anchor_depletion_mm = 0.0
        anchor_de_mm = 0.0
        anchor_few = None

    anchor_instant = deadline_instant(anchor_timestamp, anchor_offset)
    hours_after_anchor = [
        h for h in all_hours if deadline_instant(h.timestamp, response_offset) > anchor_instant
    ]
    # Split on *now*, not a calendar boundary: hours whose data already
    # covers real, already-happened time get used to catch up the real
    # depletion; only hours still ahead of now are the actual forecast
    # this run reports on. (deadline_instant is safe to reuse here even
    # though these aren't "deadlines" - it's just "the real instant this
    # timestamp's data ends", which is exactly what's needed for both.)
    catch_up_hours = [
        h for h in hours_after_anchor if deadline_instant(h.timestamp, response_offset) <= now
    ]
    future_hours = [
        h for h in hours_after_anchor if deadline_instant(h.timestamp, response_offset) > now
    ]

    catch_up_results = run_forecast_period(
        catch_up_hours,
        soil,
        taw_mm,
        dual_kc,
        anchor_depletion_mm,
        anchor_de_mm,
        anchor_few,
        monthly_kc=monthly_kc,
    )
    current_few: float | None
    if catch_up_results:
        current_timestamp = catch_up_results[-1].timestamp
        current_depletion_mm = catch_up_results[-1].depletion_mm
        # De and few both dry out / persist continuously across the
        # catch-up/future split within this one run, AND across separate
        # tool runs via anchor_de_mm/anchor_few above - see
        # run_forecast_period's docstring.
        current_de_mm = catch_up_results[-1].de_mm
        current_few = catch_up_results[-1].few
    else:
        # No hours to catch up on (e.g. the saved anchor is already "now").
        current_timestamp = anchor_timestamp
        current_depletion_mm = anchor_depletion_mm
        current_de_mm = anchor_de_mm
        current_few = anchor_few
    current_offset_seconds = (
        response_offset_seconds
        if catch_up_results
        else int(anchor_offset.utcoffset(None).total_seconds())
    )

    # Saved unconditionally, right here, before anything below that can
    # still raise (a bad efficiency/pump/EC combination reaching
    # gross_mm()/mm_to_m3()/m3_to_pump_hours(), or simply no future hours
    # left) - "now's true depletion" (the catch-up result) is fully
    # determined already, and saving it doesn't depend on whether the
    # rest of this run can produce a printable recommendation (saving
    # only at the end of each return path would let a later conversion
    # failure silently lose it - the next run would then redo catch-up
    # from the OLD anchor, and if that had since fallen outside the 7-day
    # usable window, would restart from full soil instead of this run's
    # real, already-known current state).
    save_state(
        state_path,
        FieldState(
            anchor_timestamp=current_timestamp,
            anchor_utc_offset_seconds=current_offset_seconds,
            depletion_mm=current_depletion_mm,
            field_key=key,
            de_mm=current_de_mm,
            few=current_few,
        ),
    )

    # After the save above on purpose: the catch-up depletion is real
    # even when the forecast part of the response is short, so it is kept
    # for the next run. Only the recommendation is withheld. This check
    # comes BEFORE the "no future hours" error below, because a stale
    # response that ends before now would otherwise crash with that error
    # instead of showing this warning.
    if not forecast_ends_as_requested(all_hours[-1].timestamp, response_offset, now):
        return unavailable_result(
            "the weather just fetched for this field does not end where the forecast "
            "week should end - the response may be stale or shifted, so no recommendation "
            "is given. This may be a temporary problem with the weather service; try "
            "again shortly"
        )

    if not future_hours:
        raise ValueError("weather response has no future hours after now")

    if current_depletion_mm >= raw_mm:
        # irrigation_deadline() below only scans the FUTURE hours - if the
        # catch-up phase already brought depletion to or past the no-stress
        # limit, and the future forecast's first hours happen to have
        # enough rain to bring the trajectory back down,
        # irrigation_deadline() would never see that the limit was already
        # reached and could report an ordinary later deadline instead,
        # hiding that the crop may already be stressed right now. Checked
        # directly, before looking at the future at all.
        current_instant = datetime.fromisoformat(current_timestamp).replace(
            tzinfo=timezone(timedelta(seconds=current_offset_seconds))
        )
        return unavailable_result(
            f"the modeled soil depletion has already reached the no-stress "
            f"limit as of {format_casablanca(current_instant)} "
            f"({current_depletion_mm:.1f} mm, at or above the {raw_mm:.1f} mm limit) - "
            "this tool has no earlier point than now to size a safe amount from",
            kind=KIND_SOIL_AT_LIMIT,
        )

    results = run_forecast_period(
        future_hours,
        soil,
        taw_mm,
        dual_kc,
        current_depletion_mm,
        current_de_mm,
        current_few,
        monthly_kc=monthly_kc,
    )
    deadline = irrigation_deadline(results, raw_mm, taw_mm)
    if deadline is not None and deadline.amount_mm is None:
        return unschedulable_result(deadline, response_offset)

    net_mm = weekly_net_need_mm(results, raw_mm, taw_mm)
    irrigate_by = deadline.timestamp if deadline is not None else None

    gross_water_mm = gross_mm(net_mm, efficiency, lr)
    volume_m3 = mm_to_m3(gross_water_mm, ha)
    pump_hours = m3_to_pump_hours(volume_m3, pump_m3h)

    hours_available = hours_between_utc(
        now, deadline_instant(future_hours[-1].timestamp, response_offset)
    )
    irrigate_by_display = None
    deadline_moment = None
    if irrigate_by is not None:
        instant = deadline_instant(irrigate_by, response_offset)
        deadline_moment = instant
        hours_available = hours_between_utc(now, instant)
        # Display in the farm's real Casablanca clock time, not
        # Open-Meteo's raw label - those can differ across a real offset
        # change even though `instant` (used for the arithmetic above) is
        # correct either way.
        irrigate_by_display = format_casablanca(instant)

    text = build_summary(
        net_mm, gross_water_mm, volume_m3, pump_hours, irrigate_by_display, hours_available
    )
    forecast_end = deadline_instant(future_hours[-1].timestamp, response_offset)
    result = amount_result(
        text,
        net_mm,
        gross_water_mm,
        volume_m3,
        pump_hours,
        deadline_moment,
        hours_available,
        forecast_end,
    )
    daily = daily_net_use(results, response_offset, deadline_moment)
    return dataclasses.replace(result, daily_net_use_mm=daily)


def compute_weekly_result(
    lat: float,
    lon: float,
    ha: float,
    pump_m3h: float,
    efficiency: float,
    soil_name: str,
    ec_water_ds_m: float,
    now: datetime | None = None,
    reset: bool = False,
    state_path: Path = STATE_PATH,
    monthly_kc: MonthlyKc | None = None,
) -> WeeklyResult:
    """Run the drip calculation and stamp the result with the moment it was made.

    monthly_kc: a Kc fitted for this farm (kc.py). None keeps the textbook
    dual Kc method described in the assumptions text. When set, a line saying
    so is added to the end of the report text, because the assumptions text
    above it describes the dual method.
    """
    if now is None:
        now = datetime.now(CASABLANCA_TZ)
    result = _compute_weekly_result(
        lat,
        lon,
        ha,
        pump_m3h,
        efficiency,
        soil_name,
        ec_water_ds_m,
        now=now,
        reset=reset,
        state_path=state_path,
        monthly_kc=monthly_kc,
    )
    if monthly_kc is not None:
        result = dataclasses.replace(result, text=result.text + MONTHLY_KC_NOTE)
    return dataclasses.replace(result, made_at=now)


def compute_weekly_summary(
    lat: float,
    lon: float,
    ha: float,
    pump_m3h: float,
    efficiency: float,
    soil_name: str,
    ec_water_ds_m: float,
    now: datetime | None = None,
    reset: bool = False,
    state_path: Path = STATE_PATH,
    monthly_kc: MonthlyKc | None = None,
) -> str:
    """Same as compute_weekly_result(), but returns only the report text."""
    return compute_weekly_result(
        lat,
        lon,
        ha,
        pump_m3h,
        efficiency,
        soil_name,
        ec_water_ds_m,
        now=now,
        reset=reset,
        state_path=state_path,
        monthly_kc=monthly_kc,
    ).text


def _compute_flood_weekly_result(
    lat: float,
    lon: float,
    ha: float,
    pump_m3h: float,
    efficiency: float,
    soil_name: str,
    ec_water_ds_m: float,
    last_watering_date: str,
    post_watering_fullness: float = 1.0,
    watering_time: str | None = None,
    now: datetime | None = None,
) -> WeeklyResult:
    """Fetch weather, run the water balance, and build the summary text - flood irrigation.

    Unlike compute_weekly_summary() (drip's "top-up" method, which assumes
    the field was recently topped up), flood-irrigated fields go WEEKS
    between waterings - this takes the farmer's last watering date
    directly instead of relying on a saved or assumed-recent depletion.
    With post_watering_fullness left at its default (see below), soil is
    assumed FULL starting at 00:00 Casablanca time on that date - if the
    real watering happened later in the day, this slightly overstates how
    long the soil has been drying, the same kind of one-day-granularity
    imprecision --reset already has for drip (see ASSUMPTIONS_TEXT). This
    is a safe direction to be imprecise in: it can only make the tool
    recommend water a little earlier than strictly needed, never later.

    post_watering_fullness is how full the root zone actually was, as a
    fraction of a full tank, right after last_watering_date's watering
    finished - from 0 (exclusive) to 1 (a full refill - the default, and
    the only behavior before this parameter existed). This is NOT "what
    fraction of the prior shortfall got closed" - this tool has no saved
    depletion to measure a shortfall from in the first place, only ever
    this one fresh number (a description like "how much of a full refill
    was reached" would name the wrong quantity, since it reads like the
    shortfall interpretation, while the code computes the fullness
    interpretation). Flood/basin watering rarely wets a field evenly, and
    a farmer has no meter to check it, so this is a best guess, not a
    measurement - but a guess beats always assuming full. Guessing LOW is
    the safer mistake: it starts the model drier than reality, which
    moves its next recommendation earlier/larger than strictly needed
    (wastes a little water). Guessing HIGH (or leaving the 1.0 default
    when the refill wasn't full) starts the model wetter than reality,
    which moves the recommendation later/smaller than actually needed -
    the direction that can under-water the crop (always assuming a full
    refill could silently recommend too little water, or say none was due
    at all, after a genuinely partial watering).

    watering_time is the clock time watering actually finished, "HH:MM"
    24-hour Casablanca time - ALWAYS required. It is needed even for a
    partial refill: rain before the real watering time would otherwise be
    credited against it and understate what the soil still needs (for
    example sand, post_watering_fullness=0.4, watering at 18:00, 30mm
    rain at 06:00 flips an over-the-limit case to "no irrigation due"). A
    full refill is not immune either: the "midnight is the safe default"
    idea only holds for the root zone. Drying and then rain earlier on
    the watering day can refill the modeled root zone while the thin
    surface layer is still partly dry, and a real evening watering
    re-wets that surface, which then loses more water than a midnight
    start predicts. For example, with sand, full watering on September 16
    at 18:00, 8.4 mm ET0 and 7.5 mm rain earlier that day, then 7.26 mm
    ET0/day, a September 20 run without the time says "no irrigation
    due", while with 18:00 the deadline was September 26. So there is no
    default.

    Requiring watering_time does not fully close the gap by itself,
    though: weather is only ever hourly, so a watering_time that doesn't
    fall exactly on an hour boundary (e.g. "18:30") still leaves the ONE
    hour bucket containing it straddling the real watering instant -
    Open-Meteo's rain figure for that bucket is a preceding-hour sum (see
    deadline_instant), mixing whatever fell before 18:30 with whatever
    fell after it, with no way to split the two from hourly data alone.
    _exclude_pre_watering_rain() (below) zeroes that one straddling
    bucket's rain (keeping its ET0, since losing at most an hour of real
    drying credit is a small, bounded error next to a rain total that can
    flip the whole recommendation) before catch-up/future hours are even
    split out.

    No saved state, no --reset: last_watering_date, post_watering_
    fullness and watering_time are all given fresh each run (update them
    once you've actually re-watered), so there's nothing to remember
    between runs - unlike drip, state.py is not used here at all.

    Deliberately a SEPARATE function from compute_weekly_summary() rather
    than a branch inside it, even though the two share real structure
    (fetch, split by "now", catch-up then future, the same immediate-
    warning/unschedulable checks) - lower risk than threading a second
    control flow through the existing drip logic. The shared pieces that
    don't touch drip's own logic (_validate_ha_pump_efficiency, the
    conversion/output functions, build_summary) are still reused as-is.

    now is when the tool is being run - same role as
    compute_weekly_summary's own now, not a CLI flag; defaults to the
    real current time, tests pass a fixed value instead.
    """
    _validate_ha_pump_efficiency(ha, pump_m3h, efficiency)
    if not (0.0 < post_watering_fullness <= 1.0):
        raise ValueError(
            f"post_watering_fullness ({post_watering_fullness!r}) must be greater than 0 and "
            "at most 1 - 0 means no watering happened at all (not a real last_watering_date), "
            "and more than 1 means fuller than a full tank, which this tool has no way to use"
        )
    if watering_time is None:
        raise ValueError(
            "watering_time is required - without the real time, rain and drying earlier on "
            "the watering day cannot be told apart from what happened after the watering, "
            "which can hide a needed irrigation"
        )
    lr = leaching_requirement(ec_water_ds_m, CITRUS_ECE_THRESHOLD_DS_M)

    if now is None:
        now = datetime.now(CASABLANCA_TZ)

    try:
        anchor_date = datetime.strptime(last_watering_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(
            f"last_watering_date ({last_watering_date!r}) must be an ISO date, YYYY-MM-DD"
        ) from exc
    try:
        anchor_time = datetime.strptime(watering_time, "%H:%M").time()
    except ValueError as exc:
        raise ValueError(
            f"watering_time ({watering_time!r}) must be an HH:MM 24-hour clock time"
        ) from exc
    anchor_instant = datetime.combine(anchor_date.date(), anchor_time, tzinfo=CASABLANCA_TZ)
    # Morocco's own clock changes create a genuinely repeated wall-clock
    # hour once a year (fall-back) - the SAME "--last-watering-date
    # --watering-time" combination can refer to two different real
    # instants, exactly one hour apart, and datetime.combine() silently
    # picks the EARLIER one (Python's default fold=0) with no error.
    # Reject this instead of guessing: an anchor placed an hour earlier
    # than the farmer meant can make _exclude_pre_watering_rain() drop
    # the wrong hour's rain, letting rain that actually fell AFTER the
    # real watering get wrongly excluded, or rain that fell before it
    # get wrongly credited - either way undermining the very guard that
    # flag exists for (for example sand, post_watering_fullness=0.4, and
    # watering_time landing in the repeated hour on Morocco's 2026-02-15
    # transition: the wrong rain exclusion flips an
    # already-over-the-limit case to "no irrigation is due").
    if anchor_instant.utcoffset() != anchor_instant.replace(fold=1).utcoffset():
        # A clock change gives one of two cases. In the skipped hour (clock
        # jumps forward) the time never happens, and converting it to UTC
        # and back lands on a different clock reading. In the repeated hour
        # (clock goes back) the time happens twice and the round trip
        # returns the same reading.
        round_trip = anchor_instant.astimezone(UTC).astimezone(CASABLANCA_TZ)
        if round_trip.replace(tzinfo=None) != anchor_instant.replace(tzinfo=None):
            raise ValueError(
                f"watering_time ({watering_time!r}) on {last_watering_date} does not exist - "
                "the Moroccan clock jumped forward that day and skipped this local time. "
                "Give a watering_time outside the skipped hour"
            )
        raise ValueError(
            f"watering_time ({watering_time!r}) on {last_watering_date} happens TWICE "
            "today - Morocco's clock changed that day, so this local time is ambiguous "
            "by exactly one hour. Give a watering_time outside that repeated hour, or "
            "use --last-watering-date for the day before/after if the real watering was "
            "close to midnight"
        )
    if anchor_instant > now:
        raise ValueError("last_watering_date/watering_time must not be in the future")
    # A whole-calendar-day count, not hours_between_utc's fractional-hour
    # arithmetic - past_days is itself a whole-day Open-Meteo parameter.
    # +2 days of buffer past the naive day count covers any timezone/DST
    # boundary ambiguity around exactly which hours "N past days" returns
    # - the anchor-based hour filtering below is exact regardless of a
    # little extra fetched history either way.
    days_since_watering = (now.astimezone(CASABLANCA_TZ).date() - anchor_date.date()).days
    if days_since_watering > PLAUSIBLE_MAX_PAST_DAYS - 2:
        raise ValueError(
            f"last_watering_date is {days_since_watering} days ago - beyond what this tool "
            f"can fetch weather for (Open-Meteo's own {PLAUSIBLE_MAX_PAST_DAYS}-day limit) - "
            "check the date"
        )
    past_days = days_since_watering + 2

    data = fetch_hourly_json(lat, lon, past_days=past_days)
    # See compute_weekly_summary's own comment on response_offset for why
    # this fixed-per-response offset (not CASABLANCA_TZ's real,
    # date-varying rules) is what every fetched timestamp must be
    # interpreted with.
    response_offset = timezone(timedelta(seconds=response_utc_offset_seconds(data)))
    all_hours = parse_hourly_weather(data)
    split_past_and_forecast(all_hours, past_hours=past_days * 24, forecast_hours=FORECAST_HOURS)
    # A response with the right ROW COUNT can still not actually reach
    # back to the watering anchor, if it's shifted to start later than
    # requested - split_past_and_forecast()'s count check above can't
    # catch that (for example sand, watering on Sept 10, now Sept 20 -
    # complete coverage correctly gives 78.3mm depletion and an urgent
    # warning; a response shifted to start on Sept 20 would silently
    # print "no irrigation is due" at 16.6mm, since the missing 10
    # days of real drying history are just absent from the data, not
    # flagged). Unlike drip, flood has no saved state to preserve here, so
    # there is nothing to lose by fetching again, just nothing safe to
    # calculate from an incomplete response.
    earliest_fetched_start = deadline_instant(all_hours[0].timestamp, response_offset) - timedelta(
        hours=1
    )
    if earliest_fetched_start > anchor_instant:
        return unavailable_result(
            "the weather just fetched for this field doesn't reach back far enough to "
            f"cover {last_watering_date} - the response may be incomplete. This may be a "
            "temporary problem with the weather service; try again shortly",
            FLOOD_NO_RECOMMENDATION_ASSUMPTIONS_TEXT,
        )

    # Same idea for the far end: a response can cover the watering
    # date and still stop days before the forecast week ends.
    if not forecast_ends_as_requested(all_hours[-1].timestamp, response_offset, now):
        return unavailable_result(
            "the weather just fetched for this field does not end where the forecast "
            "week should end - the response may be stale or shifted. This may be a "
            "temporary problem with the weather service; try again shortly",
            FLOOD_NO_RECOMMENDATION_ASSUMPTIONS_TEXT,
        )

    soil = SOIL_TYPES[soil_name]
    taw_mm = total_available_water_mm(soil, CITRUS_ROOT_DEPTH_M)
    raw_mm = readily_available_water_mm(taw_mm, CITRUS_DEPLETION_FRACTION_P)
    dual_kc = DualKcParams(
        kcb=CITRUS_KCB,
        canopy_fraction=CITRUS_CANOPY_FRACTION,
        wetted_fraction_table20=FLOOD_FW_TABLE20,
        apply_drip_shading=False,
        crop_height_m=CITRUS_HEIGHT_M,
        ze_m=TOPSOIL_EVAP_DEPTH_ZE_M,
    )

    # Same "filter by real instant, not by the past/future split index"
    # approach compute_weekly_summary uses for a saved-state anchor - see
    # its own comment for why deadline_instant is safe to reuse for this.
    hours_after_anchor = [
        h for h in all_hours if deadline_instant(h.timestamp, response_offset) > anchor_instant
    ]
    left_out_rain_mm = (
        hours_after_anchor[0].rain_mm
        if _first_hour_straddles_anchor(hours_after_anchor, anchor_instant, response_offset)
        else 0.0
    )
    rain_note = rain_left_out_note(left_out_rain_mm)
    hours_after_anchor = _exclude_pre_watering_rain(
        hours_after_anchor, anchor_instant, response_offset
    )
    catch_up_hours = [
        h for h in hours_after_anchor if deadline_instant(h.timestamp, response_offset) <= now
    ]
    future_hours = [
        h for h in hours_after_anchor if deadline_instant(h.timestamp, response_offset) > now
    ]
    if not future_hours:
        raise ValueError("weather response has no future hours after now")

    # post_watering_fullness only adjusts the ROOT ZONE starting point
    # (Dr) - the surface evaporation layer (De/few) still always starts
    # fully wet regardless, same as before this parameter existed. Water
    # reaching the topsoil to wet it for evaporation is a separate
    # question from how much reached the whole root depth, and a partial
    # watering understates root-zone depletion, not the evaporation
    # layer.
    starting_depletion_mm = (1.0 - post_watering_fullness) * taw_mm
    catch_up_results = run_forecast_period(
        catch_up_hours, soil, taw_mm, dual_kc, starting_depletion_mm=starting_depletion_mm
    )
    if catch_up_results:
        current_depletion_mm = catch_up_results[-1].depletion_mm
        current_de_mm = catch_up_results[-1].de_mm
        current_few = catch_up_results[-1].few
    else:
        current_depletion_mm = starting_depletion_mm
        current_de_mm = 0.0
        current_few = None

    if current_depletion_mm >= raw_mm:
        # Same reasoning as compute_weekly_summary's own check: the
        # future-only irrigation_deadline() scan below could otherwise
        # miss that the no-stress limit was already crossed during
        # catch-up, if the future forecast's first hours happen to have
        # enough rain to mask it.
        return unavailable_result(
            f"the modeled soil depletion has already reached the no-stress limit since "
            f"{last_watering_date} ({current_depletion_mm:.1f} mm, at or above the "
            f"{raw_mm:.1f} mm limit) - this tool has no earlier point than now to size a "
            "safe amount from",
            FLOOD_NO_RECOMMENDATION_ASSUMPTIONS_TEXT + rain_note,
            kind=KIND_SOIL_AT_LIMIT,
        )

    results = run_forecast_period(
        future_hours, soil, taw_mm, dual_kc, current_depletion_mm, current_de_mm, current_few
    )
    deadline = irrigation_deadline(results, raw_mm, taw_mm)
    if deadline is not None and deadline.amount_mm is None:
        return unschedulable_result(
            deadline, response_offset, FLOOD_NO_RECOMMENDATION_ASSUMPTIONS_TEXT + rain_note
        )

    net_mm = weekly_net_need_mm(results, raw_mm, taw_mm)
    if deadline is None:
        # No deadline at all within the forecast - see
        # build_no_irrigation_due_summary's own docstring for why flood
        # (unlike drip) must not fall through to a preventative top-up
        # recommendation here.
        return WeeklyResult(
            kind=KIND_NO_IRRIGATION_DUE,
            text=build_no_irrigation_due_summary(
                net_mm, raw_mm, FLOOD_NO_RECOMMENDATION_ASSUMPTIONS_TEXT + rain_note
            ),
            forecast_end=deadline_instant(future_hours[-1].timestamp, response_offset),
        )
    irrigate_by = deadline.timestamp

    gross_water_mm = gross_mm(net_mm, efficiency, lr)
    volume_m3 = mm_to_m3(gross_water_mm, ha)
    pump_hours = m3_to_pump_hours(volume_m3, pump_m3h)

    # irrigate_by is always set here - the no-deadline case returned early
    # above, unlike compute_weekly_summary's own version of this block.
    instant = deadline_instant(irrigate_by, response_offset)
    hours_available = hours_between_utc(now, instant)
    irrigate_by_display = format_casablanca(instant)

    text = build_summary(
        net_mm,
        gross_water_mm,
        volume_m3,
        pump_hours,
        irrigate_by_display,
        hours_available,
        FLOOD_ASSUMPTIONS_TEXT + rain_note,
        FLOOD_REAPPLY_NOTE,
    )
    forecast_end = deadline_instant(future_hours[-1].timestamp, response_offset)
    return amount_result(
        text,
        net_mm,
        gross_water_mm,
        volume_m3,
        pump_hours,
        instant,
        hours_available,
        forecast_end,
    )


def compute_flood_weekly_result(
    lat: float,
    lon: float,
    ha: float,
    pump_m3h: float,
    efficiency: float,
    soil_name: str,
    ec_water_ds_m: float,
    last_watering_date: str,
    post_watering_fullness: float = 1.0,
    watering_time: str | None = None,
    now: datetime | None = None,
) -> WeeklyResult:
    """Run the flood calculation and stamp the result with the moment it was made."""
    if now is None:
        now = datetime.now(CASABLANCA_TZ)
    result = _compute_flood_weekly_result(
        lat,
        lon,
        ha,
        pump_m3h,
        efficiency,
        soil_name,
        ec_water_ds_m,
        last_watering_date,
        post_watering_fullness,
        watering_time,
        now,
    )
    return dataclasses.replace(result, made_at=now)


def compute_flood_weekly_summary(
    lat: float,
    lon: float,
    ha: float,
    pump_m3h: float,
    efficiency: float,
    soil_name: str,
    ec_water_ds_m: float,
    last_watering_date: str,
    post_watering_fullness: float = 1.0,
    watering_time: str | None = None,
    now: datetime | None = None,
) -> str:
    """Same as compute_flood_weekly_result(), but returns only the report text."""
    return compute_flood_weekly_result(
        lat,
        lon,
        ha,
        pump_m3h,
        efficiency,
        soil_name,
        ec_water_ds_m,
        last_watering_date,
        post_watering_fullness,
        watering_time,
        now,
    ).text


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Weekly irrigation need for one citrus field.")
    parser.add_argument("--lat", type=float, required=True, help="field latitude")
    parser.add_argument("--lon", type=float, required=True, help="field longitude")
    parser.add_argument("--ha", type=float, required=True, help="field area, hectares")
    parser.add_argument("--pump-m3h", type=float, required=True, help="pump flow, m3/hour")
    parser.add_argument(
        "--efficiency",
        type=float,
        required=True,
        help=(
            "conveyance/application efficiency, 0-1 - water lost in pipes and at the "
            "point of application (dripper, furrow, or basin/border) BEFORE reaching "
            "the root zone. Must NOT include deep "
            "percolation (drainage below the root zone) - that is accounted for "
            "separately via --ec-water. Ask the farm; no default, see "
            "docs/fao56-reference.md"
        ),
    )
    parser.add_argument(
        "--soil", default=DEFAULT_SOIL, choices=sorted(SOIL_TYPES), help="soil texture"
    )
    parser.add_argument(
        "--ec-water",
        type=float,
        required=True,
        dest="ec_water",
        help=(
            "irrigation water salinity, dS/m (ask the farm for a lab test; no default, "
            "used for the leaching correction - see docs/fao56-reference.md)"
        ),
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "ignore any saved soil-depletion state and start fresh (full soil, now) - "
            "use this right after actually irrigating, since the tool has no other way "
            "to know that happened. Drip only - see --irrigation-method"
        ),
    )
    parser.add_argument(
        "--irrigation-method",
        default="drip",
        choices=("drip", "flood"),
        dest="irrigation_method",
        help=(
            "drip (default): assumes frequent top-ups, remembers depletion between runs "
            "(see --reset). flood: fields watered weeks apart - takes --last-watering-date "
            "instead, no saved state (see docs/fao56-reference.md)"
        ),
    )
    parser.add_argument(
        "--last-watering-date",
        dest="last_watering_date",
        help=(
            "flood only: the last date this field was watered, YYYY-MM-DD - ask the "
            "farm; no default, since this tool has no other way to know it"
        ),
    )
    parser.add_argument(
        "--post-watering-fullness",
        type=float,
        default=1.0,
        dest="post_watering_fullness",
        help=(
            "flood only: how full the root zone actually was, as a fraction of a full "
            "tank, right after --last-watering-date's watering finished - from just "
            "above 0 to 1 (default: 1.0, a full refill). NOT the fraction of a prior "
            "shortfall that got closed - this tool has no saved depletion to measure "
            "a shortfall from. Flood/basin "
            "watering rarely wets a field evenly and there is usually no meter, so this "
            "is a best guess - if unsure, guessing LOW is the safer mistake (see "
            "docs/fao56-reference.md)"
        ),
    )
    parser.add_argument(
        "--watering-time",
        dest="watering_time",
        help=(
            "flood only, required: the clock time --last-watering-date's watering "
            "finished, HH:MM 24-hour Casablanca time. Without the real time, rain and "
            "drying earlier that day cannot be told apart from what happened after the "
            "watering, which can hide a needed irrigation (see docs/fao56-reference.md)"
        ),
    )
    parser.add_argument(
        "--card-template",
        type=Path,
        dest="card_template",
        help=(
            "print a short farmer card instead of the full report, using the wording in "
            "this template file (see cards/README.md)"
        ),
    )
    parser.add_argument(
        "--card-html",
        type=Path,
        dest="card_html",
        help="also write the card as a printable web page to this file (needs --card-template)",
    )
    parser.add_argument(
        "--overwrite-card-html",
        action="store_true",
        dest="overwrite_card_html",
        help="allow --card-html to replace a file that already exists",
    )
    args = parser.parse_args(argv)
    if args.card_html is not None and args.card_template is None:
        raise SystemExit("--card-html needs --card-template")
    if args.card_html is not None:
        _check_card_html_path(args.card_html, args.card_template, args.overwrite_card_html)
    template = None
    if args.card_template is not None:
        # Everything about the card that can be checked without a result is checked
        # here, BEFORE the weather is fetched and drip saves its state, so a typo in
        # the wording file cannot leave the state advanced with no card to show for it.
        _use_utf8_output()
        template = _load_card_template(args.card_template)

    if args.irrigation_method == "flood":
        if args.reset:
            raise SystemExit("--reset has no effect with --irrigation-method flood")
        if args.last_watering_date is None:
            raise SystemExit("--last-watering-date is required with --irrigation-method flood")
        if args.watering_time is None:
            raise SystemExit("--watering-time is required with --irrigation-method flood")
        result = compute_flood_weekly_result(
            args.lat,
            args.lon,
            args.ha,
            args.pump_m3h,
            args.efficiency,
            args.soil,
            args.ec_water,
            args.last_watering_date,
            args.post_watering_fullness,
            args.watering_time,
        )
    else:
        if args.last_watering_date is not None:
            raise SystemExit("--last-watering-date only applies with --irrigation-method flood")
        if args.post_watering_fullness != 1.0:
            raise SystemExit("--post-watering-fullness only applies with --irrigation-method flood")
        if args.watering_time is not None:
            raise SystemExit("--watering-time only applies with --irrigation-method flood")
        result = compute_weekly_result(
            args.lat,
            args.lon,
            args.ha,
            args.pump_m3h,
            args.efficiency,
            args.soil,
            args.ec_water,
            reset=args.reset,
        )
    if template is None:
        print(result.text)
    else:
        _print_card(result, template, args.card_html, args.overwrite_card_html)


def _is_same_file(first: Path | None, second: Path | None) -> bool:
    """True if both paths are given, exist, and are the very same file."""
    if first is None or second is None:
        return False
    return first.exists() and second.exists() and first.samefile(second)


def _check_card_html_path(
    html_path: Path, template_path: Path | None, overwrite: bool = False
) -> None:
    """Refuse an HTML destination that could destroy something else.

    The page is written after the wording file is read and after drip has saved
    its state, so the same path would replace either with HTML. A name that
    does not end in .html or .htm is refused too, since any other existing file
    could be lost the same way. This runs before any weather call.
    """
    if _is_same_file(html_path, template_path):
        raise SystemExit("--card-html must not be the same file as the card template")
    if _is_same_file(html_path, STATE_PATH):
        raise SystemExit("--card-html must not be the same file as the saved state file")
    if html_path.suffix.lower() not in (".html", ".htm"):
        raise SystemExit("--card-html must be a file name ending in .html or .htm")
    if not html_path.parent.exists():
        raise SystemExit(f"the folder for {html_path} does not exist")
    if html_path.exists() and not overwrite:
        # A mistyped path could otherwise replace an unrelated page, such as the
        # website's index.html.
        raise SystemExit(f"{html_path} already exists; add --overwrite-card-html to replace it")


def _write_page_safely(html_path: Path, page: str, overwrite: bool = False) -> None:
    """Write the printable page without ever replacing a file by accident.

    The "does it exist?" check in _check_card_html_path() runs before the weather
    is fetched, so a file can appear before the write happens (another run, or
    the person). So the write itself decides again: without overwrite the file is
    created in a way that fails if it now exists, and with overwrite the page
    goes to a temporary file of its own and is swapped in only after the write
    worked.
    """
    if overwrite:
        _replace_page(html_path, page)
    else:
        _create_page(html_path, page)


def _create_page(html_path: Path, page: str) -> None:
    """Create a new page; fail if the file already exists (mode "x" is exclusive)."""
    created = False
    try:
        with html_path.open("x", encoding="utf-8") as handle:
            created = True
            handle.write(page)
    except FileExistsError:
        raise SystemExit(
            f"{html_path} already exists; add --overwrite-card-html to replace it"
        ) from None
    except OSError as error:
        if created:
            html_path.unlink(missing_ok=True)  # only a half-written file WE made
        raise SystemExit(f"could not write {html_path}: {error}") from None


def _replace_page(html_path: Path, page: str) -> None:
    """Replace a page through a temporary file that belongs to this run alone."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=html_path.parent,
            prefix=html_path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(page)
        temporary.replace(html_path)
    except OSError as error:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise SystemExit(f"could not write {html_path}: {error}") from None


def _use_utf8_output() -> None:
    """Make printing Arabic (or any non-Latin) card text safe.

    On Windows, Python writes to a redirected or piped output (for example `... >
    card.txt`) with the local code page, which cannot hold Arabic, so print() would
    crash after the weather was fetched and drip had saved its state. Switching both
    output streams to UTF-8 avoids that.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _load_card_template(template_path: Path) -> "CardTemplate":
    """Read and check the wording file; stop with a one-line message if it is unusable."""
    # Imported here, not at the top of the file: card.py itself imports from
    # this file (the number formatter), so a top-level import would go in a circle.
    from orchardwater.card import CardTemplateError, load_template

    try:
        return load_template(template_path)
    except (OSError, CardTemplateError) as error:
        raise SystemExit(f"card template {template_path}: {error}") from None


def _print_card(
    result: WeeklyResult,
    template: "CardTemplate",
    html_path: Path | None,
    overwrite: bool = False,
) -> None:
    """Print the card, and write the printable page if asked.

    The card goes to normal output, so `> card.txt` saves only the card. The full
    English report goes to the error stream: it carries the reasons (an unreadable
    state file, a temporary weather failure, --reset advice) that the short card
    deliberately leaves out, and the person running the tool needs to see them.
    """
    from orchardwater.card import render_html, render_text

    try:
        card_text = render_text(result, template)
        page = render_html(result, template) if html_path is not None else None
    except ValueError as error:
        raise SystemExit(f"could not build the card: {error}") from None
    print(result.text, file=sys.stderr)
    print(card_text)
    if html_path is not None and page is not None:
        _write_page_safely(html_path, page, overwrite)


if __name__ == "__main__":
    main()
