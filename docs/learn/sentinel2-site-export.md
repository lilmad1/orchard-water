# Saving one satellite day as files for the website

## Overview

A script that takes one day on which the satellites photographed a field and saves
everything the website needs as plain files: the colour photos, a coloured map of what the
software saw (cloud, soil, vegetation), the day's picture put together, and one data file
(`data.json`) with the facts for every square. The website only shows these files. It never
talks to the satellite service.

## Map

```
earth_search.py (read the photos)  ->  site_export.py  ->  a folder of files
visual.py (put the colour picture together)  ->           photo-0.png, photo-1.png,
ndvi.py (which photo each square came from)  ->           labels-0.png, labels-1.png,
ndvi_report.py (the 80 % rule)  ->                        assembled.png, labels-assembled.png,
                                                          data.json
```

## File by file

- `site_export.py` (new). `build_bundle` reads everything for one day. `write_bundle`
  writes the pictures first and `data.json` last. `load_bundle` is a checker: it refuses a
  folder where a picture is missing or changed, where a list has the wrong size, or where a
  square names a photo that does not exist.
- The command is `python -m orchardwater.site_export --field ... --day ... --out ... --credit "..."`.
  The `--credit` text is required. The script does not invent credit wording: that is left
  to the operator.

## What the data file holds

- Facts that must not be guessed: the day, the photo names and times (UTC), the credit text.
- For every square in the field: which photo it came from, whether an older photo replaced a
  cloudy square, the scene class, whether it counts as clear, and its greenness number (NDVI),
  or `null` where it is not clear or lies outside the outline.
- The day's verdict (shown or skipped) and the limit it was judged against (80 %).
- The notices every page must carry: greenness is not water stress, this shows what the
  satellite saw and not water savings, and, for the example field only, that the outline is
  invented.
- A version stamp: the package version, a fingerprint of the source files (a long code that
  changes if the code changes), and a checksum for each picture file.

## Why the pictures have no frame

The pictures are pure photos. `data.json` says where the field sits inside them
(`field_box`), and the page draws the box. This way no pixel of the field is ever changed,
so the picture and the data cannot disagree.

## New Python ideas

- A checksum. `hashlib.sha256(bytes).hexdigest()` turns a file into a short code. If
  even one byte of the file changes, the code changes. The loader compares the saved code
  with the file.
- `NoReturn`. A function marked `-> NoReturn` always raises an error and never returns.
  The `_fail` helper uses it so the checker knows the code after it only runs when all is
  well.
- `sort_keys=True` in `json.dumps`. Writes the keys in the same order every time, so two
  runs on the same input give byte-identical files.

## A worked example (29 June 2026, example field)

- Newest photo (S2A, 11:33): 94 of 1326 field squares clear = 94 / 1326 = 0.0709.
- Older photo (S2B, 11:22): 1326 of 1326 clear = 1.0.
- Put together: 94 squares from the newest photo, 1232 from the older one. 1326 clear in
  total, so the day's valid share is 1326 / 1326 = 1.0, which is above the 0.8 limit, so the
  verdict is "shown".

## Check questions

1. Why does `data.json` say where the field sits inside the pictures, instead of the
   pictures having a frame drawn around the field?
2. What does the checksum protect against, and what does it not protect against?
3. Why is `--credit` required and not given a default text?
4. A square's `ndvi` is `null`. Name two reasons that can happen.

### Answers

1. So no field pixel is ever changed or covered. The photo stays exactly what the satellite
   saw, and the page draws the box from the numbers.
2. It catches a picture that was changed or replaced after saving. It does not prove that
   the numbers in `data.json` describe the picture; that is why `load_bundle` also checks
   sizes and ranges, and why the tests decode the pictures and compare them with the record.
3. The exact wording of the Sentinel credit is a launch check that only the operator can decide.
   A default text would be an invented claim.
4. The square lies outside the field outline, or it is not clear (cloud, shadow, thin cirrus,
   no data, or no NDVI can be worked out).
