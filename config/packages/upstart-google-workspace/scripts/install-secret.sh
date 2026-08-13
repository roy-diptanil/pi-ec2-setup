#!/usr/bin/env bash
# Validate and install the UpstartClaw OAuth desktop client_secret.json into
# ~/.config/gws/client_secret.json (mode 600).
#
# Usage:
#   install-secret.sh                          # auto-uses bundled scripts/client_secret.json
#   install-secret.sh <path-to-client_secret>  # use an explicit source path
#
# The OAuth client_secret.json is bundled at scripts/client_secret.json.
# Security approved for repo inclusion — see SSD-4711.
#
# Exit codes:
#   0 — secret installed at ~/.config/gws/client_secret.json
#   1 — invalid input (file not found, not valid OAuth JSON)
set -euo pipefail

# Default to the bundled credential when no arg is given.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLED_SECRET="${SCRIPT_DIR}/client_secret.json"

SRC="${1:-$BUNDLED_SECRET}"

# Tilde expansion (the skill may pass `~/Downloads/...` literally).
SRC="${SRC/#\~/$HOME}"

if [[ ! -f "$SRC" ]]; then
  echo "Source file not found: $SRC" >&2
  if [[ -z "${1:-}" ]]; then
    echo "Bundled credential missing from ${BUNDLED_SECRET}. Reinstall the plugin: /plugin install google-workspace@upstartclaw" >&2
  fi
  exit 1
fi

# Validate JSON shape — must be the UpstartClaw/GWS desktop OAuth client.
# Pass the source path via argv to avoid breaking the Python literal when the path
# contains quotes or other shell-sensitive characters.
# shellcheck disable=SC2016  # Single quotes intentional — Python code, not shell expansion
if ! python3 -c '
import json, sys
EXPECTED_PROJECT_TOKENS = ("upstartclaw",)
src = sys.argv[1]
try:
    with open(src) as f:
        data = json.load(f)
except Exception as e:
    print(f"Not valid JSON ({e}): {src}", file=sys.stderr)
    sys.exit(1)
inst = data.get("installed")
if not inst:
    print("Not a valid desktop OAuth client artifact (missing installed stanza)", file=sys.stderr)
    sys.exit(1)
if not inst or not inst.get("client_id"):
    print("Not a valid OAuth client artifact (missing installed.client_id)", file=sys.stderr)
    sys.exit(1)
client_id = inst["client_id"]
if not client_id.endswith(".apps.googleusercontent.com"):
    print(f"Not a valid Google OAuth client ID: {client_id!r}", file=sys.stderr)
    sys.exit(1)
proj = (inst.get("project_id") or "").lower()
missing = [token for token in EXPECTED_PROJECT_TOKENS if token not in proj]
if not proj or missing:
    print(
        f"Wrong OAuth project_id {proj!r}; expected the UpstartClaw/GWS desktop client",
        file=sys.stderr,
    )
    sys.exit(1)
' "$SRC"; then
  echo "Validation failed. Confirm the downloaded file is:" >&2
  echo "  client_secret_gws_upstartclaw.apps.googleusercontent.com.json" >&2
  exit 1
fi

GWS_CONFIG_DIR="${GOOGLE_WORKSPACE_CLI_CONFIG_DIR:-$HOME/.config/gws}"
DST="$GWS_CONFIG_DIR/client_secret.json"
mkdir -p "$GWS_CONFIG_DIR"
install -m 600 "$SRC" "$DST"
echo "Installed client_secret.json at $DST (mode 600)"
