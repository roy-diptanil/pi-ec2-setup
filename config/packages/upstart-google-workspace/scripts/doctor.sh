#!/usr/bin/env bash
# Read-only health check for the gws plugin. Emits a [OK]/[FAIL] checklist on stdout.
#
# Exits 0 if all checks pass, 1 if any fail. Never mutates state.
set -uo pipefail

GWS_CONFIG_DIR="${GOOGLE_WORKSPACE_CLI_CONFIG_DIR:-$HOME/.config/gws}"
SECRET_FILE="$GWS_CONFIG_DIR/client_secret.json"
STATE_FILE="$HOME/.pi/agent/google-workspace-setup"

fails=0

check() {
  local label="$1"
  local status="$2"
  local detail="$3"
  if [[ "$status" == "OK" ]]; then
    printf '[OK]   %-22s %s\n' "$label" "$detail"
  else
    printf '[FAIL] %-22s %s\n' "$label" "$detail"
    fails=$((fails + 1))
  fi
}

# 1. gws
if command -v gws > /dev/null 2>&1; then
  ver="$(gws --version 2> /dev/null | head -1 || echo unknown)"
  check "gws on PATH" OK "$(command -v gws) ($ver)"
else
  check "gws on PATH" FAIL "not found — run /google-workspace-setup"
fi

# 2. python3
if command -v python3 > /dev/null 2>&1; then
  check "python3 on PATH" OK "$(command -v python3) ($(python3 --version 2> /dev/null))"
else
  check "python3 on PATH" FAIL "not found - install with brew install python or apt-get install python3"
fi

# 3. client_secret.json
if [[ -f "$SECRET_FILE" ]]; then
  mode="$(stat -c '%a' "$SECRET_FILE" 2> /dev/null || stat -f '%Lp' "$SECRET_FILE")"
  check "client_secret.json" OK "$SECRET_FILE (mode $mode)"
else
  check "client_secret.json" FAIL "missing at $SECRET_FILE — run /google-workspace-setup"
fi

# 4. OAuth tokens (best-effort: any successful gws call confirms tokens work).
if command -v gws > /dev/null 2>&1; then
  if gws drive files list --params '{"pageSize": 1}' > /dev/null 2>&1; then
    check "OAuth tokens" OK "gws drive call succeeded"
  else
    check "OAuth tokens" FAIL "gws drive call failed — run /google-workspace-auth"
  fi
else
  check "OAuth tokens" FAIL "gws not installed; cannot test"
fi

# 5. Sentinel state
if [[ -f "$STATE_FILE" ]]; then
  state="$(cat "$STATE_FILE")"
  if [[ "$state" == "completed" ]]; then
    check "sentinel state" OK "$STATE_FILE = completed"
  else
    check "sentinel state" FAIL "$STATE_FILE = $state (expected: completed)"
  fi
else
  check "sentinel state" FAIL "$STATE_FILE missing — run /google-workspace-setup"
fi

echo
if ((fails == 0)); then
  echo "gws plugin is healthy."
  exit 0
else
  echo "gws plugin has $fails issue(s). See remediation above."
  exit 1
fi
