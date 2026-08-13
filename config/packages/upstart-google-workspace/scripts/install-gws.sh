#!/usr/bin/env bash
# Idempotent installer for the Google Workspace CLI (`gws`).
#
# Tries Homebrew, then npm, then a direct release-binary download for the pinned
# upstream tag. Caller is responsible for confirming with the user before
# invoking — this script does not prompt.
#
# Exit codes:
#   0 — gws is on PATH after this script runs
#   1 — installation failed
set -euo pipefail

PIN="v0.22.5"

if command -v gws > /dev/null 2>&1; then
  if gws --version > /dev/null 2>&1; then
    echo "gws already installed: $(command -v gws)"
    gws --version 2>&1 | head -1 || true
    exit 0
  else
    echo "gws is on PATH but failed to run (possible GLIBC mismatch); reinstalling..." >&2
  fi
fi

OS="$(uname -s)"
ARCH="$(uname -m)"

# 1. Homebrew (macOS / Linuxbrew).
# Homebrew (and the npm fallback below) install the latest published version,
# not the plugin's pinned ${PIN}. That is intentional — pinning the CLI to a
# stale tag cuts users off from upstream bug fixes. The plugin pin only
# governs which upstream skill *content* gets bundled at sync time.
if command -v brew > /dev/null 2>&1; then
  echo "Installing gws via Homebrew..."
  brew_err="$(mktemp)"
  if brew install googleworkspace-cli 2> "$brew_err"; then
    if command -v gws > /dev/null 2>&1; then
      echo "gws installed: $(command -v gws)"
      rm -f "$brew_err"
      exit 0
    fi
    rm -f "$brew_err"
    echo "Homebrew installed gws but it is not on PATH yet." >&2
    echo "Run: eval \"\$(brew shellenv)\"  (or open a new shell), then re-run /google-workspace:setup." >&2
    exit 1
  else
    echo "Homebrew install failed:" >&2
    cat "$brew_err" >&2
    echo "Falling back to npm..." >&2
  fi
  rm -f "$brew_err"
fi

# 2. npm (cross-platform; needs Node 18+).
if command -v npm > /dev/null 2>&1; then
  node_major="$(node --version 2> /dev/null | sed -E 's/^v([0-9]+).*/\1/' || echo 0)"
  if ((node_major >= 18)); then
    echo "Installing gws via npm..."
    if npm install -g @googleworkspace/cli; then
      if command -v gws > /dev/null 2>&1 && gws --version > /dev/null 2>&1; then
        echo "gws installed: $(command -v gws)"
        exit 0
      elif command -v gws > /dev/null 2>&1; then
        echo "npm installed gws but binary failed to run (possible GLIBC mismatch); falling back to direct download..." >&2
      fi
    fi
  else
    echo "Node $node_major is too old (need 18+); skipping npm install path." >&2
  fi
fi

# 3. Release binary download from GitHub. Asset names are
# `google-workspace-cli-{arch}-{platform}.tar.gz` and the archive contains a
# `gws` binary at the top level. Intel Mac (Darwin-x86_64) is not supported here.
case "$OS-$ARCH" in
  Darwin-arm64) asset="google-workspace-cli-aarch64-apple-darwin.tar.gz" ;;
  Linux-x86_64) asset="google-workspace-cli-x86_64-unknown-linux-musl.tar.gz" ;;
  Linux-aarch64) asset="google-workspace-cli-aarch64-unknown-linux-musl.tar.gz" ;;
  *)
    echo "Unsupported platform for direct release-binary install: $OS-$ARCH" >&2
    echo "See https://github.com/googleworkspace/cli/releases/tag/$PIN" >&2
    exit 1
    ;;
esac

url="https://github.com/googleworkspace/cli/releases/download/$PIN/$asset"
sha_url="$url.sha256"
echo "Downloading $url..."
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if ! curl -fsSL "$url" -o "$tmp/$asset"; then
  echo "Download failed. The asset name may have changed in upstream releases." >&2
  echo "Browse https://github.com/googleworkspace/cli/releases/tag/$PIN to confirm." >&2
  exit 1
fi

# Verify SHA256 against the upstream-published checksum so we don't trust
# the binary based on HTTPS alone.
if ! curl -fsSL "$sha_url" -o "$tmp/$asset.sha256"; then
  echo "Could not fetch checksum at $sha_url — refusing to install unverified binary." >&2
  exit 1
fi
expected="$(awk '{print $1}' "$tmp/$asset.sha256")"
if command -v sha256sum > /dev/null 2>&1; then
  actual="$(sha256sum "$tmp/$asset" | awk '{print $1}')"
elif command -v shasum > /dev/null 2>&1; then
  actual="$(shasum -a 256 "$tmp/$asset" | awk '{print $1}')"
else
  echo "Neither sha256sum nor shasum available — cannot verify checksum." >&2
  exit 1
fi
if [[ "$expected" != "$actual" ]]; then
  echo "Checksum mismatch for $asset:" >&2
  echo "  expected $expected" >&2
  echo "  got      $actual" >&2
  exit 1
fi
echo "Checksum verified: $actual"

tar -xzf "$tmp/$asset" -C "$tmp"
binary="$(find "$tmp" -name gws -type f -perm -u+x | head -1)"
if [[ -z "$binary" ]]; then
  echo "Extracted archive does not contain a gws binary." >&2
  exit 1
fi

install_dir="$HOME/.local/bin"
mkdir -p "$install_dir"
cp "$binary" "$install_dir/gws"
chmod +x "$install_dir/gws"

if [[ ":$PATH:" != *":$install_dir:"* ]]; then
  echo "Adding $install_dir to PATH (open a new shell to pick it up)."
  echo "  Add this to your shell rc:  export PATH=\"$install_dir:\$PATH\""
fi

# Verify the newly installed binary runs correctly before checking PATH.
if ! "$install_dir/gws" --version > /dev/null 2>&1; then
  echo "gws was installed to $install_dir/gws but failed to run." >&2
  exit 1
fi

# Check PATH resolution. Use || true so set -e doesn't fire when gws is absent.
resolved_gws="$(command -v gws 2> /dev/null || true)"

if [[ "$resolved_gws" == "$install_dir/gws" ]]; then
  # Newly installed binary is what PATH resolves to — all good.
  echo "gws installed: $install_dir/gws"
  exit 0
elif [[ -n "$resolved_gws" ]]; then
  # A different (possibly broken) binary is earlier in PATH.
  echo "gws installed to $install_dir/gws but $resolved_gws is earlier in PATH." >&2
  echo "Remove $resolved_gws or prepend $install_dir to PATH, then re-run /google-workspace:setup." >&2
  exit 1
else
  # Binary installed but not on PATH yet — caller must update PATH and re-run.
  echo "gws installed at $install_dir/gws but not on PATH yet." >&2
  echo "Open a new shell or update PATH, then re-run /google-workspace:setup." >&2
  exit 1
fi
