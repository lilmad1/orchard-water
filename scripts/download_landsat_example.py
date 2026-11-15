#!/usr/bin/env python3
"""List Landsat 8/9 scenes over a field (ids and dates only; no pixels downloaded).

Thin wrapper around orchardwater.landsat.search_items, which searches the free Microsoft
Planetary Computer STAC catalogue (no key needed for searching). Needs `pip install -e .`.

Example:
    python scripts/download_landsat_example.py --field fields/example.geojson \
        --start 2025-06-01 --end 2025-06-30
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from orchardwater import landsat
from orchardwater.field import load_field

ATTRIBUTION = "Landsat Collection 2 Level 2: NASA/USGS, public domain."


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--field", type=Path, default=Path("fields/example.geojson"))
    p.add_argument("--start", type=date.fromisoformat, required=True)
    p.add_argument("--end", type=date.fromisoformat, required=True)
    a = p.parse_args()
    try:
        by_day = landsat.search_items(load_field(a.field), a.start, a.end)
    except ValueError as error:
        print(f"Failed: {error}", file=sys.stderr)
        return 1
    for day in sorted(by_day):
        for item in by_day[day]:
            cloud = item.properties.get("eo:cloud_cover")
            print(f"{day}  {item.id}  {item.properties.get('platform')}  cloud={cloud}")
    print(f"{sum(len(v) for v in by_day.values())} scenes on {len(by_day)} days")
    print(ATTRIBUTION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
