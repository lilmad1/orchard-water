"""Read a farm file: who gets the weekly message, and the blocks it covers.

The weekly engine (weekly.py) works on one field at a time. A real farm has
several irrigation blocks, each with its own valve, size and flow. A farm file
lists them once, so the weekly loop (loop.py) can run the engine for every
block and send the farmer one message.

A farm file is JSON. See farms/example.json for a made-up one. Real farm files
hold a phone number and where a farmer's land is, so they are private and never
committed (see .gitignore).
"""

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from orchardwater.fao56 import DEFAULT_SOIL, SOIL_TYPES
from orchardwater.kc import MonthlyKc, monthly_kc_from_list

# WhatsApp wants the number in international form, digits only, no "+" or
# spaces: country code first, for example 212600000000 for a Moroccan mobile.
_PHONE_PATTERN = re.compile(r"^[1-9][0-9]{7,14}$")
# Used in file names (state files, outbox), so keep it to safe characters.
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class FarmFileError(ValueError):
    """The farm file is not usable. The message says what to fix."""


@dataclass(frozen=True)
class Block:
    """One irrigation block: a part of the farm that is watered as one unit.

    block_id is a short safe name used in file names. name is what the farmer
    calls it ("Bloc A", "Nord"), shown in the message. flow_m3_per_h is how
    much water reaches this block per hour when its valve is open: the number
    that turns a volume into hours. The rest are the same inputs weekly.py takes
    for one field (see its --help).

    monthly_kc (optional): 12 Kc numbers, January first, fitted for this block from
    measured water use (python -m orchardwater.kc). Left out, the engine uses its
    textbook dual Kc.
    """

    block_id: str
    name: str
    lat: float
    lon: float
    area_ha: float
    flow_m3_per_h: float
    efficiency: float
    soil: str
    ec_water_ds_m: float
    monthly_kc: MonthlyKc | None = None


@dataclass(frozen=True)
class Farm:
    """One farm: who to message, in which language, and its blocks."""

    farm_id: str
    farmer_name: str
    phone: str
    wording_file: Path
    blocks: tuple[Block, ...]


def load_farm(path: Path) -> Farm:
    """Read and check a farm file. Raises FarmFileError if it is not usable.

    wording_file in the farm file is read relative to the farm file's own
    folder, so a farm file and its wording can be moved together.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as error:
        raise FarmFileError(f"not valid JSON ({error})") from None
    if not isinstance(data, dict):
        raise FarmFileError("the file must be one JSON object { ... }")
    farm_id = _id_text(data, "farm_id")
    phone = _text(data, "phone")
    if not _PHONE_PATTERN.match(phone):
        raise FarmFileError("phone must be digits only, country code first, e.g. 212600000000")
    blocks = _read_blocks(data.get("blocks"))
    wording_file = path.parent / _text(data, "wording_file")
    return Farm(farm_id, _text(data, "farmer_name"), phone, wording_file, blocks)


def _read_blocks(raw_blocks: object) -> tuple[Block, ...]:
    """Check the list of blocks: at least one, and no two with the same block_id."""
    if not isinstance(raw_blocks, list) or not raw_blocks:
        raise FarmFileError("blocks must be a list with at least one block")
    blocks = []
    seen_ids: set[str] = set()
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            raise FarmFileError("each block must be a JSON object { ... }")
        block = _read_block(raw)
        if block.block_id in seen_ids:
            raise FarmFileError(f"block_id {block.block_id!r} is used twice")
        seen_ids.add(block.block_id)
        blocks.append(block)
    return tuple(blocks)


def _read_block(raw: dict[str, object]) -> Block:
    """Check one block. Range checks on area, flow and efficiency stay in weekly.py."""
    soil = raw.get("soil", DEFAULT_SOIL)
    if soil not in SOIL_TYPES:
        raise FarmFileError(f"soil must be one of: {', '.join(sorted(SOIL_TYPES))}")
    assert isinstance(soil, str)  # checked just above: SOIL_TYPES keys are text
    return Block(
        block_id=_id_text(raw, "block_id"),
        name=_text(raw, "name"),
        lat=_number(raw, "lat"),
        lon=_number(raw, "lon"),
        area_ha=_number(raw, "area_ha"),
        flow_m3_per_h=_number(raw, "flow_m3_per_h"),
        efficiency=_number(raw, "efficiency"),
        soil=soil,
        ec_water_ds_m=_number(raw, "ec_water_ds_m"),
        monthly_kc=_monthly_kc(raw),
    )


def _monthly_kc(raw: dict[str, object]) -> MonthlyKc | None:
    """The optional monthly_kc list: 12 plausible numbers, or left out."""
    value = raw.get("monthly_kc")
    if value is None:
        return None
    if not isinstance(value, list):
        raise FarmFileError("monthly_kc must be a list of 12 numbers, January first")
    try:
        return monthly_kc_from_list(value)
    except ValueError as error:
        raise FarmFileError(f"monthly_kc: {error}") from None


def _text(data: dict[str, object], key: str) -> str:
    """A required, non-empty piece of text."""
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FarmFileError(f"{key} is missing or empty")
    return value.strip()


def _id_text(data: dict[str, object], key: str) -> str:
    """A required id: lower-case letters, digits, - and _ only (it goes into file names)."""
    value = _text(data, key)
    if not _ID_PATTERN.match(value):
        raise FarmFileError(f"{key} may use only a-z, 0-9, - and _ (got {value!r})")
    return value


def _number(data: dict[str, object], key: str) -> float:
    """A required, finite number. true/false are refused even though Python counts them."""
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FarmFileError(f"{key} must be a number")
    if not math.isfinite(value):
        raise FarmFileError(f"{key} must be a finite number")
    return float(value)
