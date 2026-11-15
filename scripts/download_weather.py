#!/usr/bin/env python3
"""Download daily weather from the free Open-Meteo archive API into a CSV (stdlib only).

Columns: date, et0_mm (FAO-56 Penman-Monteith), precip_mm, tmax_c, tmin_c.
src/orchardwater/weather.py only fetches the hourly forecast window, so this script does
its own (small) archive request. No API key. Open-Meteo's free API is for
non-commercial use only; see NOTICE.

Example:
    python scripts/download_weather.py --lat 30.35 --lon -9.45 \
        --start 2025-06-01 --end 2025-06-10 --out weather.csv
"""

import argparse
import csv
import json
import sys
import urllib.parse
import urllib.request

URL = "https://archive-api.open-meteo.com/v1/archive"
ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0), https://open-meteo.com"
DAILY = [
    "et0_fao_evapotranspiration",
    "precipitation_sum",
    "temperature_2m_max",
    "temperature_2m_min",
]


def fetch(lat: float, lon: float, start: str, end: str) -> dict:
    query = urllib.parse.urlencode(
        {
            "latitude": lat,
            "longitude": lon,
            "start_date": start,
            "end_date": end,
            "daily": ",".join(DAILY),
            "timezone": "auto",
        }
    )
    with urllib.request.urlopen(f"{URL}?{query}", timeout=30) as response:
        return json.load(response)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    p.add_argument("--out", default="weather.csv")
    a = p.parse_args()
    try:
        daily = fetch(a.lat, a.lon, a.start, a.end)["daily"]
    except Exception as error:
        print(f"Download failed: {error}", file=sys.stderr)
        return 1
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "et0_mm", "precip_mm", "tmax_c", "tmin_c"])
        w.writerows(zip(daily["time"], *(daily[k] for k in DAILY), strict=True))
    print(f"Wrote {len(daily['time'])} days to {a.out}")
    print(ATTRIBUTION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
