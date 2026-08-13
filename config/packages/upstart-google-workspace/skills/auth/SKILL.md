---
name: google-workspace-auth
description: |
  Re-run the gws OAuth flow. Use to re-grant scopes after a policy change, to switch the
  active Google account, or to recover from expired tokens. Idempotent.
disable-model-invocation: true
allowed-tools:
  - Bash
---

# gws auth

Re-run the OAuth consent flow against the UpstartClaw client. Tokens are stored encrypted in the OS keyring (Keychain on
macOS) by `gws`.

Ask the user to run `/google-workspace-auth`. That command places an interactive `! bash .../setup-oauth.sh` command in the Pi editor; the user must press Enter to start it. Do not run OAuth through the model's Bash tool because the browser URL needs to stream to the user while `gws` waits.

Before they run it, tell the user:

> The OAuth sign-in URL is very long. Any URL that appears in the tool output may be cut off — don't use it. I'll send
> you a full clickable link in my next message once the sign-in page opens.
>
> After sign-in completes, macOS will show **2–4 password dialogs** asking permission to store and read your tokens in
> Keychain. Enter your **Mac login password** (not your Google password). Click **Always Allow** if you'd like to avoid
> being prompted on every future `gws` command — this grants `gws` persistent Keychain access, which you can revoke any
> time in Keychain Access.app.

The direct shell output prints a `GWS_AUTH_URL:` line. The user should open that complete URL in a local browser. After authentication, run `/google-workspace-complete` to verify Drive access and enable agent `gws` calls.

If the script reports a message about `RBAC: UpstartClaw`, you are missing the GCP entitlement. Request access at
<https://go/access> (search "RBAC: UpstartClaw") or via the `@plugins/access-request/` plugin, then re-run this skill.

If the script reports `client_secret.json is not installed at ~/.config/gws/client_secret.json`, run
`/google-workspace-setup` first — that flow installs the bundled OAuth credential and completes the initial auth flow.

After auth completes, verify with:

```bash
gws drive files list --params '{"pageSize": 1}'
```

If that fails after a successful auth, the OAuth client may not have the requested API enabled in the UpstartClaw GCP
project. File the issue with the Gen AI Guild.
