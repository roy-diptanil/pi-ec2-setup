#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

bad_files="$(find . -type f \( \
  -name 'auth.json' -o \
  -name '*.jsonl' -o \
  -name 'oauth.log' -o \
  -name 'state.json' -o \
  -name 'models-store.json' -o \
  -name 'mcp-cache.json' \
\) -print)"
if [[ -n "$bad_files" ]]; then
  printf 'Forbidden credential/session files found:\n%s\n' "$bad_files" >&2
  exit 1
fi

# The bundled, approved desktop client registration is explicitly exempted.
if grep -RInE --exclude='client_secret.json' --exclude='check-no-personal-tokens.sh' \
  '(sk-ant-|sk-proj-|gh[opsu]_[A-Za-z0-9_]{20,}|Bearer[[:space:]]+[A-Za-z0-9._-]{20,}|"(access|refresh|id)_token"[[:space:]]*:)' \
  .; then
  echo 'Potential personal token material found.' >&2
  exit 1
fi

echo 'No personal token or session files detected.'
