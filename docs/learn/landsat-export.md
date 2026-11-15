# Saving the Landsat passes as one data file for the website

## Overview

The Landsat satellites measure how hot the ground is, about every 8 days over a field.
Many of those passes are not usable, mostly because of cloud. The website will have a page
that shows every pass on a timeline and says, for each skipped one, why it was skipped.
This code makes the file that page reads: `landsat-passes.json`. One entry per pass.

There are no pictures this time. The heat sensor only sees squares of about 100 m, so a
picture of one small field would look like a few blurry blocks. The numbers say more.

## Map

```
landsat.py (find the scenes, read them)  ->   landsat_export.py   ->   landsat-passes.json
thermal.py (count squares, mean/low/high) ->  (collect, check, write)
thermal_report.py (the skip rules)        ->
```

## One set of rules

The printed report (`thermal_report`) already decides two things: which days count as a
pass, and why a pass is skipped. If the website file did its own version of this, the two
could slowly disagree, and a farmer or a funder could see a different story on the page
than in the report. So both decisions live in small shared functions:

- `thermal_report.has_measurement`: does this day count as a pass at all? A scene that only
  covers the land around the field does not.
- `thermal_report.skip_decision`: gives the reason as a short code (`cloud_near_field`,
  `no_quality_information`, `low_valid_share`) and one plain sentence. Cloud is checked
  first, then missing quality information, then too little clear field.

The report and the new export both call them.

## What the data file holds, per pass

- The date, the satellite name(s), how many scenes were merged that day, and the time of the
  newest scene (UTC).
- The numbers exactly as the printed report writes them: whole percent for coverage, clear
  share and valid share, and Celsius with one decimal as text (`36.9`). Python and a web
  browser round a half differently (92.5 becomes 92 in Python, 93 in JavaScript), so the
  page must show these ready-made values and must not round the exact numbers again.
- Square counts: the squares inside the field, how many the satellite measured, how many
  were clear. From these the three shares are worked out (coverage, clear share, valid
  share). They are saved as exact numbers, not rounded.
- Cloud squares and unknown squares (counted over the field plus 2 km around it), and
  squares thrown out because the temperature was not believable.
- The verdict: shown or skipped. A skipped pass has a reason code and sentence and no
  temperatures. A shown pass has mean, lowest and highest temperature, in kelvin and Celsius,
  not rounded.

The top of the file also holds: the date range, the credit text, the notices (surface
temperature is not water stress; this does not show water savings; the outline is invented
for the example field), the limits the code used (2 km margin, 80 % rule, believable
temperature range), the totals, and a version stamp.

## Reading a long date range

The printed report is limited to 92 days per run, and the export keeps the same size
for its pieces (a chosen size, not a limit of the catalogue). So `chunk_ranges` cuts the
range into pieces of 92 days. Both ends of a piece are included, and the next piece starts the day
after, so no day is read twice and none is missed. If any piece fails, the whole run stops.
A day that could not be read is never quietly turned into "no pass". The same goes for a
scene the catalogue lists twice on one day: the run stops, because the report would read
both copies and the export must not do anything different. A field too small to hold
one 30 m square also stops the run, as it does in the report.

## The checker (`load_passes`)

It does more than check that the file opens. It works the numbers out again, with exact
equality (no "close enough"), and refuses the file if anything does not add up: a share that does not match its counts, a verdict that
does not follow from the numbers, a skipped pass with a temperature, a reason sentence that
differs from the report's, totals that do not match the list, dates out of order, a
temperature outside the believable range, a Celsius number that does not match its kelvin.
It also refuses any extra entry, any key written twice (JSON normally keeps only the last
copy and the first would stay hidden in the file), whole numbers too big to be real,
numbers written as true/false or as decimals where a whole number belongs, and dates or
times written in an unusual way. A file made by other code than the code running now is
refused too, unless it is opened for looking only.

## Edge cases that are tested

The export has tests for loose number checks, repeated keys, tiny fields, rounding on the
page and a missing grid. The thermal code also changed because of
this: a picture now remembers WHERE squares were thrown out, so a broken square in the
land around the field no longer makes a day count as a pass.

## What the real data said

On the real example field, 1 Jan 2025 to 30 Sep 2026, the export finds 77 passes: 39 shown,
38 skipped, all 38 for cloud near the field. The older notes said 76 / 38 / 38. The printed
report gives the same numbers as the new file for August and September 2026, and the extra
shown pass is 21 Sep 2026 (Landsat 8). It is not known why the old note missed it. The
notes were written on 4 Oct, so the scene may have reached the catalogue later, or it may
simply have been missed.

## Two bugs

- A share just below the limit (79.995 %) rounds to "80 %", which made the reason read
  "only 80 % (limit 80 %)". Now the number is cut off, not rounded, so it always reads
  below the limit.
- Celsius was first worked out from kelvin that had already been rounded. Then 310.099 K
  gave 36.95 C in the file, which a page rounds to 37.0, while the report prints 36.9. Now
  both are saved unrounded.

## New Python ideas

- `NoReturn` (again): `_fail` always raises, so code after it is only reached when
  things are fine.
- A default value on a frozen dataclass (`field(default_factory=list)`): a new field can
  be added to `ThermalPicture` without breaking every place that already builds one.
- Exact `==` on decimal numbers. Usually risky, but here both sides come from the same
  sums and JSON keeps a decimal number exactly. `math.isclose` ("close
  enough") would let a share off by 0.0000000001 flip a verdict, so `==` is used instead.
- `json.loads(..., object_pairs_hook=...)`: lets the code see every key as it is read,
  which is the only way to notice a key written twice.
