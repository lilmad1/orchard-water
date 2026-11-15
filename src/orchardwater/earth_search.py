"""Satellite pictures from the free Sentinel-2 copy on AWS (Element 84 Earth Search).

This is the only file that talks to Earth Search and AWS (same rule as weather.py
for Open-Meteo). It does three things:
  1. ask the catalogue which Sentinel-2 pictures cover the field on which days,
  2. read only the field's pixels out of those picture files (never a whole tile),
  3. turn the stored numbers into reflectance and hand ndvi.py a Picture.

It does no greenness math: that is ndvi.py. No account, key or quota is needed.
The data is ESA's Sentinel-2 L2A product; Copernicus Sentinel data must still be
credited wherever it is shown.

Docs used (checked 2026-10-04):
  https://element84.com/earth-search/  (catalogue at STAC_URL below)
Collection "sentinel-2-c1-l2a" has the 2022-and-later processing that adds an
offset of -0.1 to every red and near-infrared number. The offset and scale are
read from each picture's own metadata (raster:bands), never typed in here.
"""

import logging
import math
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pystac
import rasterio
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.errors import RasterioError
from rasterio.transform import Affine, from_bounds
from rasterio.warp import reproject

from orchardwater.field import Field, inside_mask
from orchardwater.ndvi import CLEAR_SCL_CLASSES, Picture, picture_from_bands

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-c1-l2a"

# Names of the picture files (assets) we read. B04 is red light, B08 is
# near-infrared light, SCL is the scene class (cloud, shadow, vegetation, ...).
RED_ASSET = "red"
NIR_ASSET = "nir"
SCL_ASSET = "scl"
# The true-colour picture: three 8-bit bands (red, green, blue), 10 m, stretched by the
# provider for looking at. Never use it for NDVI: that needs the raw red and
# near-infrared reflectance above. Source: the asset list and "raster:bands" of the
# Earth Search items for 2026-06-29, checked 2026-10-09.
VISUAL_ASSET = "visual"

# Sentinel-2's red and near-infrared bands are 10 m squares. We build the
# picture from 10 m squares, the sharpest the satellite offers.
SQUARE_SIZE_M = 10.0

# The largest picture (squares per side) we will build. field.MAX_SIDE_M (20 km =
# 2000 squares) stays below it. AWS has no such limit; this only protects memory,
# because a Picture is plain lists and a 2500 x 2500 one needs several hundred MB.
# TODO(verify): 2500 is kept from the earlier satellite service, not re-derived.
MAX_SQUARES_PER_SIDE = 2500

# The most pictures one search may return. A field gets a picture about every
# 2-3 days, so 500 covers far more than the 92-day report limit.
MAX_ITEMS = 500

# Settings for reading satellite files over the internet. AWS_NO_SIGN_REQUEST
# says "this is public data, do not look for AWS keys". The next two stop the
# reader from listing whole folders. The last three give up on a stuck download
# instead of hanging (seconds), and retry a failed one a few times.
READ_SETTINGS: dict[str, Any] = {
    "AWS_NO_SIGN_REQUEST": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_TIMEOUT": "60",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "2",
}

# Seconds to wait for the catalogue: (to connect, to receive an answer). Without a
# limit a stuck connection would freeze the report before it prints anything. It
# applies to every catalogue request, including the follow-up pages of a search.
CATALOGUE_TIMEOUT_S = (10.0, 60.0)

# The shape of a function that finds pictures: field, first day, last day in,
# list of catalogue items out. Tests pass a fake so they need no internet.
SearchFunction = Callable[[Field, date, date], list[pystac.Item]]


def grid_size(field: Field, square_size_m: float = SQUARE_SIZE_M) -> tuple[int, int]:
    """Return (width, height) of the picture in squares of about square_size_m (default 10 m)."""
    width = max(1, math.ceil(field.width_m / square_size_m))
    height = max(1, math.ceil(field.height_m / square_size_m))
    if width > MAX_SQUARES_PER_SIDE or height > MAX_SQUARES_PER_SIDE:
        raise ValueError(
            f"Picture would be {width} x {height} squares; "
            f"limit is {MAX_SQUARES_PER_SIDE} per side."
        )
    return width, height


def search_items(
    field: Field, start: date, end: date, search: SearchFunction | None = None
) -> dict[date, list[pystac.Item]]:
    """Find the pictures covering the field, grouped by the day they were taken (UTC).

    start and end are both included. Within one day the most recent picture
    comes first, because that is the one that wins where two overlap.
    """
    find = search if search is not None else _stac_search
    by_day: dict[date, list[pystac.Item]] = {}
    for item in find(field, start, end):
        if item.datetime is None:
            continue
        taken = item.datetime.astimezone(UTC)
        by_day.setdefault(taken.date(), []).append(item)
    for items in by_day.values():
        items.sort(key=_taken_at, reverse=True)
    return by_day


def _taken_at(item: pystac.Item) -> datetime:
    """The moment a picture was taken (the caller has already skipped items without one)."""
    assert item.datetime is not None
    return item.datetime


def _stac_search(field: Field, start: date, end: date) -> list[pystac.Item]:
    """The real Earth Search catalogue search for Sentinel-2."""
    return catalogue_search(field, start, end, STAC_URL, COLLECTION)


def catalogue_search(
    field: Field,
    start: date,
    end: date,
    url: str,
    collection: str,
    query: dict[str, Any] | None = None,
) -> list[pystac.Item]:
    """Search one catalogue for pictures over the field: one request for the whole range.

    landsat.py uses this too, with the Planetary Computer address and collection, and a
    query that keeps only the satellites it can read (for example
    {"platform": {"in": ["landsat-8", "landsat-9"]}}).
    """
    ring = [[lon, lat] for lon, lat in field.corners]
    ring.append(ring[0])  # GeoJSON closes a shape by repeating the first point
    try:
        client = Client.open(url, timeout=CATALOGUE_TIMEOUT_S)
        search = client.search(
            collections=[collection],
            intersects={"type": "Polygon", "coordinates": [ring]},
            datetime=f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
            query=query,
            max_items=MAX_ITEMS,
        )
        with warnings.catch_warnings():
            # pystac warns about storage details we never use; it is noise here.
            warnings.filterwarnings("ignore", message="Could not parse bucket/account")
            return list(search.items())
    except Exception as error:  # the catalogue client raises many kinds of error
        raise ValueError(f"The satellite catalogue search failed: {error}") from error


def scale_and_offset(item: pystac.Item, asset_key: str) -> tuple[float, float]:
    """Return (scale, offset) that turn a stored number into reflectance.

    reflectance = stored number x scale + offset. Both come from the picture's
    metadata: item.assets[asset_key].extra_fields["raster:bands"][0]["scale"] and
    ["offset"]. For the collection we use, expect scale 0.0001 and offset -0.1.

    Raises ValueError, naming the item and the missing piece, if the asset, the
    "raster:bands" list, or the scale or offset is missing. It must refuse
    rather than guess: a wrong offset shifts every NDVI in the report.
    """
    asset = item.assets.get(asset_key)
    if asset is None:
        raise ValueError(f"Picture {item.id} has no '{asset_key}' file.")
    bands = asset.extra_fields.get("raster:bands")
    if not isinstance(bands, list) or len(bands) == 0 or not isinstance(bands[0], dict):
        raise ValueError(f"Picture {item.id}: '{asset_key}' has no raster:bands information.")
    scale = _band_number(item, asset_key, bands[0], "scale")
    offset = _band_number(item, asset_key, bands[0], "offset")
    return scale, offset


def _band_number(item: pystac.Item, asset_key: str, band: dict[str, Any], name: str) -> float:
    """One finite number from a raster:bands entry, or a plain error naming what is wrong."""
    value = band.get(name)
    # bool is a kind of int in Python, so it is refused by name: True would read as 1.
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"Picture {item.id}: '{asset_key}' has no usable {name} in raster:bands.")
    return float(value)


def read_band(
    href: str,
    field: Field,
    dtype: type[np.generic],
    square_size_m: float = SQUARE_SIZE_M,
    *,
    band: int = 1,
    margin_squares: int = 0,
) -> np.ndarray:
    """Read the field's squares of one band of a file as a (height x width) grid.

    square_size_m is the size of one square (10 m for Sentinel-2; landsat.py asks for
    30 m). An error message never shows anything after a "?" in the address: a signed
    address carries a temporary key there.

    band says which band of the file to read (1 is the first). margin_squares adds
    that many extra squares of surrounding land on every side, so the grid is
    (height + 2 x margin) x (width + 2 x margin) and the field's own squares sit at
    rows margin to margin + height and columns margin to margin + width. A margin
    square has the same size as a field square. With both left at their defaults
    the result is exactly what this function always returned.

    The grid covers the field's box: row 0 is the north edge and column 0 the west
    edge, the same layout as before. The file is in metres (a flat UTM map);
    rasterio converts to longitude/latitude and only downloads the blocks the box
    needs. "Nearest" copies the closest original number without blending, so a
    20 m scene-class pixel becomes four identical 10 m squares and no invented
    class numbers appear. Squares the file does not cover stay 0 (so a real 0 in
    the file and "no data" look the same: do not use 0 to decide that a square
    was not photographed).
    """
    width, height = grid_size(field, square_size_m)
    _check_band_and_margin(band, margin_squares, width, height)
    grid = np.zeros((height + 2 * margin_squares, width + 2 * margin_squares), dtype=dtype)
    field_transform = from_bounds(field.west, field.south, field.east, field.north, width, height)
    # Moving the grid's origin up and left by the margin keeps every square's size.
    transform = field_transform @ Affine.translation(-margin_squares, -margin_squares)
    problem: str | None = None
    try:
        with _quiet_reader_logs(href), rasterio.Env(**READ_SETTINGS), rasterio.open(href) as source:
            reproject(
                source=rasterio.band(source, band),
                destination=grid,
                dst_transform=transform,
                dst_crs="EPSG:4326",
                resampling=Resampling.nearest,
                src_nodata=0,
                dst_nodata=0,
            )
    except RasterioError as error:
        # The reading library repeats the whole address in its message, so the key is
        # blanked there too, not only in our own text.
        key = href.partition("?")[2]
        detail = str(error).replace(key, "(key hidden)") if key else str(error)
        problem = f"Could not read the satellite file {href.split('?')[0]}: {detail}"
    if problem is not None:
        # Raised here, outside the except block, so the original error (which holds the
        # full address) is not attached to ours as its cause.
        raise ValueError(problem)
    return grid


def _check_band_and_margin(band: int, margin_squares: int, width: int, height: int) -> None:
    """Refuse a band or margin that is not a whole number in range, or a grid that is too big."""
    for name, value, smallest in (("band", band, 1), ("margin_squares", margin_squares, 0)):
        if isinstance(value, bool) or not isinstance(value, int) or value < smallest:
            raise ValueError(f"{name} must be a whole number, {smallest} or more.")
    padded_width = width + 2 * margin_squares
    padded_height = height + 2 * margin_squares
    if padded_width > MAX_SQUARES_PER_SIDE or padded_height > MAX_SQUARES_PER_SIDE:
        raise ValueError(
            f"Picture with margin would be {padded_width} x {padded_height} squares; "
            f"limit is {MAX_SQUARES_PER_SIDE} per side."
        )


@contextmanager
def _quiet_reader_logs(href: str) -> Iterator[None]:
    """While reading an address with a key after the "?", stop the reading library logging.

    The library writes the whole address, key included, into its own log lines, and
    those would show on the screen. Our own error message already says what went wrong,
    so its log is silenced, but only for addresses that carry a key.
    """
    if "?" not in href:
        yield
        return
    logger = logging.getLogger("rasterio")
    previous_level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


def asset_href(item: pystac.Item, asset_key: str) -> str:
    """The web address of one picture file, or a plain error if the item lacks it."""
    asset: Any = item.assets.get(asset_key)
    if asset is None:
        raise ValueError(f"Picture {item.id} has no '{asset_key}' file.")
    return str(asset.href)


def read_item(item: pystac.Item, field: Field) -> tuple[np.ndarray, ...]:
    """Read one picture over the field: (red, nir, scl, photographed) grids.

    red and nir are reflectance (stored number x scale + offset). A stored 0 means
    the satellite had no data there, so those squares are not photographed.
    """
    red_stored = read_band(asset_href(item, RED_ASSET), field, np.uint16)
    nir_stored = read_band(asset_href(item, NIR_ASSET), field, np.uint16)
    scl = read_band(asset_href(item, SCL_ASSET), field, np.uint8)
    red_scale, red_offset = scale_and_offset(item, RED_ASSET)
    nir_scale, nir_offset = scale_and_offset(item, NIR_ASSET)
    red = red_stored.astype(np.float64) * red_scale + red_offset
    nir = nir_stored.astype(np.float64) * nir_scale + nir_offset
    photographed = (red_stored != 0) & (nir_stored != 0)
    return red, nir, scl, photographed


def read_visual(item: pystac.Item, field: Field, margin_squares: int = 0) -> np.ndarray:
    """Read one picture's true-colour image over the field as a (height, width, 3) uint8 array.

    The three layers are red, green, blue, in that order. margin_squares adds that many
    extra squares of surrounding land on every side (see read_band), so the shape is
    (height + 2 x margin, width + 2 x margin, 3). A 0 can be a real dark value or "no
    data": it says nothing about whether the satellite photographed the square (use
    Picture.data_mask for that). This is a picture to look at, not data for NDVI.
    """
    href = asset_href(item, VISUAL_ASSET)
    layers = [
        read_band(href, field, np.uint8, band=band, margin_squares=margin_squares)
        for band in (1, 2, 3)
    ]
    return np.stack(layers, axis=-1)


def picture_for_day(field: Field, items: list[pystac.Item]) -> Picture | None:
    """Build one day's Picture from that day's items (most recent first).

    Where two pictures overlap, the more recent one that has data wins, with one
    exception: if its square is not clear (cloud, shadow, unusable light values) and
    an older picture of the same day shows that square clear, the clear square is
    used. The pictures are taken minutes apart, so the ground is the same. If no
    picture is clear there, the most recent one stays. Returns None if the day has
    no items at all. A day whose pictures miss the field entirely gives a Picture
    with nothing photographed, which the report shows as "no picture".

    picture.pictures_combined says how many items gave at least one square. The
    picture also records, for every square, which item it came from
    (source_picture, an index into picture_ids) and whether an older item replaced
    an unclear reading (replaced_unclear), so a page can explain each square.
    """
    if len(items) == 0:
        return None
    width, height = grid_size(field)
    red = np.full((height, width), np.nan)
    nir = np.full((height, width), np.nan)
    scl = np.zeros((height, width), dtype=np.uint8)
    filled = np.zeros((height, width), dtype=bool)
    settled = np.zeros((height, width), dtype=bool)  # a clear square is already stored here
    source = np.full((height, width), -1, dtype=int)  # which item gave each square
    replaced = np.zeros((height, width), dtype=bool)  # an older item replaced an unclear one
    inside = np.array(inside_mask(field, width, height), dtype=bool)
    combined = 0
    for position, item in enumerate(items):
        if inside.any() and settled[inside].all():
            # Every square inside the outline already holds a clear reading; an older
            # picture adds nothing. (Squares outside the outline do not count: they are
            # never used, and a gap there must not force another download.)
            break
        item_red, item_nir, item_scl, item_photographed = read_item(item, field)
        item_clear = _clear_squares(item_red, item_nir, item_scl, item_photographed)
        take = (item_photographed & ~filled) | (item_clear & ~settled)
        # Only squares inside the outline count: a picture that fills nothing but
        # squares outside it adds nothing to the field.
        used = take & inside if inside.any() else take
        if used.any():
            combined += 1
        replaced[item_clear & ~settled & filled] = True  # before filled and settled change
        red[take] = item_red[take]
        nir[take] = item_nir[take]
        scl[take] = item_scl[take]
        source[take] = position
        filled |= item_photographed
        settled |= item_clear
    picture = picture_from_bands(red, nir, scl, filled)
    return replace(
        picture,
        pictures_combined=max(1, combined),
        picture_ids=tuple(item.id for item in items),
        source_picture=source.tolist(),
        replaced_unclear=replaced.tolist(),
    )


def _clear_squares(
    red: np.ndarray, nir: np.ndarray, scl: np.ndarray, photographed: np.ndarray
) -> np.ndarray:
    """True where a square was photographed, has usable light values and a clear scene class.

    This is the same test the report uses later (ndvi.is_clear_square, plus the
    check in picture_from_bands that red and near-infrared are real numbers >= 0, and
    that they add up to more than 0 so an NDVI exists).
    """
    with np.errstate(invalid="ignore"):  # comparing a missing value is fine here
        light_ok = np.isfinite(red) & np.isfinite(nir) & (red >= 0) & (nir >= 0)
        # red + nir must be above zero too: ndvi() has no answer when it is not.
        light_ok &= (red + nir) > 0
    class_ok = np.isin(scl, list(CLEAR_SCL_CLASSES))
    return photographed & light_ok & class_ok
