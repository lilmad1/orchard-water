# FAO-56 reference for coding agents

Read this before writing or reviewing any water-balance code.
Source: Allen, Pereira, Raes & Smith (1998), *Crop evapotranspiration*, FAO Irrigation and
Drainage Paper 56. Full text: https://www.fao.org/4/x0490e/x0490e00.htm

Status of this file: first drafted from memory on 2026-09-25. On the same day, the equation
and table numbers marked below were checked against the FAO-56 source text at fao.org
(Chapters 5-8). Two numbers in the first draft
were wrong and have been corrected (ETc equation, deep percolation equation) and Table 19's
chapter was corrected. The operator still needs to give the final confirm, since an automated fetch of
converted HTML is not the same as reading the PDF. Code that uses a value must cite the
table/equation in a comment and keep `TODO(verify)` until the operator confirms. Never add a constant
that is not in this file or the source.

## Units and conversions (safe, pure arithmetic)

- 1 mm of water over 1 ha = 10 m³ (1 ha = 10 000 m², 1 mm = 0.001 m).
- Pump hours = volume (m³) / pump flow (m³/h).
- Gross irrigation = net irrigation / application efficiency. Efficiency is an
  input, not a constant: drip is often given as ~0.85–0.95 in FAO manuals (verify, and ask
  the operator for the value the farm actually achieves).
  This must be conveyance/application efficiency only (pipe and
  dripper/furrow losses BEFORE water reaches the root zone) - NOT a
  conventional "irrigation efficiency" figure that also bundles in deep
  percolation (drainage below the root zone), since the leaching
  requirement (below) accounts for that drainage separately. Using an
  efficiency figure that already includes percolation would double-count
  that water. This
  tool cannot verify which definition a farm actually measured - ask
  explicitly for the narrower figure, or check with an agronomist which
  number a given measurement method actually reports.

## Crop water need

- ETc = Kc × ET0 (mm/day). Chapter 6, eq. 58 (checked against fao.org text 2026-09-25, the operator to
  confirm). The same formula is introduced generally in Chapter 5 as eq. 56; eq. 58 is the
  restatement in Chapter 6 next to the Kc tables, which is the one this code uses.
- ET0 = reference evapotranspiration of a grass surface (mm/day). In v0 it is fetched from
  a weather service; later it can be computed with FAO Penman-Monteith (Chapter 2, eq. 6, verify):

  ET0 = [0.408 Δ (Rn − G) + γ (900 / (T + 273)) u2 (es − ea)] / [Δ + γ (1 + 0.34 u2)]

## Kc for citrus (Table 12, checked against fao.org text 2026-09-25, the operator to confirm)

Single crop coefficients for citrus, by canopy cover. Kc ini / Kc mid / Kc end:

| Situation | 70 % canopy | 50 % canopy | 20 % canopy |
|---|---|---|---|
| No ground cover | 0.70 / 0.65 / 0.70 | 0.65 / 0.60 / 0.65 | 0.50 / 0.45 / 0.55 |
| Active ground cover or weeds | 0.75 / 0.70 / 0.75 | 0.80 / 0.80 / 0.80 | 0.85 / 0.85 / 0.85 |

For evergreen citrus the "stages" are seasons of the year, not growth stages. Table 12
footnotes explain this, so read them before choosing values. v0 default proposal:
mature orchard, ~70 % canopy, no ground cover (clean-weeded drip orchard). This is an
`PROPOSED` until the operator confirms what typical Souss orchards look like.

## Root zone and soil water (Chapter 8, checked against fao.org text 2026-09-25, the operator to confirm)

- Root depth Zr for citrus (Table 22, checked against fao.org text 2026-09-25, the operator to
  confirm): about 1.2–1.5 m at 70 % canopy, 1.1–1.5 m at 50 %, 0.8–1.1 m at 20 %.
  Depletion fraction p = 0.50 at every canopy level. The table note: the larger Zr values are
  for soils with no layering that restricts rooting; smaller values are used for irrigation
  scheduling. Drip-irrigated trees often have shallower effective roots, so treat Zr as an input.
- Total available water: TAW = 1000 (θFC − θWP) Zr (mm). Eq. 82 (checked against fao.org text
  2026-09-25, the operator to confirm).
- Readily available water: RAW = p × TAW. Eq. 83 (checked against fao.org text 2026-09-25,
  The operator to confirm).
- Water stress coefficient: Ks = 1 when Dr ≤ RAW, else Ks = (TAW − Dr) / ((1 − p) TAW).
  Eq. 84 (checked against fao.org text 2026-09-25, the operator to confirm). Adjusted ETc = Ks × Kc × ET0.
- Daily root-zone depletion (eq. 85, checked against fao.org text 2026-09-25, the operator to confirm):
  Dr,i = Dr,i−1 − (P − RO)i − Ii − CRi + ETc,i + DPi
  with 0 ≤ Dr,i ≤ TAW. P = rain, RO = runoff, I = net irrigation, CR = capillary rise
  (0 for deep water tables, which is the usual case in Souss), DP = deep percolation.
- Deep percolation (eq. 88, checked against fao.org text 2026-09-25, the operator to confirm):
  DPi = (P − RO)i + Ii − ETc,i − Dr,i−1 ≥ 0 (water above field capacity drains away).
  Note: an earlier draft of this file cited this as eq. 86; the source text numbers it eq. 88.
- Irrigate when Dr reaches RAW (no stress). Net depth to apply = Dr.

## Typical soil water values (Table 19, Chapter 7, m³/m³, checked against fao.org text
2026-09-25, the operator to confirm)

Note: an earlier draft of this file placed Table 19 in Chapter 8; the source text has it in
Chapter 7 ("ETc - Soil evaporation and transpiration").

| Texture | θFC | θWP |
|---|---|---|
| Sand | 0.07–0.17 | 0.02–0.07 |
| Loamy sand | 0.11–0.19 | 0.03–0.10 |
| Sandy loam | 0.18–0.28 | 0.06–0.16 |
| Loam | 0.20–0.30 | 0.07–0.17 |
| Silt loam | 0.22–0.36 | 0.09–0.21 |
| Clay | 0.32–0.40 | 0.20–0.24 |

Soil type is an input. v0 default proposal: sandy loam, midpoints (`PROPOSED`).

## Effective rain and runoff

FAO-56 Chapter 8, in the soil water balance section, gives one explicit rule for small rain
(checked against fao.org text 2026-09-25, the operator to confirm): "Daily precipitation in amounts
less than about 0.2 ETo is normally entirely evaporated and can usually be ignored in the
water balance calculations."

v0 rule: rain below ~0.2 × ET0 counts as 0 mm reaching the root zone (per the quote above).
Rain at or above that threshold is assumed to fully infiltrate: RO = 0, so all of it enters
the root zone, and any excess above field capacity leaves as deep percolation through eq. 88
(`PROPOSED` for the RO = 0 part specifically, FAO-56 has no single daily runoff rule, so
this is a stated assumption, not a cited value). Write this assumption down in code and
output. Rain in Souss is small and rare, so the error from ignoring runoff is small, but it
must be stated.

Hourly resolution note: the water balance runs hourly, not daily. The
0.2 × ET0 quote above is stated for daily precipitation totals; applying the same 0.2
fraction to hourly rain and hourly ET0 is a plausible extrapolation this code makes, not a
rule FAO-56 states for hourly data. `TODO(verify)` - not checked against a source for hourly
resolution specifically. See `fao56.effective_rain_mm`.

## Remembering depletion between runs

Always assuming the soil is full at the start of whatever
forecast window was fetched (calendar midnight today), regardless of when the
tool was actually run, would make a mid-day re-run after a deadline had passed
just repeat the same problem. So the tool saves the real depletion after each run
(`.orchardwater_state.json`, one field at a time, gitignored) and picks up from
there next time, catching up on real already-happened weather in between
instead of re-guessing. The top-up method's underlying simplification (no
farmer irrigation during the tracked period, FAO-56 Eq. 85's I = 0) now
spans real farmer actions between runs too - if water was actually applied,
run with `--reset` to discard the saved state and start fresh. This is a
manual step, not automatically detected.

Every case below shares one danger: `load_state()` returns `None` for a saved
state it cannot use, and if the caller cannot tell WHY, it falls back to "assume
full soil" and the unconditional save right after then overwrites the file with
that assumed-full guess - permanently discarding a real, possibly
already-over-the-limit depletion. So `compute_weekly_summary()` first PEEKS at
the saved state (before fetching anything) and handles each case explicitly.

Stale state, widened fetch: drip's fetch always covers a fixed 7-day past window
(`weather.PAST_DAYS`). If the saved anchor is older than that - the
farmer skipped running the tool for more than a week - `load_state()`
correctly rejects it as outside the fetched range, but the caller has
no way to tell "no state" apart from "state exists, just older than
this window". Example: sand, a saved depletion of 60 mm (already above
RAW's 45 mm) from 8 days before `now` - one day past the fixed window -
would silently become a report of low need with no urgent warning. So
the saved anchor's age is peeked with `state.parse_state_file_any_field()`
(the same validation as `load_state()`, minus the field and range checks)
BEFORE fetching, and `past_days` is widened to reach it whenever needed -
the same "+2 day buffer" convention `compute_flood_weekly_summary()` uses
for its own farmer-given date. When even Open-Meteo's own history limit
(`weather.PLAUSIBLE_MAX_PAST_DAYS`, 92 days) can't reach the anchor,
the tool fetches nothing, saves nothing (preserving the real state on
disk for a later, better-informed run), and tells the farmer plainly to
check the field's actual soil moisture and re-run with `--reset` once
confirmed.

Future-dated state: the mirror image, triggered from the other
direction. A saved anchor AFTER `now` - most likely the system clock
moved backward since it was saved, or a corrupted timestamp - also
fails `load_state()`'s bounds check and would hit the same data-loss
shape. Example: a saved 60 mm depletion timestamped one hour after
`now`. Widening the fetch cannot help here (the problem isn't reach, it's
`now` being earlier than the anchor), so this case gets the same
treatment as an unrecoverably-stale anchor: fetch nothing, save nothing,
tell the farmer plainly what happened and to check the field directly
before re-running with `--reset`.

Unreadable-but-present state: `parse_state_file()` returns `None` for
THREE different reasons folded together: no file at all, a file for a
different field, or a file that exists for THIS field but could not be
parsed (corrupt JSON, an invalid timestamp string, an implausible
depletion value). Treating all three identically as "no state, safe to
assume full soil" is wrong for the third: it might be real, corrupted
recovery evidence, and silently overwriting it destroys that evidence for
good (for example an otherwise-valid sand-field 60 mm depletion whose saved
timestamp was corrupted to an unparseable string). So the peek step uses
`state.parse_state_file_any_field()` (same validation, minus the
field-match check), which tells "parses fine, just for another field"
(expected, ordinary use when switching which field this single-file
tool tracks - proceed normally, no warning) apart from "exists but
could not be trusted at all" (fetch nothing, save nothing, same
preserve-and-warn treatment as the unrecoverable cases above).

Same-field corruption that still parses: two narrower gaps would
otherwise let a same-field state that DID parse still get silently
discarded and overwritten:
- Depletion above TAW. `state.py` has no crop context, so it can't
  check a saved `depletion_mm` against a crop's total available water
  (TAW). That check lives in `compute_weekly_summary()` and runs in the
  peek step, BEFORE any fetch, because `taw_mm` only needs `soil_name`,
  not fetched weather. Example: sand (TAW 90 mm), a saved depletion of
  91 mm - physically impossible, not evidence the soil is full - is
  preserved and reported rather than discarded.
- Corrupted field identity read as a field switch.
  `parse_state_file_any_field()` checks `anchor_timestamp`'s,
  `anchor_utc_offset_seconds`'s and `field_key`'s types: a JSON `null`
  field_key would otherwise parse successfully, and the peek step's
  "parses fine but for a different field, proceed normally" branch would
  read `None != this run's real key` as evidence of an intentional field
  switch, silently skipping the preserve-and-warn path entirely.

Malformed-but-string field identity: a type check alone isn't enough -
an empty string, or a truncated key like `"30.500000,-8.875000"`
(missing the soil segment), is still a valid `str` and would hit the
same problem, since the peek step's "different field" branch only
compares it against this run's own key with `!=`. Example: a saved
60 mm sand-field depletion (already above RAW's 45 mm) with `field_key`
changed to `""` or a truncated key would flip the output to an ordinary
0 mm refill and save 0 mm over the real reading. So
`state._looks_like_a_field_key()` is checked inside
`parse_state_file_any_field()` itself: a saved key must have
`field_key()`'s exact shape (three comma-separated parts, the first two
round-tripping through the same `%.6f` formatting `field_key()` uses,
the third non-empty) or the whole state is treated as unreadable, not as
evidence of a genuine field switch. It deliberately does not check the
soil segment against `fao56.SOIL_TYPES` - `state.py` doesn't import
`fao56.py` (one-file-per-concern).

Crop-aware saved-key validation: the shape check can't tell "a real,
different field" from "corrupted in a way that keeps the shape" - an
unrecognized soil name (a typo of a real one, like `"san"` for
`"sand"`), or a structurally-formatted but geographically impossible or
non-finite coordinate, still passes it and would be read as "a different,
legitimate field." Example: a saved 60 mm sand-field depletion (already
above RAW's 45 mm) with `field_key`'s soil segment corrupted to `"san"`
would flip to an ordinary 0.0 mm recommendation and save 0.0 mm over the
real reading. `weekly._looks_like_a_real_different_field()` is the
crop-aware half of this validation, living in `weekly.py` (not
`state.py`) specifically because it needs `fao56.SOIL_TYPES`, already
imported there. A saved field_key that structurally parses is ALSO
required to have finite coordinates within real geographic bounds (-90
to 90 latitude, -180 to 180 longitude - plain geographic reality, not an
FAO-56 constant, so nothing to cite) and a soil name that is an actual
key in `SOIL_TYPES`, before the peek step will treat it as a genuine
field switch rather than corrupted identity for THIS field. (The lookup
of `SOIL_TYPES[soil_name]` elsewhere in `weekly.py` validates the CURRENT
run's `--soil` input, never the SAVED key's own soil segment.)

Fetched weather that doesn't actually cover the anchor it was sized
for: the peek step fully validates a saved state and sizes `past_days`
to reach its anchor before fetching - but the ACTUAL fetched weather
might not cover as far back as requested despite matching the expected
row count (for example the same total hour count, just shifted to start
later than requested - row-count validation alone can't catch a
shifted-but-same-length response). The post-fetch `load_state()` call
correctly rejects that anchor as outside the real fetched range - but
the caller can't tell that apart from "no state was ever saved" either,
so it would fall to the same assumed-full-soil fallback and overwrite a
real, possibly already-over-the-limit depletion. So, right after the
post-fetch `load_state()` call: if the peek step fully validated a usable
state (`peeked is not None`) but this second, authoritative check still
came back empty, that can ONLY mean the fetch didn't deliver the
coverage the peek expected - the file is preserved (no save) and the
farmer is told this looks like a weather-service problem, not a
`--reset`-worthy one.

## Salinity leaching requirement

FAO-56 Chapter 8, "Effects of soil salinity", "Application" (checked against
fao.org text 2026-09-27, the operator to confirm) quotes the FAO-29 steady-state
leaching formula:

    LR = ECiw / (5 x ECe - ECiw)

- ECiw: electrical conductivity of the irrigation water (dS/m) - a
  farm-specific measured value (lab test), like `efficiency`: no default,
  always an input (`--ec-water`).
- ECe: the soil salinity threshold below which yield isn't reduced -
  FAO-56 Table 23 gives 1.7 dS/m for orange, 1.8 dS/m for grapefruit (this
  code uses 1.7, `CITRUS_ECE_THRESHOLD_DS_M`, TODO(verify) which citrus
  variety this should actually use).
- The formula assumes a 40-30-20-10 percent water extraction pattern from
  the upper to lower quarters of the root zone before each irrigation -
  not separately verified for citrus.
- The formula predicts a STEADY-STATE salt balance, reached over repeated
  irrigations - not something one week's water guarantees on its own. It
  also doesn't know the field's actual current soil salinity, rainfall,
  or how much water really drains past the root zone. Treat this as a
  planning estimate, not a guarantee.
- Raises an error rather than returning a number when LR would be at or
  above 1.0 (100% of applied water) - this happens at ECiw = 2.5 x ECe,
  which is BEFORE the formula's denominator hits zero at ECiw = 5 x ECe
  (checking only the denominator would miss the ECiw range from 2.5x to
  5x ECe where LR is already >= 1 but the denominator hasn't hit zero
  yet). Either way, it means the irrigation water is too saline for leaching
  alone to help - a real agronomic problem this tool should not paper
  over with a meaningless number.

Combining LR with the existing gross-water calculation: FAO-56 states
the LR formula but not how to combine it with application efficiency. Two
sources checked 2026-09-27 both give the same combination:
- an FAO water-management extension guide
  (https://www.fao.org/4/t7202e/t7202e06.htm, "Determination of the
  irrigation schedule for crops other than rice")
- an irrigation-scheduling tool citing the Rhoades leaching equation
  (https://www.avocadosource.com/tools/IrrigationCalculator_files/instructions_lr.htm)

    gross = net / (efficiency x (1 - LR))

i.e. the leaching fraction is an *additional* multiplier alongside
efficiency, not a replacement for it (`units.gross_mm`). This combination
only avoids double-counting drainage if `efficiency` means water lost
BEFORE reaching the root zone (pipes, dripper/furrow application), not
deep percolation after infiltration - which is already how `efficiency`
is defined in this codebase (see `units.gross_mm`'s docstring), not a new
assumption introduced for this correction.

## Dual crop coefficient method

Under drip, only a fraction of the soil surface actually gets wet (the
area near each emitter) - a single flat Kc (as used everywhere above)
overstates evaporation from the dry, unwetted area between drippers.
FAO-56's own recommendation for drip-irrigated tree crops (its own worked
example, Example 34, is exactly this case) is the dual crop coefficient
method: split ETc into a basal (transpiration) term and a separate
soil-evaporation term, the second one decaying as the wetted patch dries
between drip cycles. This replaces the flat-Kc calculation everywhere in
this tool (the whole tool is drip-only already - see
`water_balance.py`'s module docstring), not an opt-in mode.

Every equation number below is FAO-56 Chapter 7, checked against
fao.org/4/x0490e/x0490e0c.htm, 2026-09-27, the operator to confirm.

- ETc = (Kcb + Ke) x ET0 (Eq. 69) - replaces ETc = Kc x ET0 (Eq. 58) here.
- Ke = min(Kr x (Kc_max - Kcb), few x Kc_max) (Eq. 71) - the soil
  evaporation coefficient, capped at what the exposed-and-wetted fraction
  of the surface can physically supply.
- Kc_max = max(1.2 + [0.04(u2-2) - 0.004(RHmin-45)] x (h/3)^0.3, Kcb + 0.05)
  (Eq. 72) - see the floor's own citation and reasoning further down.
- TEW = 1000 x (theta_FC - 0.5 x theta_WP) x Ze (Eq. 73) - the total
  evaporable water in the topsoil layer, reusing each soil's existing
  theta_FC/theta_WP.
- Kr = 1 while De,i-1 <= REW; else (TEW - De,i-1) / (TEW - REW) (Eq. 74) -
  how much the drying topsoil limits evaporation.
- few = min(1 - fc, fw) (Eq. 75) - the fraction of soil that is both bare
  and wetted, where evaporation can actually happen.
- Wind height conversion (Eq. 47, Chapter 4): u2 = u10 x 4.87 /
  ln(67.8x10 - 5.42), converting a 10 m measurement (what this tool's
  weather source provides) to the 2 m height Eq. 70/72 need.
- Kcb climate adjustment (Eq. 70): Kcb = Kcb_tab + [0.04(u2-2) -
  0.004(RHmin-45)] x (h/3)^0.3 - the SAME bracket term as Kc_max's
  climate correction, added to the tabulated Kcb instead. FAO-56's own
  text: "For specific adjustment in climates where RHmin differs from
  45% or where the wind speed is larger or smaller than 2 m/s, the Kcb
  mid and Kcb end values larger than 0.45 must be adjusted using the
  following equation." `CITRUS_KCB = 0.60` is above that threshold, so
  this always applies to it (the correction is needed for Kcb as well as
  for Kc_max). u2 and RHmin are both clamped to FAO-56's
  own stated validity range for this correction before use - "for
  1 m/s <= u2 <= 6 m/s", "for 20% <= RHmin <= 80%" - since an in-bounds-
  but-extreme real weather reading is still far outside where this
  formula was calibrated and can otherwise produce an absurd Kcb (an
  unclamped extreme wind reading produces Kcb ~5.0 for a citrus Kcb of
  0.60). Clamping rather
  than rejecting keeps the tool usable on a genuinely windy or dry day.
- Kc_max's own floor (also part of Eq. 72, not a separate check): Kc_max
  = max(climate_term, Kcb + 0.05) - FAO-56's own text, "Equation 72
  ensures that Kc max is always greater or equal to the sum Kcb + 0.05...
  wet soil will always increase the value for Kcb by 0.05 following
  complete wetting of the soil surface" (without the floor, the climate
  term alone could make Kc_max fall below Kcb for an extreme climate/Kcb
  combination, making Ke go negative).
- Surface evaporation balance (Eq. 77/78, adapted here the same way the
  existing root-zone balance already simplifies Eq. 85: I = 0 during the
  modelled period - see below for why - RO = 0, and Tew = 0 - FAO-56's
  own text: "except for shallow rooted crops... the amount of
  transpiration from the evaporating soil layer is small and can be
  ignored," and citrus's root depth here is well above that shallow
  threshold):
  De,i = De,i-1 - eff_rain_i + Ei/few + DPe,i, with 0 <= De,i <= TEW.
  This is mathematically the SAME clamped shape as the existing
  root-zone Eq. 85/88, so `water_balance.step_one_period()` is reused for
  it unchanged, rather than a second function.

Table 17 (Kcb, citrus, checked against fao.org 2026-09-27): for the
same row already chosen for `CITRUS_KC` (no ground cover, 70% canopy),
Kcb ini/mid/end = 0.65/0.60/0.65. `CITRUS_KCB = 0.60` (mid-season) -
deliberately NOT the same value as `CITRUS_KC` (0.65, Table 12's
single-Kc mid-season value for the same row): Kcb excludes the
soil-evaporation term Table 12's single Kc bakes in, so it is a
genuinely smaller, separate number, not a correction to `CITRUS_KC`.

Table 19 (REW, checked against fao.org 2026-09-27): readily
evaporable water, per soil texture, at Ze = 0.10 m: sand 2-7 mm, loamy
sand 4-8 mm, sandy loam 6-10 mm, loam 8-10 mm, silt loam/silt/silty
clay loam/silty clay 8-11/8-11/8-11/8-12 mm, clay 8-12 mm. This code
uses the midpoint of each range for the textures it models (sand,
sandy loam, loam, clay - `SOIL_TYPES[...].rew_mm`), same convention
already used for theta_FC/theta_WP.

Table 20 (fw, checked against fao.org 2026-09-27): fraction of soil
surface wetted, per irrigation method. Precipitation, sprinkler, and
basin/border irrigation all wet the full surface (fw = 1.0). Trickle
(drip) irrigation: fw = 0.3-0.4 - this code uses the midpoint, 0.35
(`DRIP_FW_TRICKLE`). FAO-56's own text adds a further, drip-specific
adjustment on top of the table value: "In the case of drip irrigation,
where the majority of soil wetted by irrigation may be beneath the
canopy and may therefore be shaded... the value for fw may need to be
reduced... A general approach could be to multiply fw by [1-(2/3)fc]
for drip irrigation" (`fao56.drip_wetted_fraction`).

fc (canopy fraction): rather than derive this via Eq. 76 (which
needs a further undetermined `Kc_min` constant), this code promotes the
"70% canopy" already implicit in which Table 12/17 row `CITRUS_KC`/
`CITRUS_KCB` use into its own explicit constant, `CITRUS_CANOPY_FRACTION
= 0.70`.

h (crop height), for Kc_max's (h/3)^0.3 term: Table 12 lists citrus
maximum height as "3-4 m." `CITRUS_HEIGHT_M = 3.0` - lower end, same
convention already used for `CITRUS_ROOT_DEPTH_M`.

Ze (topsoil evaporation layer depth): FAO-56's own range is
0.10-0.15 m. `TOPSOIL_EVAP_DEPTH_ZE_M = 0.10` - chosen exactly (not a
midpoint) because Table 19's REW values above are themselves stated "at
Ze=0.10 m"; picking any other Ze would make TEW and REW inconsistent
with each other.

u2 and RHmin (Eq. 70/72's wind and minimum relative humidity): a real
extrapolation, same status as the hourly effective-rain threshold above -
FAO-56 defines both as averages over the crop's growth STAGE (matching
how Table 17's Kcb values are themselves tabulated per stage), not an
instantaneous hourly reading. This tool has no growth-stage concept at
all (a documented v0 limitation already - CITRUS_KC/CITRUS_KCB are flat,
non-seasonal values), so there is no stage average to compute even in
principle without a larger redesign; each hour's own wind/humidity
reading is used directly as a stand-in instead, for both wind and
humidity. `TODO(verify)` - not
checked against a source for this specific substitution. See
`fao56.kc_max`/`fao56.climate_adjusted_kcb`.

De does not reset partway through a run. Modelling the drip system
re-wetting the surface on a fixed schedule (a `--drip-interval-days`
input) DURING the forecast itself would directly contradict this
tool's foundational I=0 assumption above (Eq. 85's simplification):
if no irrigation happens until the recommendation is followed, nothing
can be re-wetting the surface mid-forecast either. That contradiction
would make the printed amount too high for a field that actually gets
watered during the forecast week, so there is no such flag and no reset
logic.

De starts fully wet (De = 0, matching the root-zone "top-up
method" starting assumption) when there is no better starting point, and
dries out continuously, hour by hour, for as long as `run_forecast_period`
runs - across BOTH the catch-up and future halves of a single
`compute_weekly_summary` call, AND across separate tool
runs, via `state.py`, the same way Dr already is. Resetting De fresh at
the start of every run on the reasoning that real topsoil evaporation
layers dry out within about a day or two anyway, so it would cost little
accuracy, is backwards: every fresh run would wrongly assume the surface had
just been re-wetted, adding an artificial burst of extra evaporation
that compounds the more often the tool is run (the tool's normal use
pattern). Seven successive daily runs with no rain overstate net need by
about 5.8 mm compared to one continuous run, using this tool's real
citrus/sand constants. So De (and few, below) are persisted,
extending `state.FieldState` with `de_mm`/`few` fields - both default to
"fresh" (0.0 / `None`) so a state file saved before they existed still
loads.

Wetted fraction (few, Eq. 75) persists between wetting events, not
reset every hour: FAO-56's own text: "Surface is
wetted by significant rain (i.e., > 3 to 4 mm)... fw = 1"; "Where there
is neither irrigation nor significant precipitation: fw is the fw of
the previous day." Switching to the full-surface fraction on ANY
hour with effective rain (however small) would conflate the smaller
`EFFECTIVE_RAIN_ET0_FRACTION` threshold, which only decides whether rain
counts toward the water balance at all, with this separate, larger
"does it wet the WHOLE surface" threshold; and resetting back to the
drip fraction on the very next dry hour would be wrong too. `few`
threads exactly like De: continuously within a run, persisted across
separate runs, and only changes on an hour where
`hour.rain_mm >= SIGNIFICANT_RAIN_MM` (midpoint of FAO-56's 3-4 mm range,
`TODO(verify)`) - `water_balance._hour_few`.

Kr's one-period lag (Eq. 74) is deliberate, not a bug: Kr uses De,i-1
(the depletion at the START of the period), so a period with rain does
not benefit its OWN evaporation estimate from that same rain - only the
next period's does. This is FAO-56's own equation exactly as written,
the same lag the existing root-zone model has always had; running it
hourly instead of daily only makes the lag shorter (an hour instead of a
day), not new. Reordering this to react to same-period rain would
deviate from FAO-56's stated equation, not align with it more closely.

Two known limitations, disclosed rather than fixed, consistent with how
this file already treats other daily-FAO-56-rule-run-hourly extrapolations
(see `EFFECTIVE_RAIN_ET0_FRACTION` and RHmin above):
- `SIGNIFICANT_RAIN_MM` is checked one hour at a time. FAO-56 states this
  rule for a whole day's rain; a storm split across multiple hours that
  individually stay under the threshold but sum to a significant total
  (e.g. two separate 2 mm hours) will not trigger the full-surface
  switch, even though a single 4 mm hour would. `TODO(verify)` - not
  checked against a source for hourly resolution specifically, same
  status as the effective-rain threshold. Tracking a rain-accumulation
  window rather than a single-hour check would be the eventual fix.
- A state file saved by a version of this tool before `de_mm`/`few`
  existed loads with `Dr` (root-zone depletion) intact but `de_mm`/`few`
  reset to fresh ("just re-wetted"), even if `Dr` itself reflects a long
  drying history - a one-time, inconsistent pairing at the moment of
  upgrading (every subsequent save writes all three together, so this
  self-resolves after the first run). Not built as a blocking migration
  check: no real state file in the old format exists outside this
  repo's own tests, so there is nothing to actually migrate from in
  practice.

Validation of the saved surface layer (`de_mm`/`few`):
- A saved `de_mm`/`few` that fails validation does NOT discard the WHOLE
  saved anchor, including a perfectly valid `depletion_mm` (Dr) - since
  Dr is what drives the RAW-crossing/deadline check, discarding it could
  silently hide an imminent watering deadline behind a false "full soil"
  restart. Only `de_mm`/`few` reset to fresh; a valid `depletion_mm`
  always survives on its own. This holds at both layers: `state.py`'s
  `load_state()` sanitizes an implausible `de_mm`/`few` to a safe
  default (0.0 / `None`) instead of invalidating the whole state, and
  `compute_weekly_summary()` applies the same narrower recovery with its
  crop-aware checks.
- A wrong TYPE (e.g. `few` saved as a string) is treated identically to
  an implausible VALUE: a mistyped surface field says nothing about
  `depletion_mm`'s own validity either, so both sanitize, neither
  invalidates the whole state.
- `de_mm` and `few` always reset TOGETHER whenever either one is
  invalid, never one without the other - an invalid `few` alone would
  leave a stale, un-reset `de_mm` paired with a freshly-reset `few`, an
  inconsistent combination reflecting two different unknown histories.
- A saved `few` can only ever legitimately equal one of exactly two
  values this model produces (Eq. 75 at the drip fraction, or at fw=1.0),
  never something in between, so it is checked against both exactly
  (with float-serialization tolerance), not merely bounded above. A value
  below the drip fraction (the model's own minimum) is just as impossible
  as one above the maximum, and would silently understate evaporation.
- Eq. 70/72's climate correction has its own bound beyond
  `wind_speed_2m`'s generous 150 m/s plausibility ceiling, so an
  in-bounds-but-extreme wind reading cannot push Kcb to an absurd value -
  see the clamping documented under Eq. 70 above.
- `ASSUMPTIONS_TEXT` (`weekly.py`) describes the model as built: De/few
  persisted across runs, and `few` staying at the full-surface value until
  the next significant rain, not shrinking back on its own.
- `units.mm_to_m3()` guards against overflow and also against the opposite
  failure: an astronomically small but positive `mm` at the smallest
  plausible field size would underflow to exactly `0.0` m3 and print as
  "no volume needed" despite a real, positive depth (same pattern as
  `m3_to_pump_hours()`'s underflow guard). It multiplies the area
  conversion factor first (`mm * (ha * 10.0)`, both normal-magnitude),
  because computing `mm * ha * 10.0` left-to-right can underflow the
  `mm * ha` intermediate to 0.0 even when the final answer is
  representable (e.g. `mm=5e-324, ha=0.1`: `mm*ha` alone rounds to 0.0,
  but `mm*(ha*10.0)` correctly gives `5e-324`), while still catching
  genuine underflow.

## Flood irrigation - the "wait-until-low" method

Every other method here assumes the field
is topped up often - `water_balance.py`'s own module docstring says the
top-up method "is not valid for flood irrigation, where the soil
dries for longer than a week between waterings." Flood/basin/border
fields go WEEKS between waterings, so instead of assuming a recently-full
soil (or relying on saved state), this takes the farmer's last watering
date directly and fetches weather back that far.

Table 20 (fw, checked against fao.org text 2026-09-28, same fetch as
the drip research above): basin and border irrigation wet the whole ground
surface directly - fw = 1.0 exactly, the same value precipitation and
sprinkler irrigation get. "Flood" here is this code's own grouping name
for that whole category (basin/border/flood-type surface irrigation),
not a separate row the table itself lists under that word. Unlike Table
20's drip range (0.3-0.4, `DRIP_FW_TRICKLE`), this needs no midpoint and no
`TODO(verify)`: the table states the basin/border value directly.
`fao56.FLOOD_FW_TABLE20 = 1.0`.

No drip-specific shading reduction: FAO-56's "multiply fw by
[1-(2/3)fc] for drip irrigation" text (see the dual crop coefficient
section above) is specifically about drip emitters being shaded under the
canopy - it has no basis for flood, which wets the open ground everywhere.
`DualKcParams` has an `apply_drip_shading: bool` field for this
(`water_balance.py`) - `False` for flood, `True` for drip.

Weather window is variable, not fixed at 7 days:
`weather.fetch_hourly_json()` takes a `past_days` parameter (defaulting
to the fixed 7 for drip) and
`weather.split_past_and_forecast()` takes explicit `past_hours`/
`forecast_hours` instead of only reading fixed module constants. Flood
computes `past_days` as `(days since last watering) + 2` (a buffer
against calendar/timezone boundary ambiguity - the actual hour selection
is exact regardless, via real-instant comparison, not by trusting the
exact past/future split point) and rejects a date that would need more
than Open-Meteo's own documented 92-day history limit
(`weather.PLAUSIBLE_MAX_PAST_DAYS`), with a clear error rather than
silently truncating it.

Last-watering-date granularity: the tool takes the calendar date
(`--last-watering-date`, `YYYY-MM-DD`) and the clock time the watering
finished (`--watering-time`, `HH:MM`, 24-hour Casablanca time). Both are
required - see `--watering-time` below for why there is no 00:00 default.

No saved state for flood, deliberately: the farmer re-supplies
`--last-watering-date` each run (updating it once they actually
re-water) - `state.py` is not used at all for flood, since a fresh date
each run already replaces what persistence exists for elsewhere in this
tool.

`--post-watering-fullness`:
`compute_flood_weekly_summary()` could always start the root-zone
depletion (Dr) at 0 on `--last-watering-date`, i.e. assume a FULL
refill, but that can silently understate what the soil still
needs - or say no irrigation is due at all - after a genuinely partial
watering, which is common for flood/basin irrigation since it rarely
wets a field evenly. `--post-watering-fullness` (default `1.0`, a full
refill) lets the farmer state how full the root zone actually was, as a
fraction of a full tank, right after the watering finished; the starting
depletion becomes `(1 - post_watering_fullness) * taw_mm` instead of
always `0`, fed into `run_forecast_period()`'s `starting_depletion_mm`
parameter (also used for the saved-state continuation in drip). Only the
root zone is adjusted - the surface evaporation layer (De/few) still
always starts fully wet, since flood wets the whole surface directly
regardless of how much reached the full root depth. There is no meter for
this in the field, so it is a best guess: guessing too LOW is the safer
mistake (recommends water earlier/bigger than strictly needed), while
guessing too HIGH - or leaving the full-refill default when the watering
wasn't full - risks the same understated-need problem this flag exists
to fix. The flag means the fullness reached, NOT "how much of a full
refill was reached" (the fraction of a prior shortfall that got closed),
which this tool cannot even compute, since it keeps no saved depletion to
measure a shortfall from; the code computes the fullness interpretation.

`--watering-time` is always required: with
`--post-watering-fullness` below 1.0 and no real watering time, catch-up
hours would be counted from 00:00 on `--last-watering-date` - so any rain
that fell BEFORE the real watering time would be counted as if it fell
AFTER the partial refill, and wrongly credited against it, understating
what the soil still needs. Example: sand, `--post-watering-fullness 0.4`
(a starting deficit of 54 mm, already above sand's 45 mm RAW limit),
watering finished at 18:00, and 30 mm of rain at 06:00 that same day,
BEFORE the real watering - a midnight-anchored calculation lets that rain
erase the deficit, flipping an already-over-the-limit case to "no
irrigation is due."

A full refill is not immune either. The idea that "depletion cannot go
below 0, so early rain has nothing to reduce" only covers the root zone.
The thin surface layer (De/few) is wetted by the real watering, and a
real evening watering re-wets it after a day of drying and rain, which
then loses water differently than a midnight start predicts. In a case
with a September 20 run, the run without the time said "no irrigation is
due" while the same run with 18:00 gave a September 26 deadline (the
direction of the difference depends on the weather - in another fake-weather
case the midnight guess gave the earlier deadline - but in both cases the
assumed time changed the recommendation), so no default is safe.
`--watering-time` is therefore required for every flood run (a clear error
otherwise).

Straddling-hour rain, even with `--watering-time` given: requiring
`--watering-time` does not fully close the gap by itself. Weather
is only ever hourly, and Open-Meteo's rain figure per hour is a
preceding-hour sum (the bucket labelled "19:00" covers 18:00-19:00) - so
a watering time that doesn't fall exactly on an hour boundary (e.g.
`18:30`) leaves the ONE bucket containing it straddling the real
watering instant, mixing rain that fell before the watering with rain
that fell after it, with no way to split the two from hourly data alone.
Example: sand, `--post-watering-fullness 0.4`, watering
finished at 18:30, and 30 mm of rain in the 18:00-19:00 bucket - still
enough to flip an already-over-the-limit case to "no irrigation is due,"
even though `--watering-time` was supplied. So
`_exclude_pre_watering_rain()` is applied to the catch-up/future hour list
before it is even split by "now": it zeroes the one straddling bucket's
rain (keeping its ET0 - losing at most an hour of real drying credit is
a small, bounded error, next to a rain total that can flip the whole
recommendation outright). Hours entirely before or entirely after the
watering instant are untouched either way.

Ambiguous watering_time on a clock-change day: Morocco's own clock changes create
a genuinely repeated wall-clock hour once a year (fall-back) - the SAME
`--last-watering-date`/`--watering-time` combination can refer to two
real instants exactly one hour apart, and `datetime.combine()` silently
picks the earlier one (Python's default `fold=0`) with no error. An
anchor placed an hour earlier than the farmer meant can make
`_exclude_pre_watering_rain()` drop the wrong hour's rain -
undermining the very guard that flag exists for. Example: sand,
`post_watering_fullness=0.4`, and `watering_time` landing in the
repeated hour on Morocco's 2026-02-15 transition - the wrong rain
exclusion flips an already-over-the-limit case to "no irrigation is
due." Ambiguity is detected directly (comparing
`anchor_instant.utcoffset()` against the same instant with `fold=1`,
per PEP 495) and rejected outright with a clear error, rather than
silently guessing which of the two real instants the farmer meant.
A watering time of `"00:00"` is unaffected in practice, since midnight
has never landed inside Morocco's actual transition window - but the same
check would catch it too if a future transition ever did.

Incomplete weather coverage, flood side: the flood-side twin of the
drip coverage check above, in the code that doesn't use saved state at all.
A fetched response can have the exact expected ROW COUNT
(`split_past_and_forecast()`'s own check passes) while still being
shifted to start LATER than requested, silently missing the real drying
history between the watering anchor and wherever the response actually
starts - `hours_after_anchor`'s own filtering has no way to notice data
that simply isn't there. Example: sand, watering on
2026-09-10, `now` on 2026-09-20 - complete coverage correctly gives
78.3 mm depletion and an urgent already-at-limit warning; a response
with the same total hour count but starting on 2026-09-20 instead would
silently print "no irrigation is due" at 16.6 mm, since the missing
10 days of real drying are just absent, not flagged. So the earliest
fetched hour's own real start instant is checked against
`anchor_instant` right after the row-count check - if the fetch doesn't
reach back far enough, an unavailable-estimate warning is returned
instead of calculating from a response with a silent gap in it. Unlike the
drip case, there is no saved state to preserve here (flood keeps
none) - just nothing safe to calculate from an incomplete response.

No preventative top-up when the forecast never reaches the limit: drip's own
no-deadline case (`build_summary()`'s "else" branch) prints a
gross-water/pump-time recommendation, because drip is applied on a
regular weekly schedule as a preventative top-up regardless of whether
the no-stress limit (RAW) is reached. Flood is specifically
"wait-until-low": nothing is due until RAW is actually reached, so
`compute_flood_weekly_summary()` must not fall through to that same
branch and tell the farmer to pump a specific amount (and sometimes warn
the pump could not keep up) when the whole forecast week stays below RAW.
A flood-only path (`build_no_irrigation_due_summary()`) states plainly
that no irrigation is due within the forecast and prints no pump
instruction at all in that case.

Separate compute function, not a branch in the drip path:
`weekly.compute_flood_weekly_summary()` is its own function, not a
branch inside `compute_weekly_summary()`, even though the two share real
structure (fetch, split by "now", catch-up then future, the same
immediate-warning/unschedulable checks): this is lower risk than threading
a second control flow through the existing drip logic. The genuinely
method-agnostic pieces (field size/pump/efficiency validation, the
leaching correction, the mm -> m3 -> pump-hours conversion,
`build_summary`) are still shared.

## Weather data source

- Open-Meteo hourly variables `et0_fao_evapotranspiration` and
  `precipitation` (mm), endpoint `https://api.open-meteo.com/v1/forecast` with
  `past_days=7`, `forecast_days=7`, `timezone=Africa/Casablanca`. No API key. Open-Meteo
  computes hourly `et0_fao_evapotranspiration` with the same FAO-56 Penman-Monteith formula
  as the daily variable - this code does not derive it. (v0 used the daily equivalents
  `et0_fao_evapotranspiration` and `precipitation_sum`, at day resolution.)
  `past_days=7` is drip's own fixed default (`weather.PAST_DAYS`) - it is a real
  parameter, `weather.fetch_hourly_json()`'s
  `past_days`, for flood irrigation's much longer, farmer-date-driven
  window (see the "Flood irrigation" section above), up to Open-Meteo's
  own documented 92-day past-days limit
  (`weather.PLAUSIBLE_MAX_PAST_DAYS`).
- It also fetches `wind_speed_10m` (requested in m/s via
  `wind_speed_unit=ms` - Open-Meteo's default is km/h) and
  `relative_humidity_2m`, for the dual crop coefficient method's Kc_max
  formula (Eq. 72) AND the Kcb climate adjustment (Eq. 70) above -
  nothing else in the tool uses them.
- Hourly vs. daily: Open-Meteo counts API calls, not data points returned, so requesting
  hourly data instead of daily is still one call.
- Licence: Open-Meteo's free API is for non-commercial use only (checked 2026-09-25,
  https://open-meteo.com/en/terms). Development and testing is one thing. Before anyone
  relies on the output in a commercial setting, you need one of these: a paid Open-Meteo plan,
  self-hosting Open-Meteo (open source), or computing ET0 itself with eq. 6 from a source
  that allows commercial use. Keep all weather fetching in one module so this can be
  swapped without touching the water-balance code.
