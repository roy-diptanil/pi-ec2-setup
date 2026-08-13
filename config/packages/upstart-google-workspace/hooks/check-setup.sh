#!/usr/bin/env bash
# PreToolUse hook on Bash. If the user (or model) tries to invoke `gws ...`
# before `/google-workspace-setup` has completed, surface a redirect to setup
# instead of letting the call fail with an opaque "gws: command not found" or
# "no client_secret.json" error.
#
# Detection is intentionally generous: any reference to `gws` as a complete
# word in the command triggers the check. This catches wrapper patterns
# (`sudo gws`, `bash -c "gws ..."`, env-var prefixes, absolute paths) at the
# cost of false positives like `echo "gws is mentioned"`. False positives are
# benign — the user just sees the setup-redirect message.
#
# Exit 0 → allow (not a gws-touching command, setup is complete, or in
#          progress, or running one of the plugin's own setup scripts)
# Exit 2 → deny (stderr redirects to /google-workspace-setup)
set -uo pipefail

payload="$(cat || true)"
STATE_FILE="$HOME/.pi/agent/google-workspace-setup"
state="$(cat "$STATE_FILE" 2> /dev/null || echo no_setup)"

# If python3 is missing, avoid blocking unrelated Bash commands. We cannot
# parse the JSON envelope robustly, but a raw payload scan can still avoid
# matching plugin setup script names such as install-gws.sh.
if ! command -v python3 > /dev/null 2>&1; then
  if [[ "$payload" != *'"command"'* ]]; then
    exit 0
  fi

  if [[ "$state" == "completed" || "$state" == "in_progress" ]]; then
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

  if [[ ! "$payload" =~ (^|[^[:alnum:]_-])gws([^[:alnum:]_-]|$) ]]; then
    exit 0
  fi

  cat >&2 << 'EOF'
google-workspace plugin: python3 is required by the setup-redirect hook
but is not on PATH. Install python3 and retry, or run /google-workspace-setup
to complete first-time configuration.
EOF
  exit 2
fi

# shellcheck disable=SC2016  # Single quotes intentional — Python code, not shell expansion
if ! result="$(printf '%s\n' "$payload" | python3 -c '
import json, re, sys
try:
    payload = json.load(sys.stdin)
    cmd = payload.get("tool_input", {}).get("command", "")
except Exception as exc:
    print(f"failed to parse Bash tool payload: {exc}", file=sys.stderr)
    sys.exit(1)
if not cmd:
    print("ALLOW")
    sys.exit(0)
if re.search(r"(?<![A-Za-z0-9_-])gws(?![A-Za-z0-9_-])", cmd):
    print("GWS")
    print(cmd)
else:
    print("ALLOW")
' 2>&1)"; then
  cat >&2 << EOF
google-workspace plugin: failed to inspect Bash command for google-workspace
setup state. The setup hook fails closed when payload parsing or matching fails.

$result
EOF
  exit 2
fi

verdict="$(printf '%s\n' "$result" | head -1)"
cmd="$(printf '%s\n' "$result" | tail -n +2)"

if [[ "$verdict" != "GWS" ]]; then
  if [[ "$verdict" == "ALLOW" ]]; then
    exit 0
  fi

  cat >&2 << EOF
google-workspace plugin: failed to inspect Bash command for google-workspace
setup state. The setup hook received an unexpected matcher result:

$result
EOF
  exit 2
fi

# Allow setup-flow scripts to run even when state ≠ completed. CLAUDE_PLUGIN_ROOT
# must be set; if it's empty the prefix match would degrade to `*/scripts/*`
# and silently allow unrelated commands.
if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" && "$cmd" == *"${CLAUDE_PLUGIN_ROOT}/scripts/"* ]]; then
  exit 0
fi

case "$state" in
  completed | in_progress)
    # in_progress allows through so the setup skill itself can run
    # `gws auth login` mid-setup. Matches the convention in plugins/core.
    exit 0
    ;;
  *)
    cat >&2 << EOF
google-workspace plugin is not set up yet (state: $state).

Ask the user to run /google-workspace-setup to install the gws CLI
and complete the OAuth flow. Authentication requires an interactive user
invocation and cannot be completed automatically.

Blocked command:
  $cmd
EOF
    exit 2
    ;;
esac
