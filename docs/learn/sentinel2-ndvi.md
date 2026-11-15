# Greenness (NDVI) from Sentinel-2, for one field

## Overview

Until now every number came from weather. This is the first number that
comes from a satellite. The tool takes a field outline and a range of dates.
For each day the satellite photographed the field, it prints how green every
10 m by 10 m square is, plus a small text map of the field. It is shadow mode:
the numbers are for the operator to look at. They never go on the farmer card, and
nothing says greenness means the field lacks water. That has not been checked
against real fields yet.

## Map

```
fields/example.geojson   the field outline (drawn on geojson.io)
    -> field.py          reads the outline, works out its size and which squares are inside it
    -> earth_search.py   asks the catalogue which pictures cover the field, reads only the field's pixels from the free copy on AWS
    -> ndvi.py           checks the picture, does the greenness math and the cloud rule
    -> ndvi_report.py    the command: loops over days, prints one block per day
```

Run:
`python -m orchardwater.ndvi_report --field fields/example.geojson --from 2026-09-07 --to 2026-09-10`

## File by file

- `field.py` (new). Reads the GeoJSON outline and checks it is one closed
  shape with no holes, in longitude/latitude order. It works out the size of the
  box around the field in metres. It also has `inside_mask()`, which says, for a
  picture of a given size, which squares have their centre inside the outline.

- `earth_search.py` (replaces `copernicus.py`). The only file that
  talks to the satellite catalogue and to AWS, like `weather.py` does for weather.
  It needs no account and no key. `search_items()` asks the catalogue once for all
  pictures over the field in the date range and groups them by day.
  `scale_and_offset()` reads from each picture's own information how to turn the
  stored whole numbers into reflectance (number x 0.0001 - 0.1). `read_band()`
  reads only the field's pixels from one satellite file and lays them on a grid
  of 10 m squares. `picture_for_day()` does that for red, near-infrared and the
  scene class, and when two pictures of the same day overlap, the more recent one
  wins.

- `ndvi.py` (new). No internet. `picture_from_bands()` checks four grids and
  turns them into a Picture: a square with a missing or negative light value is
  counted as dropped, and a bad scene label is refused. `ndvi()` does the greenness
  formula for one square. `is_clear_square()` is the cloud rule: only squares
  labelled vegetation (4) or bare soil (5) count. `summarize_field()` gives the
  day's summary: coverage, clear share, and the mean, lowest and highest NDVI.

- `ndvi_report.py` (new). The command. It prints one block per day as soon as
  that day arrives, so if day 40 fails, days 1 to 39 are still on screen. It
  skips days with no picture, prints a WARNING when the satellite saw only part of
  the field, and skips a day (no NDVI, a line saying why) when less than 80 % of
  the field is clear and valid.

- `docs/sentinel2-reference.md` (new). The facts: where the data comes from,
  the offset, scene classes, the formula, what the real September data looked
  like, and what is still unchecked.

- Tests. `test_field.py`, `test_earth_search.py`, `test_ndvi.py` and
  `test_ndvi_report.py`. The catalogue is faked and the satellite files are tiny
  made-up ones, so no normal test needs the internet. One extra test,
  `test_earth_search_live.py`, calls the real internet. It is skipped unless
  run `python -m pytest -m network -v -s`.

## Decisions

- Data source: free Sentinel-2 copy on AWS (Earth Search).
  The first version used the Copernicus Data Space, which needs an account and has a free
  quota with unclear rules for commercial use. The data is the same ESA product.
  The AWS copy is free to use commercially, with no account, key or quota. The result was
  checked against the old way on 7 and 10 September: same days, same
  mean, lowest and highest NDVI, and 2 squares in 1,326 differing by one digit in
  the text map. Landsat (heat) comes from a different place, because on
  AWS the person who downloads Landsat pays.
- Skip days under 80 % valid. Thin cloud edges and shadow darken
  squares, which look "less green", which would read as stress and push towards
  more water. The goal is to have farmers water as little as they safely can, so a hazy
  day is skipped. The price: some partly cloudy days with usable patches are not
  shown.
- Strict cloud rule: keep only vegetation and bare soil. Real
  September 2026 pictures near Agadir showed only five scene classes, and
  strict and loose gave the same answer. Strict is the safer choice if a dusty
  day shows up later. The price: some real squares may be dropped.
- Polygon footprint mask. A picture with most of the field missing could
  still print "clear 100%", so coverage is reported separately.
- Streaming output. A long run could lose everything on a late failure, so
  each day is printed as soon as it is ready.

## New Python ideas

- `frozenset({4, 5})`: a list of values that cannot change, made for fast
  "is this in the list?" questions. `scl in CLEAR_SCL_CLASSES`.
- A generator with `yield`: `report_parts()` hands over one piece at a time
  instead of returning one big text at the end. The caller can print each piece
  the moment it exists.
- A function inside a function: `fetch_day` in `main()` is defined inside
  `main()` so it can use `found` and `field` without passing them every time.
- `numpy` arrays and `.tolist()`: `numpy` is a fast grid-of-numbers tool.
  `rasterio` returns each satellite band as one, and each band is turned into a
  plain list of rows so the rest of the math is simple loops.
- A True/False grid as a filter: `take = photographed & ~filled` is a grid of
  True and False. `red[take] = new_red[take]` changes only the squares where
  `take` is True. `&` means "and" and `~` means "not", square by square. This is
  how a more recent picture fills the squares the older one left empty.
- `READ_SETTINGS`: `f(settings)` hands every entry of a dictionary to the
  function as a named setting, so the list of reading settings sits in one place
  with a comment.
- A function passed as a value: `search_items(..., search=fake)` can be given
  another function to use instead of the real internet search. That is how tests
  run without internet.
- `raise ... from None`: shows only the plain message, not a long chain of
  internal errors.

## The math

NDVI says how green a spot is. Plants reflect a lot of near-infrared and little
red, so:

```
NDVI = (near-infrared - red) / (near-infrared + red)
```

Worked example, red 0.08 and near-infrared 0.40 (the numbers are the share of
sunlight the ground sends back):

```
(0.40 - 0.08) / (0.40 + 0.08) = 0.32 / 0.48 = 0.667
```

Dense healthy leaves are about 0.6 to 0.9. Bare soil is about 0.1 to 0.2.

Coverage and clear share are two different numbers. Say the field has 100
squares inside the outline. The satellite photographed 60 of them (coverage
60%). Of those 60, 45 are vegetation or bare soil and 15 are cloud (clear share
45 / 60 = 75%). Always read them together: "clear 75%" says nothing about the 40
squares the satellite never saw.

A square counts as inside the outline when a line drawn east from its centre
crosses an odd number of outline edges.

What the real September data showed near Agadir (example field, made up): the
satellite passed on 14 days, 5 of them were cloud over the whole field, and on
clear days the mean NDVI was about 0.27 to 0.29, mostly bare-soil squares. The
outline is made up, so this says nothing about a real orchard.

## Check questions

1. Why is a cloudy day skipped with a line saying why, instead of showing an
   average NDVI of 0?
2. The report says "coverage 50%, clear 100%". What does that mean, and why is
   "clear 100%" alone misleading?
3. Why does the cloud rule keep bare soil (class 5) as well as vegetation?
4. A stored red number is 1500, with scale 0.0001 and offset -0.1. What is the
   reflectance, and why must the offset come from the picture's own information
   and not be typed into the code?
5. Red 0.10 and near-infrared 0.30: what is the NDVI?

Answers

1. A cloud square is not a measurement of the ground. Averaging it in would make
   a healthy field look dead. So those squares are left out, and if none are
   left the mean is "none", not 0.
2. The satellite photographed only half of the field, and the half it did see was
   all clear. "Clear 100%" only speaks for the photographed part, so a small
   patch could look like a full-field result. Coverage says how much of the
   field the number is about.
3. Bare soil is a real, clear observation. An orchard has soil between the
   trees, so dropping it would leave only the tree squares and hide how open the
   field is. Only things that are not a view of the ground (cloud, shadow, snow,
   water, broken data) are dropped.
4. 1500 x 0.0001 - 0.1 = 0.15 - 0.1 = 0.05. The offset is a different number for
   different processing versions (older pictures have none). A typed-in offset
   that is wrong shifts every red and near-infrared value by the same amount, and
   NDVI would still look believable but be wrong. So the code reads it from each
   picture, and refuses to run if it is missing.
5. (0.30 - 0.10) / (0.30 + 0.10) = 0.20 / 0.40 = 0.5.
