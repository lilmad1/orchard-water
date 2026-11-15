# The weekly WhatsApp loop

## Overview

A command that runs the water engine once per block of a farm, turns each
block's water into hours per day, and sends the farmer one message with three
buttons. A second command receives the farmer's button taps and typed
messages from Meta and saves them. Until there is a Meta account, a mock
sender prints the message instead of sending it.

## Map

    farms/<farm>.json --> farm.py --+
                                    +--> loop.py send --> weekly.py (once per block)
    messages/<lang>.txt --> weekly_message.py <--------------+
                                    |
                                    +--> whatsapp.py (mock or Cloud API) --> data/whatsapp/outbox.jsonl

    Meta webhook --> webhook.py --> whatsapp.parse_webhook --> loop.handle_reply
                                                            --> data/whatsapp/replies.jsonl

## File by file

- `farm.py`: reads and checks the farm file. Refuses a phone number with a "+"
  or spaces, two blocks with the same id, and ids that could break a file name.
- `weekly_message.py`: reads the wording file, checks every blank, turns valve
  hours into hours per day, and puts the message together. Also prints the
  template text for Meta.
- `whatsapp.py`: builds the exact JSON Meta expects, sends it (or not, for the
  mock), reads Meta's webhook JSON, and checks Meta's signature.
- `webhook.py`: the small web server Meta calls when a farmer replies.
- `loop.py`: the command line, and the glue: run blocks, send, save, match a
  reply to the message it answers.

## New Python ideas

- JSON Lines: one JSON object per line in a file. Adding a record is just
  adding a line, so a crash never breaks the earlier records.
- Subcommands (`argparse.add_subparsers`): one program, several commands,
  like `git commit` and `git status`.
- HMAC signature: Meta mixes the message with a secret only the app owner and Meta
  know. If the server computes the same mix, Meta sent it.
      hmac.new(secret, body, hashlib.sha256).hexdigest()
- A class made inside a function (`make_handler`): the web server makes a
  new handler for every request and cannot pass settings, so the settings are
  baked into the class when it is made.

## The water math

Block A: 4 ha, valve flow 40 m3/h. The engine says 400 m3 for the week, so
400 / 40 = 10 valve hours. The message is made Monday 07:00 and the forecast
ends Sunday 19:00. Calendar days Monday to Sunday = 7 (Sunday counts because
the end is after 12:00). 10 / 7 = 1.43 h a day = 85.7 minutes, rounded UP to
5 minutes = 90 minutes, shown as 1:30. Up, so 7 x 1:30 = 10:30 is never less
than the 10 hours asked for.

## Check questions

1. Why does `send` refuse to run twice in the same week?
2. Why must a block line in the wording file be on one line?
3. A farmer taps OK. Is the reply marked as needing an answer?
4. Why does the webhook server check a signature?

Answers: 1) Each run moves the blocks' saved soil state forward, and the farmer
would get the message twice. 2) The block lines go inside one Meta template
blank, and Meta does not allow line breaks there. 3) No, OK needs no answer;
only Question, More info and typed messages do. 4) The address is public, so
anyone could post fake replies; only Meta knows the app secret.
