"""Persisted soil-depletion state between runs.

Without this, the tool always assumes the soil is full at the start of
whatever forecast window it fetches (calendar midnight today) - so
re-running mid-day after a deadline has passed just repeats the same
problem instead of reflecting hours already spent that day. This
module lets weekly.py save modeled depletion after each run and pick
it back up next time, catching up on already-happened weather in
between instead of re-guessing.

This is the only file that reads or writes the state file, matching the
project's one-file-per-concern pattern (weather.py fetches, fao56.py has
constants/formulas, water_balance.py runs the model, units.py converts).
"""

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# A single hidden file in the working directory - matches the current
# scope of one field at a time ("multiple fields" is explicitly out of
# scope). A future multi-field version would need one file per field
# instead.
STATE_PATH = Path(".orchardwater_state.json")


@dataclass(frozen=True)
class FieldState:
    """A saved soil-depletion anchor for one field.

    anchor_timestamp: the ISO time this depletion was computed as of.
    It is an end-of-hour label for weather-derived state, or the reset
    instant after manual irrigation.
    anchor_utc_offset_seconds: the UTC offset that timestamp should be
    interpreted with - the offset of the anchor, which may differ from
    the current weather response (see weekly.py's response_offset for why this matters
    across a real Casablanca offset change; a saved state can span one).
    depletion_mm: root-zone depletion at that instant.
    field_key: identifies which field and soil choice this state belongs
    to (see field_key()) - state for a different one must never be reused.
    de_mm: surface evaporation depletion at that instant. It is persisted
    too, because not persisting it has the opposite of the intended
    effect: every fresh run would wrongly assume the surface had just been
    re-wetted, adding a compounding overstatement across repeated runs.
    Defaults to 0.0 (fully wet) so a state file saved before this field
    existed still loads - the same "missing means start fresh" behavior
    depletion_mm already had before this field existed.
    few: the exposed-and-wetted fraction (Eq. 75) in effect at that
    instant - persisted alongside de_mm so a significant rain's effect on
    the wetted area survives across runs too, not just within one. None
    means "no significant rain has been recorded yet" - load_state() lets
    run_forecast_period() resolve that to the drip-patch fraction, same as
    a fresh run.
    """

    anchor_timestamp: str
    anchor_utc_offset_seconds: int
    depletion_mm: float
    field_key: str
    de_mm: float = 0.0
    few: float | None = None


def field_key(lat: float, lon: float, soil_name: str) -> str:
    """A stable identifier for one field + soil choice.

    Rounds lat/lon so two runs for the same field don't land on different
    keys from float-precision noise. Pump flow, area, and efficiency are
    deliberately excluded - they don't affect the depletion trajectory
    itself, only the final mm -> m3 -> hours conversion (units.py), so
    saved state should still apply if only those change between runs.
    """
    return f"{lat:.6f},{lon:.6f},{soil_name}"


def _looks_like_a_field_key(key: str) -> bool:
    """True if `key` has the exact shape field_key() produces.

    field_key() always emits "<lat, 6 decimals>,<lon, 6 decimals>,
    <soil_name>" - this checks a saved field_key actually has that shape
    (three comma-separated parts, the first two round-tripping through
    the same %.6f formatting field_key() uses, the third non-empty)
    rather than accepting any non-empty string as if it were a real,
    different field's identifier. Checking only that field_key is a str
    is not enough: an empty string, or a truncated key like
    "30.500000,-8.875000" missing the soil segment, is still a valid str
    and would be read by weekly.py's peek step as "a different,
    legitimate field", silently discarding real corrupted-but-same-field
    recovery evidence.

    Deliberately does not check the soil segment against fao56.py's
    SOIL_TYPES - state.py doesn't import fao56.py (one-file-per-concern,
    see this module's own docstring), and an unrecognized soil name is
    caught elsewhere (weekly.py's own SOIL_TYPES[soil_name] lookup)
    regardless of what a saved key contains.
    """
    parts = key.split(",")
    if len(parts) != 3:
        return False
    lat_str, lon_str, soil_name = parts
    if not soil_name:
        return False
    try:
        lat = float(lat_str)
        lon = float(lon_str)
    except ValueError:
        return False
    return f"{lat:.6f}" == lat_str and f"{lon:.6f}" == lon_str


def anchor_instant(state: FieldState) -> datetime:
    """The real instant state.anchor_timestamp/anchor_utc_offset_seconds refers to.

    Pulled out so a caller can learn a saved anchor's real age - e.g. to
    decide how much weather history to fetch - using the exact same
    interpretation load_state() itself uses for its own bounds check,
    rather than duplicating the "parse timestamp with this state's own
    saved offset" logic at each call site.
    """
    offset = timezone(timedelta(seconds=state.anchor_utc_offset_seconds))
    return datetime.fromisoformat(state.anchor_timestamp).replace(tzinfo=offset)


def parse_state_file_any_field(path: Path) -> FieldState | None:
    """Load and validate a saved state file, WITHOUT checking its field or age.

    Same validation load_state() does (missing file, corrupt JSON,
    implausible depletion_mm, de_mm/few sanitization) minus the field-
    match and "is the anchor within this run's fetched range" checks -
    split out so a caller can tell "no file at all" apart from "a file
    exists but could not be parsed or trusted safely," which
    parse_state_file()'s plain None can't distinguish once a wrong
    field_key is also folded into the same None.
    compute_weekly_summary()'s peek step must not treat ANY None from
    parse_state_file() as "no state, safe to assume full soil" - a file
    that exists and is for the SAME field, just corrupted (bad JSON, an
    invalid timestamp string), returns that same None, and silently
    discarding it would lose real recovery evidence and overwrite the
    file with an assumed-full-soil guess (for example a 60mm sand-field
    depletion whose timestamp was corrupted). A file that parses fine
    but is for a DIFFERENT field is not data loss - switching which
    field this single-file-at-a-time tool tracks (state.py's own
    documented v0 scope) is expected, ordinary use, not corruption - so
    callers must still check field_key themselves against this
    function's result before deciding whether it's actually usable.

    Everything else - a structurally corrupt anchor_timestamp/
    anchor_utc_offset_seconds/field_key, or an implausible depletion_mm
    (Dr, wrong type or value) - returns None entirely - Dr is safety-
    critical (it drives the RAW-crossing/deadline check) and this
    function has no crop context to attempt a narrower recovery.
    field_key's own type is checked here: a JSON `null` (or any
    non-string) field_key would otherwise parse successfully, and
    weekly.py's peek step would then read that as "a DIFFERENT field's
    valid state" (since it never equals this run's own key), silently
    taking the safe-to-ignore branch above instead of the
    preserve-and-warn one - corrupted field identity must never be
    mistaken for an intentional field switch. The type check alone is not
    enough: an empty string, or a truncated key missing the soil segment,
    is still a valid str and hits the exact same problem - see
    _looks_like_a_field_key(). de_mm/few, by contrast, get silently
    sanitized to a safe default (0.0 / None) rather than invalidating the
    whole state - a bad surface reading (wrong type OR implausible value,
    treated the same way) should never cost a caller a perfectly good
    depletion_mm, and the two always reset TOGETHER, never independently.
    compute_weekly_summary() still applies its own, crop-aware check on
    top of this (de_mm vs that crop's TEW, few against the exact values
    that crop's model can produce), since this function alone can't know
    those bounds.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        state = FieldState(**data)
        if (
            type(state.anchor_timestamp) is not str
            or type(state.anchor_utc_offset_seconds) is not int
            or type(state.field_key) is not str
            or not _looks_like_a_field_key(state.field_key)
        ):
            return None
        if type(state.depletion_mm) not in (int, float) or not math.isfinite(state.depletion_mm):
            return None
        if state.depletion_mm < 0:
            return None
        # de_mm/few get sanitized to a safe default here, rather than
        # invalidating the WHOLE state the way an implausible
        # depletion_mm above does - a bad surface reading should never
        # cost a caller a perfectly good depletion_mm. Two rules follow:
        # (1) a wrong TYPE (not just an implausible VALUE) must not
        # invalidate the whole state - a mistyped de_mm/few says nothing
        # about depletion_mm's own validity either, so type and value
        # problems are treated identically here; (2) de_mm and few must
        # not be sanitized INDEPENDENTLY of each other, since an invalid
        # few alone could leave a stale de_mm paired with a freshly-reset
        # few (or vice versa) - they always reset TOGETHER, never one
        # without the other.
        de_mm_valid = (
            type(state.de_mm) in (int, float) and math.isfinite(state.de_mm) and state.de_mm >= 0.0
        )
        few_valid = state.few is None or (
            type(state.few) in (int, float) and math.isfinite(state.few) and 0.0 < state.few <= 1.0
        )
        if not (de_mm_valid and few_valid):
            state = FieldState(
                anchor_timestamp=state.anchor_timestamp,
                anchor_utc_offset_seconds=state.anchor_utc_offset_seconds,
                depletion_mm=state.depletion_mm,
                field_key=state.field_key,
                de_mm=0.0,
                few=None,
            )
        # Parsed purely to fail fast on a corrupt timestamp/offset here,
        # at the same point load_state() always has - anchor_instant()
        # repeats this same parse for a caller that needs the instant
        # itself (e.g. to size a fetch window before load_state() runs).
        datetime.fromisoformat(state.anchor_timestamp).replace(
            tzinfo=timezone(timedelta(seconds=state.anchor_utc_offset_seconds))
        )
    except (json.JSONDecodeError, TypeError, KeyError, OSError, ValueError, OverflowError):
        return None
    return state


def parse_state_file(path: Path, key: str) -> FieldState | None:
    """parse_state_file_any_field(), plus checking it's actually for `key`.

    None means either "no usable state at all for THIS field" - which
    covers both "no file" and "a file exists but is for a different
    field" - or "the file exists, is for this field, but could not be
    parsed/validated" (see parse_state_file_any_field(), which keeps
    that distinction available; this one is for load_state() and any
    other caller that only needs "is there usable state for my field",
    not the finer distinction).
    """
    state = parse_state_file_any_field(path)
    if state is None or state.field_key != key:
        return None
    return state


def load_state(
    path: Path, key: str, oldest_usable: datetime, newest_usable: datetime
) -> FieldState | None:
    """Load saved state, or None if it's missing, unreadable, stale, for a
    different field, or outside this run's fetched range entirely.

    Bounds are real instants. A new Open-Meteo response may use a different
    fixed UTC offset, so comparing its labels with an older label as strings
    can skip or repeat an hour around a clock change.

    See parse_state_file() for everything this checks except the bounds
    themselves - this function is now just that plus the range check.
    """
    state = parse_state_file(path, key)
    if state is None:
        return None
    if not (oldest_usable <= anchor_instant(state) <= newest_usable):
        return None
    return state


def save_state(path: Path, state: FieldState) -> None:
    """Save state, overwriting any existing file for a previous run.

    Writes to a temporary file next to `path` first, then atomically
    renames it into place (os.replace, a single filesystem operation) -
    plain write_text() can leave a half-written, corrupt file if the
    process is interrupted mid-write. load_state() already fails safely
    on a corrupt file (falls back to a fresh start), but a torn write
    would silently lose real, previously-tracked depletion for no reason
    - this avoids that case existing at all rather than just handling it.

    The temp file's name includes the process ID and a random suffix
    (tempfile.mkstemp) rather than a fixed ".tmp" name, so two runs
    writing at the same instant can't collide on the same temp file -
    cleaned up on any failure so a crash mid-write doesn't leave
    litter behind.

    This tool is meant to be run by one person, one invocation at a time,
    for one field - it does NOT protect against two overlapping runs
    finishing in a different order than they started (e.g. a slow run
    started before a --reset finishing after it, overwriting the reset
    with older depletion). That would need real cross-process
    coordination (a lock file, or rejecting a save older than what's on
    disk), which is out of scope for a single-user CLI tool - flagged
    here rather than silently assumed away.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent or Path(), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(asdict(state)))
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
