# scripts/

Small command-line helpers that use only free, keyless public sources.
Nothing private is shipped here: no keys, no real farm or field files.

## download_weather.py

Daily FAO-56 ET0, precipitation and min/max temperature to a CSV, from the Open-Meteo
archive API. Standard library only.

    python scripts/download_weather.py --lat 30.35 --lon -9.45 \
        --start 2025-06-01 --end 2025-06-10 --out weather.csv

Licence: data is CC BY 4.0 (attribution printed by the script). The free API is for
non-commercial use only; commercial use needs a paid Open-Meteo plan
(https://open-meteo.com/en/terms).

## download_landsat_example.py

Lists Landsat 8/9 Collection 2 Level 2 scenes (id, date, cloud cover) over a field, via
`orchardwater.landsat.search_items` and the Microsoft Planetary Computer STAC catalogue. It
downloads no pixels. Needs `pip install -e .` first.

    python scripts/download_landsat_example.py --field fields/example.geojson \
        --start 2025-06-01 --end 2025-06-30

Licence: Landsat data is public domain (NASA/USGS).

See NOTICE in the repository root for all third-party credits.
