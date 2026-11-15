# The weekly farmer card

## Overview

Until now the tool printed one long English report for the person running it.
A farmer needs something much shorter, in their own language, readable
on WhatsApp or on paper. The card maker takes the week's
result, fills in the blanks of a wording file written by the operator, and gives back
plain text for WhatsApp and a printable web page. The card says only when to
water and how much; the code contains no sentences for farmers at all.

## Map

```
weekly.py (fetch weather, run the water math, as before)
    -> result.py  WeeklyResult: what happened + the numbers + the old report text
    -> card.py    reads cards/your_wording.txt, checks it, fills in the blanks
    -> plain text (WhatsApp)   and   a printable page (.html)
```

Flags on the command line: `--card-template FILE` prints the card instead of
the report, `--card-html page.html` also writes the printable page, and
`--overwrite-card-html` allows replacing a page that already exists.

## File by file

- `result.py` (new). Holds `WeeklyResult`, a small container with the
  kind of outcome, the numbers, the deadline and the old report text. There
  are nine kinds: water by a deadline, pump too slow, deadline passed, a
  weekly amount (drip, no deadline), a weekly amount with a slow pump, no
  irrigation needed (flood), soil at its limit right now, soil reaching its limit at a
  later hour, and no estimate available. It also has `summary_kind()`, which picks the kind using the same comparisons
  the printed report uses, so the card and the report cannot disagree.

- `weekly.py` (changed). Two new functions, `compute_weekly_result` and
  `compute_flood_weekly_result`, return a `WeeklyResult`. The old functions
  keep their names and return exactly the same text as before, so no old test
  had to change. The three text-building functions were not touched. The two
  command-line flags are here, with checks that stop a card page from
  overwriting the wording file, the saved soil state, or any file that does
  not end in `.html`.

- `card.py` (new). Reads the wording file and checks it strictly: all
  twelve sections must be there, only allowed blanks may be used, and some
  blanks are required, so a deadline card can never leave out the deadline.
  It fills in the volume and the deadline (on cards with no deadline, the last day
  the advice covers; on the two cards with no numbers, the day the card was made)
  and writes the two outputs. Every
  word on a card comes from that file. Three sections are extra sentences that
  are added to certain cards automatically: a caution not to apply it all
  much earlier than the deadline, a note to report back after watering (on
  every card that says "water X m3"), and
  a note that a weekly amount is for the whole week.

- `cards/test_en.txt` (new). Fake English wording, marked "NOT FOR
  FARMERS", so the tests have something to fill in. `cards/README.md` explains
  how to write a wording file.

- Tests. `test_result.py`, `test_weekly_result.py`, `test_card.py` and
  `test_weekly_cli.py`. They check hand-worked numbers, every kind of card,
  every way a wording file can be broken, and every way the page file could be
  saved wrongly.

## Decisions

- The wording is a file the operator edits, not code, so changing a word cannot break
  the program.
- The engine works in any language; the tests use English only because that
  is what the tests can check.
- The card shows only when and how much (the water in m3). The first version had "decision plus the numbers"; a real card
  looked like too much.
  The result still carries the other numbers (mm, pump hours, hours left), so
  adding one back later is a small change.
- Pump hours are not on the card. They depend on a pump flow number that can
  drift, and a solar pump delivers different flow through the day.

## New Python ideas

- `@dataclass(frozen=True)`: a class that only holds named values, and
  cannot be changed after it is made. Example: `WeeklyResult(kind="x", text="y")`.
- `@property`: a value that is worked out when it is asked for, written as
  `result.pump_short_hours` with no brackets.
- `string.Formatter().parse(text)`: walks through a text and lists every
  `{blank}` in it. It checks the wording file before filling it in.
- `text.format(volume_m3="3192.0")`: replaces each `{volume_m3}` in a text
  with a value.
- `html.escape(text)`: turns a `<` or `&` in the wording into a safe
  form, so it cannot break the printable page.
- An import inside a function: `card.py` needs a helper from `weekly.py`,
  and `weekly.py` needs `card.py` for the flags, so importing at the top of
  both would go in a circle. Importing inside the function that needs it
  avoids that.
- `raise SystemExit("message")`: stops the program with a one-line message,
  used for mistakes the person running it can fix (a wrong file name).
- Writing to a temporary file, then `replace`: the page is written to
  `card.html.tmp` first and swapped in only if the write worked, so a failed
  write cannot leave a half-empty page.

## The math

The card's amount is the water volume, turned into m3 by the program (nobody
does this by hand). One millimetre of water over one square metre is one
litre.

Worked example, 12 ha (120,000 m2) and 26.6 mm of water to pump:

```
26.6 mm = 26.6 litres per m2 = 0.0266 m3 per m2
0.0266 m3/m2 x 120,000 m2 = 3192 m3
```

At a pump flow of 25 m3 per hour, that takes 3192 / 25 = 127.68 hours. That
figure is checked against the time left before the deadline (that is how
"pump too slow" is decided), but it is not printed on the card.

The deadline is shown in Casablanca clock time. 09:00 UTC on 9 July is 10:00
in Casablanca (Morocco is on UTC+1 in July), so the card says 10:00. On the
one day a year when the clocks go back, 01:00 happens twice, so the card adds
the offset itself, like `01:00 (UTC+1)`.

## Check questions

1. Why does the card not print the pump hours, even though the program works
   them out?
2. A wording file for the "water by a deadline" card forgets `{deadline_time}`.
   What happens, and why is that better than printing the card?
3. Why does `--card-html` refuse a file that already exists unless `--overwrite-card-html` is given?
4. What are the three sentences added automatically to some cards, and who
   writes them?
5. 15 mm of water on 4 ha: how many m3?

Answers

1. Pump hours only hold if the pump flow given to the program is still right,
   and a solar pump's flow changes through the day. A wrong hours figure makes
   the farmer under-water or overwater without knowing. The volume in m3 is
   true either way, and the card was kept to when and how much.
2. The program stops with a message saying which section must contain
   `{deadline_time}`, before anything is printed. A card that never says the
   deadline could send a farmer to water at the wrong time.
3. A mistyped path could replace some other page, such as a website's
   `index.html`. The flag makes replacing a file something that must be asked for explicitly.
4. A caution not to apply it all much earlier, a note to report back after
   watering, and a note that a weekly amount is for the whole week. The operator writes
   them, because the code contains no sentences for farmers.
5. 15 mm = 0.015 m3 per m2. 4 ha = 40,000 m2. 0.015 x 40,000 = 600 m3.
