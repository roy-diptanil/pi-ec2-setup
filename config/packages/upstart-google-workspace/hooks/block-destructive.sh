#!/usr/bin/env bash
# PreToolUse hook on Bash. Rejects destructive `gws` operations and direct
# Gmail sends to recipients outside Upstart.
#
# Detection runs in Python so the matcher can robustly tolerate wrapper patterns
# (`sudo gws ...`, `command gws ...`, `FOO=bar gws ...`, absolute-path
# `/usr/local/bin/gws ...`, and even `bash -c "gws ..."` / `eval "..."` cases).
#
# Exit 0  → allow (command is not gws, or is a safe gws call)
# Exit 2  → deny (stderr message surfaces to Claude)
set -uo pipefail

payload="$(cat || true)"

# If python3 is missing, avoid blocking unrelated Bash commands. We cannot
# parse the JSON envelope robustly, but a raw payload scan is enough to fail
# closed for likely destructive gws invocations without matching setup script
# names such as install-gws.sh.
if ! command -v python3 > /dev/null 2>&1; then
  if [[ "$payload" != *'"command"'* ]]; then
    exit 0
  fi

  if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" && "$payload" == *"${CLAUDE_PLUGIN_ROOT}/scripts/"* ]]; then
    exit 0
  fi

  if [[ "$payload" == *"install-gws.sh"* ||
    "$payload" == *"install-secret.sh"* ||
    "$payload" == *"setup-oauth.sh"* ]]; then
    exit 0
  fi

  gws_word='(^|[^[:alnum:]_-])[gG][wW][sS]([^[:alnum:]_-]|$)'
  # Left boundary explicitly allows `+` (e.g. `gws gmail +send ...`) so the
  # fail-closed fallback never accidentally lets a direct-send verb through
  # when python3 is unavailable.
  destructive_word='(^|[^[:alnum:]+_-])\+?(delete|trash|remove|send|forward|reply|reply-all|clear|purge|empty)([^[:alnum:]_-]|$)'
  camel_destructive_word='(^|[^[:alnum:]_-])([[:alnum:]]+(Delete|Trash|Remove|Clear|Purge|Empty)[[:alnum:]]*|(delete|trash|remove|clear|purge|empty)[[:upper:]][[:alnum:]]*)([^[:alnum:]_-]|$)'
  replacement_word='(^|[^[:alnum:]+_-])(\+push|updateContent)([^[:alnum:]_-]|$)'
  # Bash dequotes tokens like `g'w's` or `g\ws` to `gws` (and `+'s'end` to
  # `+send`) at execution time, so a raw-substring scan alone would let an
  # obfuscated direct send skip this prefilter. Also scan a copy with single
  # quotes, double quotes, and backslashes removed, so the dequoted form is
  # matched too. This is a coarse fail-closed prefilter (python3 is missing);
  # over-matching only denies a command that could not be checked anyway.
  stripped_payload="${payload//[\'\"\\]/}"
  # Bash ANSI-C quoting can synthesize any protected token from escapes (for
  # example, $'g\x77s' and $'del\x65te'). Without Python there is no safe full
  # shell parser, so ANY ANSI-C quoting makes the command unverifiable and is
  # denied. This conservative rule avoids fragile partial escape decoding.
  ansi_c_quoted=false
  if [[ "$payload" == *"\$'"* ]]; then
    ansi_c_quoted=true
  fi
  looks_like_destructive_gws() {
    local text="$1"
    [[ "$text" =~ $gws_word &&
      ("$text" =~ $destructive_word || "$text" =~ $camel_destructive_word || "$text" =~ $replacement_word) ]]
  }
  if [[ "$ansi_c_quoted" != true ]] &&
    ! looks_like_destructive_gws "$payload" &&
    ! looks_like_destructive_gws "$stripped_payload"; then
    exit 0
  fi

  cat >&2 << 'EOF'
google-workspace plugin: python3 is required by the no-delete/no-send enforcement
hook but is not on PATH. Install python3 and retry so gws commands can be
checked safely.
EOF
  exit 2
fi

# shellcheck disable=SC2016
if ! result="$(printf '%s\n' "$payload" | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/check-destructive.py" 2>&1)"; then
  cat >&2 << EOF
google-workspace plugin: failed to inspect Bash command for destructive gws
operations or Gmail recipients. The no-delete/no-send policy fails closed when
payload parsing or matching fails.

$result
EOF
  exit 2
fi

verdict="$(printf '%s\n' "$result" | head -1)"

# Every DENY_* verdict is encoded as: line 1 = verdict, line 2 = number of
# single-line detail lines (k), lines 3..2+k = detail lines, and everything
# after = the command (which may span multiple lines for a multiline Bash
# payload). Reading the count lets us separate the details from a multiline
# command unambiguously instead of guessing at line positions.
if [[ "$verdict" == DENY_* ]]; then
  detail_count="$(printf '%s\n' "$result" | sed -n '2p')"
  if [[ ! "$detail_count" =~ ^[0-9]+$ ]]; then
    cat >&2 << EOF
google-workspace plugin: the no-delete/no-send matcher returned a malformed
result (expected a detail-line count on line 2):

$result
EOF
    exit 2
  fi
  details=""
  if ((detail_count > 0)); then
    details="$(printf '%s\n' "$result" | sed -n "3,$((2 + detail_count))p" | sed 's/^/  /')"
  fi
  # Raw (unindented) command for the copy-paste bypass hint; indented copy for
  # display under "Blocked command:".
  raw_cmd="$(printf '%s\n' "$result" | tail -n +"$((3 + detail_count))")"
  blocked_cmd="$(printf '%s\n' "$raw_cmd" | sed 's/^/  /')"
fi

if [[ "$verdict" == "DENY_DESTRUCTIVE" ]]; then
  cat >&2 << EOF
google-workspace plugin policy: destructive Google Workspace operations are
blocked, including delete, trash, remove, clear, purge, empty-trash, and
whole-resource replacement methods (no-delete policy).

Blocked command:
$blocked_cmd

To run it directly (bypassing the Pi agent policy), paste this at the Pi prompt:

  ! $raw_cmd
EOF
  exit 2
fi

if [[ "$verdict" == "DENY_EMAIL_EXTERNAL" ]]; then
  cat >&2 << EOF
google-workspace plugin policy: direct Gmail sends cannot include recipients
outside @upstart.com.

Blocked command:
$blocked_cmd

External recipient(s):
$details

Save messages to external recipients as drafts, then review and send them from
Gmail manually.
EOF
  exit 2
fi

if [[ "$verdict" == "DENY_EMAIL_UNVERIFIED" ]]; then
  cat >&2 << EOF
google-workspace plugin policy: direct Gmail sends are allowed only when every
literal --to/--cc/--bcc recipient is an @upstart.com address.

Blocked command:
$blocked_cmd

Could not verify:
$details

Use literal Upstart recipients, add --draft, or save the message as a draft for
manual review.
EOF
  exit 2
fi

if [[ "$verdict" == "DENY_SUBSTITUTION" ]]; then
  cat >&2 << EOF
google-workspace plugin policy: a command substitution in this gws command's
arguments could expand into flags, recipients, or destructive methods that the
no-delete/no-send policy cannot inspect, so it fails closed.

Blocked command:
$blocked_cmd

Unverifiable argument(s):
$details

Replace the substitution(s) with literal values, or quote a substitution used
as a flag value (e.g. --subject "\$(date)") so it cannot expand into extra
arguments.
EOF
  exit 2
fi

if [[ "$verdict" == "DENY_EMAIL_RAW" ]]; then
  cat >&2 << EOF
google-workspace plugin policy: raw Gmail send methods are blocked because the
hook cannot inspect MIME/JSON recipients.

Blocked command:
$blocked_cmd

Use \`gws gmail +send\` with literal @upstart.com --to/--cc/--bcc recipients,
or save the message as a draft for manual review.
EOF
  exit 2
fi

if [[ "$verdict" == "ERROR" ]]; then
  parser_error="$(printf '%s\n' "$result" | tail -n +2 | sed 's/^/  /')"
  cat >&2 << EOF
google-workspace plugin: failed to inspect Bash command for destructive gws
operations or Gmail recipients. The no-delete/no-send policy could not safely
parse the command and has failed closed.

Parser error:
$parser_error
EOF
  exit 2
fi

if [[ "$verdict" != "ALLOW" ]]; then
  cat >&2 << EOF
google-workspace plugin: failed to inspect Bash command for destructive gws
operations or Gmail recipients. The no-delete/no-send policy received an
unexpected matcher result:

$result
EOF
  exit 2
fi

exit 0
