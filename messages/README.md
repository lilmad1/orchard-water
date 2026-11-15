# The weekly WhatsApp loop

Every Monday, for one farm: run the water engine for each block, send the
farmer one WhatsApp message with hours per day per block and three buttons
(OK, Question, More info), and save what the farmer answers.

Status: works with the mock sender (sends nothing, prints the message).
The real WhatsApp part is written but UNTESTED against Meta, because there is
no Meta account yet. The French and Arabic wordings are DRAFTS, not yet checked by a native speaker.

## Try it (no Meta account needed)

    python -m orchardwater.loop send --farm farms/example.json
    python -m orchardwater.loop simulate-reply --farm farms/example.json --button question
    python -m orchardwater.loop replies

`send` needs internet for the weather. It prints each block's full English
report on the error stream and the farmer's message on normal output.
It saves:

- `data/whatsapp/outbox.jsonl`: one line per message sent, with each block's
  numbers (volume, valve hours, hours per day), so a week can be checked later.
- `data/whatsapp/replies.jsonl`: one line per reply. `needs_answer` is true
  for Question, More info and typed messages.
- `data/whatsapp/state/<farm>/<block>.json`: each block's soil state.

`send` refuses to send the same farm twice in one week (add `--force`), and
refuses to send if any block failed (add `--send-anyway` to leave it out).

## Files you write

A farm file (copy `farms/example.json`): farmer name, phone number
(digits only, country code first: 2126...), which wording file to use, and
one entry per block. `flow_m3_per_h` is the water that reaches that block per
hour when its valve is open; it turns the volume into hours. Real farm files
stay on your PC (they are in `.gitignore`).

A wording file (see `test_en.txt`, `draft_fr.txt`, `draft_ar.txt`). Same
idea as the cards: you write the words, the program fills the blanks.

| Section | Blanks (all required) | Used when |
|---|---|---|
| `[greeting]` | `{farmer_name}`, `{made_on}` | always, first |
| `[update_greeting]` | `{farmer_name}`, `{made_on}` | first line of a mid-week update |
| `[block_hours]` | `{block}`, `{hours_per_day}`, `{until_date}` | the block needs water |
| `[block_no_water]` | `{block}`, `{until_date}` | no water needed (rain covers it) |
| `[block_pump_too_slow]` | `{block}` | the valve cannot give enough water in time |
| `[block_check_field]` | `{block}` | soil at or near its limit, or deadline passed: look at the field |
| `[block_unavailable]` | `{block}` | no trustworthy number (weather problem and similar) |
| `[block_schedule]` | `{block}`, `{schedule}` | the block needs water and the days differ (weather) |
| `[schedule_day]` | `{day}`, `{hours}` | one day inside a schedule, like `lun 1:20` |
| `[day_off]` | none | a day with no watering inside a schedule (`rien`) |
| `[day_names]` | none | 7 short day names, Monday first, split by `|` |
| `[closing]` | none | always, last |
| `[button_ok]`, `[button_question]`, `[button_more_info]` | none | button titles, max 20 characters |

`{hours_per_day}` is shown as H:MM, rounded UP to 5 minutes (1:30 = one
hour thirty), so the week's total is never short. Block lines must be one line each, because WhatsApp templates
do not allow line breaks inside a blank.

Settings at the top: `language`, `direction` (ltr/rtl), `meta_template` (the
template name in Meta), `meta_update_template` (the second template, for
mid-week updates), `meta_language` (fr, ar, ...), `plan_separator` (what
goes between block lines in the template, for example `;`).

## How hours per day are worked out (check this)

For each block the engine gives valve hours for the period (volume / flow).
The period ends at the deadline if the engine found one, else at the end of
the forecast. The loop spreads the hours evenly over the CALENDAR days in
that period (Casablanca time), today included:

- The last day only counts if the period ends at 12:00 or later; otherwise
  the plan stops the day before.
- If the last day is a deadline day, the message shows the clock time too
  ("until 2026-11-04 15:00").
- If one day's share does not fit in the hours that day really has (today
  after the message, a deadline day before the deadline), the block gets
  the "pump too slow" line instead of an impossible number.

Example: Monday 07:00 to Sunday 19:00, 10 h -> 7 days -> 1.43 h -> 1:30 a day.

Day by day, the engine also gives each day's forecast water
use (crop use minus rain). Each day gets its own use (a hot day more, a day
where rain covers the use none), plus a share of the dryness the soil had
at the start. In a dry week that adds up to exactly the engine's weekly
total. In a week with a rainy day it adds up to a bit MORE: watered every
day, the soil is full when the rain comes, so the extra rain drains away
instead of being stored. If the
days end up less than 15 minutes apart, the farmer gets the simple
"H:MM per day" line instead. If the split does not fit in a day's real
hours, the even split is used. Example line:

    Bloc A : lun 2:00, mar 2:00, mer rien, jeu 2:00, ven 4:00, sam 2:00, dim 2:00

All days go inside the template's plan blank, so the Meta templates do not
change. Not yet: pulses.

## Mid-week updates

    python -m orchardwater.loop update --farm farms/<farm>.json --sender cloud

Run it every morning Tuesday to Sunday (Task Scheduler, 07:00). It reruns the
engine as if the farmer followed the plan so far, on a throwaway copy of the
soil state, so the real state and next Monday are untouched. It sends a short
update (all blocks, from today on) only if a block's line changes type (for
example "no water" becomes hours) or one day's hours change by 15 minutes or
more, at most once a day and twice a week. The part of the soil's start
dryness that the old plan still owed is kept in the new plan.

After an update, the farmer's OK must answer the update (the newest plan of
the week), or next Monday does not count the week as watered.

It needs a second template per language at Meta, with the SAME three
buttons. Print its text with `meta-template` (shown under "second template").
UNTESTED in the field: thresholds are guesses for field trials.

This spreading is a choice, not FAO-56.

## When the farmer taps OK

Design decision: OK means "watered as planned". Next Monday, every
block that got hours per day until the end of the week starts from full soil.
Blocks with no OK, a "no water" line or a deadline line keep drying in the
model, so they end up asking for a field check. This is a guess until the
meter reader exists.

To stop an OK being used on the wrong plan, it only
counts when it answers the LAST message of the week right before this one,
from the same sender (mock and cloud are kept apart), and nothing was sent
yet this week. A skipped week, an OK on an older or replaced message, or a
forced resend never resets the soil. A reply with a WhatsApp message id we
do not know, or from a different phone, is saved with no farm and no week.

## What you must set up at Meta yourself

Nothing here costs money until real messages go to farmers. Steps, in order:

1. Meta Business account: business.facebook.com. You can start as a
   person; business verification (needs the AB papers) comes later and
   raises the sending limits.
2. Meta app: developers.facebook.com -> Create app -> type Business ->
   add the WhatsApp product. Meta gives you a free test number and a
   temporary token (24 hours). Add your own phone as a test recipient.
3. First real test: set the variables below with the test number's
   "Phone number ID" and the temporary token, text the test number from your
   phone, then within 24 hours run:

       python -m orchardwater.loop send --farm farms/me.json --sender cloud --mode text

   (`farms/me.json` = a copy of the example with your own number.)
   `--mode text` only works inside the 24 hours after your own message.
4. Template (needed for the Monday message): WhatsApp Manager -> Message
   templates -> Create. Category Utility. Print the exact text with

       python -m orchardwater.loop meta-template --wording messages/draft_fr.txt

   and paste it. Add the three quick-reply buttons in the order shown. Meta
   asks for sample values for {{1}}, {{2}}, {{3}}. Approval usually takes
   minutes to a day. One template per language.
5. Permanent token: Business settings -> System users -> add one, give it
   the WhatsApp app with `whatsapp_business_messaging`, generate a token.
6. Real phone number: a number not already used in the WhatsApp app.
   Meta reviews the
   display name.
7. Webhook (to receive replies): Meta only calls a public https address.
   Run `python -m orchardwater.loop serve` and put a tunnel (for example Cloudflare Tunnel) in front of
   it. In the Meta app: WhatsApp ->
   Configuration -> Callback URL = the tunnel address, Verify token = your
   WHATSAPP_VERIFY_TOKEN, then subscribe to the `messages` field.
8. Payment method in WhatsApp Manager before sending templates to farmers.
9. Farmer consent: Meta requires that each farmer agreed to get WhatsApp
   messages from Orchard Water. Keep a note of when and how.

Settings, as environment variables (never in a file in this folder). See .env.example at the repository root for the names. For this shell only (Linux/macOS):

    export WHATSAPP_TOKEN=...
    export WHATSAPP_PHONE_NUMBER_ID=...
    export WHATSAPP_APP_SECRET=...      # App settings -> Basic -> App secret
    export WHATSAPP_VERIFY_TOKEN=...    # any long random word you choose
    export WHATSAPP_API_VERSION=v23.0   # optional; use the version your app shows

## Every Monday

A scheduler (cron on Linux, Task Scheduler on Windows) can run `send` at 07:00
on Mondays, for example `python -m orchardwater.loop send --farm farms/<farm>.json
--sender cloud`, started from the repository root with the virtual environment
active. The machine must be on. The webhook server must run
all the time to catch replies, so for real farmers this should later move
to a small always-on server.

## The chatbot hook

`src/orchardwater/chatbot.py` has one function, `answer()`. It is called for every
Question, More info or typed reply, and today returns nothing, so replies
wait in `replies.jsonl` for you. The chatbot step replaces its body.
