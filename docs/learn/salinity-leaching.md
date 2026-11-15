# Salinity leaching requirement

## Overview

Souss-Massa's irrigation water is never perfectly pure. It always carries
some dissolved salt. If a field only ever gets exactly what the trees need,
that salt builds up in the soil over time instead of washing away, and
citrus is sensitive to it. This feature makes the tool add a bit of
extra water on top of the normal amount, enough to flush ("leach") that
salt down below the roots, based on how salty the irrigation water is
and how much salt citrus can tolerate.

## Map

```
--ec-water (new CLI flag: how salty the irrigation water is, dS/m)
    -> fao56.py (leaching_requirement: how much extra fraction is needed)
    -> units.py (gross_mm: net water need / (efficiency * (1 - leaching)))
    -> weekly.py (format_amount: turn the numbers into printed text safely)
    -> printed summary
```

## File by file

- `fao56.py`: new function `leaching_requirement(ec_irrigation_water_ds_m,
  ec_threshold_ds_m)`. Given how salty the water is and how much salt
  citrus can tolerate before it hurts yield, it returns the fraction (0
  to just under 1) of applied water that must drain past the root zone
  to keep the soil safe. New constant `CITRUS_ECE_THRESHOLD_DS_M = 1.7`
  (FAO-56 Table 23, for oranges, `TODO(verify)` since Souss-Massa also
  grows other citrus, and grapefruit's threshold is slightly different).

- `units.py`: `gross_mm()` (already existed, for efficiency losses)
  gained a `leaching_requirement` parameter. The formula becomes
  `gross = net / (efficiency * (1 - LR))`: dividing by a smaller number
  than before, so the gross amount goes up to cover both the pipe/dripper
  losses (efficiency) and the extra leaching water. Defaults to `0.0`, so
  any code that doesn't know its water's salinity still works exactly as
  before.

- `weekly.py`: new required flag `--ec-water` (no default), like
  `--efficiency`; it is a real farm measurement the tool can't guess.
  It also gained `format_amount()`, a small helper used everywhere a number
  gets printed, so a genuinely tiny amount (like 0.04 mm) can never look
  like exactly "0.0" and read as "nothing to do."

## New Python ideas

- A loop that tries increasing precision until something shows up.
  `format_amount()` first tries the normal one-decimal display; if that
  rounds a real, nonzero number down to "0.0", it tries two decimals,
  then three, and so on, stopping as soon as a real digit appears:
  ```python
  for extra_decimals in range(2, 16):
      text = f"{value:.{extra_decimals}f}"
      if float(text) != 0.0:
          return text
  ```

- Scientific notation as a fallback. Even 15 decimal places isn't
  enough for a truly tiny number (like `0.0000000000000001`). Python's
  `:.2e` format switches to "times ten to the power of" notation
  (`1.00e-16`), which can represent a nonzero value no matter how small:
  ```python
  f"{0.0000000000000001:.2e}"  # "1.00e-16"
  ```

## The water math, worked by hand

Say a field needs 20 mm of net water this week, the irrigation
efficiency is 0.9 (90% of pumped water reaches the root zone), and the
irrigation water tests at 1.2 dS/m salinity.

- Leaching requirement: `LR = 1.2 / (5 * 1.7 - 1.2) = 1.2 / 7.3 ≈ 0.164`
  (about 16.4% of applied water needs to drain past the roots to keep
  salt from building up).
- Gross water: `20 / (0.9 * (1 - 0.164)) = 20 / (0.9 * 0.836) = 20 / 0.7524
  ≈ 26.6 mm`, about 6.6 mm more than the old efficiency-only figure
  (`20 / 0.9 ≈ 22.2 mm`), all of it the extra leaching water.

If instead the irrigation water were very salty, say 4.25 dS/m (2.5
times the citrus threshold), the formula would say `LR = 1.0`, meaning
leaching alone cannot keep the soil safe with water this salty no matter
how much extra is applied. The tool refuses to print a number in that
case rather than suggest something meaningless.

## What this does *not* fix (on purpose)

- This assumes a steady state: the salt balance the formula
  predicts is what is reached after many irrigations at this rate, not a
  guarantee from any single week's water.
- The formula assumes citrus's standard root-zone water extraction
  pattern (roughly 40% of water taken from the top quarter of the roots,
  30% from the next quarter, and so on), not separately checked for
  citrus specifically.
- Efficiency must mean losses before water reaches the root zone
  (pipes, dripper/furrow application). If a farm's efficiency number
  already includes deep percolation, combining it with the leaching
  correction would double-count that drainage. The tool can't tell which
  definition a given efficiency number uses, so this is a documented
  assumption, not something it checks.

## Check questions

1. Why does a saltier irrigation water mean the tool asks for
   *more* water, not less?
2. What does the tool do if the water is so salty that leaching alone
   can't keep the soil safe?
3. Why does `format_amount()` sometimes print more decimal places for
   one number than another in the same summary?

Answers:

1. None of that extra water is for the trees. It is water that
   has to pass all the way through and past the root zone to physically
   carry dissolved salt down and out of reach, before the salt has a
   chance to build up in the soil. The saltier the water, the bigger a
   fraction of the total has to be "given up" this way, so the total
   pumped amount has to grow to still deliver the same amount the trees
   actually need.
2. It refuses to give an answer and raises an error instead of printing
   a computed but meaningless number (like "infinite water is needed" or "150% of the water must drain away"). This happens
   at water 2.5 times saltier than citrus's tolerance
   threshold, which is well before the formula's math actually breaks
   (divide-by-zero), because the answer stops making agronomic sense
   before that point.
3. It only adds decimals when the plain one-decimal version
   would hide a real amount by rounding it down to "0.0", which would
   read as "no water needed" when there actually is some, however small.
   A normal-sized number never needs the extra decimals, so most numbers
   in the summary still print with just one decimal place.
