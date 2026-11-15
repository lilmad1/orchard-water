# FAO-56 weekly irrigation need for one citrus field

## Overview

A command-line tool that answers one question: for one citrus field, how
much water does it need this week, and when? It fetches a free weather
forecast, runs a simple day-by-day soil "bank account" (FAO-56's water
balance), and prints the millimetres, cubic metres and pump-hours to
apply. Every constant it uses (crop coefficient, root depth, soil values) is
a documented FAO-56 table value, marked as unconfirmed until the operator checks it
against the real orchards.

## Map

How the data flows from a weather API to a printed sentence:

```
weather.py (Open-Meteo API)
    -> fao56.py (FAO-56 constants + small formulas: ETc, TAW, RAW, effective rain)
    -> water_balance.py (day-by-day soil depletion, the "deadline" the soil hits RAW)
    -> units.py (mm -> m3 -> pump hours)
    -> weekly.py (ties it all together, builds the printed summary, the CLI entry point)
```

## File by file

- `weather.py`: the only file that talks to the internet. Calls
  Open-Meteo (free, no API key) for 7 past days + 7 forecast days of ET0
  (reference evapotranspiration) and rain, for one lat/lon. Rejects a day
  with a missing, negative, or non-finite (NaN/infinity) value instead of
  passing it through, a broken weather reading should stop the tool with a
  clear error, not silently turn into a nonsense "0 mm needed" result.

- `fao56.py`: the crop and soil science, with no weather or CLI code in
  it. Holds the constants (`CITRUS_KC`, `CITRUS_ROOT_DEPTH_M`,
  `CITRUS_DEPLETION_FRACTION_P`, `SOIL_TYPES`) and the small formulas that
  only need those constants: `etc_mm_per_day` (crop water use),
  `total_available_water_mm` / `readily_available_water_mm` (how much water
  the soil can hold, and how much of that the crop can safely use before
  stress), and `effective_rain_mm` (how much of a day's rain actually
  reaches the roots instead of evaporating off the surface).

- `water_balance.py`: the day-by-day soil "bank account." Starts each
  week assuming the soil is full (the "top-up" method, valid for drip
  irrigation, not for flood, where soil dries out over more than a week).
  Each day: subtract effective rain, add the day's crop water use, clip to
  between 0 and TAW (the soil's total capacity). `irrigation_deadline` scans
  the week for the first day depletion would reach RAW (the no-stress
  limit) and works out the day-before amount that would prevent it, unless
  that later day's own water use alone is already bigger than RAW, in which
  case no earlier top-up could have prevented it, and the tool says so
  instead of naming a false "fix."

- `units.py`: three small, named conversions: mm of water over a
  hectare to cubic metres, cubic metres to pump-hours at a given flow rate,
  and net water needed to gross water to pump (accounting for irrigation
  efficiency, pipe and dripper losses). Each one rejects an impossible
  input (zero or negative area, pump flow, or efficiency) right where the
  conversion happens.

- `weekly.py`: the CLI entry point (`python -m orchardwater.weekly ...`).
  Wires the other four files together: fetch weather, run the water
  balance, convert to real-world units, and print a short summary with an
  assumptions list. This is also where every warning about what the tool
  *doesn't* know lives, most importantly, that a day or week is named as a
  water-need total, never as a confirmed pumping schedule.

## New Python ideas

- `@dataclass(frozen=True)`: a plain data container that can't be
  changed after it's created (`frozen=True`). Used for `DailyWeather`,
  `SoilType`, and `DayResult` so a day's ET0/rain, or a day's computed
  depletion, can't accidentally be edited somewhere downstream.
  ```python
  @dataclass(frozen=True)
  class SoilType:
      name: str
      field_capacity: float
  ```

- `str | None` and checking for `None`: a function parameter or return
  value that's sometimes absent. `IrrigationDeadline.amount_mm: float |
  None` means "a number, or nothing to report", the caller must check
  which one it got before using it.
  ```python
  if deadline is not None and deadline.amount_mm is None:
      ...  # handle the "no safe amount" case
  ```

- `math.isfinite()`: checks a number isn't `NaN` (Not a Number, what
  invalid math such as `0/0`) or infinity. A plain `x > 0` check
  lets both of those slip through, since neither compares normally against
  zero.
  ```python
  if not (math.isfinite(ha) and ha > 0):
      raise ValueError("ha must be a positive, finite number")
  ```

- `zip(..., strict=True)` (used in `weather.py`), pairs up multiple
  lists item-by-item, but raises an error if the lists are different
  lengths instead of silently dropping the extra items. Used to line up
  dates, ET0 values, and rain values from the weather API response.

## The water math, worked by hand

One field: 12 ha, sandy loam soil, pump flow 25 m³/h, 90% efficiency.

1. Soil capacity. Sandy loam: field capacity 0.23, wilting point 0.11,
   root depth 1.2 m. TAW = 1000 × (0.23 − 0.11) × 1.2 = 144 mm. RAW = 0.5
   × 144 = 72 mm (the no-stress limit).
2. Daily crop use. Say ET0 = 6 mm/day, Kc = 0.65 → ETc = 0.65 × 6 =
   3.9 mm/day. No rain this week, so effective rain = 0 every day.
3. A week of depletion, starting from 0 (soil full): 3.9, 7.8, 11.7,
   15.6, 19.5, 23.4, 27.3 mm. Never reaches 72 mm RAW, so the full week's
   depletion (27.3 mm) is the net amount needed.
4. Efficiency loss. Gross water to pump = 27.3 / 0.9 = 30.33 mm
   (kept at full precision for the next steps; only the final printed
   numbers get rounded to one decimal).
5. mm → m³. 1 mm over 1 ha = 10 m³ (1 mm = 0.001 m depth; 1 ha =
   10,000 m²; 0.001 × 10,000 = 10). So 30.33... mm × 12 ha × 10 = 3,640 m³
   exactly.
6. m³ → pump hours. 3,640 ÷ 25 m³/h = 145.6 hours.

That last number (145.6 hours) is a total, not a schedule, the tool deliberately never
claims when during the week to start pumping, or whether 145 hours fits in
the time available. That's a documented limitation (see
`ASSUMPTIONS_TEXT` in `weekly.py`), not an oversight.

## Check questions

1. Why does the tool assume the soil starts full at the beginning of every
   week, and when would that assumption be wrong?
2. If the forecast says the soil will hit RAW (the no-stress limit) on
   Thursday, why does the tool give Wednesday's amount,
   not Thursday's?
3. The tool prints a warning that it "cannot confirm a safe pumping
   window." What real-world problem does that warning exist to prevent?

Answers:

1. It's the "top-up" method, which fits drip irrigation where farmers water
   often and the soil rarely gets very dry. It would be wrong for flood
   irrigation, where a field might go more than a week between waterings;
   that needs a different method (the flood method in
   `docs/fao56-reference.md`).
2. Depletion values are measured at the *end* of each day. If Thursday's
   end-of-day depletion has already reached RAW, the crop was stressed
   *during* Thursday, which is too late. Wednesday is the last day where topping up
   the soil (by Wednesday's own depletion amount) would have kept Thursday
   from ever crossing the limit.
3. A pump job takes real hours to run, and this tool only knows daily
   totals. It has no idea what time of day it is, or how many hours are
   left before a deadline. Claiming a "safe" pumping time without that
   information could tell a farmer to start early enough that they'd water
   *before* the day's actual water use had happened, which would overwater
   and waste water. Attempted fixes for this kept being subtly wrong, so the
   tool says plainly what it doesn't know instead of guessing.
