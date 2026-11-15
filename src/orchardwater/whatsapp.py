"""Send the weekly message and read the farmer's replies over WhatsApp.

Two senders, with the same send() method:
- MockSender: sends nothing. It writes what WOULD be sent to a file and
  prints it. Use it until the Meta account, phone number and template exist.
- CloudApiSender: the real thing, through Meta's WhatsApp Cloud API.
  UNTESTED against Meta: there is no account yet. The request shapes follow
  Meta's published Cloud API docs.

The other half is reading Meta's webhook: when a farmer taps a button or
types, Meta POSTs a JSON body to our server. parse_webhook() turns that body
into Reply objects, and signature_is_valid() checks Meta really sent it.

Standard library only: urllib for the web request, hmac for the signature.
"""

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchardwater.weekly_message import WeeklyMessage

# Meta's API address. The version changes about every few months; set
# WHATSAPP_API_VERSION to the one shown in your Meta app if this one is retired.
GRAPH_URL = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v23.0"

# Settings are read from environment variables, never from a file in the repo.
ENV_TOKEN = "WHATSAPP_TOKEN"
ENV_PHONE_NUMBER_ID = "WHATSAPP_PHONE_NUMBER_ID"
ENV_APP_SECRET = "WHATSAPP_APP_SECRET"
ENV_VERIFY_TOKEN = "WHATSAPP_VERIFY_TOKEN"
ENV_API_VERSION = "WHATSAPP_API_VERSION"

# How a message goes out:
# - "template": an approved Meta template. Needed for the Monday message,
#   because a business may only send free text within 24 hours of the
#   farmer's own last message.
# - "text": free text with reply buttons. Only works inside that 24-hour
#   window, so it is for testing on your own phone after you text the number.
MODE_TEMPLATE = "template"
MODE_TEXT = "text"


@dataclass(frozen=True)
class SendResult:
    """What a sender reports back: Meta's message id (None for the mock) and the request body."""

    message_id: str | None
    payload: dict[str, Any]


def template_payload(
    phone: str, message: WeeklyMessage, name: str, language: str
) -> dict[str, Any]:
    """The Cloud API request body for the approved template, with its blanks and buttons.

    Each quick-reply button gets a payload ("ok", "question", "more_info") that
    comes back in the webhook when the farmer taps it, so we know which one it was.
    """
    body_params = [{"type": "text", "text": value} for value in message.template_params]
    components: list[dict[str, Any]] = [{"type": "body", "parameters": body_params}]
    for index, (button_id, _title) in enumerate(message.buttons):
        components.append(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(index),
                "parameters": [{"type": "payload", "payload": button_id}],
            }
        )
    return {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {"name": name, "language": {"code": language}, "components": components},
    }


def text_payload(phone: str, message: WeeklyMessage) -> dict[str, Any]:
    """The Cloud API request body for free text with three reply buttons (24-hour window only)."""
    buttons = [
        {"type": "reply", "reply": {"id": button_id, "title": title}}
        for button_id, title in message.buttons
    ]
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": message.text},
            "action": {"buttons": buttons},
        },
    }


class MockSender:
    """Sends nothing. Prints the message and returns no message id."""

    def send(self, payload: dict[str, Any], preview_text: str) -> SendResult:
        print(preview_text)
        return SendResult(message_id=None, payload=payload)


# A function that takes (url, body bytes, headers) and returns the response text.
# The real one uses urllib; tests pass a fake one so no test touches the internet.
PostFunction = Callable[[str, bytes, dict[str, str]], str]


def _post_with_urllib(url: str, body: bytes, headers: dict[str, str]) -> str:
    """POST to Meta and return the response text. Meta's error text is kept in the error."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text: str = response.read().decode("utf-8")
            return text
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"WhatsApp API said {error.code}: {detail}") from None


class CloudApiSender:
    """Sends through Meta's WhatsApp Cloud API. UNTESTED against Meta (no account yet)."""

    def __init__(
        self,
        token: str,
        phone_number_id: str,
        api_version: str = DEFAULT_API_VERSION,
        post: PostFunction = _post_with_urllib,
    ) -> None:
        self._token = token
        self._url = f"{GRAPH_URL}/{api_version}/{phone_number_id}/messages"
        self._post = post

    @classmethod
    def from_environment(cls) -> "CloudApiSender":
        """Build a sender from the WHATSAPP_* environment variables, or stop if one is missing."""
        missing = [name for name in (ENV_TOKEN, ENV_PHONE_NUMBER_ID) if not os.environ.get(name)]
        if missing:
            raise SystemExit(f"set these environment variables first: {', '.join(missing)}")
        version = os.environ.get(ENV_API_VERSION) or DEFAULT_API_VERSION
        return cls(os.environ[ENV_TOKEN], os.environ[ENV_PHONE_NUMBER_ID], version)

    def send(self, payload: dict[str, Any], preview_text: str) -> SendResult:
        """Send one message. Returns Meta's message id (it starts with "wamid.")."""
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        response_text = self._post(self._url, json.dumps(payload).encode("utf-8"), headers)
        response = json.loads(response_text)
        try:
            message_id = str(response["messages"][0]["id"])
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected WhatsApp API answer: {response_text}") from None
        return SendResult(message_id=message_id, payload=payload)


def template_create_payload(
    name: str, language: str, body: str, examples: list[str], buttons: list[str]
) -> dict[str, Any]:
    """The request body that asks Meta to create a Utility template with quick-reply buttons."""
    return {
        "name": name,
        "language": language,
        "category": "UTILITY",
        "components": [
            {"type": "BODY", "text": body, "example": {"body_text": [examples]}},
            {
                "type": "BUTTONS",
                "buttons": [{"type": "QUICK_REPLY", "text": title} for title in buttons],
            },
        ],
    }


def create_template(
    waba_id: str, payload: dict[str, Any], post: "PostFunction | None" = None
) -> str:
    """Submit a template to Meta for review. Returns Meta's answer as text.

    waba_id is the WhatsApp Business Account ID (Meta app -> WhatsApp ->
    API Setup). Needs WHATSAPP_TOKEN with whatsapp_business_management
    (the temporary API Setup token has it).
    """
    token = os.environ.get(ENV_TOKEN)
    if not token:
        raise SystemExit(f"set {ENV_TOKEN} first")
    version = os.environ.get(ENV_API_VERSION) or DEFAULT_API_VERSION
    url = f"{GRAPH_URL}/{version}/{waba_id}/message_templates"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return (post or _post_with_urllib)(url, json.dumps(payload).encode("utf-8"), headers)


@dataclass(frozen=True)
class Reply:
    """One thing a farmer sent us.

    kind is "button" (tapped OK, Question or More info: button_id says which)
    or "text" (typed a message), or "other" (a photo, voice note, ...).
    replied_to is Meta's id of our message they answered, when WhatsApp
    tells us (it does for button taps), so a reply can be tied to one week.
    """

    message_id: str
    phone: str
    received_at: str
    kind: str
    button_id: str | None
    text: str
    replied_to: str | None


def parse_webhook(body: dict[str, Any]) -> list[Reply]:
    """Pull every farmer message out of one webhook body. Delivery receipts are skipped.

    Meta nests it as entry[] -> changes[] -> value -> messages[]. A body with
    only "statuses" (sent, delivered, read) gives an empty list.
    """
    replies = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            for raw in change.get("value", {}).get("messages", []):
                replies.append(_reply_from_message(raw))
    return replies


def _reply_from_message(raw: dict[str, Any]) -> Reply:
    """One message from the webhook as a Reply."""
    kind, button_id, text = _content(raw)
    seconds = int(raw.get("timestamp", "0"))
    return Reply(
        message_id=str(raw.get("id", "")),
        phone=str(raw.get("from", "")),
        received_at=datetime.fromtimestamp(seconds, UTC).isoformat(),
        kind=kind,
        button_id=button_id,
        text=text,
        replied_to=raw.get("context", {}).get("id"),
    )


def _content(raw: dict[str, Any]) -> tuple[str, str | None, str]:
    """(kind, button_id, text) for the three shapes a reply can have."""
    message_type = raw.get("type")
    if message_type == "button":
        # A tap on a template's quick-reply button.
        button = raw.get("button", {})
        return "button", button.get("payload"), str(button.get("text", ""))
    if message_type == "interactive":
        # A tap on a reply button of a free-text message.
        reply = raw.get("interactive", {}).get("button_reply", {})
        return "button", reply.get("id"), str(reply.get("title", ""))
    if message_type == "text":
        return "text", None, str(raw.get("text", {}).get("body", ""))
    return "other", None, f"[{message_type}]"


def signature_is_valid(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """True if the X-Hub-Signature-256 header proves Meta sent this exact body.

    Meta signs the raw body with the app secret (HMAC-SHA256). Anyone can POST
    to a public address, so a body without a matching signature is ignored.
    compare_digest compares in constant time, so the check leaks nothing.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def append_record(path: Path, record: dict[str, Any]) -> None:
    """Add one JSON record as one line at the end of a log file (creates folders as needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_records(path: Path) -> list[dict[str, Any]]:
    """All records from a log file written by append_record(). A missing file is empty."""
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records
