#!/usr/bin/env bash
# Run the gws OAuth flow against the bundled UpstartClaw client_secret.json.
# Install the secret first via install-secret.sh if not already present.
#
# Exit codes:
#   0 — auth complete; tokens stored
#   1 — auth failed (missing client secret, gws not installed, OAuth declined,
#       or network error)
set -euo pipefail

GWS_CONFIG_DIR="${GOOGLE_WORKSPACE_CLI_CONFIG_DIR:-$HOME/.config/gws}"
SECRET="$GWS_CONFIG_DIR/client_secret.json"

# Scopes — keep in sync with the OAuth scopes table in README.md.
# No gmail.send (drafts only). gmail.compose technically permits sending drafts,
# so send + delete enforcement both happen at the PreToolUse-hook layer.
SCOPES=(
  "https://www.googleapis.com/auth/gmail.compose"
  "https://www.googleapis.com/auth/gmail.modify"
  "https://www.googleapis.com/auth/gmail.labels"
  "https://www.googleapis.com/auth/calendar"
  "https://www.googleapis.com/auth/drive"
  "https://www.googleapis.com/auth/spreadsheets"
  "https://www.googleapis.com/auth/presentations"
  "https://www.googleapis.com/auth/forms.body"
  "https://www.googleapis.com/auth/forms.responses.readonly"
  "https://www.googleapis.com/auth/tasks"
  "https://www.googleapis.com/auth/apps.groups.settings"
  "https://www.googleapis.com/auth/admin.reports.audit.readonly"
  "https://www.googleapis.com/auth/admin.reports.usage.readonly"
)
SCOPE_LIST="$(
  IFS=,
  echo "${SCOPES[*]}"
)"

if [[ ! -f "$SECRET" ]]; then
  cat >&2 << EOF
client_secret.json is not installed at $SECRET

Run /google-workspace-setup to install the bundled UpstartClaw OAuth
credential. If the plugin install is incomplete, re-run:
  /plugin install google-workspace@upstartclaw
EOF
  exit 1
fi

if ! command -v gws > /dev/null 2>&1; then
  echo "gws is not on PATH; install it first via /google-workspace-setup." >&2
  exit 1
fi

auth_log="$(mktemp)"
trap 'rm -f "$auth_log"' EXIT

echo "Starting OAuth flow..."
echo "A browser tab should open for Google consent."
echo ""
echo "Open the complete URL printed after GWS_AUTH_URL in your local browser."
echo ""
echo "macOS Keychain: after sign-in, macOS will show 2–4 password dialogs asking permission"
echo "to store and read your tokens. Enter your Mac login password (not your Google password)."
echo "Click 'Always Allow' if you'd like to avoid being prompted on future gws commands."
echo ""

# Intercept gws output line-by-line in real-time. The moment we see the Google auth URL,
# emit GWS_AUTH_URL: so the skill can relay it as a clickable link while gws is still
# waiting for the OAuth callback. Also retry the browser open as a fallback.
_emit_url_marker() {
  local url_emitted=0
  while IFS= read -r line; do
    printf '%s\n' "$line"
    if [[ "$url_emitted" -eq 0 ]]; then
      local found
      found="$(printf '%s' "$line" | grep -oE 'https://accounts\.google\.com[^ ]+' | head -1 || true)"
      if [[ -n "$found" ]]; then
        url_emitted=1
        printf '\nGWS_AUTH_URL: %s\n' "$found"
        # Retry opening the browser in case gws's own open call was silently skipped
        # (e.g. running headless or from a terminal that doesn't support xdg-open).
        open "$found" 2> /dev/null || xdg-open "$found" 2> /dev/null || true
      fi
    fi
  done
}

# set -e is suspended so we can inspect the exit code ourselves.
set +e
gws auth login --scopes "$SCOPE_LIST" 2>&1 | _emit_url_marker | tee "$auth_log"
gws_rc="${PIPESTATUS[0]}"
set -e

if [[ "$gws_rc" -ne 0 ]]; then
  if grep -qi "serviceusage.serviceUsageConsumer\|SERVICE_DISABLED\|is not enabled\|Access Not Configured" "$auth_log"; then
    cat >&2 << 'EOF'

You are missing the UpstartClaw GCP entitlement required to use this plugin.
Request access via one of:
  • https://go/access  (search for "RBAC: UpstartClaw")
  • @plugins/access-request/ in Claude Code

Once access is granted, re-run /google-workspace-auth.
EOF
  fi
  exit 1
fi

echo ""
echo "OAuth complete. Verify with: gws drive files list --params '{\"pageSize\": 1}'"
