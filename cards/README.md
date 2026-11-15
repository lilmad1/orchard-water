# Writing a farmer card

A card is one text file. You write the words; the program fills in the
numbers. Nothing in the code contains a sentence for a farmer.

## Try it

    python -m orchardwater.weekly --lat 30.47 --lon -8.88 --ha 12 --pump-m3h 25 ^
        --efficiency 0.9 --ec-water 1.2 --card-template cards/test_en.txt

The card is printed on normal output, so `> card.txt` saves only the card. The
full English report, with the reasons the card leaves out (a saved state that
cannot be read, a weather problem), goes to the error stream and stays on your
screen.

Add `--card-html card.html` to also write a page you can open in the browser
and print (Ctrl+P). `cards/test_en.txt` is fake test wording. Copy it and
change the words to make your own, for example `cards/darija.txt`.
The page name must end in `.html` or `.htm`. If the file already exists the
program refuses, so a typo cannot replace some other page; add
`--overwrite-card-html` when you do want to replace last week's card.

## The file

Settings first, one per line:

    language = ar
    direction = rtl
    title = the heading at the top of the printed page

`direction` is `ltr` (left to right) or `rtl` (right to left, for Arabic).
Lines starting with `#` are notes for you and are ignored, everywhere in the
file: a wording line that starts with `#` would be dropped, so put a space or a
word in front of it. A wording line that is only a word in square brackets is
read as a section heading. Save the file as UTF-8 (Notepad's "UTF-8 with BOM"
is fine too).

Then twelve sections, each starting with its name in square brackets on its
own line, and your wording under it. All twelve must be there. A blank line
starts a new paragraph on the printed page.

| Section | When the farmer gets it |
|---|---|
| `[irrigate_by_deadline]` | Water by a time, and the pump can do it |
| `[pump_too_slow]` | There is a deadline, but the pump cannot deliver in time |
| `[deadline_passed]` | The deadline is already over |
| `[weekly_amount]` | Drip: an amount for the week, no deadline. A week that needs no water at all (an amount of exactly 0, for example when rain covers it) uses `[no_irrigation_due]` instead, so this card never says "water 0 m3" |
| `[weekly_pump_too_slow]` | Same, but the pump is too slow |
| `[no_irrigation_due]` | Flood: nothing needed this week |
| `[soil_at_limit]` | The program says the soil has reached the stress limit, or will reach it within hours, and it cannot size a safe amount. Write it as an urgent "check the field now" |
| `[soil_limit_ahead]` | The program says one hour's own water use alone will reach the stress limit at a LATER hour, which can be days away, and it cannot size a safe amount. It says which day and time. Write it as a warning to check the field, not as "now" |
| `[early_watering_caution]` | Not a situation: this wording is added at the end of every `[irrigate_by_deadline]` card. The report warns that the amount is worked out for the moment of the deadline and applying it all much earlier could overwater. The program cannot write that sentence, so you must |
| `[after_watering_note]` | Not a situation either: added at the end of every card that says "water X m3": `[irrigate_by_deadline]` (after the caution), `[pump_too_slow]`, `[weekly_amount]` and `[weekly_pump_too_slow]` (after the weekly note). The next card is only right if the program is told that water was applied, and the person who runs the program is not the farmer reading the card, so this is what the farmer can do: report back when and how much was watered. The program cannot write that sentence, so you must |
| `[weekly_timing_note]` | Another added sentence, not a situation: added at the end of the two drip cards that have no deadline (`[weekly_amount]` and `[weekly_pump_too_slow]`). The report says that amount is worked out from the soil at the END of the forecast week, not for any single earlier hour, so applying all of it right away could overwater. The program cannot write that sentence, so you must |
| `[unavailable]` | The program cannot give a trustworthy number: the weather service failed or gave wrong data, the saved state cannot be read, and similar. No reason is shown, on purpose: the reason is English text written by the code, not by you. Because the farmer cannot tell these cases apart, word it so it also says to check the field yourself |

## Blanks

A blank is a word in curly brackets, like `{deadline_date}`. The program
replaces it with a number or a time. Each section may only use certain blanks,
and some blanks are required, so a card can never leave out the deadline or
the amount by mistake. If you break a rule, the program stops with a message
saying which section and which blank.

| Blank | Meaning | Example |
|---|---|---|
| `{deadline_date}` | day to have watered by, Casablanca time | 2026-09-21 |
| `{deadline_time}` | time of day. On the one day a year when Morocco's clocks go back, an hour happens twice; then the program adds the offset itself, like `01:00 (UTC+1)`, so the farmer cannot mix the two up | 21:00 |
| `{volume_m3}` | how much water to apply, in cubic metres | 5832.0 |
| `{made_on}` | the day this card was made, Casablanca time. Only on the two cards with no numbers, `[soil_at_limit]` and `[unavailable]`, and required there: an old printed copy or a forwarded message would otherwise look like a fresh one | 2026-09-30 |
| `{limit_date}`, `{limit_time}` | the day and time the soil is expected to reach its limit, Casablanca time. Only on `[soil_limit_ahead]`, and required there | 2026-10-02, 14:00 |
| `{valid_until}` | the last day the advice covers, Casablanca time. Only on the three cards that have no deadline (`[weekly_amount]`, `[weekly_pump_too_slow]`, `[no_irrigation_due]`), and required there: the forecast can be well under seven days on a run late in the day, so without a date the farmer cannot tell when the card runs out | 2026-09-26 |

Only these seven exist on purpose: a card should say just when and how much.
The program also knows the amount in mm, the pump hours, the hours left and how
far a slow pump is short by, but a card cannot show them. If you later want one
of them on a card, that is a small change in `card.py`.

Which sections may use which blanks, and which they must use, is written at
the top of `src/orchardwater/card.py` (`ALLOWED_BLANKS` and `REQUIRED_BLANKS`).
To print a curly bracket itself, write it twice: `{{` and `}}`.

## Things to check yourself

- Dates are always written as year-month-day, numbers with a decimal point.
  The program cannot change these to another style yet.
- Arabic and other right-to-left text: open the printed page and send a
  test message on your own phone. The program cannot check that it looks
  right.
