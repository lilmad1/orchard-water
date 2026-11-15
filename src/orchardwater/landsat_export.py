"""Save the Landsat passes over one field as one data file for the website.

Run it like this:
    python -m orchardwater.landsat_export --field fields/example.geojson --from 2025-01-01 \
        --to 2026-09-30 --out site-data/landsat --credit "<the credit text>" --example-field

It writes landsat-passes.json into the folder: one entry per day that a Landsat 8 or 9
satellite measured the field, with the numbers the thermal report would print and, for a
pass that was skipped, the reason. The website uses it for the "why we skip a pass"
timeline. SHADOW MODE: surface temperature is not water stress, and nothing here shows
water savings.

Use a folder of its own. The Sentinel-2 export (site_export.py) refuses a folder that holds
files it did not write.

What counts as a pass is decided by thermal_report (has_measurement) and why a pass is
skipped by thermal_report.skip_decision, the same code the printed report uses, so the two
cannot disagree. A "pass" here is one UTC day: scenes of the same day are merged by
landsat.picture_for_day, exactly like the report.

The file holds only chosen facts, never a catalogue item, an address, a folder name or an
error message. The export reads the whole range first and writes only if every day worked
and the finished data passed the same checks as load_passes(), so a failure never leaves
a half-finished timeline behind.
"""

import argparse
import hashlib
import json
import math
import os
import re
from collections.abc import Callable
from datetime import UTC, date, timedelta
from pathlib import Path
from typing import Any, NoReturn

import pystac

from orchardwater import earth_search, landsat, ndvi_report, thermal, thermal_report
from orchardwater.field import Field, area_m2, load_field
from orchardwater.site_export import (
    FINGERPRINT_FORMAT,
    PACKAGE_VERSION_FORMAT,
    loads_strict,
    package_version,
    parse_day,
    validate_credit,
)

# Bump this when the layout of landsat-passes.json changes, so an old page refuses a new file.
SCHEMA_VERSION = 1
KIND = "landsat-passes"
DATA_FILE = "landsat-passes.json"

# A long range is read in pieces of this many days, the same size as the printed report's
# limit per run (ndvi_report.MAX_DAYS). It is a chosen size, not a catalogue limit.
CHUNK_DAYS = ndvi_report.MAX_DAYS
# A bigger range would mean hundreds of satellite reads in one run.
MAX_RANGE_DAYS = 1100

# Source files whose text is fingerprinted into the version stamp (the Landsat chain; the
# Sentinel-2 export has its own list).
FINGERPRINTED_FILES = (
    "earth_search.py",
    "field.py",
    "landsat.py",
    "landsat_export.py",
    "ndvi_report.py",
    "thermal.py",
    "thermal_report.py",
)

SATELLITE_NAMES = ("Landsat 8", "Landsat 9")

NOTICES = (
    "Surface temperature is not water stress and it is not the air temperature.",
    "This shows what the satellite measured. It does not show water savings.",
    "The heat sensor really sees squares of about 100 m. Only the numbers for the whole "
    "field mean something.",
)
INVENTED_OUTLINE_NOTICE = "The field outline is invented, for demonstration only."

DEFINITIONS = {
    "coverage": "Squares of the field that the satellite measured, divided by all squares "
    "inside the outline.",
    "clear_share": "Clear squares divided by measured squares (0 if none were measured).",
    "valid_share": "Clear squares divided by all squares inside the outline.",
    "cloud_squares": "Squares of the field and the area around it that are cloud, thin "
    "cirrus or cloud shadow.",
    "unknown_squares": "Squares of the field and the area around it with no usable quality "
    "number, where cloud cannot be ruled out.",
    "discarded_squares": "Squares of the field and the area around it whose temperature was "
    "outside the believable range and was thrown out.",
    "discarded_field_squares": "The same, counted only inside the field outline.",
    "printed_numbers": "The _percent and _c_text entries are the numbers exactly as the "
    "printed report writes them (whole percent, Celsius with one decimal). Show these and do "
    "not round the exact numbers again, because browsers and Python round halves differently.",
}

PASS_KEYS = frozenset(
    {
        "date",
        "satellites",
        "scenes",
        "newest_scene_time_utc",
        "field_squares",
        "observed_squares",
        "clear_squares",
        "coverage",
        "clear_share",
        "valid_share",
        "coverage_percent",
        "clear_percent",
        "valid_percent",
        "cloud_squares",
        "unknown_squares",
        "discarded_squares",
        "discarded_field_squares",
        "real_readings",
        "verdict",
        "reason_code",
        "reason",
        "mean_k",
        "min_k",
        "max_k",
        "mean_c",
        "min_c",
        "max_c",
        "mean_c_text",
        "min_c_text",
        "max_c_text",
    }
)
TOP_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "from",
        "to",
        "credit",
        "notices",
        "definitions",
        "version",
        "settings",
        "totals",
        "passes",
    }
)
SETTINGS_KEYS = frozenset(
    {"buffer_m", "square_size_m", "min_valid_share", "min_kelvin", "max_kelvin"}
)
TIME_FORMAT = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]", re.ASCII)
MAX_REASON_LENGTH = 400
# No grid has more squares than this, so a bigger count in a file is wrong (and would
# be too big for the page's numbers).
MAX_COUNT = earth_search.MAX_SQUARES_PER_SIDE**2
# The largest field the code reads, in real 100 m sensor readings (area of the biggest grid
# divided by the area of one sensor square).
MAX_READINGS = MAX_COUNT * landsat.SQUARE_SIZE_M**2 / thermal.SENSOR_SQUARE_M**2
# Characters that never occur in the report's own reason sentences but do occur in
# addresses and file paths.
FORBIDDEN_IN_REASON = "?:/\\#"

ReadDay = Callable[[Field, list[pystac.Item]], thermal.ThermalPicture | None]
FindPasses = Callable[[Field, date, date], dict[date, list[pystac.Item]]]


def source_fingerprint() -> str:
    """SHA-256 of the text of the source files that decide what this export contains."""
    folder = Path(__file__).parent
    digest = hashlib.sha256()
    for name in sorted(FINGERPRINTED_FILES):
        digest.update(name.encode("utf-8"))
        digest.update((folder / name).read_bytes())
    return digest.hexdigest()


def current_settings() -> dict[str, float]:
    """The limits in the code right now that decide what a pass shows."""
    return {
        "buffer_m": landsat.BUFFER_M,
        "square_size_m": landsat.SQUARE_SIZE_M,
        "min_valid_share": thermal_report.MIN_VALID_SHARE,
        "min_kelvin": thermal.MIN_KELVIN,
        "max_kelvin": thermal.MAX_KELVIN,
    }


def chunk_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """Split start..end (both included) into runs of at most CHUNK_DAYS days that do not overlap."""
    if end < start:
        raise ValueError("--to must not be before --from.")
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValueError(f"That range is longer than {MAX_RANGE_DAYS} days.")
    ranges: list[tuple[date, date]] = []
    chunk_start = start
    while True:
        # Compare days left first, so no date past the end is ever built.
        if (end - chunk_start).days < CHUNK_DAYS:
            ranges.append((chunk_start, end))
            return ranges
        chunk_end = chunk_start + timedelta(days=CHUNK_DAYS - 1)
        ranges.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)


def satellite_names(items: list[pystac.Item]) -> list[str]:
    """The satellites of a day's scenes, like ["Landsat 9"], sorted, from the catalogue's names."""
    names = {
        str(item.properties.get("platform", "")).replace("-", " ").capitalize() for item in items
    }
    return sorted(names)


def refuse_repeated_scenes(items: list[pystac.Item]) -> None:
    """Stop if the catalogue lists the same scene twice on one day.

    The printed report would read both copies and merge them, and two copies that
    differ could give another result than one. The export refuses instead of
    guessing, so it can never disagree with the report.
    """
    if len({item.id for item in items}) != len(items):
        raise ValueError("The catalogue listed the same scene twice on one day.")


def pass_entry(
    day: date, items: list[pystac.Item], picture: thermal.ThermalPicture, summary: Any
) -> dict[str, Any]:
    """The data-file entry for one pass: the summary's exact numbers plus the verdict."""
    decision = thermal_report.skip_decision(summary)
    entry: dict[str, Any] = {
        "date": day.isoformat(),
        "satellites": satellite_names(items),
        "scenes": len(items),
        "newest_scene_time_utc": _time_text(items[0]),
        "field_squares": summary.field_squares,
        "observed_squares": summary.observed_squares,
        "clear_squares": summary.clear_squares,
        "coverage": summary.coverage,
        "clear_share": summary.clear_share,
        "valid_share": summary.valid_share,
        "coverage_percent": thermal_report.percent(summary.coverage),
        "clear_percent": thermal_report.percent(summary.clear_share),
        "valid_percent": thermal_report.percent(summary.valid_share),
        "cloud_squares": summary.cloud_squares,
        "unknown_squares": summary.unknown_squares,
        "discarded_squares": picture.discarded_squares,
        "discarded_field_squares": summary.discarded_field_squares,
        "real_readings": summary.real_readings,
        "verdict": "shown" if decision is None else "skipped",
        "reason_code": None if decision is None else decision[0],
        "reason": None if decision is None else decision[1],
    }
    entry.update(_temperature_fields(summary, decision is None))
    return entry


def _time_text(newest: pystac.Item) -> str:
    """HH:MM in UTC of the newest scene of the day (not of every scene)."""
    if newest.datetime is None:
        raise ValueError("A scene has no time.")
    return f"{newest.datetime.astimezone(UTC):%H:%M}"


def _temperature_fields(summary: Any, shown: bool) -> dict[str, float | str | None]:
    """Mean, lowest and highest temperature in K and C for a shown pass, else all None."""
    empty: dict[str, float | str | None] = {
        "mean_k": None,
        "min_k": None,
        "max_k": None,
        "mean_c": None,
        "min_c": None,
        "max_c": None,
        "mean_c_text": None,
        "min_c_text": None,
        "max_c_text": None,
    }
    if not shown:
        return empty
    if summary.mean_k is None or summary.min_k is None or summary.max_k is None:
        raise ValueError("A shown pass has no temperatures.")
    fields = dict(empty)
    for name, kelvin in (("mean", summary.mean_k), ("min", summary.min_k), ("max", summary.max_k)):
        # Exact numbers, plus the text the printed report shows for them.
        fields[f"{name}_k"] = kelvin
        fields[f"{name}_c"] = thermal.to_celsius(kelvin)
        fields[f"{name}_c_text"] = thermal_report.celsius_text(kelvin)
    return fields


def collect_passes(
    field: Field,
    start: date,
    end: date,
    find: FindPasses = landsat.search_items,
    read: ReadDay = landsat.picture_for_day,
) -> list[dict[str, Any]]:
    """Read every day of start..end and return one entry per day that counts as a pass.

    find and read are replaced by fakes in tests. Any failure stops the whole run: a day
    that could not be read is never turned into "no pass".
    """
    masks: dict[tuple[int, int], list[list[bool]]] = {}
    entries: list[dict[str, Any]] = []
    seen_days: set[date] = set()
    for chunk_start, chunk_end in chunk_ranges(start, end):
        found = find(field, chunk_start, chunk_end)
        for day in sorted(found):
            if day < chunk_start or day > chunk_end or day in seen_days:
                raise ValueError("The catalogue returned a day outside the asked range.")
            seen_days.add(day)
            items = found[day]
            refuse_repeated_scenes(items)
            picture = read(field, items) if items else None
            if picture is None:
                continue
            entry = _entry_for_day(field, day, items, picture, masks)
            if entry is not None:
                entries.append(entry)
    return entries


def _entry_for_day(
    field: Field,
    day: date,
    items: list[pystac.Item],
    picture: thermal.ThermalPicture,
    masks: dict[tuple[int, int], list[list[bool]]],
) -> dict[str, Any] | None:
    """The entry for one day, or None if the scene did not measure the field."""
    # The report's own mask function: it refuses a field too small or thin to hold a square.
    inside = thermal_report.mask_for(field, picture, masks)
    summary = thermal.summarize_field(picture, inside, area_m2(field))
    if not thermal_report.has_measurement(summary):
        return None
    return pass_entry(day, items, picture, summary)


def build_data(
    entries: list[dict[str, Any]], start: date, end: date, credit: str, example_field: bool
) -> dict[str, Any]:
    """The whole data file as a dict, with totals counted from the entries."""
    shown = sum(1 for entry in entries if entry["verdict"] == "shown")
    notices: list[str] = list(NOTICES)
    if example_field:
        notices.append(INVENTED_OUTLINE_NOTICE)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "credit": validate_credit(credit),
        "notices": notices,
        "definitions": dict(DEFINITIONS),
        "version": {
            "package_version": package_version(),
            "source_fingerprint": source_fingerprint(),
            "revision": "unknown",
        },
        "settings": current_settings(),
        "totals": {
            "passes": len(entries),
            "shown": shown,
            "skipped": len(entries) - shown,
        },
        "passes": entries,
    }


def _fail(message: str) -> NoReturn:
    raise ValueError(f"{DATA_FILE}: {message}")


def _is_count(value: Any) -> bool:
    """A whole number of squares: an int (not a bool, not a float), 0 up to MAX_COUNT."""
    return type(value) is int and 0 <= value <= MAX_COUNT


def _is_number(value: Any) -> bool:
    """A real, finite number (an int or a float, not a bool)."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # a whole number too big for a float
        return False


def _is_plain_text(value: Any, limit: int) -> bool:
    """Plain printable text of 1 to limit characters, with nothing that looks like an
    address or a file path."""
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        return False
    if not value.isprintable() or any(c in value for c in FORBIDDEN_IN_REASON):
        return False
    return "http" not in value.lower()


def validate_data(data: Any, require_current_code: bool = True) -> None:
    """Check a data dict for shape, types, arithmetic and the verdict rules.

    Raises ValueError with a plain message at the first problem. The verdicts, reason codes
    and totals are worked out again from the numbers in the file and must match.
    """
    if not isinstance(data, dict) or set(data) != TOP_KEYS:
        _fail("the top level has missing or extra entries.")
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        _fail("wrong schema version.")
    if data["kind"] != KIND:
        _fail("wrong kind.")
    start, end = _check_header(data, require_current_code)
    settings = data["settings"]
    passes = data["passes"]
    if not isinstance(passes, list):
        _fail("passes is not a list.")
    previous: date | None = None
    for entry in passes:
        day = _check_pass(entry, settings)
        if (entry["field_squares"], entry["real_readings"]) != (
            passes[0]["field_squares"],
            passes[0]["real_readings"],
        ):
            _fail("the passes disagree about the size of the field.")
        if day < start or day > end or (previous is not None and day <= previous):
            _fail(f"pass {entry['date']} is outside the range, repeated or out of order.")
        previous = day
    shown = sum(1 for entry in passes if entry["verdict"] == "shown")
    expected = {"passes": len(passes), "shown": shown, "skipped": len(passes) - shown}
    totals = data["totals"]
    if (
        not isinstance(totals, dict)
        or set(totals) != set(expected)
        or any(type(totals[key]) is not int for key in expected)
        or totals != expected
    ):
        _fail("the totals do not match the list of passes.")


def _parse_day(text: Any, what: str) -> date:
    """A date written exactly as YYYY-MM-DD (other forms Python accepts are refused)."""
    try:
        if isinstance(text, str):
            day = date.fromisoformat(text)
            if day.isoformat() == text:
                return day
    except ValueError:
        pass
    _fail(f"{what} is not a date written like 2026-09-30.")


def _check_header(data: dict[str, Any], require_current_code: bool) -> tuple[date, date]:
    """Check everything outside the passes. Returns the range."""
    start = _parse_day(data["from"], "from")
    end = _parse_day(data["to"], "to")
    if end < start:
        _fail("the range ends before it starts.")
    try:
        credit_ok = (
            isinstance(data["credit"], str) and validate_credit(data["credit"]) == data["credit"]
        )
    except ValueError:
        credit_ok = False
    if not credit_ok:
        _fail("the credit text is not plain text.")
    notices = data["notices"]
    if notices not in (list(NOTICES), [*NOTICES, INVENTED_OUTLINE_NOTICE]):
        _fail("the notices are not the ones this export writes.")
    if data["definitions"] != DEFINITIONS:
        _fail("the definitions are not the ones this export writes.")
    settings = data["settings"]
    if not isinstance(settings, dict) or set(settings) != SETTINGS_KEYS:
        _fail("settings has missing or extra entries.")
    if not all(_is_number(value) for value in settings.values()):
        _fail("a setting is not a number.")
    version = data["version"]
    if not isinstance(version, dict) or set(version) != {
        "package_version",
        "source_fingerprint",
        "revision",
    }:
        _fail("the version stamp has missing or extra entries.")
    if (
        not isinstance(version["package_version"], str)
        or PACKAGE_VERSION_FORMAT.fullmatch(version["package_version"]) is None
        or not isinstance(version["source_fingerprint"], str)
        or FINGERPRINT_FORMAT.fullmatch(version["source_fingerprint"]) is None
        or version["revision"] != "unknown"
    ):
        _fail("the version stamp holds something other than a version number and a fingerprint.")
    if require_current_code:
        if version["source_fingerprint"] != source_fingerprint():
            _fail("made by other code than the code running now; export again.")
        if settings != current_settings():
            _fail("made with other limits than the code running now; export again.")
    return start, end


def _check_pass(entry: Any, settings: dict[str, Any]) -> date:
    """Check one pass entry. Returns its date."""
    if not isinstance(entry, dict) or set(entry) != PASS_KEYS:
        _fail("a pass has missing or extra entries.")
    day = _parse_day(entry["date"], "a pass date")
    label = entry["date"]
    names = entry["satellites"]
    if not isinstance(names, list) or not names or not all(n in SATELLITE_NAMES for n in names):
        _fail(f"pass {label}: unknown satellite.")
    if names != sorted(set(names)):
        _fail(f"pass {label}: satellites must be sorted and not repeated.")
    time_text = entry["newest_scene_time_utc"]
    if not isinstance(time_text, str) or TIME_FORMAT.fullmatch(time_text) is None:
        _fail(f"pass {label}: the time is not HH:MM.")
    _check_counts(entry, label)
    if len(names) > entry["scenes"]:
        _fail(f"pass {label}: more satellites than scenes.")
    _check_ratios(entry, label)
    _check_verdict(entry, label, settings)
    return day


def _check_counts(entry: dict[str, Any], label: str) -> None:
    """Counts are whole and in order: clear <= observed <= field; at least one scene."""
    names = ("field_squares", "observed_squares", "clear_squares")
    counts = (
        "cloud_squares",
        "unknown_squares",
        "discarded_squares",
        "discarded_field_squares",
        "scenes",
    )
    if not all(_is_count(entry[name]) for name in (*names, *counts)):
        _fail(f"pass {label}: a count is not a whole number of at least 0.")
    if entry["scenes"] < 1 or entry["field_squares"] < 1:
        _fail(f"pass {label}: no scene or no field squares.")
    if entry["cloud_squares"] + entry["unknown_squares"] > MAX_COUNT:
        _fail(f"pass {label}: more cloud and unknown squares than the biggest grid holds.")
    if not entry["clear_squares"] <= entry["observed_squares"] <= entry["field_squares"]:
        _fail(f"pass {label}: the counts are out of order.")
    if entry["discarded_field_squares"] > entry["discarded_squares"]:
        _fail(f"pass {label}: more squares thrown out in the field than in the whole area.")
    if entry["discarded_field_squares"] > entry["field_squares"] - entry["observed_squares"]:
        _fail(f"pass {label}: thrown-out squares and measured squares overlap.")
    if entry["observed_squares"] == 0 and entry["discarded_field_squares"] == 0:
        _fail(f"pass {label}: this is not a pass over the field.")


def _check_ratios(entry: dict[str, Any], label: str) -> None:
    """The three shares equal the counts they are made from."""
    field, observed, clear = (
        entry[n] for n in ("field_squares", "observed_squares", "clear_squares")
    )
    wanted = {
        "coverage": observed / field,
        "clear_share": clear / observed if observed > 0 else 0.0,
        "valid_share": clear / field,
    }
    # Exact equality on purpose: the same sums and divisions as thermal.summarize_field,
    # and JSON keeps a float exactly, so a share that is even slightly off is a wrong file.
    for name, value in wanted.items():
        if not _is_number(entry[name]) or entry[name] != value:
            _fail(f"pass {label}: {name} does not match the counts.")
    printed = {
        "coverage_percent": thermal_report.percent(wanted["coverage"]),
        "clear_percent": thermal_report.percent(wanted["clear_share"]),
        "valid_percent": thermal_report.percent(wanted["valid_share"]),
    }
    for name, number in printed.items():
        if type(entry[name]) is not int or entry[name] != number:
            _fail(f"pass {label}: {name} is not the number the report prints.")
    if not _is_number(entry["real_readings"]) or not 0 < entry["real_readings"] <= MAX_READINGS:
        _fail(f"pass {label}: real_readings is not a possible number.")


def _check_verdict(entry: dict[str, Any], label: str, settings: dict[str, Any]) -> None:
    """Verdict, reason and temperatures agree with each other and with the reason order."""
    if entry["cloud_squares"] > 0:
        code: str | None = thermal_report.REASON_CLOUD
    elif entry["unknown_squares"] > 0:
        code = thermal_report.REASON_NO_QUALITY
    elif (
        entry["clear_squares"] / entry["field_squares"] < settings["min_valid_share"]
        or entry["clear_squares"] == 0
    ):
        code = thermal_report.REASON_LOW_VALID
    else:
        code = None
    verdict = "shown" if code is None else "skipped"
    if entry["verdict"] != verdict or entry["reason_code"] != code:
        _fail(f"pass {label}: the verdict or reason does not follow from the numbers.")
    names = ("mean", "min", "max")
    if code is not None:
        if not _is_plain_text(entry["reason"], MAX_REASON_LENGTH):
            _fail(f"pass {label}: a skipped pass needs a plain-text reason.")
        if settings == current_settings():
            # Worked out again with the report's own function, so the sentence cannot
            # contradict the numbers beside it.
            if thermal_report.skip_decision(_summary_of(entry)) != (code, entry["reason"]):
                _fail(f"pass {label}: the reason does not match the report's wording.")
        if any(entry[f"{n}_{unit}"] is not None for n in names for unit in ("k", "c", "c_text")):
            _fail(f"pass {label}: a skipped pass must not carry temperatures.")
        return
    if entry["reason"] is not None:
        _fail(f"pass {label}: a shown pass must not carry a reason.")
    _check_temperatures(entry, label, settings)


def _summary_of(entry: dict[str, Any]) -> thermal.ThermalSummary:
    """Rebuild the thermal summary of a skipped entry (no temperatures) from its counts."""
    return thermal.ThermalSummary(
        field_squares=entry["field_squares"],
        observed_squares=entry["observed_squares"],
        clear_squares=entry["clear_squares"],
        coverage=entry["coverage"],
        clear_share=entry["clear_share"],
        valid_share=entry["valid_share"],
        unknown_squares=entry["unknown_squares"],
        cloud_squares=entry["cloud_squares"],
        mean_k=None,
        min_k=None,
        max_k=None,
        real_readings=entry["real_readings"],
        discarded_field_squares=entry["discarded_field_squares"],
    )


def _check_temperatures(entry: dict[str, Any], label: str, settings: dict[str, Any]) -> None:
    """Shown pass: finite, inside the believable range, ordered, and C matches K."""
    for name in ("mean", "min", "max"):
        kelvin, celsius = entry[f"{name}_k"], entry[f"{name}_c"]
        if not _is_number(kelvin) or not _is_number(celsius):
            _fail(f"pass {label}: a temperature is missing or not a number.")
        if not settings["min_kelvin"] <= kelvin <= settings["max_kelvin"]:
            _fail(f"pass {label}: a temperature is outside the believable range.")
        expected = thermal.to_celsius(kelvin)
        if celsius != expected or math.copysign(1.0, celsius) != math.copysign(1.0, expected):
            _fail(f"pass {label}: Celsius does not match kelvin.")
        if entry[f"{name}_c_text"] != thermal_report.celsius_text(kelvin):
            _fail(f"pass {label}: the printed Celsius text does not match.")
    if not entry["min_k"] <= entry["mean_k"] <= entry["max_k"]:
        _fail(f"pass {label}: lowest, mean and highest are out of order.")
    # With n clear squares, one of them is the lowest and one the highest, so the mean lies
    # between (lowest*(n-1)+highest)/n and (lowest+highest*(n-1))/n. For n = 1 both ends are
    # the same number, for n = 2 the mean is exactly in the middle.
    n = entry["clear_squares"]
    lowest_mean = (entry["min_k"] * (n - 1) + entry["max_k"]) / n
    highest_mean = (entry["min_k"] + entry["max_k"] * (n - 1)) / n
    slack = 1e-9 * entry["max_k"]  # sums of many decimals differ in the last digits
    if n == 1 and not entry["min_k"] == entry["mean_k"] == entry["max_k"]:
        _fail(f"pass {label}: one clear square cannot have three different temperatures.")
    if not lowest_mean - slack <= entry["mean_k"] <= highest_mean + slack:
        _fail(f"pass {label}: the mean cannot come from this many squares.")


def write_data(out_dir: Path, data: dict[str, Any]) -> None:
    """Write the data file after it has passed validate_data. Replaces an old file whole."""
    validate_data(data)
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = out_dir / (DATA_FILE + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, out_dir / DATA_FILE)


def load_passes(folder: Path, require_current_code: bool = True) -> dict[str, Any]:
    """Read landsat-passes.json from a folder and check it. Returns the data.

    require_current_code (default True) also refuses a file made by other code or other
    limits than the code running now. Pass False only to read an old file for looking at,
    never to publish it. Raises ValueError with a plain message at the first problem.
    """
    try:
        text = (folder / DATA_FILE).read_text(encoding="utf-8")
        data = loads_strict(text)  # refuses a repeated key and NaN
    except (OSError, ValueError, RecursionError):
        _fail("missing or not valid JSON.")
    validate_data(data, require_current_code)
    return data  # type: ignore[no-any-return]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Save Landsat passes as a data file for the site.")
    parser.add_argument("--field", type=Path, required=True, help="GeoJSON file with the outline")
    parser.add_argument("--from", dest="start", type=parse_day, required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="end", type=parse_day, required=True, help="YYYY-MM-DD")
    parser.add_argument("--out", type=Path, required=True, help="folder to write the file into")
    parser.add_argument("--credit", required=True, help="the credit text shown with the numbers")
    parser.add_argument(
        "--example-field",
        action="store_true",
        help="mark the outline as invented (only for the example field)",
    )
    args = parser.parse_args(argv)
    try:
        field = load_field(args.field)
        entries = collect_passes(field, args.start, args.end)
        data = build_data(entries, args.start, args.end, args.credit, args.example_field)
        write_data(args.out, data)
        load_passes(args.out)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    totals = data["totals"]
    print(f"Wrote {DATA_FILE} to {args.out}")
    print(f"{totals['passes']} passes: {totals['shown']} shown, {totals['skipped']} skipped")


if __name__ == "__main__":
    main()
