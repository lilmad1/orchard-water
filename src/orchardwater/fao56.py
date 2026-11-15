"""FAO-56 crop and soil constants and the calculations that use them.

FAO-56 is "Crop evapotranspiration - Guidelines for computing crop water
requirements", FAO Irrigation and Drainage Paper 56 (Allen, Pereira, Raes,
Smith, 1998). It is the standard reference for this kind of calculation.

Every constant below has a source comment. Values marked TODO(verify) are a
best reading of the FAO-56 tables from memory and must be checked against the
PDF (or a local agronomist) before they are used for a real farm.
"""

import math
from dataclasses import dataclass

# --- Crop coefficient (Kc) ---------------------------------------------------

# FAO-56 Table 12: citrus, no ground cover, 70% canopy, Kc mid-season.
# TODO(verify): confirm canopy-cover row and ground-cover assumption against
# the actual orchards we calibrate for; Kc varies with canopy size and cover
# crop / bare soil under the trees.
CITRUS_KC = 0.65


def etc_mm(kc: float, et0_mm: float) -> float:
    """Crop water use for one period (mm) = Kc * ET0 (FAO-56 Eq. 58).

    Eq. 58 has no built-in time step - it just scales whatever ET0 value
    you give it by Kc. Renamed from etc_mm_per_day: the formula is
    identical whether et0_mm is a day's or an hour's reference
    evapotranspiration, since Open-Meteo computes both directly with the
    same FAO-56 Penman-Monteith formula rather than us deriving one from
    the other.
    """
    return kc * et0_mm


# --- Root zone and soil water (FAO-56 chapter 8) -----------------------------

# FAO-56 Table 22: citrus root depth, lower end of the 1.2-1.5 m range
# (drip-irrigated orchards develop shallower roots than the full range).
# TODO(verify): confirm against local root surveys.
CITRUS_ROOT_DEPTH_M = 1.2

# FAO-56 Table 22: citrus depletion fraction for no stress (p).
# TODO(verify): FAO-56 lists 0.50 for citrus at ETc ~5 mm/day; p should be
# adjusted for actual ETc in a later version - not FAO-56 Eq. 84, which is
# the Ks stress-coefficient equation (see docs/fao56-reference.md), a
# different formula. The correct equation number for the ETc adjustment to
# p is not yet checked against the source.
CITRUS_DEPLETION_FRACTION_P = 0.50

# FAO-56 Chapter 8, "Soil water balance": "Daily precipitation in amounts less
# than about 0.2 ETo is normally entirely evaporated and can usually be
# ignored in the water balance calculations." Checked against fao.org text
# 2026-09-25, the operator to confirm.
EFFECTIVE_RAIN_ET0_FRACTION = 0.2


@dataclass(frozen=True)
class SoilType:
    """Soil water-holding limits, volumetric fraction (m^3 water / m^3 soil)."""

    name: str
    field_capacity: float  # theta_FC
    wilting_point: float  # theta_WP
    rew_mm: float  # readily evaporable water (REW), see total_evaporable_water_mm


# FAO-56 Table 19 (Chapter 7) gives typical ranges per soil texture. Values
# below are the midpoint of each range, checked against fao.org text
# 2026-09-25 (the operator to confirm), then rounded to 2 decimals:
# sand 0.07-0.17 / 0.02-0.07, sandy loam 0.18-0.28 / 0.06-0.16,
# loam 0.20-0.30 / 0.07-0.17, clay 0.32-0.40 / 0.20-0.24.
# TODO(verify) against real Souss-Massa soil surveys.
#
# rew_mm is also FAO-56 Table 19, "readily evaporable water" - the midpoint
# of each texture's range, at Ze=0.10 m (this table's own stated basis -
# see TOPSOIL_EVAP_DEPTH_ZE_M below for why 0.10 m, not some other value in
# FAO-56's 0.10-0.15 m range, was chosen): sand 2-7, sandy loam 6-10, loam
# 8-10, clay 8-12 mm. Checked against fao.org text 2026-09-27, the operator
# to confirm.
SOIL_TYPES: dict[str, SoilType] = {
    "sand": SoilType("sand", field_capacity=0.12, wilting_point=0.045, rew_mm=4.5),
    "sandy_loam": SoilType("sandy_loam", field_capacity=0.23, wilting_point=0.11, rew_mm=8.0),
    "loam": SoilType("loam", field_capacity=0.25, wilting_point=0.12, rew_mm=9.0),
    "clay": SoilType("clay", field_capacity=0.36, wilting_point=0.22, rew_mm=10.0),
}

# Default soil when the farmer/field data doesn't specify one.
# TODO(verify): confirm sandy loam is a reasonable Souss-Massa default.
DEFAULT_SOIL = "sandy_loam"


def total_available_water_mm(soil: SoilType, root_depth_m: float) -> float:
    """TAW (mm) = 1000 * (theta_FC - theta_WP) * Zr (FAO-56 Eq. 82)."""
    return 1000.0 * (soil.field_capacity - soil.wilting_point) * root_depth_m


def readily_available_water_mm(taw_mm: float, p: float) -> float:
    """RAW (mm) = p * TAW (FAO-56 Eq. 83)."""
    return p * taw_mm


def effective_rain_mm(rain_mm: float, et0_mm: float) -> float:
    """Rain that actually reaches and stays in the root zone that period.

    v0 rule (docs/fao56-reference.md, "Effective rain and runoff"): rain
    below EFFECTIVE_RAIN_ET0_FRACTION * ET0 is lost to surface evaporation
    and counts as zero. Rain at or above that threshold is assumed to fully
    infiltrate (flat orchard, no runoff modelled). Whatever the soil can't
    hold is removed later by the water balance (deep percolation).

    Uses "<" (strictly below), not "<=", so rain exactly at the threshold
    counts as infiltrating, matching the documented rule ("<=" would
    discard rain exactly at the threshold instead of counting it).

    TODO(verify): FAO-56's source text states this 0.2 x ET0 rule for
    DAILY precipitation totals (see docs/fao56-reference.md). This
    function applies the same fraction to hourly rain and hourly ET0
    values, which is a plausible extrapolation, not something FAO-56
    states directly - not yet checked against a source for hourly
    resolution specifically.
    """
    threshold = EFFECTIVE_RAIN_ET0_FRACTION * et0_mm
    if rain_mm < threshold:
        return 0.0
    return rain_mm


# --- Salinity leaching requirement -------------------------

# FAO-56 Chapter 8, "Effects of soil salinity", Table 23: citrus salt
# tolerance. Threshold ECe (the soil salinity level below which yield is
# not reduced) for orange is 1.7 dS/m (checked against fao.org text
# 2026-09-27, the operator to confirm). Grapefruit is listed separately at
# 1.8 dS/m; lemon/lime not yet looked up - TODO(verify) which citrus
# variety this should actually use.
CITRUS_ECE_THRESHOLD_DS_M = 1.7

# Seawater is about 50-60 dS/m - a generous, round upper bound for any
# real electrical conductivity reading this tool should ever see. Not
# from FAO-56; a plausibility check against unit mistakes and absurd
# inputs, not a cited agronomic value.
PLAUSIBLE_MAX_EC_DS_M = 100.0


def leaching_requirement(ec_irrigation_water_ds_m: float, ec_threshold_ds_m: float) -> float:
    """Fraction of applied water that must pass below the root zone to keep
    soil salinity at or below ec_threshold_ds_m, given water this salty.

    LR = ECiw / (5 * ECe - ECiw) - the FAO-29 steady-state leaching formula,
    quoted directly in FAO-56 Chapter 8, "Effects of soil salinity",
    "Application" (checked against fao.org text 2026-09-27, the operator to
    confirm). Assumes the standard 40-30-20-10 percent root-zone water
    extraction pattern the formula is based on - not separately verified
    for citrus specifically. Also assumes STEADY STATE (the salt balance
    this predicts is reached over repeated irrigations, not from one
    week's water alone) - see weekly.py's printed caveat about this.

    Raises ValueError if the irrigation water is too saline for this
    formula to give a meaningful answer:
    - LR >= 1.0 (this happens at ECiw = 2.5 * ECe - well BEFORE the
      denominator hits zero at ECiw = 5 * ECe - and checking only the
      denominator would return a nonsensical LR of 1.0 or more, meaning
      "more than all the applied water must drain away," from anywhere in
      that gap). LR >= 1 means leaching alone cannot keep soil salinity at
      or below the threshold with this water - a real agronomic problem
      this tool should not paper over with a meaningless number.
    - ECiw >= 5 * ECe (the formula's denominator is zero or negative) -
      a subset of the LR >= 1 case above, kept as an explicit check since
      it would otherwise divide by zero or a negative number.
    """
    if not (math.isfinite(ec_irrigation_water_ds_m) and ec_irrigation_water_ds_m >= 0):
        raise ValueError("ec_irrigation_water_ds_m must be a non-negative, finite number")
    if not (math.isfinite(ec_threshold_ds_m) and ec_threshold_ds_m > 0):
        raise ValueError("ec_threshold_ds_m must be a positive, finite number")
    # Seawater itself is only about 50-60 dS/m - anything past
    # PLAUSIBLE_MAX_EC_DS_M is almost certainly a unit mistake (e.g.
    # entering microS/cm, which is dS/m x 1000), not a real reading. This
    # also keeps 5 * ec_threshold_ds_m well away from float overflow (an
    # astronomical ECe like 1e308 overflows "5 * ECe" to infinity,
    # silently producing a wrong LR instead of the error a value that far
    # outside reality should raise).
    if ec_irrigation_water_ds_m > PLAUSIBLE_MAX_EC_DS_M:
        raise ValueError(
            f"ec_irrigation_water_ds_m ({ec_irrigation_water_ds_m} dS/m) is beyond "
            f"seawater salinity ({PLAUSIBLE_MAX_EC_DS_M} dS/m) - check the units (dS/m, "
            "not microS/cm) rather than trust this as a real reading"
        )
    if ec_threshold_ds_m > PLAUSIBLE_MAX_EC_DS_M:
        raise ValueError(
            f"ec_threshold_ds_m ({ec_threshold_ds_m} dS/m) is beyond seawater salinity "
            f"({PLAUSIBLE_MAX_EC_DS_M} dS/m) - check the units (dS/m, not microS/cm) "
            "rather than trust this as a real threshold"
        )
    denominator = 5.0 * ec_threshold_ds_m - ec_irrigation_water_ds_m
    if denominator <= 0:
        raise ValueError(
            f"irrigation water salinity ({ec_irrigation_water_ds_m} dS/m) is too high "
            f"relative to the soil salinity threshold ({ec_threshold_ds_m} dS/m) for this "
            "formula - leaching alone cannot keep soil salinity at or below the threshold "
            "with this water; check with an agronomist rather than trust this tool here"
        )
    lr = ec_irrigation_water_ds_m / denominator
    if lr >= 1.0:
        raise ValueError(
            f"irrigation water salinity ({ec_irrigation_water_ds_m} dS/m) is too high "
            f"relative to the soil salinity threshold ({ec_threshold_ds_m} dS/m) - the "
            f"leaching requirement this implies (LR={lr:.2f}) is at or above 100% of the "
            "applied water, meaning leaching alone cannot keep soil salinity at or below "
            "the threshold with this water; check with an agronomist rather than trust "
            "this tool here"
        )
    return lr


# --- Dual crop coefficient method for drip irrigation ------
#
# FAO-56 Chapter 7 splits crop water use into a basal (transpiration) term
# and a separate soil-evaporation term, instead of the one flat Kc used
# above - see docs/fao56-reference.md for why: under drip, only the wetted
# fraction of the soil surface can evaporate, and the flat Kc method has no
# way to account for that. Every equation number below is from FAO-56
# Chapter 7 (checked against fao.org/4/x0490e/x0490e0c.htm, 2026-09-27,
# The operator to confirm - same status as every other constant in this file).

# FAO-56 Table 17: citrus, no ground cover, 70% canopy, Kcb mid-season.
# Deliberately NOT the same number as CITRUS_KC (0.65, Table 12's single-Kc
# mid-season value for the same row) - Kcb excludes the soil-evaporation
# component that the single Kc bundles in, so it is a genuinely smaller,
# separate value, not a correction to CITRUS_KC. TODO(verify): same
# canopy-cover/ground-cover caveat as CITRUS_KC.
CITRUS_KCB = 0.60

# The "70% canopy" already implicit in which Table 12/17 row CITRUS_KC and
# CITRUS_KCB use, promoted to its own explicit, cited constant (needed
# directly by exposed_wetted_fraction below). TODO(verify): same caveat as
# CITRUS_KC/CITRUS_KCB - confirm against real Souss-Massa orchard canopy
# cover, not just the table row chosen for the Kc/Kcb values.
CITRUS_CANOPY_FRACTION = 0.70

# FAO-56 Table 12 lists citrus maximum crop height as 3-4 m. Lower end,
# same "pick the conservative end of the range" convention already used for
# CITRUS_ROOT_DEPTH_M. TODO(verify).
CITRUS_HEIGHT_M = 3.0

# FAO-56's own range for the topsoil evaporation layer depth is 0.10-0.15 m.
# 0.10 m is chosen exactly (not a midpoint) because Table 19's REW values
# (SOIL_TYPES.rew_mm above) are themselves stated "at Ze=0.10 m" - picking
# any other Ze would make TEW and REW inconsistent with each other.
TOPSOIL_EVAP_DEPTH_ZE_M = 0.10

# FAO-56 Table 20: fraction of the soil surface wetted by trickle (drip)
# irrigation, 0.3-0.4. Midpoint, same convention as SOIL_TYPES. This is the
# BEFORE-shading value from the table - drip_wetted_fraction below applies
# FAO-56's own further drip-specific reduction on top of it. TODO(verify).
DRIP_FW_TRICKLE = 0.35

# FAO-56 Table 20: fraction of the soil surface wetted by basin and
# border irrigation (checked against fao.org text 2026-09-28) -
# fw = 1.0 exactly (the whole ground surface), same as precipitation and
# sprinkler irrigation. "Flood" here is this code's own grouping name for
# that whole category (basin/border/flood-type surface irrigation), not
# a separate row the table itself lists under that word. No range, no
# TODO(verify): Table 20 states this value directly, not a range to pick
# a midpoint from. No drip-specific shading reduction applies to it
# either - see DualKcParams.apply_drip_shading in water_balance.py.
FLOOD_FW_TABLE20 = 1.0

# FAO-56 Chapter 7's own text: "Surface is wetted by significant rain (i.e.,
# > 3 to 4 mm)... fw = 1" - a DIFFERENT, larger threshold from
# EFFECTIVE_RAIN_ET0_FRACTION above (which decides whether rain counts
# toward the water balance AT ALL, not whether it wets the whole surface).
# Midpoint of the 3-4 mm range, same convention as DRIP_FW_TRICKLE (the two
# thresholds must not be conflated, which would switch to full-surface
# wetting on ANY effective rain, however small).
SIGNIFICANT_RAIN_MM = 3.5


def total_evaporable_water_mm(soil: SoilType, ze_m: float) -> float:
    """TEW (mm) = 1000 * (theta_FC - 0.5 * theta_WP) * Ze (FAO-56 Eq. 73).

    The total depth of water that can be evaporated from a full topsoil
    evaporation layer, once it starts fully wet. Mirrors
    total_available_water_mm's shape exactly, but for the shallow surface
    layer instead of the full root zone.
    """
    return 1000.0 * (soil.field_capacity - 0.5 * soil.wilting_point) * ze_m


# The highest surface wind gust ever recorded is about 113 m/s (Barrow
# Island, 1996). A generous, round upper bound past that - not from
# FAO-56, a plausibility check against unit mistakes (e.g. entering
# km/h or knots directly as m/s), same pattern as PLAUSIBLE_MAX_EC_DS_M
# (an implausible but finite wind speed could otherwise reach the dual
# coefficient method's equations and produce an absurd-but-finite
# hourly result).
PLAUSIBLE_MAX_WIND_SPEED_MS = 150.0


def wind_speed_2m(wind_speed_10m_ms: float) -> float:
    """Convert wind speed measured at 10 m to the 2 m height FAO-56 needs.

    u2 = u10 * 4.87 / ln(67.8 * 10 - 5.42) (FAO-56 Eq. 47, for the common
    case of a measurement height of 10 m - the general form of Eq. 47 takes
    any height, but this tool only ever measures at 10 m, via Open-Meteo's
    wind_speed_10m).
    """
    if not (math.isfinite(wind_speed_10m_ms) and wind_speed_10m_ms >= 0.0):
        raise ValueError("wind_speed_10m_ms must be a non-negative, finite number")
    if wind_speed_10m_ms > PLAUSIBLE_MAX_WIND_SPEED_MS:
        raise ValueError(
            f"wind_speed_10m_ms ({wind_speed_10m_ms}) is beyond a generous plausibility "
            f"ceiling ({PLAUSIBLE_MAX_WIND_SPEED_MS} m/s, itself already well past the "
            "highest surface wind gust ever recorded, ~113 m/s) - check the units rather "
            "than trust this as a real reading"
        )
    return wind_speed_10m_ms * 4.87 / math.log(67.8 * 10.0 - 5.42)


def _validate_climate_inputs(
    wind_speed_2m_ms: float, relative_humidity_min_pct: float, crop_height_m: float
) -> None:
    """Shared input checks for the two FAO-56 climate-adjustment equations below."""
    if not (math.isfinite(wind_speed_2m_ms) and wind_speed_2m_ms >= 0.0):
        raise ValueError("wind_speed_2m_ms must be a non-negative, finite number")
    if not (math.isfinite(relative_humidity_min_pct) and 0.0 <= relative_humidity_min_pct <= 100.0):
        raise ValueError("relative_humidity_min_pct must be a finite percentage from 0 to 100")
    if not (math.isfinite(crop_height_m) and crop_height_m > 0.0):
        raise ValueError("crop_height_m must be a positive, finite number")


# FAO-56's own stated validity range for this correction (both Eq. 70 and
# Eq. 72's bracket term, checked against fao.org text 2026-09-27): "for 1
# m/s <= u2 <= 6 m/s", "for 20% <= RHmin <= 80%". Clamping to these
# bounds before applying the correction (rather than rejecting a real but
# extreme weather reading outright) keeps the tool usable on a genuinely
# windy or dry day - FAO-56 doesn't say the correction becomes
# meaningless past these bounds, only that it was calibrated within them,
# so the nearest validated value is a reasonable approximation past the
# edge (an unclamped, in-bounds-but-extreme wind reading - e.g.
# wind_speed_2m's own generous 150 m/s plausibility ceiling - could
# otherwise produce an absurd climate-adjusted Kcb, e.g. about 5.0 for a
# citrus Kcb of 0.60).
CLIMATE_CORRECTION_MIN_WIND_SPEED_2M_MS = 1.0
CLIMATE_CORRECTION_MAX_WIND_SPEED_2M_MS = 6.0
CLIMATE_CORRECTION_MIN_RHMIN_PCT = 20.0
CLIMATE_CORRECTION_MAX_RHMIN_PCT = 80.0


def _climate_correction_term(
    wind_speed_2m_ms: float, relative_humidity_min_pct: float, crop_height_m: float
) -> float:
    """[0.04*(u2-2) - 0.004*(RHmin-45)] * (h/3)^0.3 - shared by Eq. 70 and Eq. 72.

    Both equations correct a reference-condition (sub-humid climate,
    RHmin~45%, u2~2 m/s) tabulated value for a real climate, using the
    exact same bracket term - Eq. 70 adds it to Kcb directly, Eq. 72 adds
    it to a flat 1.2 to get Kc_max. u2 and RHmin are clamped to FAO-56's
    own stated validity range for this correction before use - see the
    constants above.
    """
    clamped_u2 = min(
        max(wind_speed_2m_ms, CLIMATE_CORRECTION_MIN_WIND_SPEED_2M_MS),
        CLIMATE_CORRECTION_MAX_WIND_SPEED_2M_MS,
    )
    clamped_rhmin = min(
        max(relative_humidity_min_pct, CLIMATE_CORRECTION_MIN_RHMIN_PCT),
        CLIMATE_CORRECTION_MAX_RHMIN_PCT,
    )
    return (0.04 * (clamped_u2 - 2.0) - 0.004 * (clamped_rhmin - 45.0)) * float(
        (crop_height_m / 3.0) ** 0.3
    )


def kc_max(
    wind_speed_2m_ms: float, relative_humidity_min_pct: float, crop_height_m: float, kcb: float
) -> float:
    """Kc_max (FAO-56 Eq. 72): the upper limit on Kcb + Ke after a wetting event.

    Kc_max = max(1.2 + climate_term, kcb + 0.05)

    The second term is part of Eq. 72 itself, not a separate check:
    FAO-56's own text says "Equation 72 ensures that Kc max is always
    greater or equal to the sum Kcb + 0.05... wet soil will always
    increase the value for Kcb by 0.05 following complete wetting of the
    soil surface." Without it, an extreme-but-in-range Kcb/climate
    combination can make the first term smaller than Kcb, which would make
    evaporation_coefficient's Ke go negative - physically meaningless
    (evaporation cannot be negative).

    relative_humidity_min_pct should be the day's MINIMUM relative
    humidity - FAO-56's own definition. This tool only has Open-Meteo's
    hourly (instantaneous) relative_humidity_2m, not a true daily minimum;
    using the hourly value directly is an extrapolation, not something
    FAO-56 states (TODO(verify), same pattern as effective_rain_mm's
    hourly-vs-daily extrapolation flag above).
    """
    _validate_climate_inputs(wind_speed_2m_ms, relative_humidity_min_pct, crop_height_m)
    if not (math.isfinite(kcb) and kcb >= 0.0):
        raise ValueError("kcb must be a non-negative, finite number")
    climate_term = 1.2 + _climate_correction_term(
        wind_speed_2m_ms, relative_humidity_min_pct, crop_height_m
    )
    return max(climate_term, kcb + 0.05)


# FAO-56's own text: "For specific adjustment in climates where RHmin
# differs from 45% or where the wind speed is larger or smaller than
# 2 m/s, the Kcb mid and Kcb end values larger than 0.45 must be adjusted
# using the following equation [Eq. 70]." Below this threshold, no
# adjustment applies.
CITRUS_KCB_ADJUSTMENT_THRESHOLD = 0.45


def climate_adjusted_kcb(
    kcb_tab: float,
    wind_speed_2m_ms: float,
    relative_humidity_min_pct: float,
    crop_height_m: float,
) -> float:
    """Kcb corrected for real climate away from FAO-56's reference conditions (Eq. 70).

    Same bracket-term correction already used for Kc_max (Eq. 72), applied
    here as an ADDITIVE correction to the tabulated Kcb instead (without
    it Kcb would stay at its uncorrected, reference-condition value even
    in a real climate).

    Only applies above CITRUS_KCB_ADJUSTMENT_THRESHOLD (FAO-56's own
    stated condition) - returns kcb_tab unchanged below it.
    """
    if not (math.isfinite(kcb_tab) and kcb_tab >= 0.0):
        raise ValueError("kcb_tab must be a non-negative, finite number")
    if kcb_tab <= CITRUS_KCB_ADJUSTMENT_THRESHOLD:
        return kcb_tab
    _validate_climate_inputs(wind_speed_2m_ms, relative_humidity_min_pct, crop_height_m)
    return kcb_tab + _climate_correction_term(
        wind_speed_2m_ms, relative_humidity_min_pct, crop_height_m
    )


def evaporation_reduction_coefficient(de_prev_mm: float, tew_mm: float, rew_mm: float) -> float:
    """Kr (FAO-56 Eq. 74): how much the drying topsoil limits evaporation.

    Kr = 1 while the evaporating layer is still wet enough (de_prev_mm at or
    below rew_mm, the "readily evaporable" stage 1 water) - evaporation runs
    at its full potential rate. Past that point, Kr falls linearly toward 0
    as de_prev_mm approaches tew_mm (the layer's full evaporable capacity),
    modelling water becoming harder to pull to the surface as the topsoil
    dries out (stage 2 drying).
    """
    if not (math.isfinite(tew_mm) and tew_mm > 0.0):
        # Without this, a non-finite tew_mm (e.g. infinity) passed both
        # the de_prev_mm and rew_mm checks below (since "x <= infinity"
        # is always true), silently returning NaN instead of raising -
        # unreachable via the real CLI (TEW is always computed from
        # finite soil constants), but this function had no guard of its
        # own against a caller who passed a bad tew_mm directly.
        raise ValueError("tew_mm must be a positive, finite number")
    if not (math.isfinite(de_prev_mm) and 0.0 <= de_prev_mm <= tew_mm):
        raise ValueError("de_prev_mm must be a finite number between 0 and tew_mm")
    if not (math.isfinite(rew_mm) and 0.0 <= rew_mm < tew_mm):
        raise ValueError("rew_mm must be a finite number between 0 and tew_mm (exclusive)")
    if de_prev_mm <= rew_mm:
        return 1.0
    return (tew_mm - de_prev_mm) / (tew_mm - rew_mm)


def drip_wetted_fraction(fw_table20: float, canopy_fraction: float) -> float:
    """Drip-adjusted fw: FAO-56's own further reduction for trickle irrigation.

    "A general approach could be to multiply fw by [1-(2/3)fc] for drip
    irrigation" (FAO-56 Chapter 7, drip irrigation text) - drippers
    typically sit under and are shaded by the canopy, so less of the
    already-small wetted area (fw_table20, Table 20) actually evaporates
    freely than the table's own value alone would suggest.
    """
    if not (math.isfinite(fw_table20) and 0.0 < fw_table20 <= 1.0):
        raise ValueError("fw_table20 must be a finite number greater than 0 and at most 1")
    if not (math.isfinite(canopy_fraction) and 0.0 <= canopy_fraction < 1.0):
        raise ValueError("canopy_fraction must be a finite number from 0 up to (not including) 1")
    return fw_table20 * (1.0 - (2.0 / 3.0) * canopy_fraction)


def exposed_wetted_fraction(canopy_fraction: float, fw: float) -> float:
    """few (FAO-56 Eq. 75): the fraction of soil that is both bare AND wetted.

    few = min(1 - fc, fw) - evaporation can only happen from soil that is
    both exposed (not shaded by canopy) and actually wet, so this takes
    whichever fraction is smaller.
    """
    if not (math.isfinite(canopy_fraction) and 0.0 <= canopy_fraction < 1.0):
        raise ValueError("canopy_fraction must be a finite number from 0 up to (not including) 1")
    if not (math.isfinite(fw) and 0.0 < fw <= 1.0):
        raise ValueError("fw must be a finite number greater than 0 and at most 1")
    return min(1.0 - canopy_fraction, fw)


def evaporation_coefficient(kr: float, kcx: float, kcb: float, few: float) -> float:
    """Ke (FAO-56 Eq. 71): the soil-evaporation part of the crop coefficient.

    Ke = min(Kr * (Kc_max - Kcb), few * Kc_max) - capped at few * Kc_max
    because evaporation can never exceed what the exposed-and-wetted
    fraction of the surface can physically supply, however wet the layer
    still is.
    """
    if not (math.isfinite(kr) and 0.0 <= kr <= 1.0):
        raise ValueError("kr must be a finite number from 0 to 1")
    if not (math.isfinite(kcx) and kcx > 0.0):
        raise ValueError("kcx (Kc_max) must be a positive, finite number")
    if not (math.isfinite(kcb) and kcb >= 0.0):
        raise ValueError("kcb must be a non-negative, finite number")
    if not (math.isfinite(few) and 0.0 < few <= 1.0):
        raise ValueError("few must be a finite number greater than 0 and at most 1")
    if kcx < kcb + 0.05:
        # kc_max() itself always enforces this floor (Eq. 72's own kcx
        # >= kcb + 0.05), so a real production call never reaches this
        # - but this function has no way to verify its caller used
        # kc_max() for kcx, and kr*(kcx-kcb) goes negative below this
        # floor, which is physically meaningless (evaporation cannot be
        # negative).
        raise ValueError(
            f"kcx ({kcx}) must be at least kcb + 0.05 ({kcb + 0.05}) - Eq. 72's own floor - "
            "check these values rather than trust a negative Ke"
        )
    return min(kr * (kcx - kcb), few * kcx)


def etc_dual_mm(kcb: float, ke: float, et0_mm: float) -> float:
    """Crop water use for one period (mm), dual coefficient version (Eq. 69).

    ETc = (Kcb + Ke) * ET0 - replaces etc_mm's ETc = Kc * ET0 (Eq. 58) when
    the dual method is in use. Same "no built-in time step" property as
    etc_mm: works for a day's or an hour's ET0 unchanged.
    """
    return (kcb + ke) * et0_mm
