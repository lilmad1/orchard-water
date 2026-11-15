"""A small web server that receives farmer replies from Meta's webhook.

Meta talks to it in two ways:
- GET once, when you register the webhook address in the Meta app: Meta
  sends a challenge number and the verify token you chose; we send the
  number back only if the token matches.
- POST every time something happens (a farmer replies, a message is
  delivered). We check Meta's signature, save each reply, and answer 200
  quickly, because Meta retries when it gets no answer.

UNTESTED against Meta (no account yet). Meta only calls a public https
address, so on a home PC this needs a tunnel in front of it (see
messages/README.md).
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from orchardwater.loop import handle_reply
from orchardwater.whatsapp import (
    ENV_APP_SECRET,
    ENV_VERIFY_TOKEN,
    parse_webhook,
    signature_is_valid,
)


def verify_challenge(query: str, verify_token: str) -> str | None:
    """The challenge to echo back if Meta's verify request is genuine, else None."""
    params = parse_qs(query)
    mode = params.get("hub.mode", [""])[0]
    token = params.get("hub.verify_token", [""])[0]
    challenge = params.get("hub.challenge", [""])[0]
    if mode == "subscribe" and verify_token and token == verify_token and challenge:
        return challenge
    return None


def make_handler(data_dir: Path, app_secret: str | None, verify_token: str) -> type:
    """Build the request handler class, with the settings it needs built in.

    http.server creates a new handler object per request and gives it no
    way to pass settings, so the settings are captured here instead.
    app_secret None means "do not check signatures" (local testing only).
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
            challenge = verify_challenge(urlparse(self.path).query, verify_token)
            if challenge is None:
                self._answer(403, "forbidden")
            else:
                self._answer(200, challenge)

        def do_POST(self) -> None:  # noqa: N802 - the name http.server looks for
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
            signature = self.headers.get("X-Hub-Signature-256")
            if app_secret is not None and not signature_is_valid(raw_body, signature, app_secret):
                self._answer(403, "bad signature")
                return
            try:
                replies = parse_webhook(json.loads(raw_body))
            except (ValueError, AttributeError, TypeError):
                self._answer(400, "unreadable body")
                return
            for reply in replies:
                record = handle_reply(reply, data_dir)
                print(f"reply saved: {record['farm_id']} {record['button_id'] or record['text']}")
            self._answer(200, "ok")

        def _answer(self, status: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(port: int, data_dir: Path, no_signature_check: bool = False) -> None:
    """Run the webhook server until Ctrl+C. Needs WHATSAPP_VERIFY_TOKEN and WHATSAPP_APP_SECRET."""
    verify_token = os.environ.get(ENV_VERIFY_TOKEN, "")
    app_secret = os.environ.get(ENV_APP_SECRET)
    if not verify_token:
        raise SystemExit(f"set {ENV_VERIFY_TOKEN} (any long random word you also type into Meta)")
    if no_signature_check:
        app_secret = None
        print("WARNING: signatures are not checked. Local testing only.", file=sys.stderr)
    elif not app_secret:
        raise SystemExit(f"set {ENV_APP_SECRET} (from the Meta app's Basic settings)")
    server = HTTPServer(("127.0.0.1", port), make_handler(data_dir, app_secret, verify_token))
    print(f"listening on http://127.0.0.1:{port} - Ctrl+C to stop", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
