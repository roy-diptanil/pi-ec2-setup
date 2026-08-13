# Upstream provenance

This plugin bundles a curated subset of skills from [googleworkspace/cli](https://github.com/googleworkspace/cli)
(Apache-2.0).

## Pinned upstream

- **Repo**: <https://github.com/googleworkspace/cli>
- **Tag**: `v0.22.5` (released 2026-03-31)
- **License**: Apache-2.0

Upstream is pre-1.0; expect breaking changes. The maintainer is responsible for re-running `scripts/sync-upstream.sh`
against a newer tag periodically and reviewing the resulting diff before committing.

## Bundled skills

The following 18 upstream skills are synced into `skills/` with minimal local post-processing: `sync-upstream.sh` strips
`See Also` links that point to non-bundled skills (to avoid broken relative links). No frontmatter injection is
performed — skills are bundled as-is from upstream.

| Skill                        | Purpose                                                              |
| ---------------------------- | -------------------------------------------------------------------- |
| `gws-shared`                 | Shared auth, global flags, output formatting (foundation for others) |
| `gws-gmail-read`             | Read a message, extract body or headers                              |
| `gws-gmail`                  | Full Gmail CRUD surface                                              |
| `gws-calendar-agenda`        | Show upcoming events                                                 |
| `gws-calendar-insert`        | Create a calendar event                                              |
| `gws-calendar`               | Full Calendar CRUD surface                                           |
| `gws-sheets-read`            | Read values from a spreadsheet                                       |
| `gws-sheets-append`          | Append a row                                                         |
| `gws-sheets`                 | Full Sheets CRUD surface                                             |
| `gws-docs-write`             | Append text to a doc                                                 |
| `gws-docs`                   | Full Docs CRUD surface                                               |
| `gws-drive-upload`           | Upload a file with metadata                                          |
| `gws-drive`                  | Full Drive CRUD surface                                              |
| `gws-slides`                 | Full Slides CRUD surface                                             |
| `gws-forms`                  | Full Forms CRUD surface                                              |
| `gws-tasks`                  | Full Tasks CRUD surface                                              |
| `gws-workflow-meeting-prep`  | Agenda, attendees, linked docs for next meeting                      |
| `gws-workflow-weekly-digest` | Weekly meeting + unread email summary                                |

## Excluded upstream skills

Filter is **name-based** (body-grep is unreliable: every umbrella service skill documents the full Discovery API
surface, including delete operations).

### Excluded for mail-send policy

- `gws-gmail-send`
- `gws-gmail-reply`, `gws-gmail-reply-all`
- `gws-gmail-forward`

### Excluded for chat-send policy

- `gws-chat`, `gws-chat-send`
- `gws-workflow-file-announce` (posts to Chat)

### Excluded as out-of-scope services

- `gws-keep` (Google Keep API not approved for UpstartClaw OAuth client)
- `gws-meet`, `gws-classroom`
- `gws-events`, `gws-events-renew`, `gws-events-subscribe`
- `gws-modelarmor`, `gws-modelarmor-create-template`, `gws-modelarmor-sanitize-prompt`,
  `gws-modelarmor-sanitize-response`
- `gws-script`, `gws-script-push`

### Excluded persona / recipe content

All `persona-*` (10 skills) and most `recipe-*` (44+ skills) reference `gmail.send` or `chat.send` in their workflows.
None are bundled in v1. A future iteration could selectively include the ~8 send-free recipes
(`recipe-find-large-files`, `recipe-find-free-time`, `recipe-bulk-download-folder`, `recipe-block-focus-time`,
`recipe-organize-drive-folder`, `recipe-copy-sheet-for-new-month`, `recipe-create-expense-tracker`,
`recipe-schedule-recurring-event`).

## How to refresh from upstream

```bash
cd plugins/gws
./scripts/sync-upstream.sh v0.23.0   # or whichever new tag
git diff skills/                     # review what changed
# Smoke-test the affected skills, then commit if safe.
```

The sync script is intentionally maintainer-only — it is not invoked at install time. End users get a reproducible,
marketplace-installable plugin.

## License compliance

Each bundled skill carries upstream's Apache-2.0 license. The original LICENSE file from `googleworkspace/cli` is
mirrored at `LICENSE.upstream` in this directory.
