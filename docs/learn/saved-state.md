# Remembering soil depletion between runs

## Overview

The tool used to always assume the soil was full at the start of whatever
week it fetched weather for, no matter what time of day it was actually run,
so a re-run a few hours later repeated the same "already too late"
answer. Now it saves the real, calculated soil depletion
after every run, and picks up from there next time, using the real
(already-happened) weather in between to catch up, instead of guessing.

## Map

```
weekly.py (compute_weekly_summary)
    -> state.py (load_state: was there a usable saved depletion?)
    -> water_balance.py (run_forecast_period: catch up from the saved
       point to "now", then continue from "now" into the future)
    -> state.py (save_state: write the new "now" depletion back down)
    -> printed summary
```

## File by file

- `state.py` (new): the only file that reads or writes the saved
  depletion. Holds `FieldState` (a timestamp + a depletion number + which
  field it belongs to), `load_state` (returns a usable saved state, or
  `None` if there isn't one: missing file, wrong field, too old, or
  corrupted), and `save_state` (writes it back, safely, see below).

- `water_balance.py`: one small change: `run_forecast_period` can
  now start from a given depletion number instead of always starting at
  0. The water-balance math itself didn't change at all.

- `weekly.py`: the real work. Every run now:
  1. Tries to load a saved depletion for this exact field and soil.
  2. If there isn't one (first run ever, `--reset` was used, or the saved
     one is too old to trust), starts from "full soil," same as before.
  3. Splits the fetched weather into two groups: hours that have already
     happened (used to catch up to the real current depletion) and hours
     still ahead (the actual forecast the answer is about).
  4. Saves the resulting "right now" depletion for next time.

## New Python ideas

- A "temp file, then rename" save: `save_state` doesn't write
  straight to the real file. It writes to a throwaway file first, then
  uses `os.replace()` to swap it into place in one step. Writing directly
  risks a half-written, broken file if the program gets interrupted
  mid-write (power cut, closed terminal, etc.); the swap step is
  effectively instant, so there's no "half-done" state possible.
  ```python
  tmp.write_text(json.dumps(data))
  os.replace(tmp, real_path)  # atomic, can't half-happen
  ```

- `tempfile.mkstemp()`: generates a guaranteed-unique filename (using
  the program's process ID and a random suffix) instead of a fixed name
  like `"state.tmp"`. This matters if the tool is ever run twice at the
  exact same moment: two runs would each get their own temp file instead
  of overwriting each other's in-progress write.

## The water math, worked by hand

Say a saved state says: at `09:00`, depletion was 10.0 mm. The tool is run
again at `11:00`. The two hours in between (`10:00` and `11:00` on
the weather data) each used 0.65 mm of water (Kc 0.65 × ET0 1.0 mm/hour),
no rain.

- Catch-up: 10.0 + 0.65 + 0.65 = 11.3 mm. This is the *real* depletion
  right now, not a guess.
- The tool then treats 11.3 mm as the new starting point for everything it
  reports about the rest of the week.

Re-running the tool assuming "full soil" (the old behavior)
would wrongly start counting from 0 mm again, understating how
dry the soil actually is by exactly the 11.3 mm already
used.

## What this does *not* fix (on purpose)

- `--reset` only tells the tool "treat the soil as full, right now". It
  can't tell the tool *how much* water was actually applied. After a
  partial watering, the tool will think the soil is fuller than it
  really is.
- The tool only knows depletion once per hour, never more precisely.
  With a `--reset` partway through an hour, that whole hour's weather
  still gets counted on top of the reset, including the part from before
  the watering.
- Two people (or two accidental double-runs) using the tool for the same
  field at the exact same moment could overwrite each other's saved
  state. The tool assumes one person running it, one run at a time.

## Check questions

1. Why does the tool split the week's weather into "already happened" and
   "still ahead" instead of using the whole week as before?
2. If the saved state is for a *different* field, or is a week old, what
   does the tool do with it?
3. Why does `save_state` write to a temporary file first instead of
   writing the real file directly?

Answers:

1. Because "now" isn't always midnight. When the tool runs at 3pm, the
   hours from midnight to 3pm have already happened, so it
   uses the real weather for those hours to figure out the *true* current
   depletion, instead of pretending the day hasn't started yet. Only the
   hours still ahead of "now" get treated as the actual forecast.
2. It ignores it and starts fresh, exactly like it would if there were no
   saved file at all - full soil, starting now. `state.py`'s `load_state`
   checks the field/soil match and the age before ever trusting a saved
   number.
3. So an interruption (closing the terminal, a crash, a power cut) can
   never leave the real state file half-written and broken. Writing to a
   throw-away file first means the only thing that touches the real file
   is a single, instant swap: it either fully happened or didn't happen
   at all.
