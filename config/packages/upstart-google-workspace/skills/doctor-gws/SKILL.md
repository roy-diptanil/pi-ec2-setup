---
name: google-workspace-doctor
description: |
  Verify the gws plugin's setup is healthy. Checks that the gws CLI is on PATH,
  that the bundled client_secret.json was copied to ~/.config/gws/, that OAuth tokens
  are present and valid, that expected scopes are granted, and that the setup sentinel
  reads `completed`. Read-only — does not modify any state.
allowed-tools:
  - Bash
  - Read
---

# gws plugin doctor-gws

Read-only diagnostic. Do not write any files or run installers.

Run the doctor script and present the result to the user:

```bash
bash "__PI_AGENT_DIR__/packages/upstart-google-workspace/scripts/doctor.sh"
```

The script emits a checklist in this format:

```text
[OK]   gws on PATH           (/opt/homebrew/bin/gws, version 0.22.5)
[OK]   python3 on PATH       (/usr/bin/python3, Python 3.12.0)
[OK]   client_secret.json    (~/.config/gws/client_secret.json, mode 600)
[FAIL] OAuth tokens          (gws auth status reports 'no active session')
[OK]   sentinel state        (~/.pi/agent/google-workspace-setup = completed)
```

For each `[FAIL]` row, suggest the remediation:

- gws missing → `/google-workspace-setup`
- python3 missing → `brew install python` (macOS) or `apt-get install python3` (Linux)
- client_secret.json missing → `/google-workspace-setup` (will copy it)
- OAuth tokens missing or expired → `/google-workspace-auth`
- Sentinel state ≠ `completed` → `/google-workspace-setup`

If everything is `[OK]`, tell the user the plugin is healthy.
