---
name: google-workspace-mail-draft
description: |
  Compose and save a Gmail draft (never sends). Use when the user asks to draft an email,
  save an email for later, prepare a message for review, or compose a reply they want to
  edit before sending. Wraps `gws gmail users drafts create`. Sending is blocked by the
  plugin's PreToolUse hook; drafting is the only write operation available.
disable-model-invocation: true
allowed-tools:
  - Bash
---

# mail-draft

Compose and save a Gmail draft to the authenticated user's Drafts folder. Sending is blocked by the plugin's
`PreToolUse` hook, which rejects any `gws ... send` invocation.

## When to use

Auto-trigger on phrases like:

- "draft an email to ..."
- "save a Gmail draft for me ..."
- "prepare a reply to ... that I'll edit later"
- "compose a message to ... for review"

Do **not** auto-trigger when the user asks to "send" something — that's not in scope. If the user clearly wants to send,
tell them: "This plugin only drafts mail; you'll need to open Gmail to send."

## Inputs

Gather these from the user's prompt (ask if any are missing and not inferable):

- `to` — comma-separated recipient(s)
- `cc`, `bcc` — optional
- `subject` — required
- `body` — required; markdown is fine, will be sent as `text/plain`. If the user wants HTML, mention they can edit the
  draft in Gmail before sending.
- `in_reply_to_message_id` — optional; the message ID of an existing thread to attach the draft to. If the user is
  replying to a Gmail thread, look up the message ID first via `gws-gmail-read`.

## How to call

Build a JSON payload with the inputs and pipe it to the encoder script. Then build a Gmail `users.drafts.create` request
body and call the raw `gws` method with the required `userId` path parameter:

```bash
ENCODED=$(cat <<'JSON' | "__PI_AGENT_DIR__/packages/upstart-google-workspace/scripts/encode-rfc2822.py"
{
  "to": "alice@upstart.com",
  "cc": "",
  "bcc": "",
  "subject": "Q3 review notes",
  "body": "Hi Alice,\n\nHere's my draft notes for our review...\n\n— me",
  "in_reply_to_message_id": ""
}
JSON
)
DRAFT_BODY=$(ENCODED="$ENCODED" python3 - <<'PY'
import json
import os

print(json.dumps({"message": {"raw": os.environ["ENCODED"]}}))
PY
)
gws gmail users drafts create --params '{"userId":"me"}' --json "$DRAFT_BODY"
```

The CLI returns a JSON response with the draft ID (something like `r2384572983457234`). Echo it to the user along with
the Gmail web URL so they can open the draft to review and send:

```text
Draft saved.
ID:   r2384572983457234
View: https://mail.google.com/mail/u/0/#drafts/r2384572983457234
```

## Threading

If `in_reply_to_message_id` is provided, the encoder will set the `In-Reply-To` and `References` headers so the draft
appears in the existing Gmail thread when the user reviews it.

## Errors

If `gws gmail users drafts create` fails:

- `401` — OAuth tokens expired. Tell the user to run `/google-workspace-auth`.
- `403 insufficient scope` — `gmail.compose` was not granted. Run `/google-workspace-auth` to re-grant.
- Other — surface stderr to the user.
