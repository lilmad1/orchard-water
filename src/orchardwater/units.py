"""Unit conversions for the irrigation engine.

Everything upstream works in millimetres (mm) of water depth, because that is
how FAO-56 and weather services report it. These two functions turn that
depth into what a farmer actually schedules: cubic metres and pump hours.
"""

import math

# 1 mm of water over 1 hectare (10,000 m^2) is 10 m^3.
# mm -> m: divide by 1000. m^2 -> ha: multiply by 10,000. 10,000 / 1000 = 10.
MM_PER_HA_TO_M3 = 10.0

# Plausibility bounds: ha/pump_m3h/efficiency only checked "greater than
# zero", so a value that is technically positive and finite but physically
# absurd (e.g. a pump moving 1e308 m^3/h, or a field of 5e-324 ha) could
# still produce a nonsensical-but-finite printed volume or pump time - the
# isfinite() checks below only catch actual overflow, not "wildly
# unrealistic but still a normal float". These are generous sanity
# ceilings/floors against unit mistakes (e.g. entering m^2 instead of ha,
# or L/h instead of m^3/h), not real agronomic or equipment limits - same
# pattern as fao56.PLAUSIBLE_MAX_EC_DS_M.
PLAUSIBLE_MIN_HA = 0.01  # 100 m^2 - smaller than any real orchard plot.
PLAUSIBLE_MAX_HA = 10_000.0  # 100 km^2 - far bigger than a single field.
PLAUSIBLE_MIN_PUMP_M3H = 0.01  # a trickle, smaller than any real irrigation pump.
PLAUSIBLE_MAX_PUMP_M3H = 10_000.0  # industrial-scale, beyond a single farm's pump.
PLAUSIBLE_MIN_EFFICIENCY = 0.05  # even poor flood irrigation rarely loses over 90%.

# Bounding each input to gross_mm() separately (efficiency,
# leaching_requirement) still lets them combine - e.g. water salinity just
# below the LR=1.0 boundary together with efficiency near its floor - into
# a gross-water figure of ~1e17 mm, a physically absurd result no
# individual input bound catches. Rather than keep chasing new input
# combinations, this bounds the OUTPUT directly: a full metre of water
# applied in one week is already far beyond anything a real irrigation
# system would ever need or apply, for any soil or crop this tool models
# (a generous sanity ceiling against bad or nonsensical inputs, not a real
# agronomic limit).
PLAUSIBLE_MAX_GROSS_WATER_MM = 1_000.0


def mm_to_m3(mm: float, ha: float) -> float:
    """Convert a water depth (mm) over a field area (ha) to a volume (m^3)."""
    if not (math.isfinite(mm) and mm >= 0.0):
        # A water depth can't be negative. Unreachable via the real CLI
        # (every caller here already validates net_mm/gross_mm are
        # non-negative before this point), but this function had no
        # guard of its own against a caller who skipped that.
        raise ValueError("mm must be a non-negative, finite water depth")
    if not PLAUSIBLE_MIN_HA <= ha <= PLAUSIBLE_MAX_HA:
        raise ValueError(
            f"ha ({ha}) is outside a realistic single field size "
            f"({PLAUSIBLE_MIN_HA}-{PLAUSIBLE_MAX_HA} ha) - check the units rather "
            "than trust this as a real field area"
        )
    # Multiplying ha * MM_PER_HA_TO_M3 first (both normal-magnitude) before
    # bringing in mm avoids an intermediate underflow that (mm * ha) * ...
    # could otherwise hit for an astronomically small mm - e.g. mm=5e-324,
    # ha=0.1 underflows at "mm * ha" alone even though the true,
    # representable answer (5e-324) survives this order.
    m3 = mm * (ha * MM_PER_HA_TO_M3)
    if not math.isfinite(m3):
        # gross_mm() rejects a gross depth that is itself non-finite, but a
        # finite-yet-astronomically-large depth (from an efficiency near
        # the smallest representable float) can still overflow once
        # multiplied by field area here.
        raise ValueError("mm and ha combined are too large to give a usable volume")
    if mm > 0.0 and m3 == 0.0:
        # The opposite failure: an astronomically small but positive mm
        # (e.g. from the dual crop coefficient method's own hourly water
        # balance) can underflow this multiplication to exactly 0.0,
        # which would print as "no volume needed" despite a real,
        # positive depth - same pattern as m3_to_pump_hours's own
        # underflow guard below.
        raise ValueError("mm and ha combined underflow to a volume of exactly 0.0 m3")
    return m3


def m3_to_pump_hours(m3: float, pump_m3h: float) -> float:
    """Convert a volume (m^3) to pump run time (hours) at a given flow rate (m^3/h)."""
    if not (math.isfinite(m3) and m3 >= 0.0):
        # A volume can't be negative - same reasoning as mm_to_m3's own
        # guard.
        raise ValueError("m3 must be a non-negative, finite volume")
    if not PLAUSIBLE_MIN_PUMP_M3H <= pump_m3h <= PLAUSIBLE_MAX_PUMP_M3H:
        raise ValueError(
            f"pump_m3h ({pump_m3h}) is outside a realistic irrigation pump flow "
            f"({PLAUSIBLE_MIN_PUMP_M3H}-{PLAUSIBLE_MAX_PUMP_M3H} m3/h) - check the "
            "units rather than trust this as a real pump"
        )
    hours = m3 / pump_m3h
    if not math.isfinite(hours):
        # Same overflow risk one step further down the chain.
        raise ValueError("m3 and pump_m3h combined are too large to give a usable pump time")
    if m3 > 0.0 and hours == 0.0:
        # An astronomically small but positive m3 can underflow this
        # division to exactly 0.0, which would print as "no pump time
        # needed" despite a real, positive volume of water.
        raise ValueError("m3 and pump_m3h combined underflow to a pump time of exactly 0.0 hours")
    return hours


def gross_mm(net_mm: float, efficiency: float, leaching_requirement: float = 0.0) -> float:
    """Water to pump (mm) = water the trees need (mm) / (efficiency * (1 - LR)).

    Efficiency accounts for water lost in pipes and at the dripper/furrow
    BEFORE it reaches the root zone (docs/fao56-reference.md, "Units and
    conversions") - conveyance and application losses only. It must NOT
    include deep percolation (water that infiltrates the root zone and
    later drains past it), because the leaching_requirement below is a
    separate, deliberate accounting of exactly that drainage - a
    conventional "irrigation efficiency" figure that already bundles
    percolation in with conveyance/application losses would double-count
    that water once here and once via leaching_requirement (this is
    reachable, since neither this function nor the CLI can verify which
    definition a farm actually measured). It is a farm-specific measured
    value, not a constant, so it is always passed in - there is no
    default.

    leaching_requirement (LR, see fao56.leaching_requirement) is the extra
    fraction of water that must pass below the root zone to keep soil
    salinity under control, given how salty the irrigation water is.
    Defaults to 0.0 (no leaching correction) so callers that don't know
    their irrigation water's salinity aren't forced to guess one - checked
    against two independent sources (an FAO water-management extension
    guide and an irrigation-scheduling tool citing the Rhoades leaching
    equation), since FAO-56 itself states the LR formula but not this
    combination with efficiency: gross = net / (efficiency * (1 - LR)).

    Multiplying efficiency and (1 - LR) together like this only avoids
    double-counting drainage if `efficiency` means water lost BEFORE it
    ever reaches the root zone (pipes, dripper/furrow application - see
    above), not water that infiltrates and later drains past the root
    zone (deep percolation) - drainage from inefficiency and the
    deliberate leaching fraction would otherwise partly be the same water
    counted twice. This is already how `efficiency` is defined in this
    module - the two corrections were checked for consistency with that
    existing definition, not assumed compatible by coincidence.
    """
    if not (math.isfinite(net_mm) and net_mm >= 0.0):
        raise ValueError("net_mm must be a non-negative, finite water depth")
    if not 0.0 < efficiency <= 1.0:
        raise ValueError("efficiency must be greater than 0 and at most 1")
    if efficiency < PLAUSIBLE_MIN_EFFICIENCY:
        # Another example: an efficiency like 1e-300 passes the range
        # check above but is far below any real irrigation system's loss
        # rate, and combined with a large field/pump downstream can still
        # produce a technically-finite but nonsensical printed result.
        raise ValueError(
            f"efficiency ({efficiency}) is below a realistic irrigation efficiency "
            f"floor ({PLAUSIBLE_MIN_EFFICIENCY}) - check the units rather than trust "
            "this as a real measured efficiency"
        )
    if not 0.0 <= leaching_requirement < 1.0:
        raise ValueError("leaching_requirement must be at least 0 and less than 1")
    denominator = efficiency * (1.0 - leaching_requirement)
    # An efficiency near the smallest representable float (e.g. 1e-308) is
    # inside the 0.0-1.0 range checked above but still makes this division
    # overflow to infinity, or underflow the denominator itself to exactly
    # 0.0 (a ZeroDivisionError) - both would otherwise reach the printed
    # summary as a nonsensical gross-water figure.
    if denominator <= 0.0:
        raise ValueError("efficiency and leaching_requirement combined must be greater than 0")
    gross = net_mm / denominator
    if not math.isfinite(gross):
        raise ValueError(
            "efficiency is too small relative to leaching_requirement to give a "
            "usable gross water figure - check these values rather than trust an "
            "infinite result"
        )
    if gross > PLAUSIBLE_MAX_GROSS_WATER_MM:
        raise ValueError(
            f"gross water need ({gross:.1f} mm) is beyond a realistic amount to apply "
            f"in one week ({PLAUSIBLE_MAX_GROSS_WATER_MM} mm) - check net_mm, "
            "efficiency and leaching_requirement rather than trust this figure"
        )
    return gross
