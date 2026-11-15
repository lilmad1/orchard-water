"""Print the surface temperature of one field for every Landsat pass in a date range.

Run it like this:
    python -m orchardwater.thermal_report --field fields/example.geojson
        --from 2026-08-20 --to 2026-09-30   (all on one line)

For every day a Landsat satellite measured the field, it prints how much of the field
was measured (coverage), how much of that was clear (not cloud, shadow, snow or water),
and the mean, lowest and highest surface temperature. Days with no pass are not listed
(Landsat 8 and 9 together pass about every 8 days). This is SHADOW MODE: for the operator to
look at, never for the farmer card, and temperature alone is not a measure of water
stress.

Only one number per field is printed, no map: the heat sensor's real detail is about
100 m, so a 1 ha field holds about one real reading (see thermal.py).

The pictures come from Microsoft Planetary Computer (landsat.py). It needs no account.
"""

import argparse
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from orchardwater import landsat, thermal
from orchardwater.field import Field, area_m2, inside_mask, load_field
from orchardwater.ndvi_report import days_between, parse_date

# A pass is skipped if ANY cloud, thin cirrus or cloud shadow shows up in the area read
# (the field plus landsat.BUFFER_M around it), however small. Squares next to cloud are
# blended with its cold and read too cold (see landsat.BUFFER_M). Only after that check
# does the share below apply, for the other reasons a square is unusable.
#
# A pass is only reported when at least this share of the field's squares are clear
# and valid. Why so high: cloud and cloud shadow read too COLD, and a too-cold field
# looks unstressed, which could lead to too little water. That is the unsafe
# direction, so a hazy pass is skipped rather than shown.
# TODO(verify): 0.8 is copied from the Sentinel-2 report (the operator's strict choice);
# check it against real fields.
MIN_VALID_SHARE = 0.8

SHADOW_HEADER = (
    "SHADOW MODE: surface temperature from Landsat 8 and 9, for checking only.\n"
    "Surface temperature is not water stress and it is not the air temperature. "
    "Do not use it for watering decisions yet."
)

LEGEND = (
    "Coverage = share of the field the satellite measured that pass. "
    "Clear = share of the measured part that is not cloud, shadow, snow, water or unsure. "
    "Valid = clear squares as a share of the whole field. A pass is skipped if cloud or "
    "shadow shows anywhere in the area around the field.\n"
    "The heat sensor really sees squares of about 100 m. The 30 m squares in the file are "
    "blended from those readings, so only the field-level numbers above mean something."
)


@dataclass(frozen=True)
class Pass:
    """One day's Landsat measurement of the field.

    label: which satellite and when, for example "Landsat 9, 11:32 UTC".
    picture: the temperature picture over the field's box.
    """

    label: str
    picture: thermal.ThermalPicture


def _distance_text(metres: float) -> str:
    """A distance for the report text: "2 km" from 1000 m up, otherwise "300 m"."""
    if metres >= 1000.0:
        return f"{metres / 1000.0:g} km"
    return f"{metres:.0f} m"


# The short codes for why a pass is skipped, in the order they are checked. The website
# export (landsat_export.py) writes these codes, so do not rename them without bumping its
# schema version.
REASON_CLOUD = "cloud_near_field"
REASON_NO_QUALITY = "no_quality_information"
REASON_LOW_VALID = "low_valid_share"
REASON_CODES = (REASON_CLOUD, REASON_NO_QUALITY, REASON_LOW_VALID)


def _percent_text(share: float) -> str:
    """A share as a percentage for the report text, never rounded up to the limit.

    A share just under the limit (0.7996 or 0.79995) would round to "80%" and read "only 80%
    (limit 80%)", which looks like a mistake. In that one case the percentage is cut off
    (not rounded) at three decimals and kept below the limit.
    """
    whole = round(share * 100)
    if share < MIN_VALID_SHARE and whole >= round(MIN_VALID_SHARE * 100):
        # Cut off at three decimals; the min() covers a share so close to the limit that
        # the cut-off value would still equal it (needs about 10**16 squares).
        cut = math.floor(share * 100000) / 1000
        return f"{min(cut, MIN_VALID_SHARE * 100 - 0.001):.3f}%"
    return f"{whole}%"


def percent(share: float) -> int:
    """A share (0 to 1) as the whole percentage the report prints. Python rounds .5 to even."""
    return round(share * 100)


def celsius_text(kelvin: float) -> str:
    """A temperature in kelvin as the one-decimal Celsius text the report prints."""
    return f"{thermal.to_celsius(kelvin):.1f}"


def skip_decision(summary: thermal.ThermalSummary) -> tuple[str, str] | None:
    """Why this pass must not show a temperature: (code, plain sentence), or None if it may.

    Checked in this order: cloud near the field, no quality information, too little clear
    and valid. The code is one of REASON_CODES.
    """
    if summary.cloud_squares > 0:
        return (
            REASON_CLOUD,
            "cloud, thin cirrus or cloud shadow seen within about "
            f"{_distance_text(landsat.BUFFER_M)} of the field ({summary.cloud_squares} squares). "
            "Squares near cloud read too cold, and the cold reaches further than the cloud flags.",
        )
    if summary.unknown_squares > 0:
        return (
            REASON_NO_QUALITY,
            f"no quality information for {summary.unknown_squares} squares of the area "
            "(the satellite file does not cover them, or they are fill), "
            "so cloud near the field cannot be ruled out.",
        )
    if summary.valid_share < MIN_VALID_SHARE or summary.mean_k is None:
        return (
            REASON_LOW_VALID,
            f"only {_percent_text(summary.valid_share)} of the field is clear and valid "
            f"(limit {round(MIN_VALID_SHARE * 100)}%)",
        )
    return None


def _skip_reason(summary: thermal.ThermalSummary) -> str | None:
    """The sentence from skip_decision, or None if the pass may show a temperature."""
    decision = skip_decision(summary)
    return None if decision is None else decision[1]


def format_pass(
    day: date,
    measured: Pass,
    summary: thermal.ThermalSummary,
) -> str:
    """The printed block for one day that has a measurement of the field.

    A skipped pass says SKIPPED on its first line, so that its coverage and valid numbers
    cannot be taken for a clean pass at a glance; the reason follows below.
    """
    picture = measured.picture
    coverage = percent(summary.coverage)
    clear = percent(summary.clear_share)
    valid = percent(summary.valid_share)
    reason = _skip_reason(summary)
    skipped = "SKIPPED  " if reason is not None else ""
    first_line = (
        f"{day.isoformat()}  {skipped}{measured.label}  coverage {coverage}%, clear {clear}%, "
        f"valid {valid}% ({summary.clear_squares} of {summary.field_squares} squares)"
    )
    if summary.coverage < 1.0:
        first_line += "\n  WARNING: the satellite measured only part of the field"
    if picture.discarded_squares > 0:
        first_line += (
            f"\n  WARNING: {picture.discarded_squares} squares (in the field and the margin "
            "around it) were left out because their temperature was outside the believable range"
        )
    if reason is not None:
        return f"{first_line}\n  skipped: {reason}"
    low, high = summary.min_k, summary.max_k
    if summary.mean_k is None or low is None or high is None:
        raise ValueError("Summary has no mean, lowest or highest temperature to print.")
    numbers = (
        f"  Surface temperature mean {celsius_text(summary.mean_k)} C "
        f"({summary.mean_k:.1f} K), lowest {celsius_text(low)} C, "
        f"highest {celsius_text(high)} C"
    )
    detail = (
        f"  The field area is worth about {summary.real_readings:.1f} real 100 m readings; "
        "values inside that are blended, not measured."
    )
    return "\n".join([first_line, numbers, detail])


def has_measurement(summary: thermal.ThermalSummary) -> bool:
    """Whether a day counts as a pass over the field at all, for the report and the website.

    A catalogue scene can cover only the margin around the field: then no field square was
    measured and none was thrown out, and the day is left out as "no pass". A day where
    every temperature INSIDE the field was thrown out as unbelievable still counts (it
    prints a warning). A thrown-out square in the margin alone does not make a pass.
    """
    return summary.observed_squares > 0 or summary.discarded_field_squares > 0


def mask_for(
    field: Field, picture: thermal.ThermalPicture, cache: dict[tuple[int, int], list[list[bool]]]
) -> list[list[bool]]:
    """The inside-the-outline mask for this picture's size (worked out once per size)."""
    height = len(picture.kelvin)
    width = len(picture.kelvin[0]) if height > 0 else 0
    if (width, height) not in cache:
        # The picture covers the field plus a margin (landsat.view_field), so the mask
        # is worked out on that bigger box.
        mask = inside_mask(landsat.view_field(field), width, height)
        if not any(any(row) for row in mask):
            raise ValueError(
                "No 30 m square has its centre inside this outline: the field is too small or thin."
            )
        cache[(width, height)] = mask
    return cache[(width, height)]


def _day_block(
    field: Field,
    day: date,
    fetch_day: Callable[[date], Pass | None],
    masks: dict[tuple[int, int], list[list[bool]]],
) -> str | None:
    """The printed block for one day, or None if no Landsat measured the field.

    Its own function so the day's picture is freed as soon as the block is built.
    """
    measured = fetch_day(day)
    if measured is None:
        return None
    inside = mask_for(field, measured.picture, masks)
    summary = thermal.summarize_field(measured.picture, inside, area_m2(field))
    if not has_measurement(summary):
        return None
    return format_pass(day, measured, summary)


def report_parts(
    field: Field, days: list[date], fetch_day: Callable[[date], Pass | None]
) -> Iterator[str]:
    """Yield the report piece by piece: header, one block per pass, then a tail.

    fetch_day turns a date into that day's Pass, or None if no Landsat passed. Real
    runs pass a function that reads from Planetary Computer; tests pass a fake. Because
    this is a generator, a failure on day N leaves the earlier days already printed.
    """
    yield SHADOW_HEADER
    yield (
        f"Field box: {field.width_m:.0f} m x {field.height_m:.0f} m. Each pass is checked over "
        f"the field plus {_distance_text(landsat.BUFFER_M)} around it."
    )
    masks: dict[tuple[int, int], list[list[bool]]] = {}
    shown = 0
    skipped = 0
    for day in days:
        block = _day_block(field, day, fetch_day, masks)
        if block is None:
            skipped += 1  # no Landsat pass over the field this day
            continue
        shown += 1
        yield block
    yield LEGEND
    yield f"{shown} days with a measurement, {skipped} days without."


def build_report(field: Field, days: list[date], fetch_day: Callable[[date], Pass | None]) -> str:
    """The whole report as one string (used by tests; main prints piece by piece)."""
    return "\n\n".join(report_parts(field, days, fetch_day))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Surface temperature of one field, shadow mode.")
    parser.add_argument(
        "--field", type=Path, required=True, help="GeoJSON file with the field outline"
    )
    parser.add_argument(
        "--from", dest="start", type=parse_date, required=True, help="first day, YYYY-MM-DD"
    )
    parser.add_argument(
        "--to", dest="end", type=parse_date, required=True, help="last day, YYYY-MM-DD"
    )
    args = parser.parse_args(argv)
    printed = 0
    try:
        field = load_field(args.field)
        days = days_between(args.start, args.end)
        found = landsat.search_items(field, days[0], days[-1])

        def fetch_day(day: date) -> Pass | None:
            items = found.get(day, [])
            picture = landsat.picture_for_day(field, items)
            if picture is None:
                return None
            return Pass(landsat.pass_label(items), picture)

        for part in report_parts(field, days, fetch_day):
            print(part, end="\n\n", flush=True)
            printed += 1
    except ValueError as error:
        note = " The days printed above are complete." if printed > 2 else ""
        raise SystemExit(f"{error}{note}") from None


if __name__ == "__main__":
    main()
