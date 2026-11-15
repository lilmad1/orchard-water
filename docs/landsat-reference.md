# Landsat surface temperature: reference

Read this before changing `landsat.py`, `thermal.py` or `thermal_report.py`.
Everything here is for SHADOW MODE: numbers for the operator to look at. Surface temperature is
not water stress and not the air temperature, and nothing here goes on the farmer card.

Facts marked (checked) were read from the live catalogue or tested live on the date given.
Facts marked TODO(verify) are not confirmed.

## Where the pictures come from

(checked live 2026-10-04) Microsoft Planetary Computer, STAC catalogue
`https://planetarycomputer.microsoft.com/api/stac/v1`, collection `landsat-c2-l2`
(Landsat Collection 2 Level-2, the USGS surface-temperature product). Searching needs no
account. Reading a file needs a short-lived read key added to the end of its web address;
the `planetary-computer` package fetches it (`planetary_computer.sign_url`), keeps it, and
renews it when under a minute is left. Landsat is NOT taken from AWS: there the person who
downloads pays.

- Landsat 8 and 9 each pass a point every 16 days, 8 days apart, so together about every
  8 days. Over Agadir (path 203, row 039, UTM zone 29N, EPSG:32629 like Sentinel-2) passes
  were found on 20 Aug (L8), 28 Aug (L9), 5 Sep (L8), 13 Sep (L9) and 29 Sep (L9) 2026.
  Most days have NO pass. (checked live 2026-10-04) 21 Sep 2026, the expected L8 pass, is
  missing because the whole path 203 has no scene that day (0 of the normal 33 rows in the
  catalogue), while Landsat 8 did image other places on 21 Sep. So it is not our field or our
  filters. TODO(verify): whether the satellite did not image that path or the scene is not yet in
  the catalogue.
- The passes are at about 11:10 UTC (items on 20 Aug to 29 Sep), midday local time.
- Files (assets) we read: `lwir11` (surface temperature) and `qa_pixel` (quality flags).
- Only Landsat 8 and 9 are read (`landsat.SUPPORTED_PLATFORMS`). The collection also holds
  Landsat 4, 5 and 7, whose temperature file has another name, so the search asks the catalogue
  for `platform in [landsat-8, landsat-9]` and the code drops anything else again. (checked live
  2026-10-04: June to August 2013 gave 7 items with Landsat 7 and 5 without it.) Before
  Landsat 9 (launched 2021) the revisit was 16 days, not 8; a 2013 run showed one pass per
  16 days.

## Temperature

- `lwir11` holds whole numbers (uint16). Kelvin = stored number x scale + offset.
  (checked live 2026-10-04) `raster:bands` lists scale 0.00341802 and offset 149 and
  nodata 0. The code reads them from each picture (`earth_search.scale_and_offset()`) and
  refuses to guess if they are missing. Never type them in. A stored 0 means "no data".
  Hand check: stored 51500 = 51500 x 0.00341802 + 149 = 325.028 K = 51.878 C.
- Celsius = kelvin - 273.15.
- A temperature outside 200 to 400 K is dropped and counted (`thermal.MIN_KELVIN`,
  `MAX_KELVIN`). TODO(verify): this is a sanity range chosen here, not a documented one.
- (checked live) A real read over the made-up example field gave 321 to 327 K (48 to 54 C)
  on 13 Sep, plausible for dry ground at midday. On the 5 passes: mean 44.9 C (20 Aug),
  44.4 C (28 Aug), 52.2 C (13 Sep), 47.7 C (29 Sep); 5 Sep was skipped for cloud.
- This is the temperature of the ground and the leaves ("surface temperature"), not the air.
  It depends on sun, wind, soil and the time of day. Comparing it with the air temperature
  or a reference is later work; the report only prints it.

## How sharp it is

The heat sensor really sees squares of about 100 m. The files are delivered on a 30 m grid
(`SQUARE_SIZE_M = 30.0`), but the numbers are smoothly blended from the 100 m readings
(a live read of 143 squares gave 127 different values), so the 30 m grid looks more
detailed than it is. A 1 ha field holds about one real reading. The report therefore prints
one set of numbers per field, no text map, and says how many 100 m readings the field area
is worth (`ThermalSummary.real_readings` = the outline's area in m2 from `field.area_m2`
divided by 10,000 m2; the 12.8 ha example field is worth 12.8). It is NOT worked out from the
number of grid squares: those are fitted into the field's box and are a little smaller than
30 m, so counting them would overstate the area.

## Quality flags and the cloud rule (`qa_pixel`)

(checked live 2026-10-04, from the catalogue's own band description) The quality number
packs yes/no flags into bits: 0 fill, 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow,
5 snow or ice, 6 clear, 7 water, 8-9 cloud confidence, 10-11 shadow confidence, 12-13 snow
confidence, 14-15 cirrus confidence. A completely clear square reads 21824 (bit 6 set and
the "low confidence" value in each confidence pair).

The rule is "strict": a square counts only if it has a temperature, bit
6 (clear) is set, none of bits 0, 1, 2, 3, 4, 5, 7 is set (`thermal.BLOCKING_FLAGS` = 191),
AND none of the four confidence pairs is 2 (medium) or 3 (high). The last part is needed
because bit 6 only says the cloud and dilated-cloud flags are off, not that the
satellite is sure, so a square with medium cloud confidence (for example the number 22080) can
carry the clear flag. A fully clear square has 1 (low) in each pair; 0 (none) also passes.
Why strict: cloud and cloud shadow read too COLD, and a too-cold field looks
unstressed, which could lead to too little water, the unsafe direction. Water is dropped
too (a reservoir is not the crop), like SCL class 6 in the Sentinel-2 rule.
(checked 2026-10-04 against USGS's own page "Landsat Collection 2 Quality Assessment Bands",
as well as the live catalogue) The bit meanings and the confidence encoding
are as listed above: bit 6 "Clear" only says the cloud and dilated-cloud bits are not set; a
confidence of 2 is "reserved" and unused for shadow, snow and cirrus. Not used,
on purpose: the distance-to-cloud layer (`cdist`) and the temperature uncertainty layer
(`qa`), because they need two more files per pass and thresholds we cannot yet source.
Also unchecked: Saharan dust and plastic greenhouses.

## Cloud near the field (the margin)

Measured on 2026-10-04 with live data. Every temperature value is blended
from about 100 m of ground, so a square that is itself clear but lies next to cloud or cloud
shadow is still too cold. Three cloudy passes (29 Mar, 30 Apr, 8 May 2026) were compared with
clear ones: squares that passed the strict rule but sat 1-2 squares (30-60 m) from a flagged
square read 2.1, 9.8 and 3.4 K too cold, and 1.2, 5.4 and 2.0 K too cold at 120-210 m (the
clear passes were 16-35 days apart, so land change adds some noise, but the distance pattern
held in all three). A cloud or shadow just OUTSIDE the field's box is never seen if only the
box is read, and the 80 % share lets up to 20 % of the field be shadow. A too-cold field looks
unstressed, the unsafe direction.

So `landsat.picture_for_day` reads the field's box plus `landsat.BUFFER_M` = 2000 m (2 km) on every
side (`landsat.view_field`, built with `field.with_margin`; the outline and its area are
unchanged), and the report skips a pass if ANY cloud, thin cirrus or cloud shadow shows up
anywhere in that area (`thermal.is_cloud_square`: flags dilated cloud, cirrus, cloud, shadow, or
a medium or high confidence in the cloud, shadow or cirrus pair; water, snow and missing
neighbours do not count as cloud). Reading stops once every square of that bigger area is
filled, since a gap in the margin could hide a cloud. TODO(verify): 2 km is an estimate from
the data below, not a tested optimum; the area is a box, so on a long, thin or tilted outline
it reaches further than 2 km at the corners, which only makes the check stricter. Not used: the
`cdist` layer (distance to cloud); it is not known whether it covers cloud shadow.

Third area, run with the same method (`bias.py`, kept in the session scratchpad only):
Massa, 30.05 N -9.30 E, 2025-01 to 2026-09, 18 partly cloudy passes. Median excess cold, over
passes: 0-60 m -5.5 K, 60-120 m -4.5, 120-210 m -3.5, 210-300 m -3.0, 300-450 m -2.4, 450-600 m
-1.9, 600-900 m -1.6, 900-1500 m -1.25, 1500-3000 m -0.5 (zero beyond 3 km by construction);
single passes far worse (6 Sep 2026: -17 K at 300-450 m with only 2 % cloud in the box). So the
effect appears in a third area, is if anything larger there, and even 2 km leaves a median of
about 0.5 to 1.3 K at its edge. The same caveats apply (reference passes up to 24 days apart,
the baseline is the change seen more than 3 km from cloud). What a wider margin costs on the 76
passes at the example field (shown of 76): 300 m 54, 1 km 42, 2 km 38, 3 km 33, 5 km 25.

Why 2 km and not 300 m (a second analysis, 2026-10-04, live data). It read a 10 km box for
every Landsat 8 and 9 pass from January 2025 to October 2026 in two areas (the coast at the
example field, and inland near Taroudant at -8.87, 30.47) and compared each partly cloudy pass
with the 1-2 nearest cloud-free passes within 24 days, square by square, grouped by distance to
the nearest cloud evidence (the rule in `thermal.cloud_evidence`) and corrected for the change
seen in squares more than 3 km from any cloud. Median excess change (too cold), coast / inland:
300-450 m: -1.1 K / -1.5 K; 450-600 m: -0.7 / -1.35 K; 600-900 m: -0.2 / -1.0 K; 900-1500 m:
-0.05 / -0.8 K. Single passes were worse (up to -4.9 K at 300-450 m). The bias is far wider than
the 100 m sensor blur, probably thin cloud edges or haze that the quality flags miss. With the
old 300 m margin, among accepted ~4 ha windows with cloud 300-600 m away, 22 % (coast) and 34 %
(inland) read more than 2 K too cold; with no cloud within 2 km only 2 % did (the noise level).
On the example field the report then showed four passes with cloud 0.6-1 km away that read
2.4-3.9 K too cold (14 Jun 2025, 12 Oct 2025, 9 Feb 2026, 13 Jan 2025). Not fully proven: the
reference passes were 1-24 days apart, so land change adds noise, but the pattern held in both
areas and every distance group. The margin is therefore 2 km. Result on the 76 real passes at the example
field (2025-01 to 2026-09), checked after the change: 38 shown and 38 skipped (the earlier
analysis counted 39 and 37); 18 or more of the skips are cloud over the whole area, and the partly cloudy
ones it checked look right (10 Mar 2025 read 16.8 C against about 30 C nearby). The rule is not
too strict to be useful, but about half the passes are lost, so a measurement comes about every
16 days instead of every 8. Not used: a warning-only version (print "cloud seen N m away") and
the USGS uncertainty layer `qa` (a weak hint: at least 2.38 K on all four cold passes but also
on four normal ones, unproven as a filter).

Rules that apply to this margin check (each has a regression test):
- Cloud is judged from the quality number alone, never from the temperature. The temperature
  has known gaps, and a cloud flag next to a gap (or next to a temperature rejected as
  unbelievable) must still stop the pass. `ThermalPicture.qa_valid` holds where a quality
  number is available, separately from `data_mask`.
- A quality number of 0, or one with the fill flag set, is "not available" (`thermal.
  quality_available`): fill means outside the picture, and counting it as information would
  hide an older scene's cloud flag. Any such square anywhere in the area (`unknown_squares`)
  makes the report skip the pass ("no quality information for N squares"), because cloud near
  the field cannot be ruled out. This is strict: a field at the edge of a scene's coverage is
  skipped until another scene of the same day fills the gap. Live, none of the four clear
  example passes is affected.
- The catalogue search uses the whole area as a rectangle (`landsat.search_area`), so a scene
  that covers only the margin is found and can supply the quality numbers there.
- Reading stops when the temperature is filled for every square INSIDE the outline and the
  quality number for every square of the whole area (a temperature gap in the margin is
  harmless, only the quality number is used there).
- Same-day scenes are merged separately for temperature and quality, but cloud evidence is
  never thrown away: where an earlier scene supplied the quality number and a later scene read
  says cloud, the square is flagged cloud (`thermal.cloud_evidence`). A scene that is not read
  (because the newer one already has everything) contributes neither temperature nor evidence.

Still open (not fixed, said plainly): on a small field (about 1 ha, one real reading) the
number is mostly the surrounding land, so a dry field next to an irrigated orchard or a farm
water basin can read cool, and the water flag only removes the basin's own squares. The report
prints how many real readings the field is worth, but no limit is applied.

## What the report prints, and when a pass is skipped

`python -m orchardwater.thermal_report --field fields/example.geojson --from 2026-08-20 --to 2026-09-30`
(one line). For each day with a pass: satellite and time, coverage, clear share, valid
share, mean, lowest and highest surface temperature, and how many 100 m readings the field
is worth.

- Coverage = squares the satellite measured / squares inside the outline. Below 100 % the
  report prints a WARNING.
- Clear share = clear squares / measured squares. Valid share = clear squares / ALL squares
  inside the outline.
- A pass is skipped (a line saying why, no numbers) in three cases, checked in this order:
  (1) any cloud, thin cirrus or cloud shadow anywhere in the area read, the field plus 2 km
  around it (see the section above); (2) any square of that area with no usable quality
  number; (3) valid share below `thermal_report.MIN_VALID_SHARE` = 0.8, which now covers the
  other reasons a square is unusable (water, snow, a missing or unbelievable temperature). (checked live 2026-10-04) On the 76 real passes at the example field (2025-01 to 2026-09) the
  0.8 limit decides nothing: all 38 shown passes have 100 % valid, and the 38 skipped ones are 18
  with 100 % valid inside the field (skipped for cloud within 2 km) and 20 with under 20 % valid;
  no pass lies between 20 and 99 %, so any limit from 20 to 100 % gives the same result.
  (checked live 2026-10-09, `landsat_export`) The same range now gives 77 passes: 39 shown and 38
  skipped, all 38 for cloud within 2 km. The extra shown pass is 21 Sep 2026 (Landsat 8,
  mean 50.0 C), which the printed report also shows today. 5 Sep 2026 reads 14 % valid
  with 9369 cloud squares, not the 19 % and 572 above. The cause of the difference from the
  older counts is not known (the catalogue may have changed, or the old counts may have
  been slightly off); the text above is left as it was written.
  TODO(verify): on a field with water, snow or temperature gaps it could matter; 0.8 was copied
  from the Sentinel-2 report (a deliberately strict choice). (checked live 2026-10-04) 5 Sep 2026
  had 19 % valid and 572 cloud squares in the area, and was skipped; the four clear passes
  (20 Aug, 28 Aug, 13 Sep, 29 Sep) have no cloud within 2 km and are shown. A skipped pass says SKIPPED on its first line.
- Where two passes of the same day overlap, the more recent with data wins, and reading
  stops when every square of the area read is filled.
- A pass where every measured square is dropped for an unbelievable temperature is NOT
  counted as "no pass": it prints with coverage 0 % and a WARNING. The number in that
  warning counts the field and the margin around it together.
- If the key service does not answer or answers in a form we do not expect, the report stops
  with a plain message. The message never quotes the answer (it could contain the key).

## Limits and licence

- No account or key of ours. `MAX_DAYS = 92` (from `ndvi_report`) caps a run. A run of 42
  days took about 5 seconds (checked 2026-10-04).
- The `planetary-computer` package sets no network time limit of its own, and its web library
  passes "no limit" explicitly, so a limit set on the connection does not work (proved with a
  local server that never answers; the first version of `landsat.py` got this wrong). So
  `landsat.sign_href` runs the package call in a helper thread and stops waiting after 60 s
  (`SIGNING_TIMEOUT_S`), with a plain error. The abandoned thread is a daemon thread and
  cannot keep the program from ending. The catalogue search has its own 10 s / 60 s limit (`earth_search.CATALOGUE_TIMEOUT_S`) and the file reads use
  the GDAL limits in `earth_search.READ_SETTINGS`.
- The package reads no project file. It reads an optional settings file in the home folder
  (`~/.planetarycomputer/settings.env`) and the optional variable `PC_SDK_SUBSCRIPTION_KEY`;
  neither exists here. (checked 2026-10-04, from the package source, version 1.0.0)
- A signed address carries a temporary key after the "?". `earth_search.read_band` keeps it out
  of our error message, out of the reading library's own text, off the screen (the library's
  log level is raised while an address with a key is read), and off the error chain (the error
  is raised outside the `except` block). Tests cover all four.
- (checked live 2026-10-04) Anonymous Planetary Computer reads were not rate-limited in practice:
  all 76 passes of 2025-2026 were read over a 4 km area in 8 report runs, and again 5 times for
  the margin comparison (about 380 pass reads in total), with no refusal or slowdown. A refusal
  would stop the report with the plain "no read key" message.
- Licence (checked 2026-10-04 on usgs.gov, "Data Citation", "Copyrights and Credits", the FAQ on
  Landsat in the cloud): USGS Landsat data is federally created, in the public domain, free to
  use and redistribute including commercially, with no permission needed. The catalogue lists the
  licence as "proprietary"; that label does not change USGS's own statement. USGS asks for credit
  as a request, not a legal requirement. Its example wording: "Landsat Collection 2 Level-2 image
  courtesy of the U.S. Geological Survey" (the general form is "(product name) courtesy of the
  U.S. Geological Survey"). Use it wherever Orchard Water shows a Landsat-derived number.
- Accuracy: USGS itself documents a known error in the Landsat surface temperature near cloud and
  possibly cloud shadow (it cites Cook et al. 2014), which agrees with the cold bias found here.
  Its `ST_QA` band gives an uncertainty in kelvin from uncertainty values and distance to cloud.
  USGS plans improvements for Collection 3. Nothing about the absolute accuracy of the
  temperature is claimed in this repo. TODO(verify): read the USGS product guide before any
  accuracy claim.

## Sources

- Catalogue and item metadata queried 2026-10-04 (`raster:bands`, `classification:bitfields`,
  passes over Agadir).
- Package source of `planetary-computer` 1.0.0 (token cache, settings), read 2026-10-04.
