---
name: google-workspace-setup
description: |
  First-time setup for the google-workspace plugin. Installs the gws CLI (Homebrew, npm, or
  pinned release-binary fallback), installs the bundled UpstartClaw OAuth client_secret.json to
  ~/.config/gws/, runs the gws OAuth consent flow with the configured scopes, and marks setup
  complete. Run this once after installing the plugin.
disable-model-invocation: true
allowed-tools:
  - Bash
  - Read
  - Write
---

# google-workspace plugin setup

Walk the user through first-time setup for the `google-workspace` plugin. The setup is idempotent — re-running is safe.

## Step 1 — Read state

Read `~/.pi/agent/google-workspace-setup` with the Read tool.

- File missing or `Read` returns "file not found" → state is `no_setup`.
- File contents are one of `no_setup`, `in_progress`, `completed`.

## Step 2 — Branch on state

### If state is `completed`

Tell the user: "Google Workspace is already set up. Run `/google-workspace-doctor` to verify it, or `/google-workspace-auth` to re-grant OAuth scopes."

To re-run setup from scratch, write `no_setup` to `~/.pi/agent/google-workspace-setup`, then ask the user to invoke `/google-workspace-setup` again.

Stop. Do not continue to step 3.

### If state is `no_setup` or `in_progress`

Continue to step 3. (`in_progress` means a previous run was interrupted; the install scripts are idempotent so we can
resume.)

## Step 3 — Install CLIs

Mark setup as in progress: write `in_progress` to `~/.pi/agent/google-workspace-setup`.

First verify that `python3` is on `PATH`, because the plugin's Bash hooks and secret-validation script require it:

```bash
command -v python3 && python3 --version
```

If this fails, tell the user to install Python 3 first (`brew install python` on macOS, or
`sudo apt-get update && sudo apt-get install -y python3` on Debian/Ubuntu). Leave state as `in_progress` so the next run
resumes here.

Run the gws installer:

```bash
bash "__PI_AGENT_DIR__/packages/upstart-google-workspace/scripts/install-gws.sh"
```

Branch on exit code as above. The installer tries Homebrew first, then npm, then a direct release-binary download for
the pinned tag (`v0.22.5`).

If either install instructed the user to open a new shell to pick up `$PATH`, tell them to do so and re-run
`/google-workspace-setup`. Stop here in that case.

## Step 4 — Install the UpstartClaw OAuth client secret

The plugin bundles the shared UpstartClaw Google Cloud desktop OAuth client at `scripts/client_secret.json`
(security-approved for repo inclusion — see SSD-4711).

First, check whether the secret is already in place:

```bash
test -f "${HOME}/.config/gws/client_secret.json" && echo "INSTALLED" || echo "MISSING"
```

If it prints `INSTALLED`, skip to Step 5.

If `MISSING`, install from the bundled credential:

```bash
bash "__PI_AGENT_DIR__/packages/upstart-google-workspace/scripts/install-secret.sh"
```

The script validates the file is a real OAuth client artifact and copies it to `~/.config/gws/client_secret.json` with
mode 600.

Branch on exit code:

- `0` — installed. Continue.
- `1` — failed. Show stderr. If it reports the bundled file is missing, the plugin installation is incomplete — re-run
  `/plugin install google-workspace@upstartclaw`.

## Step 5 — OAuth flow

Ask the user to run `/google-workspace-auth`. It prepares an interactive user-shell command in the Pi editor; the user must press Enter to start it. Do not invoke OAuth through the model's Bash tool because output must stream while `gws` waits for the callback.

Before they run it, tell the user:

> The OAuth sign-in URL is very long. Any URL that appears in the tool output may be cut off — don't use it. I'll send
> you a full clickable link in my next message once the sign-in page opens.
>
> After sign-in completes, macOS will show **2–4 password dialogs** asking permission to store and read your tokens in
> Keychain. Enter your **Mac login password** (not your Google password). Click **Always Allow** if you'd like to avoid
> being prompted on every future `gws` command — this grants `gws` persistent Keychain access, which you can revoke any
> time in Keychain Access.app.
> ([What is the macOS Keychain?](https://support.apple.com/guide/keychain-access/welcome/mac))

The direct shell output prints a `GWS_AUTH_URL:` line. The user should open that complete URL in a local browser. On a remote host they may need SSH callback forwarding or to copy the failed localhost callback URL, depending on `gws` behavior.

This gives the user a single-click fallback even if the browser tab appeared behind another window.

After the direct command finishes, ask the user to run `/google-workspace-complete`. It verifies a tiny Drive read and marks setup complete. If OAuth fails, common causes are:
  - **Missing GCP entitlement**: the script prints a message about `RBAC: UpstartClaw`. Direct the user to request
    access at <https://go/access> (search "RBAC: UpstartClaw") or via the `@plugins/access-request/` plugin. Once
    granted, re-run `/google-workspace-auth`.
  - User denied consent; network error reaching `accounts.google.com`; missing `client_secret.json` (re-run Step 4).
    Show the error output and stop with state `in_progress`.

## Step 6 — Verify

Run a tiny Drive read to confirm the tokens work:

```bash
gws drive files list --params '{"pageSize": 1}' > /dev/null && echo OK || echo FAIL
```

If the verification prints `OK`, continue. If `FAIL`, run `gws auth status` (or `gws auth export --unmasked` to inspect
the token) and surface the error.

## Step 7 — Mark complete

Run `/google-workspace-complete`, which verifies Drive access before writing `completed` to `~/.pi/agent/google-workspace-setup`.

Print a summary:

```text
✓ gws:        installed
✓ Secret:     installed at ~/.config/gws/client_secret.json
✓ OAuth:      authenticated
✓ Verified:   Drive read succeeded

Setup complete. Run `/google-workspace-doctor` to verify any time, or `/google-workspace-auth` to re-grant scopes.
```

## Reset

To reset, write `no_setup` to `~/.pi/agent/google-workspace-setup` and re-run `/google-workspace-setup`. This does NOT
uninstall gws, and does NOT revoke OAuth tokens — it only clears the sentinel so the setup steps run again.

To revoke OAuth tokens, run `gws auth logout` separately.
