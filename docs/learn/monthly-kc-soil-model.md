# Monthly Kc, and a soil slot for the FNO/PINO neural operator

## What changed

The engine can now use a Kc (crop coefficient) learned from one farm's own
measured water use, one number per month. And the soil part of the engine is
now its own swappable piece, so an FNO/PINO neural operator soil model can replace the simple
"bucket" later without touching the Kc code or the weekly advice code.
Blocks without a fitted Kc work exactly as before (checked to the last digit).

## The two halves of the engine

The engine answers two questions every hour:

1. How much water does the crop want? (crop demand)
   Crop water use = Kc x ET0. ET0 is the weather's demand. Kc says how thirsty
   this orchard is compared with grass. This half lives in `kc.py` (fitted Kc)
   and `fao56.py` (textbook Kc).
2. What happens to the water in the soil? (soil model)
   Water comes in (rain, later irrigation), the crop takes some out, extra
   drains away below the roots. The answer is the depletion: how many mm
   it would take to fill the root zone back up. This half lives in
   `soil_model.py`.

The weekly advice only looks at the depletion. So any
soil model that can report depletion in mm can drive the advice.

```
weather -> [crop demand: Kc x ET0] -> demand (mm) -> [soil model] -> depletion (mm) -> advice
              kc.py / fao56.py                       soil_model.py                 water_balance.py
                                                     bucket today,                 weekly.py
                                                     FNO/PINO neural operator later
```

## New file: kc.py (the crop side)

- `fit_monthly_kc(days)`: give it measured days (date, ET0, measured water
  use) and it returns 12 Kc numbers.
- First it works out the farm's flat Kc: all water use added up, divided
  by all ET0 added up.
- Then each month gets its own number, but pulled toward the flat Kc. A
  month with 30 measured days is half its own number and half the farm
  average. A month with no data is exactly the farm average. Why: a month
  with few days gives a noisy number, and noise makes advice worse.
- It refuses (instead of guessing) when there are fewer than 60 measured
  days, or when a Kc comes out below 0.15 or above 1.5. That almost always
  means bad data, such as wrong units or the two columns swapped.
- Command line: `python -m orchardwater.kc days.csv` reads a CSV with columns
  `date, et0_mm, eta_mm` and prints the 12 numbers ready to paste into a
  farm file.

## New file: soil_model.py (the soil side, where the FNO/PINO neural operator goes)

- `CropDemand`: the crop's demand for one hour, total mm plus the part that
  evaporates from wet soil. The bucket only needs the total. An FNO/PINO neural operator needs the
  two parts apart, because roots take water from deep down and evaporation
  takes it from the top. The textbook (dual Kc) path already fills in both
  parts. A fitted Kc measures both together, so there it is all one number.
- `SoilWaterModel`: the rules every soil model must follow. It must say how
  much water the root zone holds (`taw_mm`), start from a depletion, and take
  one step at a time: old state + demand + water in -> new state + depletion,
  actual water use, drainage.
- `BucketModel`: today's FAO-56 bucket, moved here. Same arithmetic as before.

Why "pure function" matters for the FNO/PINO neural operator: `step()` takes the old state
and gives back a new one, and changes nothing else. That is the shape a
PyTorch training loop needs. The FNO/PINO neural operator is built in PyTorch, and so is NVIDIA
PhysicsNeMo, planned for the 2D/3D drip twin. Its `step()` follows the same rules, and because PyTorch can
take gradients through it, it can learn soil parameters from probe data.

## Changed files

- `water_balance.py`: `run_forecast_period` takes two new optional inputs,
  `monthly_kc` and `soil_model`. Left out, nothing changes.
- `weekly.py`: `compute_weekly_result` passes `monthly_kc` through (drip
  only). When it is used, the report text gets one line saying so.
- `farm.py`: a block may have `"monthly_kc": [12 numbers]`.
- `loop.py`: passes a block's `monthly_kc` to the engine.

## What the backtest says

One test orchard, leave-one-year-out (flux-tower data, not included here):

| | Daily miss |
|---|---|
| Fitted flat Kc | 0.54 mm/day |
| Engine + monthly Kc, pulled to farm average | 0.555 mm/day |
| Engine + monthly Kc, not pulled | 0.568 mm/day |

Monthly Kc does not beat one flat number on this orchard. Pulling thin
months toward the farm average removes most of the cost. Monthly is kept
because other farms may have a real seasonal pattern, and with little data
it falls back to the flat number. To get the flat number for a farm,
paste the same value 12 times.

The physics is not shown to beat FAO-56. What the data supports: per-farm calibration
halved the daily error at one orchard.

## Not done yet (for when the FNO/PINO neural operator arrives)

1. `state.py` saves one depletion number per field. An FNO/PINO neural operator has a whole
   water-content profile, so state saving must grow.
2. The thin surface layer that dries after wetting (De) is still in
   `water_balance.py`. A Richards-based neural operator model handles the top of the soil
   itself, so it would take that over.
3. `irrigation_deadline()` still uses the bucket equation for one safety
   check ("would a full refill the hour before still cross the limit?").
4. The stress limit is still FAO-56's p x TAW. An FNO/PINO neural operator could give one from
   soil suction instead. That is a separate decision.
5. Fitting Kc from a water meter instead of a flux tower: the fit takes
   any (ET0, water use) pairs, but turning meter readings into water use is
   not written yet. Needs real meter data.
6. Flood blocks still use the textbook Kc only.
