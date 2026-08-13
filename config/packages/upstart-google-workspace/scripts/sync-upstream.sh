#!/usr/bin/env bash
# Maintainer-only — refresh bundled upstream skills from googleworkspace/cli.
#
# Usage:
#   ./scripts/sync-upstream.sh                 # uses default pin (see below)
#   ./scripts/sync-upstream.sh v0.23.0         # override pin
#
# Sparse-clones the upstream repo at the given tag, copies the curated subset of
# skills into ./skills/, and refreshes LICENSE.upstream. Does NOT commit. Run
# `git diff skills/` afterwards to review.
#
# This script is invoked manually by the plugin maintainer. It is NOT run at
# plugin install time. End users receive a marketplace-installable plugin with
# bundled skills already in place.

set -euo pipefail

DEFAULT_PIN="v0.22.5"
PIN="${1:-$DEFAULT_PIN}"

# Resolve plugin root (parent of scripts/).
# PLUGIN_ROOT can be set by a caller (e.g. CI running the script via pipe/stdin)
# to override the $0-based auto-detection, which breaks when bash reads from stdin.
PLUGIN_ROOT="${PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SKILLS_DIR="$PLUGIN_ROOT/skills"

# Skills bundled from upstream — name-based filter. See UPSTREAM.md for rationale.
INCLUDE=(
  gws-shared
  gws-gmail-read
  gws-calendar-agenda
  gws-calendar-insert
  gws-sheets-read
  gws-sheets-append
  gws-docs-write
  gws-drive-upload
  gws-drive
  gws-gmail
  gws-calendar
  gws-sheets
  gws-slides
  gws-forms
  gws-docs
  gws-tasks
  gws-workflow-meeting-prep
  gws-workflow-weekly-digest
)

# Upstart-authored skills that must NOT be touched by this script (preserve them).
# These skills live alongside the bundled upstream ones in `skills/` but were
# renamed to drop the `gws-` prefix for cleaner slash invocations
# (e.g. `/google-workspace:setup`).
PROTECTED=(
  setup
  doctor-gws
  auth
  mail-draft
)

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "Sparse-cloning googleworkspace/cli@${PIN} ..."
git clone \
  --depth 1 \
  --branch "$PIN" \
  --filter=blob:none \
  --sparse \
  https://github.com/googleworkspace/cli "$WORK_DIR/upstream" \
  > /dev/null 2>&1

# `--no-cone` so we can pin a non-directory path (LICENSE) alongside `skills/`;
# default cone mode rejects non-directory entries.
git -C "$WORK_DIR/upstream" sparse-checkout set --no-cone skills LICENSE > /dev/null

# Refresh license mirror.
cp "$WORK_DIR/upstream/LICENSE" "$PLUGIN_ROOT/LICENSE.upstream"

# Sync each bundled skill. Skip if missing upstream (warn).
# After copying, strip `See Also` bullets that point to upstream skills we did
# not bundle (e.g. umbrella `gws-gmail`, `gws-calendar`). Otherwise the shipped
# skills carry broken relative links.
synced=0
missing=()
include_set=" ${INCLUDE[*]} "
for skill in "${INCLUDE[@]}"; do
  src="$WORK_DIR/upstream/skills/$skill"
  dst="$SKILLS_DIR/$skill"
  if [[ ! -d "$src" ]]; then
    missing+=("$skill")
    continue
  fi
  rm -rf "$dst"
  cp -R "$src" "$dst"
  # Strip `See Also` bullets pointing to non-bundled upstream skills.
  if [[ -f "$dst/SKILL.md" ]]; then
    python3 - "$dst/SKILL.md" "$include_set" << 'PY'
import re, sys
path, include_set = sys.argv[1], sys.argv[2].split()
text = open(path).read()
# Strip `See Also` bullets pointing to non-bundled upstream skills.
text = re.sub(
    r"^- \[([^\]]+)\]\(\.\./([^/]+)/SKILL\.md\)[^\n]*\n",
    lambda m: m.group(0) if m.group(2) in include_set else "",
    text,
    flags=re.M,
)
# Strip markdown table rows linking to non-bundled upstream skills.
# Matches rows like: | [Label](../skill-name/SKILL.md) | ... |
text = re.sub(
    r"^\|[^\n]*\(\.\./([^/]+)/SKILL\.md\)[^\n]*\|\n",
    lambda m: m.group(0) if m.group(1) in include_set else "",
    text,
    flags=re.M,
)
open(path, "w").write(text)
PY
  fi
  synced=$((synced + 1))
done

# Verify protected skills are still present (defensive).
for skill in "${PROTECTED[@]}"; do
  if [[ ! -d "$SKILLS_DIR/$skill" ]]; then
    echo "WARN: protected skill missing: $skill (this script did not touch it)"
  fi
done

echo
echo "Synced $synced upstream skills at pin ${PIN}."
if ((${#missing[@]} > 0)); then
  echo "WARN: ${#missing[@]} skills not found at upstream pin: ${missing[*]}"
fi
echo
echo "Next steps:"
echo "  1. Review:  git -C $PLUGIN_ROOT diff skills/ LICENSE.upstream"
echo "  2. Update UPSTREAM.md if the pin changed."
echo "  3. Smoke-test affected skills."
echo "  4. Commit."
