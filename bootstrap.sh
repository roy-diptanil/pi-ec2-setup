#!/usr/bin/env bash
# Reproduce the token-free Pi setup used on the source EC2 instance.
set -euo pipefail

PI_VERSION="0.84.1"
NODE_VERSION="22.23.2"
SUBAGENTS_VERSION="0.15.0"
MCP_ADAPTER_VERSION="2.24.0"

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
LOCAL_BIN="$HOME/.local/bin"
NODE_ROOT="$HOME/.local/share/pi-node/node-v${NODE_VERSION}-linux"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Linux" ]] || die "This bootstrap currently supports Linux EC2 instances only."

install_prereqs() {
  local missing=()
  for cmd in curl git python3 tar sha256sum; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  command -v xz >/dev/null 2>&1 || missing+=("xz")
  ((${#missing[@]} == 0)) && return

  log "Installing OS prerequisites: ${missing[*]}"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl git python3 tar xz-utils
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y ca-certificates curl git python3 tar xz
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y ca-certificates curl git python3 tar xz
  else
    die "Install curl, git, python3, tar, xz, and sha256sum, then rerun."
  fi
}

install_node() {
  local machine arch archive url tmp expected actual
  machine="$(uname -m)"
  case "$machine" in
    x86_64) arch="x64" ;;
    aarch64|arm64) arch="arm64" ;;
    *) die "Unsupported architecture: $machine" ;;
  esac

  archive="node-v${NODE_VERSION}-linux-${arch}.tar.xz"
  url="https://nodejs.org/dist/v${NODE_VERSION}"
  if [[ ! -x "$NODE_ROOT/bin/node" ]]; then
    log "Installing Node.js v${NODE_VERSION} under $NODE_ROOT"
    tmp="$(mktemp -d)"
    trap 'rm -rf "${tmp:-}"' EXIT
    curl -fsSLo "$tmp/$archive" "$url/$archive"
    curl -fsSLo "$tmp/SHASUMS256.txt" "$url/SHASUMS256.txt"
    expected="$(awk -v f="$archive" '$2 == f {print $1}' "$tmp/SHASUMS256.txt")"
    [[ -n "$expected" ]] || die "Could not find Node checksum for $archive"
    actual="$(sha256sum "$tmp/$archive" | awk '{print $1}')"
    [[ "$actual" == "$expected" ]] || die "Node archive checksum mismatch"
    mkdir -p "$(dirname "$NODE_ROOT")"
    tar -xJf "$tmp/$archive" -C "$(dirname "$NODE_ROOT")"
    mv "$(dirname "$NODE_ROOT")/node-v${NODE_VERSION}-linux-${arch}" "$NODE_ROOT"
    rm -rf "$tmp"
    trap - EXIT
  fi

  mkdir -p "$LOCAL_BIN"
  export PATH="$NODE_ROOT/bin:$LOCAL_BIN:$PATH"
  hash -r
}

persist_path() {
  local rc="$HOME/.bashrc" begin='# >>> pi-ec2-setup >>>' end='# <<< pi-ec2-setup <<<'
  touch "$rc"
  python3 - "$rc" "$begin" "$end" "$NODE_ROOT" "$LOCAL_BIN" <<'PY'
from pathlib import Path
import sys
rc, begin, end, node_root, local_bin = sys.argv[1:]
p = Path(rc)
text = p.read_text()
start = text.find(begin)
if start >= 0:
    stop = text.find(end, start)
    if stop >= 0:
        text = text[:start] + text[stop + len(end):]
block = f'''{begin}
export PATH="{node_root}/bin:{local_bin}:$PATH"
{end}'''
p.write_text(text.rstrip() + "\n\n" + block + "\n")
PY
}

install_pi() {
  log "Installing Pi ${PI_VERSION}"
  npm install -g --prefix "$HOME/.local" --ignore-scripts \
    "@earendil-works/pi-coding-agent@${PI_VERSION}"
}

install_config() {
  log "Installing token-free Pi configuration"
  mkdir -p "$AGENT_DIR/skills" "$AGENT_DIR/packages"
  cp "$REPO_DIR/config/settings.json" "$AGENT_DIR/settings.json"
  cp "$REPO_DIR/config/mcp.json" "$AGENT_DIR/mcp.json"
  rm -rf "$AGENT_DIR/skills/glean" "$AGENT_DIR/packages/upstart-google-workspace"
  cp -a "$REPO_DIR/config/skills/glean" "$AGENT_DIR/skills/"
  cp -a "$REPO_DIR/config/packages/upstart-google-workspace" "$AGENT_DIR/packages/"

  python3 - "$AGENT_DIR" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1])
for path in root.rglob('*'):
    if not path.is_file():
        continue
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        continue
    if '__PI_AGENT_DIR__' in text:
        path.write_text(text.replace('__PI_AGENT_DIR__', str(root)))
PY

  chmod 700 "$AGENT_DIR"
  chmod 600 "$AGENT_DIR/settings.json" "$AGENT_DIR/mcp.json"
  find "$AGENT_DIR/packages/upstart-google-workspace/hooks" \
       "$AGENT_DIR/packages/upstart-google-workspace/scripts" \
       -type f -name '*.sh' -exec chmod +x {} +
  find "$AGENT_DIR/packages/upstart-google-workspace/scripts" \
       -type f -name '*.py' -exec chmod +x {} +
  [[ -e "$AGENT_DIR/google-workspace-setup" ]] || printf 'no_setup\n' > "$AGENT_DIR/google-workspace-setup"
  chmod 600 "$AGENT_DIR/google-workspace-setup"
}

install_packages() {
  log "Installing pinned Pi packages"
  mkdir -p "$AGENT_DIR/npm"
  if [[ ! -f "$AGENT_DIR/npm/package.json" ]]; then
    printf '{"name":"pi-extensions","private":true}\n' > "$AGENT_DIR/npm/package.json"
  fi
  npm install --prefix "$AGENT_DIR/npm" --save-exact --ignore-scripts \
    "@tintinweb/pi-subagents@${SUBAGENTS_VERSION}" \
    "pi-mcp-adapter@${MCP_ADAPTER_VERSION}"
}

verify() {
  log "Verifying installation"
  [[ "$(node --version)" == "v${NODE_VERSION}" ]] || die "Unexpected Node version: $(node --version)"
  [[ "$(pi --version)" == "$PI_VERSION" ]] || die "Unexpected Pi version: $(pi --version)"
  pi list
  printf '\nInstalled successfully.\n'
  printf 'Open a new shell (or run: source ~/.bashrc), then start: pi\n\n'
  printf 'Interactive authentication still required inside Pi:\n'
  printf '  1. Configure OPENAI_API_KEY securely, or run /login\n'
  printf '  2. Run /mcp-auth glean\n'
  printf '  3. Run /google-workspace-setup\n'
  printf '  4. Run /google-workspace-doctor\n'
}

install_prereqs
install_node
persist_path
install_pi
install_config
install_packages
verify
