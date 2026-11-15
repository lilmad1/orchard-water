"""Save one day of satellite pictures as plain files for the website.

Run it like this:
    python -m orchardwater.site_export --field fields/example.geojson --day 2026-06-29 \
        --out site-data/2026-06-29 --credit "<the credit text>" --example-field

It writes picture files (PNG) and one data file (data.json) into the folder. The website
only displays them; it never talks to the satellite service. SHADOW MODE: the data is for
looking at. Greenness (NDVI) is not water stress, and nothing here shows water savings.

What is saved, for a day with one or more Sentinel-2 pictures of the field:
  photo-N.png         the true-colour photo of picture N (0 = newest) with surrounding land
  labels-N.png        the same area coloured by the scene class (cloud, soil, vegetation ...)
  assembled.png       the day's colour picture put together square by square
  labels-assembled.png the scene classes of the assembled picture
  data.json           per-square facts, the day's verdict, thresholds and a version stamp

The pictures hold no frame: the field's box inside the picture is given in data.json
(field_box) and the page draws it. Every file name is fixed and the data file lists only
chosen facts, never a catalogue item, an address, a folder name or an error message.

load_bundle() checks that a saved folder is complete and consistent. A website build
should call it (or repeat its checks), otherwise nothing protects the page from a
mismatched set of files.
"""

import argparse
import hashlib
import json
import math
import re
import struct
import warnings
import zlib
from datetime import UTC, date, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any, NoReturn

import numpy as np
import pystac
import rasterio
from rasterio.errors import NotGeoreferencedWarning

from orchardwater import earth_search, ndvi, ndvi_report, visual
from orchardwater.field import Field, inside_mask, load_field
from orchardwater.ndvi import Picture

# Bump this when the layout of data.json changes, so an old page refuses a new file.
SCHEMA_VERSION = 1

DATA_FILE = "data.json"

# Extra squares of surrounding land on every side of the field's box, so a visitor can
# see where the field sits. 40 squares is about 380 m; chosen by eye on 2026-10-09.
DEFAULT_MARGIN_SQUARES = 40

# Source files whose text is fingerprinted into the version stamp. The package version
# alone (0.0.1) cannot tell two states of the code apart.
FINGERPRINTED_FILES = (
    "earth_search.py",
    "field.py",
    "ndvi.py",
    "ndvi_report.py",
    "visual.py",
    "site_export.py",
)

# Scene classes (SCL) as named by the Sentinel-2 L2A product, with the colour each one
# gets in the labels pictures. Names follow the comment in ndvi.py (Copernicus docs).
# TODO(verify): cross-check the names with the ESA Sen2Cor documentation (same open
# item as CLEAR_SCL_CLASSES). The colours are our own choice.
LABEL_NAMES = {
    0: "no data",
    1: "defective",
    2: "dark area",
    3: "cloud shadow",
    4: "vegetation",
    5: "bare soil",
    6: "water",
    7: "low-probability cloud or unclassified",
    8: "cloud",
    9: "cloud",
    10: "thin cirrus",
    11: "snow",
}
LABEL_COLOURS = {
    0: (0, 0, 0),
    1: (255, 0, 255),
    2: (70, 70, 70),
    3: (110, 110, 110),
    4: (40, 150, 60),
    5: (200, 170, 110),
    6: (40, 90, 200),
    7: (190, 190, 190),
    8: (255, 255, 255),
    9: (255, 255, 255),
    10: (200, 220, 255),
    11: (150, 230, 255),
}
UNKNOWN_LABEL_COLOUR = (255, 0, 255)  # a class number we have no name for

# Item ids are used as text in data.json: only these characters are allowed.
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

# The names this export gives its pictures (see the top of the file).
GENERATED_PICTURE = re.compile(r"^(photo-\d+|labels-\d+|assembled|labels-assembled)\.png$")
MAX_CREDIT_LENGTH = 300
# A drive letter, a colon and a slash, like C:/Users (a file path on a Windows computer).
PATH_START = re.compile(r"(?<![A-Za-z])[A-Za-z]:[/\\]")
# A word that starts like a Unix path: /home/..., ./x, ../x or ~/x. A lone " / " is fine.
UNIX_PATH = re.compile(r"(^|\s)(\.{1,2}|~)?/\S")

# What a version stamp may hold (also used by landsat_export).
PACKAGE_VERSION_FORMAT = re.compile(r"[0-9A-Za-z.+-]{1,40}", re.ASCII)
FINGERPRINT_FORMAT = re.compile(r"[0-9a-f]{64}", re.ASCII)

NOTICES = (
    "Greenness (NDVI) is not water stress and must not be used for watering decisions.",
    "This shows what the satellite saw on one day. It does not show water savings.",
)
INVENTED_OUTLINE_NOTICE = "The field outline is invented, for demonstration."
REASON_SHOWN = "enough of the field is clear and valid"
REASON_SKIPPED = "too little of the field is clear and valid"
UNITS = {
    "time": "UTC, ISO 8601",
    "ndvi": "dimensionless, -1 to 1, 3 decimals",
    "shares": "0 to 1, exact (clear squares over a count of squares), not rounded",
    "square_size": "metres",
}

# The exact entries data.json holds, level by level. load_bundle refuses anything else, so
# nothing (an address, a folder name) can ride along in a published file unchecked.
TOP_KEYS = frozenset(
    {
        "schema_version",
        "day",
        "credit",
        "notices",
        "version",
        "units",
        "thresholds",
        "label_legend",
        "grid",
        "photos",
        "assembled",
        "verdict",
        "squares",
        "images",
    }
)
VERSION_KEYS = frozenset({"package_version", "source_fingerprint_sha256", "revision"})
SUMMARY_KEYS = frozenset(
    {"field_squares", "observed_squares", "clear_squares", "coverage", "clear_share", "valid_share"}
)
PHOTO_KEYS = SUMMARY_KEYS | {"position", "id", "taken_utc"}
ASSEMBLED_KEYS = SUMMARY_KEYS | {"pictures_combined"}
VERDICT_KEYS = frozenset({"shown", "valid_share", "limit", "reason"})
THRESHOLD_KEYS = frozenset({"clear_scene_classes", "min_valid_share"})
LEGEND_KEYS = frozenset({"class", "name", "rgb"})
GRID_KEYS = frozenset({"rows", "columns", "margin_squares", "field_box", "square_size_m"})
BOX_KEYS = frozenset({"row", "column", "rows", "columns"})
SQUARE_SIZE_KEYS = frozenset({"east_west", "north_south"})
IMAGE_KEYS = frozenset({"file", "width", "height", "sha256"})
ZERO_ONE_LISTS = ("inside_outline", "usable_data", "replaced_unclear", "clear")
SQUARE_LISTS = frozenset({*ZERO_ONE_LISTS, "source", "scene_class", "ndvi"})
# No grid has more squares than this, so a bigger count in a file is wrong.
MAX_COUNT = earth_search.MAX_SQUARES_PER_SIDE**2
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# rasterio's PNG writer (the GDAL PNG driver) writes exactly these chunks for our pictures:
# IHDR (size and pixel layout), one or more IDAT (the compressed pixels) and IEND. Checked on
# the real 29 June 2026 export: no PLTE (no palette), and no text, time, gamma, colour-space
# or pixel-size chunks. Any other chunk could carry hidden text, so load_bundle refuses it.
# The layout: 8 bits per channel, colour type 2 (RGB), standard compression and filter,
# no interlacing.
PNG_LAYOUT = (8, 2, 0, 0, 0)
# Characters a scene-class name never needs but an address or a file path does.
FORBIDDEN_IN_NAME = "?:/\\#"


def validate_credit(credit: str) -> str:
    """Return the credit text if it is plain text of reasonable length, else raise ValueError.

    The exact wording of the Sentinel credit is a launch check for the operator; nothing is
    invented here. This only refuses text that could carry an address or a line break.
    """
    text = credit.strip()
    if text == "" or len(text) > MAX_CREDIT_LENGTH:
        raise ValueError(f"The credit text must be 1 to {MAX_CREDIT_LENGTH} characters.")
    if any(ord(character) < 32 for character in text):
        raise ValueError("The credit text must not contain line breaks or control characters.")
    if not text.isprintable():
        raise ValueError(
            "The credit text must not contain invisible or direction-changing characters."
        )
    lowered = text.lower()
    if "http" in lowered or "://" in text or "?" in text or "#" in text or "//" in text:
        raise ValueError("The credit text must be plain text, with no web address.")
    if "\\" in text or PATH_START.search(text) or UNIX_PATH.search(text) or text.count("/") >= 2:
        raise ValueError("The credit text must be plain text, with no file path.")
    return text


def source_fingerprint() -> str:
    """SHA-256 of the text of the source files that decide what the export contains."""
    folder = Path(__file__).parent
    digest = hashlib.sha256()
    for name in sorted(FINGERPRINTED_FILES):
        digest.update(name.encode("utf-8"))
        digest.update((folder / name).read_bytes())
    return digest.hexdigest()


def package_version() -> str:
    """The installed package version, or "unknown" if the package is not installed."""
    try:
        return metadata.version("orchardwater")
    except metadata.PackageNotFoundError:
        return "unknown"


def current_thresholds() -> dict[str, Any]:
    """The limits in the code right now that decide which squares count and the verdict."""
    return {
        "clear_scene_classes": sorted(ndvi.CLEAR_SCL_CLASSES),
        "min_valid_share": ndvi_report.MIN_VALID_SHARE,
    }


def current_legend() -> list[dict[str, Any]]:
    """The scene-class legend (number, name, colour) in the code right now."""
    return [
        {"class": number, "name": LABEL_NAMES[number], "rgb": list(LABEL_COLOURS[number])}
        for number in sorted(LABEL_NAMES)
    ]


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed in JSON")


def _refuse_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a dict from JSON pairs, refusing a key that appears twice.

    Without this, json.loads keeps only the last copy and the first would stay in the
    published file unseen by the checks.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("a key is repeated")
        result[key] = value
    return result


def loads_strict(text: str) -> Any:
    """json.loads that refuses a key written twice and NaN or Infinity. Raises ValueError."""
    return json.loads(
        text, parse_constant=_refuse_constant, object_pairs_hook=_refuse_repeated_keys
    )


def label_picture(scl: np.ndarray) -> np.ndarray:
    """Colour a (height, width) grid of scene classes as a (height, width, 3) uint8 picture."""
    table = np.zeros((256, 3), dtype=np.uint8)
    table[:] = UNKNOWN_LABEL_COLOUR
    for number, colour in LABEL_COLOURS.items():
        table[number] = colour
    return table[scl.astype(np.uint8)]


def read_item_images(
    item: pystac.Item, field: Field, margin_squares: int
) -> tuple[np.ndarray, np.ndarray]:
    """Read one picture's true-colour photo and its coloured scene classes, both with the margin."""
    photo = earth_search.read_visual(item, field, margin_squares)
    scl = earth_search.read_band(
        earth_search.asset_href(item, earth_search.SCL_ASSET),
        field,
        np.uint8,
        margin_squares=margin_squares,
    )
    return photo, label_picture(scl)


def summary_numbers(summary: ndvi.FieldSummary) -> dict[str, Any]:
    """The counts and shares of one FieldSummary, as plain numbers.

    valid_share is clear squares over ALL squares inside the outline (the number the report
    compares with its limit). clear_share is clear squares over the squares the satellite
    photographed. The shares are NOT rounded: a share just under the limit must not be able
    to read as exactly the limit. A page that shows a rounded number must round down.
    """
    return {
        "field_squares": summary.field_squares,
        "observed_squares": summary.observed_squares,
        "clear_squares": summary.clear_squares,
        "coverage": summary.coverage,
        "clear_share": summary.clear_share,
        "valid_share": ndvi_report.valid_share(summary),
    }


def verdict_numbers(summary: ndvi.FieldSummary) -> dict[str, Any]:
    """The day's verdict, by the same rule as the report: shown only if valid share >= limit."""
    valid = ndvi_report.valid_share(summary)
    shown = valid >= ndvi_report.MIN_VALID_SHARE and summary.mean_ndvi is not None
    return {
        "shown": shown,
        "valid_share": valid,
        "limit": ndvi_report.MIN_VALID_SHARE,
        "reason": REASON_SHOWN if shown else REASON_SKIPPED,
    }


def utc_time(item: pystac.Item) -> datetime:
    """The moment a picture was taken, in UTC (the catalogue always gives one here)."""
    if item.datetime is None:
        raise ValueError(f"Picture {item.id} has no time.")
    return item.datetime.astimezone(UTC)


def square_grids(picture: Picture, inside: list[list[bool]]) -> dict[str, Any]:
    """Per-square facts for the field's box, as lists of rows (row 0 = north, column 0 = west).

    Needs a Picture from earth_search.picture_for_day. ndvi is dimensionless greenness
    (-1 to 1) rounded to 3 decimals, or None where the square is not clear or lies
    outside the outline. It is greenness only, not water stress.
    """
    if picture.source_picture is None or picture.replaced_unclear is None:
        raise ValueError("This Picture does not record where each square came from.")
    values = ndvi.ndvi_grid(picture)
    clear: list[list[int]] = []
    shown_ndvi: list[list[float | None]] = []
    for row_index, row in enumerate(values):
        clear_row = []
        ndvi_row: list[float | None] = []
        for col_index, value in enumerate(row):
            is_clear = inside[row_index][col_index] and value is not None
            clear_row.append(1 if is_clear else 0)
            ndvi_row.append(round(value, 3) if is_clear and value is not None else None)
        clear.append(clear_row)
        shown_ndvi.append(ndvi_row)
    return {
        "inside_outline": [[1 if flag else 0 for flag in row] for row in inside],
        "usable_data": picture.data_mask,
        "source": picture.source_picture,
        "replaced_unclear": [
            [1 if flag else 0 for flag in row] for row in picture.replaced_unclear
        ],
        "scene_class": picture.scl,
        "clear": clear,
        "ndvi": shown_ndvi,
    }


def build_data(
    field: Field,
    ordered_items: list[pystac.Item],
    picture: Picture,
    day: date,
    credit: str,
    margin_squares: int,
    example_field: bool,
) -> dict[str, Any]:
    """The contents of data.json, except the list of picture files (added when they are written)."""
    height = len(picture.red)
    width = len(picture.red[0])
    inside = inside_mask(field, width, height)
    summary = ndvi.summarize_field(picture, inside)
    photos = []
    for position, item in enumerate(ordered_items):
        photo_summary = ndvi.summarize_field(
            ndvi.picture_from_bands(*earth_search.read_item(item, field)), inside
        )
        photos.append(
            {
                "position": position,
                "id": item.id,
                "taken_utc": utc_time(item).isoformat(),
                **summary_numbers(photo_summary),
            }
        )
    notices = list(NOTICES)
    if example_field:
        notices.append(INVENTED_OUTLINE_NOTICE)
    return {
        "schema_version": SCHEMA_VERSION,
        "day": day.isoformat(),
        "credit": credit,
        "notices": notices,
        "version": {
            "package_version": package_version(),
            "source_fingerprint_sha256": source_fingerprint(),
            "revision": "unknown",
        },
        "units": dict(UNITS),
        "thresholds": current_thresholds(),
        "label_legend": current_legend(),
        "grid": {
            "rows": height,
            "columns": width,
            "margin_squares": margin_squares,
            "field_box": {
                "row": margin_squares,
                "column": margin_squares,
                "rows": height,
                "columns": width,
            },
            "square_size_m": {
                "east_west": round(field.width_m / width, 2),
                "north_south": round(field.height_m / height, 2),
            },
        },
        "photos": photos,
        "assembled": {**summary_numbers(summary), "pictures_combined": picture.pictures_combined},
        "verdict": verdict_numbers(summary),
        "squares": square_grids(picture, inside),
    }


def write_png(path: Path, picture: np.ndarray) -> None:
    """Write a (height, width, 3) uint8 array as a PNG with nothing but the pixels."""
    height, width, _ = picture.shape
    with warnings.catch_warnings():
        # A PNG has no map position; rasterio says so every time. That is expected here.
        warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
        with rasterio.open(
            path, "w", driver="PNG", height=height, width=width, count=3, dtype="uint8"
        ) as target:
            for band in range(3):
                target.write(picture[:, :, band], band + 1)


def file_sha256(path: Path) -> str:
    """SHA-256 of a finished file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_bundle(out_dir: Path, images: dict[str, np.ndarray], data: dict[str, Any]) -> None:
    """Write the pictures, then data.json (last), into out_dir.

    Any old data.json is removed first, so a half-written folder can never pair an old data
    file with new pictures. The checksums go into data.json after the pictures are closed.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / DATA_FILE).unlink(missing_ok=True)
    keep = {f"{name}.png" for name in images}
    for old in out_dir.glob("*.png"):
        if old.name not in keep and GENERATED_PICTURE.match(old.name):
            old.unlink()  # a picture from an earlier export with more photos
    listed: dict[str, dict[str, Any]] = {}
    for name, picture in sorted(images.items()):
        path = out_dir / f"{name}.png"
        write_png(path, picture)
        listed[name] = {
            "file": path.name,
            "width": int(picture.shape[1]),
            "height": int(picture.shape[0]),
            "sha256": file_sha256(path),
        }
    full = {**data, "images": listed}
    text = json.dumps(full, sort_keys=True, separators=(",", ":")) + "\n"
    (out_dir / DATA_FILE).write_text(text, encoding="utf-8")


def build_bundle(
    field: Field,
    items: list[pystac.Item],
    day: date,
    credit: str,
    margin_squares: int = DEFAULT_MARGIN_SQUARES,
    example_field: bool = False,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Read everything for one day and return (pictures by name, data without the file list)."""
    credit = validate_credit(credit)
    picture = earth_search.picture_for_day(field, items)
    if picture is None:
        raise ValueError("No satellite picture covers the field on that day.")
    by_id = {item.id: item for item in items}
    ordered = [by_id[picture_id] for picture_id in picture.picture_ids]
    for item in ordered:
        if not SAFE_ID.match(item.id):
            raise ValueError("A picture id has characters that are not allowed in the export.")
        if utc_time(item).date() != day:
            raise ValueError(f"Picture {item.id} was not taken on {day.isoformat()} (UTC).")
    photos: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    for item in ordered:
        photo, label = read_item_images(item, field, margin_squares)
        photos.append(photo)
        labels.append(label)
    images: dict[str, np.ndarray] = {
        "assembled": visual.assemble_visual(photos, picture, margin_squares),
        "labels-assembled": visual.assemble_visual(labels, picture, margin_squares),
    }
    for position in range(len(ordered)):
        images[f"photo-{position}"] = photos[position]
        images[f"labels-{position}"] = labels[position]
    data = build_data(field, ordered, picture, day, credit, margin_squares, example_field)
    return images, data


def _fail(message: str) -> NoReturn:
    raise ValueError(f"The saved files are not usable: {message}")


def load_bundle(folder: Path, require_current_code: bool = True) -> dict[str, Any]:
    """Read data.json from a folder and check that the whole set is complete and consistent.

    data.json must hold exactly the entries build_data writes, each of the right kind: whole
    numbers are real integers (not true/false, not 1.0), decimals are finite, counts and
    shares agree with each other, texts are the fixed texts or plain text with no address or
    file path, and NDVI is empty or a number from -1 to 1 on exactly the clear squares. A key
    written twice and NaN or Infinity are refused. The folder must hold exactly the listed
    pictures plus data.json, every picture with the listed SHA-256 and size, and the field
    box must sit where the margin says. Returns the data. Raises ValueError with a plain
    message at the first problem.

    require_current_code (default True) also refuses a bundle whose source fingerprint, limits
    or legend differ from the code running now: the rules that made its numbers may have
    changed since. Pass False only to read an old bundle for looking at, never to publish it.
    """
    try:
        data = loads_strict((folder / DATA_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        _fail(f"{DATA_FILE} is missing, is not valid JSON, or repeats a key.")
    if not isinstance(data, dict):
        _fail(f"{DATA_FILE} is not a JSON object.")
    version = data.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        _fail(f"schema version is {version}, expected {SCHEMA_VERSION}.")
    try:
        _check_layout(data, require_current_code)
        _check_squares(data)
        _check_square_values(data)
        _check_images(folder, data)
        _check_code_version(data, require_current_code)
    except (KeyError, TypeError, IndexError):
        _fail(f"{DATA_FILE} is missing a required entry or has one of the wrong kind.")
    return data


def _check_layout(data: dict[str, Any], require_current_code: bool) -> None:
    """Every entry outside the per-square lists and the picture list."""
    day = _check_header(data)
    _check_thresholds(data["thresholds"], require_current_code)
    _check_legend(data["label_legend"], require_current_code)
    _check_grid_shape(data["grid"])
    _check_photos(data["photos"], day)
    _check_assembled_and_verdict(data)


def _keys(value: Any, keys: frozenset[str], what: str) -> None:
    """Stop unless value is a dict with exactly these keys."""
    if not isinstance(value, dict) or set(value) != keys:
        _fail(f"{what} has missing or extra entries.")


def _is_count(value: Any) -> bool:
    """A whole number of squares: an int (not a bool, not a float), 0 up to MAX_COUNT."""
    return type(value) is int and 0 <= value <= MAX_COUNT


def _is_real(value: Any) -> bool:
    """A real, finite number (an int or a float, not a bool). 1e999 reads as infinity: refused."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # a whole number too big for a float
        return False


def _is_byte(value: Any) -> bool:
    """A whole number from 0 to 255 (a scene class or one colour channel)."""
    return type(value) is int and 0 <= value <= 255


def _is_plain_name(value: Any) -> bool:
    """Short printable text with nothing that looks like an address or a file path."""
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or not value.isprintable():
        return False
    return not any(c in value for c in FORBIDDEN_IN_NAME) and "http" not in value.lower()


def _check_header(data: dict[str, Any]) -> date:
    """Top-level entries, day, credit, notices, units and version stamp. Returns the day."""
    _keys(data, TOP_KEYS, f"the top level of {DATA_FILE}")
    day = data["day"]
    try:
        parsed = date.fromisoformat(day) if isinstance(day, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != day:
        _fail("the day is not a date written like 2026-06-29.")
    try:
        credit_ok = (
            isinstance(data["credit"], str) and validate_credit(data["credit"]) == data["credit"]
        )
    except ValueError:
        credit_ok = False
    if not credit_ok:
        _fail("the credit text is not plain text.")
    if data["notices"] not in (list(NOTICES), [*NOTICES, INVENTED_OUTLINE_NOTICE]):
        _fail("the notices are not the ones this export writes.")
    if data["units"] != UNITS:
        _fail("the units are not the ones this export writes.")
    _check_version_stamp(data["version"])
    return parsed


def _check_version_stamp(version: Any) -> None:
    """Exactly a package version, a 64-character fingerprint and the revision "unknown"."""
    _keys(version, VERSION_KEYS, "the version stamp")
    package = version["package_version"]
    fingerprint = version["source_fingerprint_sha256"]
    if (
        not isinstance(package, str)
        or PACKAGE_VERSION_FORMAT.fullmatch(package) is None
        or not isinstance(fingerprint, str)
        or FINGERPRINT_FORMAT.fullmatch(fingerprint) is None
        or version["revision"] != "unknown"
    ):
        _fail("the version stamp holds something other than a version, a fingerprint, 'unknown'.")


def _check_thresholds(thresholds: Any, require_current_code: bool) -> None:
    """Clear scene classes (sorted, no repeats) and a share limit from 0 to 1."""
    _keys(thresholds, THRESHOLD_KEYS, "thresholds")
    classes = thresholds["clear_scene_classes"]
    limit = thresholds["min_valid_share"]
    if not isinstance(classes, list) or not all(_is_byte(number) for number in classes):
        _fail("the clear scene classes are not whole numbers from 0 to 255.")
    if classes != sorted(set(classes)) or not _is_real(limit) or not 0 <= limit <= 1:
        _fail("the thresholds are not sorted classes and a share from 0 to 1.")
    if require_current_code and thresholds != current_thresholds():
        _fail("it was made with other limits than the code running now.")


def _check_legend(legend: Any, require_current_code: bool) -> None:
    """Each legend entry: a class number, a plain name and three colour numbers."""
    if not isinstance(legend, list) or not legend:
        _fail("the scene-class legend is not a list.")
    for entry in legend:
        _keys(entry, LEGEND_KEYS, "a scene-class legend entry")
        rgb = entry["rgb"]
        if (
            not _is_byte(entry["class"])
            or not _is_plain_name(entry["name"])
            or not isinstance(rgb, list)
            or len(rgb) != 3
            or not all(_is_byte(channel) for channel in rgb)
        ):
            _fail("a scene-class legend entry is not a number, a plain name and a colour.")
    if require_current_code and legend != current_legend():
        _fail("the scene-class legend is not the one in the code running now.")


def _check_grid_shape(grid: Any) -> None:
    """Grid and field box sizes are whole numbers in range; square sizes are positive."""
    _keys(grid, GRID_KEYS, "the grid")
    box = grid["field_box"]
    sizes = grid["square_size_m"]
    _keys(box, BOX_KEYS, "the field box")
    _keys(sizes, SQUARE_SIZE_KEYS, "the grid square size")
    largest = earth_search.MAX_SQUARES_PER_SIDE
    sides = (grid["rows"], grid["columns"], box["rows"], box["columns"])
    offsets = (grid["margin_squares"], box["row"], box["column"])
    if not all(type(n) is int and 1 <= n <= largest for n in sides):
        _fail("the grid sizes are not whole numbers in range.")
    if not all(type(n) is int and 0 <= n <= largest for n in offsets):
        _fail("the grid margin or field box position is not a whole number in range.")
    if (box["rows"], box["columns"]) != (grid["rows"], grid["columns"]):
        _fail("the grid and the field box have different sizes.")
    if not all(_is_real(size) and size > 0 for size in sizes.values()):
        _fail("the grid square size is not a positive number of metres.")


def _check_summary(entry: dict[str, Any], what: str) -> None:
    """Counts are whole and in order; the three shares are exactly what the counts give."""
    field, observed, clear = (
        entry[name] for name in ("field_squares", "observed_squares", "clear_squares")
    )
    if not all(_is_count(n) for n in (field, observed, clear)) or not clear <= observed <= field:
        _fail(f"{what}: the square counts are not whole numbers in order.")
    # The same divisions as ndvi.summarize_field and ndvi_report.valid_share. Exact equality
    # on purpose: JSON keeps a float exactly, so a share even slightly off is a wrong file.
    wanted = {
        "coverage": observed / field if field > 0 else 0.0,
        "clear_share": clear / observed if observed > 0 else 0.0,
        "valid_share": clear / field if field > 0 else 0.0,
    }
    for name, value in wanted.items():
        if not _is_real(entry[name]) or entry[name] != value:
            _fail(f"{what}: {name} does not match the counts.")


def _check_photos(photos: Any, day: date) -> None:
    """Each photo: its position, a safe id, a UTC time on the day, and its counts."""
    if not isinstance(photos, list) or not photos:
        _fail("there are no photos.")
    for position, photo in enumerate(photos):
        what = f"photo {position}"
        _keys(photo, PHOTO_KEYS, what)
        if type(photo["position"]) is not int or photo["position"] != position:
            _fail(f"{what}: the position is not its place in the list.")
        if not isinstance(photo["id"], str) or SAFE_ID.fullmatch(photo["id"]) is None:
            _fail("a photo id has characters that are not allowed.")
        if _utc_day(photo["taken_utc"]) != day:
            _fail(f"{what}: the time is not a UTC time on {day.isoformat()}.")
        _check_summary(photo, what)


def _utc_day(text: Any) -> date | None:
    """The day of a time written exactly as utc_time().isoformat() writes it, else None."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.isoformat() != text or moment.utcoffset() != timedelta(0):
        return None
    return moment.date()


def _check_assembled_and_verdict(data: dict[str, Any]) -> None:
    """The assembled counts, and a verdict that follows from them by the report's rule."""
    assembled = data["assembled"]
    _keys(assembled, ASSEMBLED_KEYS, "assembled")
    _check_summary(assembled, "assembled")
    combined = assembled["pictures_combined"]
    if type(combined) is not int or not 1 <= combined <= len(data["photos"]):
        _fail("the number of pictures combined is not between 1 and the number of photos.")
    verdict = data["verdict"]
    _keys(verdict, VERDICT_KEYS, "the verdict")
    limit = data["thresholds"]["min_valid_share"]
    # verdict_numbers: shown if valid share >= limit and there is a mean (a clear square).
    shown = assembled["valid_share"] >= limit and assembled["clear_squares"] > 0
    # Entry by entry with type checks, not one dict comparison: Python says True == 1.0,
    # so a dict comparison would accept true as a share of 1.
    if (
        type(verdict["shown"]) is not bool
        or verdict["shown"] != shown
        or not _is_real(verdict["valid_share"])
        or verdict["valid_share"] != assembled["valid_share"]
        or not _is_real(verdict["limit"])
        or verdict["limit"] != limit
        or verdict["reason"] != (REASON_SHOWN if shown else REASON_SKIPPED)
    ):
        _fail("the verdict does not follow from the numbers.")


def _check_images(folder: Path, data: dict[str, Any]) -> None:
    """Every listed picture exists, has the listed checksum and the grid's size."""
    grid = data["grid"]
    margin = grid["margin_squares"]
    expected_size = (grid["columns"] + 2 * margin, grid["rows"] + 2 * margin)
    photo_count = len(data["photos"])
    wanted = {"assembled", "labels-assembled"}
    wanted |= {f"{kind}-{n}" for n in range(photo_count) for kind in ("photo", "labels")}
    if not isinstance(data["images"], dict) or set(data["images"]) != wanted:
        _fail("the list of pictures does not match the list of photos.")
    present = {path.name for path in folder.iterdir()}
    extra = present - {DATA_FILE} - {f"{name}.png" for name in wanted}
    if extra:
        _fail("the folder holds files that are not part of the export: " + ", ".join(sorted(extra)))
    for name, entry in data["images"].items():
        _check_image_entry(folder, name, entry, expected_size)


def _check_image_entry(folder: Path, name: str, entry: Any, expected_size: tuple[int, int]) -> None:
    """One picture: exact entries, the file exists, checksum and size match."""
    _keys(entry, IMAGE_KEYS, f"picture {name}")
    if type(entry["width"]) is not int or type(entry["height"]) is not int:
        _fail(f"picture {name}: the size is not whole numbers.")
    if (
        not isinstance(entry["sha256"], str)
        or FINGERPRINT_FORMAT.fullmatch(entry["sha256"]) is None
    ):
        _fail(f"picture {name}: the checksum is not a SHA-256.")
    path = folder / entry["file"]
    if entry["file"] != f"{name}.png" or not path.is_file():
        _fail(f"picture {name} is missing.")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        _fail(f"picture {name} has changed since it was saved (checksum differs).")
    _check_png(name, raw, expected_size)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)
        with rasterio.open(path) as source:
            size = (source.width, source.height)
    if size != expected_size or (entry["width"], entry["height"]) != expected_size:
        _fail(f"picture {name} has size {size}, expected {expected_size}.")


def png_chunks(raw: bytes) -> list[tuple[bytes, bytes]]:
    """Split PNG bytes into (chunk type, chunk data), up to and including IEND.

    Checks the signature, that no chunk is cut short, every chunk's CRC (a checksum of its
    type and data) and that nothing follows IEND. Raises ValueError with the reason.
    """
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG (the first 8 bytes are wrong)")
    chunks: list[tuple[bytes, bytes]] = []
    position = len(PNG_SIGNATURE)
    while position < len(raw):
        # Each chunk: 4 bytes length, 4 bytes type, the data, 4 bytes CRC.
        if position + 12 > len(raw):
            raise ValueError("the file is cut short")
        (length,) = struct.unpack(">I", raw[position : position + 4])
        kind = raw[position + 4 : position + 8]
        end = position + 8 + length
        if end + 4 > len(raw):
            raise ValueError("the file is cut short")
        data = raw[position + 8 : end]
        (crc,) = struct.unpack(">I", raw[end : end + 4])
        if zlib.crc32(kind + data) != crc:
            raise ValueError("a chunk checksum (CRC) is wrong")
        chunks.append((kind, data))
        position = end + 4
        if kind == b"IEND":
            break
    if position != len(raw):
        raise ValueError("there are bytes after the end (IEND)")
    return chunks


def _check_png(name: str, raw: bytes, expected_size: tuple[int, int]) -> None:
    """The picture holds only IHDR, IDAT... and IEND, in that order, at the expected size."""
    try:
        chunks = png_chunks(raw)
    except ValueError as error:
        _fail(f"picture {name} is not a plain PNG: {error}.")
    kinds = [kind for kind, _ in chunks]
    for kind in kinds:
        if kind not in (b"IHDR", b"IDAT", b"IEND"):
            text = kind.decode("ascii", "replace")
            _fail(f"picture {name} is not a plain PNG: it holds a {text} chunk.")
    if len(kinds) < 3 or kinds[0] != b"IHDR" or kinds[-1] != b"IEND":
        _fail(f"picture {name} is not a plain PNG: the chunks are not IHDR, IDAT..., IEND.")
    if any(kind != b"IDAT" for kind in kinds[1:-1]):
        _fail(f"picture {name} is not a plain PNG: the chunks are not IHDR, IDAT..., IEND.")
    header = chunks[0][1]
    if len(header) != 13:
        _fail(f"picture {name} is not a plain PNG: the IHDR chunk has the wrong length.")
    width, height, *layout = struct.unpack(">IIBBBBB", header)
    if (width, height) != expected_size or tuple(layout) != PNG_LAYOUT:
        _fail(f"picture {name} is not a plain PNG: its size or pixel layout is not the export's.")


def _check_squares(data: dict[str, Any]) -> None:
    """The per-square lists are lists of rows with the grid's size; the box fits the pictures."""
    grid = data["grid"]
    box = grid["field_box"]
    margin = grid["margin_squares"]
    if (box["row"], box["column"]) != (margin, margin):
        _fail("the field box does not start where the margin says.")
    if (
        box["row"] + box["rows"] > grid["rows"] + 2 * margin
        or box["column"] + box["columns"] > grid["columns"] + 2 * margin
    ):
        _fail("the field box does not fit inside the pictures.")
    _keys(data["squares"], SQUARE_LISTS, "squares")
    for name, lists in data["squares"].items():
        if (
            not isinstance(lists, list)
            or len(lists) != box["rows"]
            or any(not isinstance(row, list) or len(row) != box["columns"] for row in lists)
        ):
            _fail(f"the per-square list {name} has the wrong size.")


def _cells(squares: dict[str, Any], name: str) -> list[Any]:
    """All values of one per-square list, row after row."""
    return [value for row in squares[name] for value in row]


def _check_square_values(data: dict[str, Any]) -> None:
    """Every per-square value is of the right kind, and the lists agree with each other
    and with the stored counts."""
    squares = data["squares"]
    for name in ZERO_ONE_LISTS:
        if any(type(value) is not int or value not in (0, 1) for value in _cells(squares, name)):
            _fail(f"the per-square list {name} holds something other than only 0 and 1.")
    photo_count = len(data["photos"])
    for number in _cells(squares, "source"):
        if type(number) is not int or not -1 <= number < photo_count:
            _fail("a source number is not a whole number naming a photo (or -1).")
    if not all(_is_byte(number) for number in _cells(squares, "scene_class")):
        _fail("a scene class is not a whole number from 0 to 255.")
    _check_clear_squares(squares, set(data["thresholds"]["clear_scene_classes"]))
    _check_square_counts(data)


def _check_clear_squares(squares: dict[str, Any], clear_classes: set[int]) -> None:
    """A clear square lies inside the outline and has usable data, a source photo, a clear
    scene class and an NDVI value (ndvi.is_clear_square plus ndvi.ndvi). Any other square
    has no NDVI value."""
    names = ("clear", "inside_outline", "usable_data", "source", "scene_class", "ndvi")
    columns = zip(*(_cells(squares, name) for name in names), strict=True)
    for is_clear, is_inside, usable, source, scene_class, value in columns:
        if is_clear and not is_inside:
            _fail("a square outside the outline is marked clear.")
        if is_clear and (usable != 1 or source < 0 or scene_class not in clear_classes):
            _fail("a clear square has no usable data, no source photo or no clear scene class.")
        if is_clear and not (_is_real(value) and -1 <= value <= 1):
            _fail("an NDVI value is not a number from -1 to 1 on a clear square.")
        if not is_clear and value is not None:
            _fail("an NDVI value is given for a square that is not clear.")


def _check_square_counts(data: dict[str, Any]) -> None:
    """The stored counts are the counts of the per-square lists.

    Assembled: field, observed and clear squares are counted again from the lists and must
    be equal. Each photo covers the same outline, so its field_squares must be the outline's
    count (which also keeps it within the grid). The lists do not hold each photo's own
    squares, but every square the assembled picture took from photo k was observed (and, if
    clear, clear) in photo k too, so photo k's counts can be no smaller than that.
    """
    squares = data["squares"]
    inside, usable = _cells(squares, "inside_outline"), _cells(squares, "usable_data")
    clear, source = _cells(squares, "clear"), _cells(squares, "source")
    observed = [1 if i == 1 and u == 1 else 0 for i, u in zip(inside, usable, strict=True)]
    outline = sum(inside)
    assembled = data["assembled"]
    counted = (outline, sum(observed), sum(clear))
    stored = (assembled["field_squares"], assembled["observed_squares"], assembled["clear_squares"])
    if counted != stored:
        _fail("the per-square lists do not add up to the assembled counts.")
    for position, photo in enumerate(data["photos"]):
        if photo["field_squares"] != outline:
            _fail(f"photo {position}: field_squares is not the outline's {outline} squares.")
        took = [k == position for k in source]
        took_clear = sum(1 for t, c in zip(took, clear, strict=True) if t and c)
        took_observed = sum(1 for t, o in zip(took, observed, strict=True) if t and o)
        if photo["clear_squares"] < took_clear or photo["observed_squares"] < took_observed:
            _fail(
                f"photo {position} has fewer clear squares or fewer observed squares than the "
                "assembled picture took from it."
            )


def _check_code_version(data: dict[str, Any], require_current_code: bool) -> None:
    """Refuse a bundle made by other code than the code running now, if that is required."""
    if (
        require_current_code
        and data["version"]["source_fingerprint_sha256"] != source_fingerprint()
    ):
        _fail("it was made by other code than the code running now (the fingerprint differs).")


def parse_day(text: str) -> date:
    """argparse helper: read YYYY-MM-DD, with a plain message if it is wrong."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date like 2026-06-29") from None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Save one satellite day as files for the website.")
    parser.add_argument("--field", type=Path, required=True, help="GeoJSON file with the outline")
    parser.add_argument("--day", type=parse_day, required=True, help="the day, YYYY-MM-DD (UTC)")
    parser.add_argument("--out", type=Path, required=True, help="folder to write the files into")
    parser.add_argument("--credit", required=True, help="the credit text shown with the pictures")
    parser.add_argument(
        "--margin", type=int, default=DEFAULT_MARGIN_SQUARES, help="squares of land around"
    )
    parser.add_argument(
        "--example-field",
        action="store_true",
        help="mark the outline as invented (only for the example field)",
    )
    args = parser.parse_args(argv)
    try:
        field = load_field(args.field)
        items = earth_search.search_items(field, args.day, args.day).get(args.day, [])
        images, data = build_bundle(
            field, items, args.day, args.credit, args.margin, args.example_field
        )
        write_bundle(args.out, images, data)
        load_bundle(args.out)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    verdict = data["verdict"]
    print(f"Wrote {len(images)} pictures and {DATA_FILE} to {args.out}")
    print(f"Verdict: {'shown' if verdict['shown'] else 'skipped'}, valid {verdict['valid_share']}")


if __name__ == "__main__":
    main()
