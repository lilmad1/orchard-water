# Sentinel-2 greenness (NDVI): reference

Read this before changing `ndvi.py`, `earth_search.py` or `ndvi_report.py`.
Everything here is for SHADOW MODE: numbers for the operator to look at. Greenness is
not water stress, and nothing here goes on the farmer card.

Facts marked (checked) were read in official docs or tested live on the date
given. Facts marked TODO(verify) are not confirmed.

## Where the pictures come from

(checked live 2026-10-04) The pictures come from the free Sentinel-2
copy on AWS, served by Element 84's "Earth Search" catalogue
(`https://earth-search.aws.element84.com/v1`), collection `sentinel-2-c1-l2a`.
It is the same ESA product as before, with no account, key or quota. The earlier
source was the Copernicus Data Space (Sentinel Hub Process API); it was replaced
because its free quota terms were unclear. Landsat is not
fetched here (it is handled separately in `landsat.py`, and Landsat on AWS is
paid by the downloader, so it uses a different source).

- Sentinel-2 is a pair of European satellites (2A and 2B, plus newer ones). Each
  point on land is seen about every 5 days, so most days have NO picture. The
  report counts those as "days without".
- We use the "L2A" product: light readings already corrected for the
  atmosphere, so they describe the ground, not the air above it.
- Files (assets) we read, per picture: `red` (B04, 10 m squares), `nir` (B08,
  10 m squares) and `scl` (scene class, 20 m squares). Each is a Cloud Optimized
  GeoTIFF on a public AWS web address, in the map projection of the tile (for
  Agadir: UTM zone 29N, EPSG:32629).
- Numbers are stored as whole numbers (uint16). To get reflectance, the share of
  sunlight the ground sends back (0 = none, 1 = all):
  reflectance = stored number x scale + offset.
  (checked live 2026-10-04) Items of this collection carry, in
  `assets[...].extra_fields["raster:bands"][0]`, a scale of 0.0001 and an offset
  of -0.1 for `red` and `nir`; `scl` has neither. The code reads them from each
  picture (`earth_search.scale_and_offset()`) and refuses to guess if they are
  missing. Never type them in. A stored 0 means "no data". Because of the -0.1
  offset, stored numbers below 1000 give negative reflectance; such squares are
  counted as discarded (see below).
- Several pictures can exist for one day: on 7 September 2026 there were two of
  the same tile (2A and 2B, 11 minutes apart). They are merged square by square,
  the most recent one with data winning. This is also what the earlier service
  did by default.

## How the picture is built (`earth_search.py`)

1. One catalogue search for the whole date range, using the field outline as the
   search shape. Results are grouped by UTC day.
2. For each day, for each picture: read `red`, `nir` and `scl` over the field's
   box only. rasterio downloads just the blocks that box needs, never a whole
   100 MB tile, and converts from the tile's metre map to longitude/latitude.
   Reading is public, so `AWS_NO_SIGN_REQUEST=YES` is set.
3. Resampling is "nearest": each of our squares copies the closest original
   number, with no blending. So a 20 m scene-class pixel becomes four identical
   10 m squares (tested), and no invented class numbers appear.
4. The grid is `grid_size(field)` squares of about 10 m over the field's box:
   row 0 is the north edge, column 0 the west edge. `field.inside_mask()` works
   out which squares have their centre inside the outline.
5. A square is "photographed" when both red and near-infrared have a stored
   number other than 0. Everything else counts as no data.

Checked against the earlier service on 7 and 10 September 2026 over the example
field: the same days had a picture, the same 1,326 squares, the same mean,
lowest and highest NDVI, and 2 of 1,326 squares per day differed by one digit in
the text map (0.2 %).

## NDVI

    NDVI = (NIR - red) / (NIR + red)

Plants reflect a lot of near-infrared and absorb red, so leaves score high.
Typical values: dense healthy leaves 0.6 to 0.9, sparse crop 0.3 to 0.5, bare
soil 0.1 to 0.2, water and cloud near or below 0. `ndvi()` returns None (no
answer) if red + NIR is not above zero or a value is not a real number.

What NDVI is NOT: it does not say a plant lacks water. A stressed plant may
stay green for days. A young orchard with bare soil between trees will read low
while perfectly healthy. Checking NDVI against real fields is the work of later
tasks.

## Scene classes (SCL) and the cloud rule

(checked) The class list, from the Copernicus docs for Sentinel-2 L2A:

| Class | Meaning | Kept by our rule? |
|---|---|---|
| 0 | No data | no |
| 1 | Saturated or defective | no |
| 2 | Dark area pixels | no |
| 3 | Cloud shadows | no |
| 4 | Vegetation | YES |
| 5 | Bare soil | YES |
| 6 | Water | no |
| 7 | Clouds, low probability, or unclassified | no |
| 8 | Clouds, medium probability | no |
| 9 | Clouds, high probability | no |
| 10 | Thin cirrus | no |
| 11 | Snow or ice | no |

The rule is "strict": a square counts only if it was photographed and its class
is 4 or 5 (`ndvi.CLEAR_SCL_CLASSES`). This stays the same after the source
switch: only classes 4 and 5 count, not 6 or 7.

Why strict: the real check for 1 to 30 September 2026 near Agadir (example
field, 14 days with a picture) showed only classes 4, 5, 8, 9 and 10. Classes
2, 3, 6, 7 and 11 never appeared. So on that month strict and loose give the
same answer, and strict is the safer choice if a dusty or odd day appears later.
Five of the 14 days were cloud (class 8 or 9) over the whole field; none had
cloud over only part of it.

TODO(verify): the class list is from the Copernicus docs only. Cross-check with
ESA's own Sen2Cor documentation. Also unchecked: how Sen2Cor behaves on Saharan
dust days and on plastic greenhouses, which are common in Souss-Massa. Look at
the clear-share numbers for such days before trusting them.

## What the report prints, and when a day is skipped

- Each day with a picture prints: coverage, clear share, valid share, the
  mean, lowest and highest NDVI, and a text map.
- Coverage = photographed squares / squares inside the outline. It drops below
  100 % when the satellite photographed only part of the field, and the report
  then prints a WARNING.
- Clear share = clear squares / photographed squares, so it only speaks for the
  photographed part. Read it together with coverage.
- Valid share = clear squares / ALL squares inside the outline. A day is skipped
  (no NDVI, no map, a line saying why) when this is below `MIN_VALID_SHARE`
  (0.8). Reason, from the goal that farmers water as little as they safely
  can: cloud edges and shadow darken squares and make trees look less green,
  which would read as stress and push towards more water, while canopy greenness
  changes over weeks, so skipping a hazy day costs little. TODO(verify): 0.8
  against real fields.
- A photographed square whose red or near-infrared value is missing or below 0
  is treated as not photographed, so coverage drops, the WARNING prints and
  `Picture.discarded_squares` counts it. A scene label that is not a whole
  number 0 to 11 on a photographed square refuses the whole picture. Reflectance
  above 1 is allowed (a bright surface can read slightly above 1).
- A day where every photographed square is dropped is NOT counted as "satellite
  did not pass". It prints as a day with a picture, coverage 0 %, a WARNING that
  says how many squares were left out, and the skipped line.
- Only one day's picture is held at a time. Known limit: the largest picture we
  accept (2,500 x 2,500 squares) becomes plain Python lists and needs several
  hundred MB; real fields are about 1,000 squares.
- The field outline is checked: at least 3 different corners, no edge crossing
  or touching another, and a real area. A bow-tie, a figure of 8 or a flat line
  is refused with a plain message.

## Limits and licence

- No account, key or quota. One run does one catalogue search plus a few file
  reads per day with a picture (about 11 seconds for 4 days, 2 with pictures,
  checked 2026-10-04). `MAX_DAYS = 92` caps a run to keep it small.
- Catalogue results are capped at `MAX_ITEMS = 500` per search.
- TODO(verify): `MAX_SQUARES_PER_SIDE = 2500` is kept from the earlier service,
  not re-derived; it now only protects memory. `field.MAX_SIDE_M` (20 km, 2,000
  squares) stays below it.
- (checked live 2026-10-04, 281 days with a picture at the example field, 2025-01 to
  2026-10) 28 days have two pictures (Sentinel-2A and 2B, 10 minutes apart). The newest wins
  per square even if it is cloudy, as in the earlier service. On 23 of 28 days both agree on
  what is clear and the mean NDVI differs by 0.00 to 0.024. On 5 days the clear shares differ
  (2025-04-05, 2025-08-03, 2026-02-19, 2026-06-29, 2026-09-07): merged keeps the newer (2A)
  look, which was the better one on 3 of them and on 2025-08-03 (139 clear squares against 884 in 2B)
  and 2026-06-29 (94 against all 1,326 in 2B) throws away a clearer picture; that is the safe
  direction (fewer squares, never wrong ones), but data is lost and the day may be skipped by
  the 80 % rule. A merge that prefers the newest CLEAR square per field square would recover it;
  not changed.
- Licence: Copernicus Sentinel data is free to use, including commercially,
  with an acknowledgement, whichever copy it is read from. TODO(verify): the
  exact acknowledgement wording before any Orchard Water page or document shows
  Sentinel-derived numbers.
- Reading from a real network: the live check is
  `python -m pytest -m network -v -s` (skipped in normal runs).

## What the real data looked like (example field, made up, near Agadir)

- 7, 10 and 12 September 2026: 100 % clear, mean NDVI 0.27 to 0.29, lowest about
  0.10, highest 0.66 to 0.78. The text map showed the same patches on all days:
  a greener area in the lower right and a low, bare area on the left.
- The outline is made up, so these numbers say nothing about a real orchard.
  Redo the check on a real outline before drawing any conclusion.

## Sources

- Earth Search catalogue: https://earth-search.aws.element84.com/v1 (queried
  2026-10-04; asset names and `raster:bands` values read from live items).
- https://documentation.dataspace.copernicus.eu/APIs/SentinelHub/Data/S2L2A.html
  (SCL classes), fetched 2026-09-30.
