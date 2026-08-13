#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# ///
"""Encode a JSON envelope from stdin into a base64url-encoded RFC 2822 message
suitable for `gws gmail users drafts create --params '{"userId":"me"}' --json '{"message":{"raw":"<stdout>"}}'`.

Input (stdin) — JSON with these fields (only `to`, `subject`, `body` required):

    {
      "to": "alice@example.com, bob@example.com",
      "cc": "carol@example.com",
      "bcc": "",
      "subject": "Q3 review notes",
      "body": "Hi Alice,\\n\\n...\\n",
      "in_reply_to_message_id": "<abc123@mail.gmail.com>"
    }

Output (stdout) — single line: base64url-encoded message, no padding.
"""

import base64
import json
import sys
from email.message import EmailMessage


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"invalid JSON on stdin: {e}", file=sys.stderr)
        return 1

    to = payload.get("to", "").strip()
    subject = payload.get("subject", "").strip()
    body = payload.get("body", "")

    if not to:
        print("missing required field: to", file=sys.stderr)
        return 1
    if not subject:
        print("missing required field: subject", file=sys.stderr)
        return 1
    if not body:
        print("missing required field: body", file=sys.stderr)
        return 1

    msg = EmailMessage()
    msg["To"] = to
    if cc := payload.get("cc", "").strip():
        msg["Cc"] = cc
    if bcc := payload.get("bcc", "").strip():
        msg["Bcc"] = bcc
    msg["Subject"] = subject

    if reply_to := payload.get("in_reply_to_message_id", "").strip():
        # Gmail API returns IDs without angle brackets; RFC 2822 requires <id> form.
        if not reply_to.startswith("<"):
            reply_to = f"<{reply_to}>"
        msg["In-Reply-To"] = reply_to
        msg["References"] = reply_to

    msg.set_content(body)

    encoded = base64.urlsafe_b64encode(bytes(msg)).rstrip(b"=").decode("ascii")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
