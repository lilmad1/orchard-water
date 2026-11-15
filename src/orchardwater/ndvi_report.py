"""Print the greenness (NDVI) of one field for a range of dates.

Run it like this:
    python -m orchardwater.ndvi_report --field fields/example.geojson \\
        --from 2026-09-01 --to 2026-09-30

For every day the satellite photographed the field, it prints how much of the
field was seen (coverage), how much of that was clear (not cloud), the mean
NDVI, and a small text map of the field. Days with no picture are skipped.
This is SHADOW MODE: for the operator to look at, never for the farmer card, and
greenness is not a measure of water stress.

Each day is printed as soon as it arrives, so if a later day fails (the
service is busy, the connection drops) the days above it are still on screen.

The pictures come from the free Sentinel-2 copy on AWS (earth_search.py). It
needs no account or key. The date range is capped (MAX_DAYS) to keep one run small.

A day is skipped (no NDVI, no map) when too little of the field is clear and
valid (MIN_VALID_SHARE).
"""

import argparse
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from pathlib import Path

from orchardwater import earth_search, ndvi
from orchardwater.field import Field, inside_mask, load_field

# A safety limit on how many days one run may ask for, to keep one run small.
MAX_DAYS = 92

# A day is only reported when at least this share of the field's squares are
# clear and valid (photographed, not cloud or shadow, usable light values).
# Why so high: the goal is that farmers water as little as they safely can. Thin
# cloud edges and shadow darken squares, and darker squares look "less green",
# which would read as stress and push towards MORE water. Greenness of a citrus
# canopy changes over weeks, so skipping a hazy day costs little.
# TODO(verify): 0.8 is the operator's strict choice; check it against real fields.
MIN_VALID_SHARE = 0.8

# The text map gets too wide to read past this many squares per row.
MAX_MAP_WIDTH = 80

MAP_LEGEND = (
    "Map: each character is one 10 m square. digit = NDVI x 10 (3 means 0.3 to 0.39, "
    "9 means 0.9 or more), - = below 0, . = cloud or no data, blank = outside the outline.\n"
    "Coverage = share of the field the satellite photographed that day. "
    "Clear = share of the photographed part that is not cloud or unsure."
)

SHADOW_HEADER = (
    "SHADOW MODE: greenness (NDVI) from Sentinel-2, for checking only.\n"
    "Greenness is not water stress. Do not use it for watering decisions yet."
)


def days_between(start: date, end: date) -> list[date]:
    """Every calendar day from start to end, both included."""
    if end < start:
        raise ValueError("--to must not be before --from.")
    count = (end - start).days + 1
    if count > MAX_DAYS:
        raise ValueError(f"That is {count} days; the limit is {MAX_DAYS} per run.")
    return [start + timedelta(days=offset) for offset in range(count)]


def map_character(value: float | None) -> str:
    """One character for one square of the text map."""
    if value is None:
        return "."
    if value < 0.0:
        return "-"
    return str(min(9, int(value * 10)))


def format_map(picture: ndvi.Picture, inside: list[list[bool]]) -> list[str]:
    """The text map of the field, one string per row of squares."""
    grid = ndvi.ndvi_grid(picture)
    if len(grid) > 0 and len(grid[0]) > MAX_MAP_WIDTH:
        return [f"(field is {len(grid[0])} squares wide; map skipped, limit is {MAX_MAP_WIDTH})"]
    rows: list[str] = []
    for row_index, row in enumerate(grid):
        characters = [
            map_character(value) if inside[row_index][col_index] else " "
            for col_index, value in enumerate(row)
        ]
        rows.append("".join(characters))
    return rows


def valid_share(summary: ndvi.FieldSummary) -> float:
    """Clear and valid squares as a share of ALL squares inside the outline (0 to 1)."""
    if summary.field_squares == 0:
        return 0.0
    return summary.clear_squares / summary.field_squares


def format_day(
    day: date,
    picture: ndvi.Picture,
    summary: ndvi.FieldSummary,
    inside: list[list[bool]],
) -> str:
    """The printed block for one day that has a picture of the field."""
    coverage = round(summary.coverage * 100)
    clear = round(summary.clear_share * 100)
    squares = f"{summary.clear_squares} of {summary.observed_squares} photographed squares"
    valid = valid_share(summary)
    first_line = (
        f"{day.isoformat()}  coverage {coverage}%, clear {clear}%, valid {round(valid * 100)}% "
        f"({squares})"
    )
    if summary.coverage < 1.0:
        first_line += "\n  WARNING: the satellite photographed only part of the field"
    if picture.pictures_combined > 1:
        first_line += (
            f"\n  NOTE: put together from {picture.pictures_combined} pictures taken the same day"
        )
    if picture.discarded_squares > 0:
        first_line += (
            f"\n  WARNING: {picture.discarded_squares} photographed squares were left out "
            "because their light values were missing or negative"
        )
    if valid < MIN_VALID_SHARE or summary.mean_ndvi is None:
        return (
            f"{first_line}\n  skipped: only {round(valid * 100)}% of the field is clear and "
            f"valid (limit {round(MIN_VALID_SHARE * 100)}%)"
        )
    low, high = summary.min_ndvi, summary.max_ndvi
    if low is None or high is None:
        raise ValueError("Summary has a mean NDVI but no lowest or highest value.")
    numbers = f"  NDVI mean {summary.mean_ndvi:.2f}, lowest {low:.2f}, highest {high:.2f}"
    map_rows = ["  " + row for row in format_map(picture, inside)]
    return "\n".join([first_line, numbers, *map_rows])


def _mask_for(
    field: Field, picture: ndvi.Picture, cache: dict[tuple[int, int], list[list[bool]]]
) -> list[list[bool]]:
    """The inside-the-outline mask for this picture's size (worked out once per size)."""
    height = len(picture.red)
    width = len(picture.red[0]) if height > 0 else 0
    if (width, height) not in cache:
        mask = inside_mask(field, width, height)
        if not any(any(row) for row in mask):
            raise ValueError(
                "No 10 m square has its centre inside this outline: the field is too small or thin."
            )
        cache[(width, height)] = mask
    return cache[(width, height)]


def _day_block(
    field: Field,
    day: date,
    fetch_day: Callable[[date], ndvi.Picture | None],
    masks: dict[tuple[int, int], list[list[bool]]],
) -> str | None:
    """The printed block for one day, or None if the satellite did not photograph the field.

    Kept as its own function so the day's picture is freed as soon as the block is
    built, and only one picture is in memory at a time, however many days run.
    """
    picture = fetch_day(day)
    if picture is None:
        return None
    inside = _mask_for(field, picture, masks)
    summary = ndvi.summarize_field(picture, inside)
    if summary.observed_squares == 0 and picture.discarded_squares == 0:
        return None
    return format_day(day, picture, summary, inside)


def report_parts(
    field: Field, days: list[date], fetch_day: Callable[[date], ndvi.Picture | None]
) -> Iterator[str]:
    """Yield the report piece by piece: header, then one block per day, then a tail.

    fetch_day turns a date into that day's Picture, or None if the satellite did
    not pass. Real runs pass a function that reads from AWS; tests pass a fake,
    so they need no internet.
    Because this is a generator, a failure on day N leaves days 1 to N-1
    already yielded (and printed).
    """
    yield SHADOW_HEADER
    yield f"Field box: {field.width_m:.0f} m x {field.height_m:.0f} m."
    masks: dict[tuple[int, int], list[list[bool]]] = {}
    shown = 0
    skipped = 0
    for day in days:
        block = _day_block(field, day, fetch_day, masks)
        if block is None:
            skipped += 1  # the satellite did not photograph the field this day
            continue
        shown += 1
        yield block
    yield MAP_LEGEND
    yield f"{shown} days with a picture, {skipped} days without."


def build_report(
    field: Field, days: list[date], fetch_day: Callable[[date], ndvi.Picture | None]
) -> str:
    """The whole report as one string (used by tests; main prints piece by piece)."""
    return "\n\n".join(report_parts(field, days, fetch_day))


def parse_date(text: str) -> date:
    """argparse helper: read YYYY-MM-DD, with a plain message if it is wrong."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date like 2026-09-30") from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Greenness (NDVI) of one field, shadow mode.")
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
        found = earth_search.search_items(field, days[0], days[-1])

        def fetch_day(day: date) -> ndvi.Picture | None:
            return earth_search.picture_for_day(field, found.get(day, []))

        for part in report_parts(field, days, fetch_day):
            print(part, end="\n\n", flush=True)
            printed += 1
    except ValueError as error:
        note = " The days printed above are complete." if printed > 2 else ""
        raise SystemExit(f"{error}{note}") from None


if __name__ == "__main__":
    main()
