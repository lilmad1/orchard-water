"""Where the question chatbot will plug in later (this module is only the hook).

When a farmer taps Question or More info, or types a message, loop.py saves
the reply and then calls answer(). Today answer() returns None: no automatic
answer is sent, and the reply waits in the replies file for the operator to read.

When the chatbot is built, it replaces the body of answer(). It gets the saved
reply record (farm, week, button or text) and returns the text to send back,
or None to leave it for a person. Nothing else in the loop needs to change.
"""

from typing import Any


def answer(reply_record: dict[str, Any]) -> str | None:
    """Return a reply to send to the farmer, or None for "a person answers". Always None for now."""
    return None
