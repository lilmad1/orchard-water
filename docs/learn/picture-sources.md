# Remembering where each square came from, and fetching colour photos

## Overview

On a day when the two Sentinel-2 satellites both pass over a field, the code can put
the field together from both photos. Before, it kept only the result and forgot which
photo each square came from. Now it remembers. The code can also fetch the
real colour photos and put a colour picture together using that memory. With both,
the website has what it needs to show "click a square and see why".

## Map

```
earth_search.py: picture_for_day  ->  Picture (numbers + source record)
earth_search.py: read_visual      ->  colour photos, one per picture
                      \                       /
                       visual.py: assemble_visual  ->  one colour picture
ndvi.py: square_source  ->  "S2B, an older picture, because ..."
```

Nothing here writes a file for the website yet. That is the job of `sentinel2-site-export.md`.

## File by file

- `ndvi.py`: the `Picture` record got three new fields. `picture_ids` is the list of
  photo names, newest first. `source_picture` is a grid with one number per square: 0 is the
  newest photo, 1 the next older one, -1 means no photo had data. `replaced_unclear` is a
  yes/no grid: did an older photo replace an unclear square? `square_source(picture, row, col)`
  turns those into a photo name and one sentence.
- `earth_search.py`: `picture_for_day` fills the new fields while it merges, so the record
  cannot disagree with the result. `read_band` can now read any band of a file, and can add a
  margin of extra squares around the field. `read_visual` reads the three colour bands
  (red, green, blue) and stacks them into one colour picture.
- `visual.py` (new): `assemble_visual` builds one colour picture from several. It copies the
  record: each field square takes its colour from the photo the record names. The margin
  around the field always takes the newest photo.

## New Python ideas

- Keyword-only arguments. In `read_band(..., *, band=1, margin_squares=0)` the `*` means
  the two options after it must be written with their names, for example `band=2`. This keeps
  old calls (which use positions) working exactly as before.
- A boolean mask. `from_here = source == 1` makes a grid of True/False. `box[from_here] =
  other[from_here]` then copies only the True squares. It replaces a loop over every square.
- A view. `box = result[3:9, 3:11]` does not copy: it is a window into `result`. Writing
  into `box` changes `result`. That is why `assemble_visual` works on a window.
- The `@` sign for moving a grid. `field_transform @ Affine.translation(-3, -3)` slides the
  grid's start 3 squares up and 3 squares left, without changing the size of any square.

## The math: one worked example

The test field is 8 squares wide and 6 squares tall. Take a margin of 3 squares on every side.

- Width with margin: 8 + 3 + 3 = 14. Height with margin: 6 + 3 + 3 = 12.
- The field's own squares are in rows 3 to 8 and columns 3 to 10 (counting from 0).
- A read with the margin, cut back to rows 3 to 8 and columns 3 to 10, must equal a read with
  no margin. A test checks this with a picture where every square has its own number, so a
  shift of even one square would show.

## Check questions

1. In `source_picture`, what do the numbers 0, 1 and -1 mean?
2. Why must `assemble_visual` handle -1 on purpose, and what colour does that square get?
3. A real, very dark pixel can have a colour value of 0. Why is it wrong to use 0 to decide
   that a square was "not photographed"?
4. Why does `read_band` use `*` before `band` and `margin_squares`?

### Answers

1. 0 is the newest photo of the day, 1 is the next older one, -1 means no photo had data.
2. In Python, index -1 means "the last item", so without a special rule a -1 square would
   quietly take its colour from the last photo. It is made black instead, so the picture
   never shows a colour the satellite did not record there.
3. A true 0 and "no data" look the same in the file. The code that decides coverage uses the
   red and near-infrared photos (`Picture.data_mask`), not the colour values.
4. So that the old calls, such as the Landsat code passing a 30 m size by position, keep
   working, and so the new options are always written with their names.
