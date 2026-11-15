"""Landsat 8 and 9 surface-temperature pictures from Microsoft Planetary Computer.

This is the only file that talks to Planetary Computer (same rule as earth_search.py
for Sentinel-2). It does three things:
  1. ask the catalogue which Landsat passes cover the field on which days,
  2. read only the field's pixels of the temperature and quality files,
  3. turn the stored numbers into kelvin and hand thermal.py a ThermalPicture.

It does no temperature math beyond the unit conversion: that is thermal.py.
Landsat is fetched here and NOT from AWS, because on AWS the person who downloads
Landsat pays. Planetary Computer needs no account: each file address just needs a
short-lived key added to the end, which the planetary_computer package fetches.

Facts checked on 2026-10-04 (collection "landsat-c2-l2" on
https://planetarycomputer.microsoft.com/api/stac/v1): asset "lwir11" holds the
surface temperature as whole numbers, kelvin = stored x scale + offset with scale
0.00341802 and offset 149 (read from each picture's own raster:bands, never typed in
here), and a stored 0 means no data. Asset "qa_pixel" holds the quality flags
(see thermal.py). Both files are on a 30 m grid in the tile's metre map (UTM 29N
for Agadir). See docs/landsat-reference.md.
"""

import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date
from typing import Any

import numpy as np
import planetary_computer
import pystac

from orchardwater import earth_search
from orchardwater.field import Field, inside_mask, with_margin
from orchardwater.thermal import (
    CLOUD_FLAG,
    ThermalPicture,
    cloud_evidence,
    quality_available,
    thermal_picture_from_bands,
)

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "landsat-c2-l2"

# The satellites we can read. The collection also holds Landsat 4, 5 and 7, whose
# temperature file has another name ("lwir", not "lwir11"), so they are left out.
SUPPORTED_PLATFORMS = ("landsat-8", "landsat-9")

# Names of the picture files (assets) we read.
TEMPERATURE_ASSET = "lwir11"
QUALITY_ASSET = "qa_pixel"

# The files come on a 30 m grid (the heat sensor's real detail is about 100 m, see
# thermal.py). We build our picture on that same 30 m grid.
SQUARE_SIZE_M = 30.0

# How much extra map (metres, on every side) we read around the field. Every temperature
# value is blended from about 100 m of ground, so a square that is itself clear but lies
# next to cloud or cloud shadow is still too cold, and a cloud just OUTSIDE the field would
# never be seen if we read only the field's own box. The report skips a pass if any cloud,
# thin cirrus or shadow shows up anywhere in the area we read (see thermal_report.py).
# Measured with live data. First: clear squares 1-2 squares (30-60 m) from a flagged one
# read 2-10 K too cold, and still 1-5 K at 120-210 m. Second (76 real passes over two areas
# near Agadir and Taroudant, 2025-2026) showed the cold bias reaches much further than the
# sensor blur, probably through thin cloud edges and haze that the quality flags miss:
# median 1.1-1.5 K too cold at 300-450 m, 0.7-1.35 K at 450-600 m, 0.2-1.0 K at 600-900 m,
# and up to 0.8 K inland at 900-1500 m; single passes up to 4.9 K. With the old 300 m margin
# about 22-34 % of accepted small fields read more than 2 K too cold when cloud lay 300-600
# m away; with no cloud within 2 km only 2 % did (the noise). The margin is 2 km. Price: on
# the example field 39 of 76 passes are shown instead of 54.
# TODO(verify): 2 km is an estimate from that data, not a tested optimum; the reference
# passes in that analysis were 1-24 days apart, so land change adds some noise. The area
# read is a box, so on a field with a long, thin or tilted outline it reaches further than
# 2 km from the outline at the corners, which only makes the check stricter.
BUFFER_M = 2000.0

# Seconds we wait for the read key. The planetary_computer package sets no time limit
# of its own, so we give up waiting after this long (see _call_with_timeout).
SIGNING_TIMEOUT_S = 60.0

# The shape of a function that adds the read key to a file address. Tests pass a
# fake so they need no internet.
Signer = Callable[[str], str]


def search_items(
    field: Field,
    start: date,
    end: date,
    search: earth_search.SearchFunction | None = None,
) -> dict[date, list[pystac.Item]]:
    """Find the Landsat 8 and 9 passes covering the field, grouped by UTC day, newest first.

    Items from any other satellite, or without a platform name, are dropped here even
    if the search returned them: one unreadable older item must not stop the report.
    """
    find = search if search is not None else _stac_search

    def supported_only(field: Field, start: date, end: date) -> list[pystac.Item]:
        return [
            item
            for item in find(field, start, end)
            if item.properties.get("platform") in SUPPORTED_PLATFORMS
        ]

    return earth_search.search_items(search_area(field), start, end, supported_only)


def search_area(field: Field) -> Field:
    """The field as the catalogue should see it: the whole area we read, as a rectangle.

    A scene that covers only the margin around the field still supplies the quality
    numbers there, so it must be found.
    """
    view = view_field(field)
    corners = [
        (view.west, view.south),
        (view.east, view.south),
        (view.east, view.north),
        (view.west, view.north),
    ]
    return replace(view, corners=corners)


def _stac_search(field: Field, start: date, end: date) -> list[pystac.Item]:
    """The real Planetary Computer catalogue search for Landsat 8 and 9."""
    query = {"platform": {"in": list(SUPPORTED_PLATFORMS)}}
    return earth_search.catalogue_search(field, start, end, STAC_URL, COLLECTION, query)


class _SigningTimeout(Exception):
    """The key service did not answer in time (our own error, so it cannot be confused with
    the many ValueError subclasses that the package's libraries raise)."""


def view_field(field: Field) -> Field:
    """The field with BUFFER_M metres of extra map on every side: the area we read.

    The outline is unchanged; only the box around it is bigger. Use inside_mask() on
    this field to find which squares of the picture lie inside the real outline.
    """
    return with_margin(field, BUFFER_M)


def _call_with_timeout(function: Callable[[], str], seconds: float) -> str:
    """Run function in a helper thread and give up after the given number of seconds.

    The planetary_computer package asks the key service without any time limit, and its
    web library then ignores a limit set on the connection, so a stalled service would
    freeze the report for ever (shown with a local server that never answers). A thread
    we stop waiting for keeps running in the background, but it is a daemon thread: it
    cannot stop the program from ending.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = function()
        except BaseException as error:  # handed back to the waiting thread below
            outcome["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise _SigningTimeout(
            f"Planetary Computer gave no read key within {seconds:.0f} seconds "
            "(its key service may be busy or limiting requests; try again later)."
        )
    if "error" in outcome:
        raise outcome["error"]
    return str(outcome["value"])


def sign_href(href: str) -> str:
    """Add the short-lived read key to a Planetary Computer file address.

    The package keeps the key and gets a new one when it is about to expire, so one
    run does not ask for a key per file. A failure becomes a plain ValueError.
    """
    problem: str
    try:
        return _call_with_timeout(lambda: planetary_computer.sign_url(href), SIGNING_TIMEOUT_S)
    except _SigningTimeout as error:
        problem = str(error)
    except ValueError as error:
        # An answer in the wrong form (not JSON, a missing field). The library's own text
        # can quote the answer, which is where a key would be, so only the kind is shown.
        problem = (
            "Could not get a read key from Planetary Computer: it answered with something "
            f"unexpected ({type(error).__name__})."
        )
    except Exception as error:  # the package and its web library raise many kinds of error
        problem = f"Could not get a read key from Planetary Computer: {error}"
    # Raised outside the except blocks so the original error is not attached to ours.
    raise ValueError(problem)


def read_item(
    item: pystac.Item, field: Field, sign: Signer
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Read one Landsat pass over the field: (kelvin, qa, photographed, qa_valid) grids.

    kelvin = stored number x scale + offset, read from the picture's own
    information. A stored 0 means no data, so those squares are not photographed. The
    quality number is read separately: it can be available where the temperature is
    missing, and a cloud flag there still counts. A quality number of 0, or one with the
    fill flag set, says nothing about the ground (see thermal.quality_available).
    """
    stored = earth_search.read_band(
        sign(earth_search.asset_href(item, TEMPERATURE_ASSET)), field, np.uint16, SQUARE_SIZE_M
    )
    qa = earth_search.read_band(
        sign(earth_search.asset_href(item, QUALITY_ASSET)), field, np.uint16, SQUARE_SIZE_M
    )
    scale, offset = earth_search.scale_and_offset(item, TEMPERATURE_ASSET)
    kelvin = stored.astype(np.float64) * scale + offset
    return kelvin, qa, stored != 0, quality_available(qa)


def picture_for_day(
    field: Field, items: list[pystac.Item], sign: Signer = sign_href
) -> ThermalPicture | None:
    """Build one day's ThermalPicture from that day's items (most recent first).

    The picture covers view_field(field): the field's box plus BUFFER_M on every side.
    Where two passes overlap, the more recent one that has data wins, for the temperature
    and for the quality number separately, except that a cloud flag from ANY pass is
    kept (the square is then flagged cloud, which fails closed).

    Reading stops as soon as the temperature is filled for every square inside the
    outline and the quality number for every square of the whole area. A gap in the
    quality number could hide a cloud, so the margin counts there. A temperature gap in
    the margin is harmless, as only the quality number is used there.

    Returns None if the day has no items at all.
    """
    if len(items) == 0:
        return None
    view = view_field(field)
    width, height = earth_search.grid_size(view, SQUARE_SIZE_M)
    kelvin = np.full((height, width), np.nan)
    qa = np.zeros((height, width), dtype=np.uint16)
    filled = np.zeros((height, width), dtype=bool)
    qa_filled = np.zeros((height, width), dtype=bool)
    inside = np.array(inside_mask(view, width, height), dtype=bool)
    for item in items:
        if inside.any() and filled[inside].all() and qa_filled.all():
            break  # a newer pass has everything we need; an older one adds nothing
        item_kelvin, item_qa, item_photographed, item_qa_valid = read_item(item, view, sign)
        take = item_photographed & ~filled
        kelvin[take] = item_kelvin[take]
        filled |= item_photographed
        take_qa = item_qa_valid & ~qa_filled
        qa[take_qa] = item_qa[take_qa]
        # Cloud evidence is never thrown away. Where an earlier scene already supplied the
        # quality number but THIS scene says cloud, the square is flagged cloud too. This
        # scene may be the one lending the temperature to a neighbouring square, and a
        # temperature must not be called clear if any scene it came from saw cloud near it.
        also_seen = item_qa_valid & qa_filled
        qa[also_seen & cloud_evidence(item_qa)] |= CLOUD_FLAG
        qa_filled |= item_qa_valid
    return thermal_picture_from_bands(kelvin, qa, filled, qa_filled)


def pass_label(items: list[pystac.Item]) -> str:
    """A short label for the report, like "Landsat 9, 11:32 UTC".

    The name comes from the catalogue's platform field and the time from the most
    recent item of the day (the first in the list).
    """
    names = sorted(
        {
            str(item.properties.get("platform", "landsat")).replace("-", " ").capitalize()
            for item in items
        }
    )
    taken = items[0].datetime
    when = f", {taken.astimezone(UTC):%H:%M} UTC" if taken is not None else ""
    return ", ".join(names) + when
