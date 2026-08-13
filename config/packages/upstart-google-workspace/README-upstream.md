# google-workspace

Google Workspace integration for Upstart engineers. Wraps the `gws`
([googleworkspace/cli](https://github.com/googleworkspace/cli)) CLI with a shared UpstartClaw OAuth client so any
engineer can read, create, and update content across Google Workspace from Claude Code without managing their own Google
Cloud project.

## Scope

- **Services**: Gmail, Slides, Forms, Calendar, Drive, Sheets, Tasks, Groups, Admin Reports
- **Operations**: read, create, update — never delete; Gmail sends are limited to `@upstart.com` recipients

The no-delete and Gmail-send policies are enforced in two layers:

1. The OAuth client never requests `gmail.send`. Note: `gmail.compose` technically permits sending drafts, so the hook
   below is the hard enforcement layer for direct helper sends.
2. A `PreToolUse` hook rejects destructive `gws` verbs and blocks non-draft `gws gmail +send` / `+forward` helper sends
   unless every literal `--to`, `--cc`, and `--bcc` recipient is `@upstart.com`.

`gws gmail +reply` and `+reply-all` infer recipients from an existing thread, so non-draft invocations remain blocked;
save replies as drafts when recipient inspection is not possible.

## Prerequisites

### Access entitlement

You must be in the **[RBAC: UpstartClaw](https://upstartnetwork.atlassian.net/browse/TEAM-548942)** entitlement group.
To request access:

- Via plugin: install the [access-request plugin](../access-request/README.md) and run `/access-request` → search for
  "RBAC: UpstartClaw"
- Via browser: <https://go/access> → search for "RBAC: UpstartClaw"

Approval is typically instant via auto-approval. Without it, OAuth setup will fail with an access-denied error.

### python3

`python3` must be on `PATH`. The plugin's Bash hooks use it to inspect Claude Code tool payloads, and the mail-draft
skill uses it to encode Gmail draft payloads. Install it with `brew install python` on macOS, or
`sudo apt-get update && sudo apt-get install -y python3` on Debian/Ubuntu.

## Installation

```text
/plugin install google-workspace@upstartclaw
/google-workspace:setup
```

`/google-workspace:setup` is idempotent. It will:

1. Install `gws` if missing. Tries Homebrew first, then npm, then a direct release-binary download for the plugin's
   pinned `v0.22.5`. Note: Homebrew and npm install the latest published `gws` rather than the pinned tag — this is
   intentional. The plugin pin governs which upstream skill _content_ is bundled, not which CLI version runs locally;
   pinning the CLI to a stale tag would cut you off from upstream bug fixes.
2. Install the bundled UpstartClaw OAuth `client_secret.json` to `~/.config/gws/client_secret.json` (mode 600). The
   credential is included in the plugin (security-approved for repo inclusion — see SSD-4711).
3. Open a browser to complete OAuth consent for the configured scopes.
4. Mark setup complete (state file at `~/.claude/google-workspace-setup`).

After setup, run `/google-workspace:doctor-gws` at any time to verify the install. Use `/google-workspace:auth` to
re-run the OAuth flow if you need to re-grant scopes or switch accounts.

## Skills shipped

**Upstart-authored:**

- `/google-workspace:setup`, `/google-workspace:doctor-gws`, `/google-workspace:auth` — install, verify, re-auth.
- `mail-draft` — compose and save a Gmail draft (never sends), auto-triggers on phrases like "draft an email".

**Bundled from upstream `googleworkspace/cli@v0.22.5`** (Apache-2.0 — see `UPSTREAM.md`):

- `gws-shared` — auth + format conventions used by the others.
- Gmail: `gws-gmail-read`, `gws-gmail`.
- Calendar: `gws-calendar-agenda`, `gws-calendar-insert`, `gws-calendar`.
- Sheets: `gws-sheets-read`, `gws-sheets-append`, `gws-sheets`.
- Docs: `gws-docs-write`, `gws-docs`.
- Drive: `gws-drive-upload`, `gws-drive`.
- Slides: `gws-slides`. Forms: `gws-forms`. Tasks: `gws-tasks`.
- Workflows: `gws-workflow-meeting-prep`, `gws-workflow-weekly-digest`.

Upstream skills are bundled (not cloned at runtime) and pinned to `v0.22.5`. To refresh from a newer upstream tag, run
`scripts/sync-upstream.sh <tag>` and review the resulting diff. See `UPSTREAM.md` for filtering rationale and the full
exclusion list.

## OAuth scopes

Granted at first login (`gws auth login --scopes ...`):

| API      | Scope                                                          |
| -------- | -------------------------------------------------------------- |
| Gmail    | `gmail.compose`, `gmail.modify`, `gmail.labels`                |
| Calendar | `calendar`                                                     |
| Drive    | `drive`                                                        |
| Sheets   | `spreadsheets`                                                 |
| Slides   | `presentations`                                                |
| Forms    | `forms.body`, `forms.responses.readonly`                       |
| Groups   | `apps.groups.settings`                                         |
| Tasks    | `tasks`                                                        |
| Admin    | `admin.reports.audit.readonly`, `admin.reports.usage.readonly` |

Tokens are encrypted at rest in your OS keyring (Keychain on macOS) by upstream `gws`.

## Platform support

- **macOS** — primary. Homebrew preferred.
- **Linux** — supported. npm or release-binary for `gws`.
- **Windows** — out of scope.

## FAQ

### Why does macOS show 2–4 password dialogs during setup?

After the OAuth consent flow completes, macOS shows **2–4 password dialogs** — one pair when `gws` stores your tokens
and another pair when the setup verification (`gws drive files list`) reads them back.

These are standard macOS Keychain security prompts, **not** Google prompts. What to do:

- Enter your **Mac login password** (not your Google password).
- Click **Always Allow** (not just "Allow") if you'd like to avoid being prompted on every future `gws` command. "Always
  Allow" tells macOS to grant `gws` Keychain access automatically going forward — you can revoke it any time in Keychain
  Access.app under the entry for `gws`.

Your tokens are stored in the [macOS Keychain](https://support.apple.com/guide/keychain-access/welcome/mac) — the same
encrypted credential store used by Safari, SSH, and 1Password. On Apple Silicon, access is protected by the Secure
Enclave.

## Troubleshooting

`/google-workspace:doctor-gws` covers most issues. Common cases:

- **`gws: command not found`** — re-run `/google-workspace:setup`.
- **`401 unauthorized` from a `gws` call** — tokens expired or scope missing. Run `/google-workspace:auth` to re-grant.
- **Scope rejected at OAuth consent** — UpstartClaw OAuth client doesn't have that API enabled. File a request with the
  Gen AI Guild.

## Rotating the bundled OAuth credential

The bundled `scripts/client_secret.json` is the UpstartClaw desktop OAuth client (security-approved under SSD-4711). If
it needs to be rotated (e.g., the secret is compromised or the GCP project is reorganised):

1. Download the new `client_secret_*.json` from the Google Cloud Console → APIs & Services → Credentials, under the
   `upstartclaw` project.
2. Rename it to `client_secret.json` and replace `plugins/gws/scripts/client_secret.json` in this repo.
3. Re-run the secrets baseline so the new value is tracked: `uvx detect-secrets scan --baseline .secrets.baseline`
4. Update `.secrets.baseline.md` with the new entry.
5. Notify engineers to re-run `/google-workspace:setup`, which will copy the new credential to
   `~/.config/gws/client_secret.json` and prompt them to re-authenticate.

Existing user tokens remain valid until Google revokes them. Running `/google-workspace:auth` after setup re-grants
scopes against the new credential.

## Changelog

- **1.0.36-beta** — covers indexed-array subscripts evaluated by Bash's `test -v`, `[ -v ]`, and `[[ -v ]]` forms.
- **1.0.35-beta** — preserves later recipient flags when an earlier recipient flag is missing its value.
- **1.0.34-beta** — covers indexed-array assignment targets evaluated by Bash's declaration and `unset` builtins.
- **1.0.33-beta** — covers indexed-array assignment targets evaluated by Bash's `printf -v` and `read` builtins.
- **1.0.32-beta** — carries Bash alias definitions into later invocations so a bare alias for `gws` cannot hide
  destructive methods or external Gmail recipients appended at the call site.
- **1.0.30-beta** — covers indexed-array and transitive recursive arithmetic evaluation, and removes resolved hook
  limitations from the security documentation.
- **1.0.29-beta** — covers Bash `((...))` and `let` recursive arithmetic evaluation without blocking unrelated
  arithmetic, and excludes Gmail message content from `userId` enforcement.
- **1.0.28-beta** — fails closed when a `gws` command is hidden behind Bash's recursive arithmetic-variable evaluation.
- **1.0.17-beta** — inspects protected `gws` commands executed through Linux `ionice` while preserving its non-executing
  PID, process-group, and user modes.
- **1.0.16-beta** — inspects commands executed through GNU and moreutils `parallel`, including moreutils command lists
  and its value-taking `-l` option.
- **1.0.11-beta** — fails closed on generated shell script operands and recognizes all Go boolean flag values.
- **1.0.10-beta** — fails closed on substituted `--params` / `--json` values, recognizes commands executed through
  `env`, and documents the conservative no-Python fallback for ANSI-C-quoted Bash payloads.
- **1.0.8-beta** — hardens GWS hook parsing for command substitutions, value-taking flags (incl. `-a`/`--attach` and
  `+reply-all --remove`, which previously allowed a `--<flag> --draft` bypass), draft helpers, and multiline /
  `case`-clause (`;;`) command separators. The two hooks now share one tokenizer, the value-taking flag set is pinned
  against the `gws` CLI's `--help` output, and multiline blocked-command messages no longer interleave with recipients.
- **1.0.7-beta** — allows direct Gmail sends only to literal `@upstart.com` recipients and hardens GWS hook parsing.
- **0.1.0-beta** — initial release. Replaces the standalone `google-docs` plugin.
