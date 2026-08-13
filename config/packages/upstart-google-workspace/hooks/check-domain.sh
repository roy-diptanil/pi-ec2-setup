#!/usr/bin/env bash
# PreToolUse hook on Bash. For any gws invocation, validates that userId
# parameters and bare email addresses use @upstart.com domains.
# Blocks commands targeting external domains.
#
# Defence in depth — complements the OAuth-scope layer and the Workspace
# admin controls filed in ITENG-694.
#
# Exit 0  → allow
# Exit 2  → deny (stderr message surfaces to Claude)
set -uo pipefail

payload="$(cat || true)"

if ! command -v python3 > /dev/null 2>&1; then
  gws_word='(^|[^[:alnum:]_-])[gG][wW][sS]([^[:alnum:]_-]|$)'
  # ANSI-C quoting can hide either `gws` or an address delimiter (for example,
  # $'g\x77s' or $'\x40'). With no Python parser available, ANY ANSI-C quoting
  # makes the command unverifiable, so fail closed instead of partially
  # decoding escapes in shell.
  if [[ "$payload" == *"\$'"* ]]; then
    cat >&2 << 'EOF'
google-workspace plugin: python3 is required by the domain-restriction enforcement
hook but is not on PATH. Install python3 and retry so gws commands can be
checked safely.
EOF
    exit 2
  fi
  # Bash dequotes `g'w's` and `g\ws` to `gws`. Mirror the destructive hook's
  # conservative fallback scan so quote/backslash obfuscation cannot skip the
  # domain check when Python is unavailable.
  stripped_payload="${payload//[\'\"\\]/}"
  if [[ ! "$payload" =~ $gws_word && ! "$stripped_payload" =~ $gws_word ]]; then
    exit 0
  fi
  # gws command present — if an @-sign is literal or JSON-escaped, we cannot
  # safely validate the address without Python decoding the command envelope.
  if [[ "$payload" == *"@"* || "$payload" == *"\\u0040"* ]]; then
    cat >&2 << 'EOF'
google-workspace plugin: python3 is required by the domain-restriction enforcement
hook but is not on PATH. Install python3 and retry so gws commands can be
checked safely.
EOF
    exit 2
  fi
  # gws command with no visible or JSON-escaped @-sign: no email address to validate.
  exit 0
fi

# shellcheck disable=SC2016
if ! result="$(printf '%s\n' "$payload" | python3 "${CLAUDE_PLUGIN_ROOT}/scripts/check-domain.py" 2>&1)"; then
  cat >&2 << EOF
google-workspace plugin: domain check script failed unexpectedly.

$result
EOF
  exit 2
fi

verdict="$(printf '%s\n' "$result" | head -1)"
offender="$(printf '%s\n' "$result" | tail -n +2)"

if [[ "$verdict" == "DENY" ]]; then
  cat >&2 << EOF
google-workspace plugin: gws command blocked — external domain detected.

  $offender

Only @upstart.com addresses are permitted in gws user-identifier parameters.
If this is intentional (e.g. reading an email FROM an external sender), use
  ! gws ...
at the Pi prompt to run the command directly, bypassing this check.
EOF
  exit 2
fi

if [[ "$verdict" == "ERROR" ]]; then
  cat >&2 << EOF
google-workspace plugin: failed to inspect Bash command for external-domain
targets. The domain policy fails closed when shell parsing is incomplete.

  $offender
EOF
  exit 2
fi

if [[ "$verdict" != "ALLOW" ]]; then
  cat >&2 << EOF
google-workspace plugin: failed to inspect Bash command for external-domain
targets. The domain policy received an unexpected checker result:

$result
EOF
  exit 2
fi

exit 0
