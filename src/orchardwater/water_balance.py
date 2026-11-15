"""Hourly soil water balance for one field, one week.

This module uses the "top-up" method: start from a saved depletion when
available, or assume full soil when starting fresh, then run FAO-56's
root-zone depletion equation (Eq. 85) hour by hour. This fits drip
irrigation, where farmers top up the soil often. It is not valid for flood
irrigation, where the soil dries for longer than a week between waterings -
that needs a different method (see the flood section of
docs/fao56-reference.md).

The balance is tracked once an hour rather than once a day, so the
deadline below can be hour-precise instead of day-precise, and so
weekly.py can genuinely check whether a pump can finish in time - a
daily version couldn't answer that honestly. The equations are the
daily ones; only the time step is smaller. FAO-56's own effective-rain
threshold (see fao56.effective_rain_mm) is documented for daily totals,
and applying it per hour is not separately verified (flagged as such
there).

We simplify FAO-56 Eq. 85 for v0: no farmer irrigation during the week we are
forecasting (I = 0, since the whole point is to say how much to irrigate),
no runoff (RO = 0, per docs/fao56-reference.md) and no capillary rise
(CR = 0, deep water table, the usual case in Souss). That leaves:

    Dr,i = Dr,i-1 - eff_rain_i + ETc,i + DP,i

with 0 <= Dr,i <= TAW (Eq. 85), and DP,i the water that overflows above field
capacity (Eq. 88). i is now an hour, not a day.

ETc,i above comes from FAO-56's dual crop coefficient method (Kcb + Ke)
instead of a flat Kc - see fao56.py's "Dual crop coefficient method"
section. This needs a second, coupled depletion layer, De (surface
evaporation depletion, Eq. 77/78) alongside the existing root-zone Dr - De's
own balance has the exact same clamped shape as Dr's, so it reuses
step_one_period() unchanged rather than a second function.

De does NOT reset partway through a run: modelling the drip system re-wetting
the surface on a fixed schedule DURING the forecast itself would directly
contradict the I=0 assumption above - if no irrigation happens until the
recommendation is followed, nothing can be re-wetting the surface
mid-forecast either. De just starts fully wet (De = 0, matching the "top-up
method" starting assumption above) and dries out continuously, hour by hour,
for as long as run_forecast_period runs - across BOTH the catch-up and future
halves of a single compute_weekly_summary call, AND across separate tool
runs, via state.py, the same way Dr already is: NOT persisting De would have
the opposite effect from a "costs little accuracy" argument - a fresh run
always assumes the surface JUST got re-wetted, so every run would add an
artificial burst of extra evaporation that compounds the more often the tool
is used (about 5.8 mm/week of overstated need across daily runs with no
rain).

Wetted fraction (few, Eq. 75) persists between wetting events, not reset
every hour. FAO-56's own text: "Surface is wetted by significant rain
(i.e., > 3 to 4 mm)... fw = 1"; "Where there is neither irrigation nor
significant precipitation: fw is the fw of the previous day." Switching
to the full-surface fraction on ANY hour with effective rain (however
small) would conflate the different, smaller EFFECTIVE_RAIN_ET0_FRACTION
threshold, which only decides whether rain counts toward the water
balance at all, with this separate, larger "does it wet the WHOLE
surface" threshold; and resetting back to the drip fraction on the very
next dry hour would be wrong too - it stays at the full-surface fraction
until another significant rain. `few` threads exactly like De:
continuously within a run, persisted across separate runs via state.py.

Kr (Eq. 74) is deliberately computed from the depletion at the START of
each period (De,i-1), not any rain that arrives during that same period -
this is FAO-56's own equation as written (Eq. 74 explicitly uses De,i-1),
the same one-period lag the existing root-zone model has always had;
running it hourly instead of daily only makes that lag shorter, not new.
Reordering to react to same-hour rain would deviate from FAO-56's stated
equation, not align with it more closely.
"""

from dataclasses import dataclass
from typing import Any

from orchardwater.fao56 import (
    SIGNIFICANT_RAIN_MM,
    SoilType,
    climate_adjusted_kcb,
    drip_wetted_fraction,
    effective_rain_mm,
    etc_dual_mm,
    evaporation_coefficient,
    evaporation_reduction_coefficient,
    exposed_wetted_fraction,
    kc_max,
    total_evaporable_water_mm,
    wind_speed_2m,
)
from orchardwater.kc import MonthlyKc
from orchardwater.soil_model import BucketModel, CropDemand, SoilWaterModel
from orchardwater.weather import HourlyWeather


@dataclass(frozen=True)
class DualKcParams:
    """Crop-specific inputs for the dual Kc method.

    kcb: basal crop coefficient (FAO-56 Table 17), BEFORE the climate
        adjustment run_forecast_period applies each hour (Eq. 70).
    canopy_fraction: fraction of ground covered by canopy (fc).
    wetted_fraction_table20: FAO-56 Table 20 wetted fraction for the
        irrigation method - stays in effect until significant rain
        switches the wetted fraction to the full-surface value (see
        module docstring; "significant," not just "effective," rain -
        two different thresholds, see SIGNIFICANT_RAIN_MM in fao56.py).
    apply_drip_shading: whether to apply FAO-56's drip-specific
        reduction on top of wetted_fraction_table20 (see
        fao56.drip_wetted_fraction) - True for drip (emitters are
        typically shaded by the canopy), False for methods that wet the
        open ground directly (flood/basin/border) where that
        shading reduction doesn't apply (applying the drip-only shading
        formula to flood's fw=1.0 would wrongly shrink it).
    crop_height_m: mean plant height (h), for Kc_max (Eq. 72) and the
        Kcb climate adjustment (Eq. 70).
    ze_m: topsoil evaporation layer depth (Ze), for TEW (Eq. 73).
    """

    kcb: float
    canopy_fraction: float
    wetted_fraction_table20: float
    apply_drip_shading: bool
    crop_height_m: float
    ze_m: float


@dataclass(frozen=True)
class PeriodResult:
    """One hour's water balance, all in mm."""

    timestamp: str
    etc_mm: float  # crop water use that hour
    eff_rain_mm: float  # rain that reached the root zone that hour
    deep_percolation_mm: float  # water lost below the root zone that hour
    depletion_mm: float  # root-zone depletion at the end of the hour
    de_mm: float  # surface evaporation depletion at the end of the hour
    few: float  # exposed-and-wetted fraction in effect at the end of the hour


def step_one_period(
    depletion_mm: float, etc_mm: float, eff_rain_mm: float, taw_mm: float
) -> tuple[float, float]:
    """Move the root-zone depletion forward by one period (an hour, in v1).

    Returns (new_depletion_mm, deep_percolation_mm). FAO-56 Eq. 85 (I=RO=CR=0)
    and Eq. 88 (deep percolation is what pushes depletion below 0). The
    equation itself has no built-in time step - it just relates one period's
    start and end depletion - so the same function works whether the caller
    feeds it a day's or an hour's worth of etc_mm/eff_rain_mm.
    """
    depletion_before_limits = depletion_mm - eff_rain_mm + etc_mm
    deep_percolation_mm = max(0.0, -depletion_before_limits)
    new_depletion_mm = min(taw_mm, max(0.0, depletion_before_limits))
    return new_depletion_mm, deep_percolation_mm


def _initial_few(dual_kc: DualKcParams) -> float:
    """The wetted fraction (Eq. 75) in effect before any significant rain.

    Applies the drip-specific shading reduction (Eq. 75's fw input) only
    when apply_drip_shading is set - see DualKcParams' own docstring for
    why flood/basin/border must not get this reduction.
    """
    fw = (
        drip_wetted_fraction(dual_kc.wetted_fraction_table20, dual_kc.canopy_fraction)
        if dual_kc.apply_drip_shading
        else dual_kc.wetted_fraction_table20
    )
    return exposed_wetted_fraction(dual_kc.canopy_fraction, fw)


def _hour_few(hour_rain_mm: float, few_prev: float, dual_kc: DualKcParams) -> float:
    """This hour's exposed-and-wetted fraction (Eq. 75) - see module docstring.

    Only a SIGNIFICANT rain (FAO-56's own > 3-4 mm threshold, not the
    smaller effective-rain-for-the-water-balance threshold) switches to
    the full-surface fraction; otherwise it stays whatever it already was
    (few_prev) - it does not reset back to the drip fraction on its own.
    """
    if hour_rain_mm >= SIGNIFICANT_RAIN_MM:
        return exposed_wetted_fraction(dual_kc.canopy_fraction, fw=1.0)
    return few_prev


def _hour_dual_kc_demand(
    hour: HourlyWeather,
    de_start_mm: float,
    eff_rain_mm: float,
    tew_mm: float,
    rew_mm: float,
    few: float,
    dual_kc: DualKcParams,
) -> tuple[CropDemand, float]:
    """One hour's crop demand (dual method, Eq. 69) and the surface layer's end-of-hour depletion.

    Returns (demand, new_de_mm). demand.total_mm is etc_dual_mm (Eq. 69),
    demand.evaporation_mm the soil evaporation part (Ke x ET0), and the rest is the
    basal transpiration part (Kcb x ET0). They are split so a future Richards-based
    neural operator soil model can take transpiration from the roots and evaporation
    from the top (see soil_model.py); the bucket only uses the sum. Reuses
    step_one_period for De's own balance - see the module docstring for why that's
    valid (Eq. 77/78 has the same clamped shape as the existing root-zone Eq. 85/88).
    """
    u2 = wind_speed_2m(hour.wind_speed_ms)
    kcb_hour = climate_adjusted_kcb(
        dual_kc.kcb, u2, hour.relative_humidity_pct, dual_kc.crop_height_m
    )
    kr = evaporation_reduction_coefficient(de_start_mm, tew_mm, rew_mm)
    kcx = kc_max(u2, hour.relative_humidity_pct, dual_kc.crop_height_m, kcb_hour)
    ke = evaporation_coefficient(kr, kcx, kcb_hour, few)
    new_de_mm, _ = step_one_period(de_start_mm, ke * hour.et0_mm / few, eff_rain_mm, tew_mm)
    demand = CropDemand(etc_dual_mm(kcb_hour, ke, hour.et0_mm), ke * hour.et0_mm)
    return demand, new_de_mm


def run_forecast_period(
    hours: list[HourlyWeather],
    soil: SoilType,
    taw_mm: float,
    dual_kc: DualKcParams,
    starting_depletion_mm: float = 0.0,
    starting_de_mm: float = 0.0,
    starting_few: float | None = None,
    monthly_kc: MonthlyKc | None = None,
    soil_model: SoilWaterModel[Any] | None = None,
) -> list[PeriodResult]:
    """Run the depletion loop for a week of HourlyWeather (see weather.py).

    Starts the root zone (Dr) at starting_depletion_mm, which defaults to 0
    (soil full) per the top-up assumption above - the right default when
    there's no better starting point. This parameter lets a caller with a
    real, previously-computed depletion (see state.FieldState, weekly.py)
    continue from it instead of always re-assuming a full tank; the
    equations themselves are unchanged. Takes HourlyWeather objects, not
    plain tuples, so timestamp/et0/rain can't get swapped by mistake at the
    call site.

    starting_de_mm is the surface evaporation layer's starting depletion,
    separate from starting_depletion_mm - defaults to 0 (fully wet), the
    right default for a fresh run with no better starting point.

    starting_few is the exposed-and-wetted fraction already in effect -
    defaults to None, meaning "no significant rain has wetted the surface
    yet," which this function resolves to the drip-patch fraction. A
    caller with a real, previously-computed De/few (state.py, weekly.py)
    can continue from them instead of restarting fresh - both thread the
    same way starting_depletion_mm does, across the catch-up/future
    split within one run AND across separate tool runs.

    monthly_kc: a Kc fitted for this farm (see kc.py). When given, each
    hour's crop water use is Kc(month) x ET0 (FAO-56's single Kc method,
    Eq. 56) instead of the dual Kcb + Ke method. A fitted Kc already
    includes soil evaporation, because it was learned from total measured
    water use, so the surface layer is not modelled on top of it: De and
    few are passed through unchanged (few resolved to the drip value when
    None, so a saved state stays valid for weekly.py's checks). When None,
    the dual method runs exactly as before.

    soil_model: the soil water model that turns demand and rain into
    depletion (see soil_model.py). None means FAO-56's bucket with taw_mm,
    the same arithmetic as the original built-in engine. This is where a
    Richards-based neural operator soil model plugs in later. If one is
    given, its taw_mm must equal taw_mm.
    """
    tew_mm = total_evaporable_water_mm(soil, dual_kc.ze_m)
    if soil_model is None:
        soil_model = BucketModel(taw_mm)
    elif soil_model.taw_mm != taw_mm:
        raise ValueError("soil_model.taw_mm must equal taw_mm")

    soil_state = soil_model.initial_state(starting_depletion_mm)
    de_mm = starting_de_mm
    few = _initial_few(dual_kc) if starting_few is None else starting_few
    results = []
    for hour in hours:
        hour_eff_rain_mm = effective_rain_mm(hour.rain_mm, hour.et0_mm)
        if monthly_kc is None:
            few = _hour_few(hour.rain_mm, few, dual_kc)
            demand, de_mm = _hour_dual_kc_demand(
                hour, de_mm, hour_eff_rain_mm, tew_mm, soil.rew_mm, few, dual_kc
            )
        else:
            demand = CropDemand(monthly_kc.for_timestamp(hour.timestamp) * hour.et0_mm)
        soil_state, step = soil_model.step(soil_state, demand, hour_eff_rain_mm)

        results.append(
            PeriodResult(
                timestamp=hour.timestamp,
                etc_mm=step.actual_et_mm,
                eff_rain_mm=hour_eff_rain_mm,
                deep_percolation_mm=step.drainage_mm,
                depletion_mm=step.depletion_mm,
                de_mm=de_mm,
                few=few,
            )
        )
    return results


@dataclass(frozen=True)
class IrrigationDeadline:
    """The hour and amount a soil-depletion estimate is sized from, and how much.

    timestamp: the hour whose depletion the amount is sized to fully refill,
    when amount_mm is set. This says nothing about how long pumping that
    amount takes - see weekly.py's feasibility check for that (wording that
    implied a confirmed pumping window would be wrong unless the model can
    back it up, and weekly.py's check is what does). Applying the amount
    earlier could overwater, if that earlier hour's depletion is smaller
    (the excess would just drain away as deep percolation) - though rain can
    make depletion fall between hours, so this is not guaranteed for every
    earlier hour.

    amount_mm is None when this week's model cannot size a safe amount at
    all. unschedulable_reason then explains why, in plain English, for the
    caller to print - one of two cases:
    - RAW is reached within the very first forecast hour: there is no
      earlier forecast hour to size a safe amount from.
    - A later hour's own water use (its ETc minus effective rain) is
      already at least as large as RAW on its own, so even a full soil
      refill the hour before (depletion reset to 0) would not stop that
      hour crossing RAW. Naming hour_before with its own (smaller)
      depletion as "the fix" would be wrong here. This case's reason
      contains a literal "{timestamp}" placeholder instead of the raw
      hour label - this module has no timezone information to display it
      correctly (Open-Meteo's raw label isn't necessarily the farm's real
      Casablanca clock time - see weekly.py's deadline_instant()), so the
      caller (weekly.py) fills in a properly converted, displayable time
      before printing (baking the raw label directly into this text would
      bypass that conversion).
    """

    timestamp: str
    amount_mm: float | None
    unschedulable_reason: str | None = None


def irrigation_deadline(
    results: list[PeriodResult], raw_mm: float, taw_mm: float
) -> IrrigationDeadline | None:
    """Find the hour and amount a safe soil-refill estimate is sized from, or None.

    RAW ("readily available water", Eq. 83) is the FAO-56 no-stress limit:
    FAO-56 keeps the crop stress coefficient Ks at 1 (no stress) while
    depletion stays at or below RAW (docs/fao56-reference.md), so stress
    becomes possible only once depletion goes above RAW. This function
    treats reaching RAW exactly (>=, not >) as already too late, a
    deliberate safety margin, not a claim that stress itself has begun at
    that exact point.
    Depletion values here are end-of-period, so if hour N's depletion has
    already reached RAW, hour N is too late to call the deadline. The
    deadline is the hour *before* that: its own depletion (the
    amount needed to bring the soil back to full) is the amount that would
    stop hour N from ever reaching RAW - unless hour N's own water use alone
    is already enough to cross RAW even from a full refill, in which case
    there is no safe hour_before amount to name (see IrrigationDeadline).

    Returns None if the week never reaches RAW.

    (Three things shaped this logic: naming the period before the
    crossing period as the deadline, not the crossing period itself; no
    fake "0 mm now" instruction on the first-period case; and the
    depletion_after_full_refill check below for a later period whose own
    water use alone exceeds RAW. A "period" is an hour; the reasoning is
    the same as for a day.)
    """
    for index, result in enumerate(results):
        if result.depletion_mm >= raw_mm:
            if index == 0:
                return IrrigationDeadline(
                    timestamp="immediately (before the forecast week starts)",
                    amount_mm=None,
                    unschedulable_reason=(
                        "this forecast predicts the soil reaching the no-stress "
                        "limit within the very first hour, before this tool's "
                        "hour-by-hour model has an earlier point to size a safe "
                        "amount from"
                    ),
                )
            period_before = results[index - 1]
            depletion_after_full_refill, _ = step_one_period(
                0.0, result.etc_mm, result.eff_rain_mm, taw_mm
            )
            if depletion_after_full_refill >= raw_mm:
                return IrrigationDeadline(
                    timestamp=result.timestamp,
                    amount_mm=None,
                    unschedulable_reason=(
                        "this forecast predicts the soil reaching the "
                        "no-stress limit at {timestamp}, and that hour's own "
                        "water use is at least as large as the readily "
                        "available water by itself - so even a full soil "
                        "refill the hour before would not stop it"
                    ),
                )
            return IrrigationDeadline(
                timestamp=period_before.timestamp, amount_mm=period_before.depletion_mm
            )
    return None


def weekly_net_need_mm(results: list[PeriodResult], raw_mm: float, taw_mm: float) -> float:
    """Net water to apply this week (mm).

    Normally this is the depletion at the end of the week. But if the soil
    is forecast to reach RAW (the no-stress limit) before the week is over,
    a single end-of-week application would leave the crop stressed for
    hours - so this returns the smaller, safe amount due at the irrigation
    deadline instead (see irrigation_deadline). That amount does not cover
    the rest of the week; the caller must tell the farmer to re-run this
    tool for the time after the deadline.

    Returns 0.0, not a real recommendation, when IrrigationDeadline.amount_mm
    is None (this week's model cannot schedule a safe amount at all) -
    callers must check irrigation_deadline() themselves and handle that case
    separately instead of printing this 0.0 as an irrigation amount.
    """
    if not results:
        return 0.0
    deadline = irrigation_deadline(results, raw_mm, taw_mm)
    if deadline is not None:
        return deadline.amount_mm if deadline.amount_mm is not None else 0.0
    return results[-1].depletion_mm
