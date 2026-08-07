#!/bin/bash
# S4L universal installer: curl -fsSL https://s4l.ai/install | sh
#
# Zero-toolchain by design: needs only stock macOS (curl, unzip, python3).
# Downloads the release bundle (the .mcpb zip, which vendors its own Node
# runtime and the whole pipeline), extracts it to a durable host-neutral dir,
# registers the MCP server with whichever agent app is present (ChatGPT/Codex
# and/or Claude Code), and stamps the draft provider for the invoking host.
# The heavy runtime (Python venv, Chromium) self-provisions on first server
# boot, exactly as it does for .mcpb installs.
#
# Host detection order:
#   1. Invoking agent env: CODEX_THREAD_ID => codex chat; CLAUDE_CODE_*/CLAUDECODE
#      => Claude Code chat. The invoking host's engine becomes the draft provider.
#   2. Otherwise: whatever is installed (both is fine; provider prefers claude-p
#      when the claude CLI exists, else codex-exec).
#
# Channels: S4L_CHANNEL=staging pulls the staging-latest prerelease instead of
# the stable release. Optional S4L_INSTALL_HOST=codex|claude forces the provider
# choice regardless of detection.

set -euo pipefail

REPO="m13v/s4l"
STABLE_URL="https://github.com/$REPO/releases/latest/download/social-autoposter.mcpb"
STAGING_URL="https://github.com/$REPO/releases/download/staging-latest/social-autoposter.mcpb"
STATE_DIR="${S4L_STATE_DIR:-$HOME/.social-autoposter-mcp}"
APP_DIR="$STATE_DIR/app"
CODEX_BIN="/Applications/ChatGPT.app/Contents/Resources/codex"

log() { echo "[s4l-install] $*"; }
die() { echo "[s4l-install] ERROR: $*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "macOS only for now."
[ "$(uname -m)" = "arm64" ] || die "Apple Silicon only for now (the bundle vendors a darwin-arm64 Node)."

URL="$STABLE_URL"
[ "${S4L_CHANNEL:-}" = "staging" ] && URL="$STAGING_URL"

# 1. Download + extract. The .mcpb is a plain zip.
TMP="$(mktemp -d /tmp/s4l-install.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
log "downloading $URL"
/usr/bin/curl -fsSL -o "$TMP/s4l.mcpb" "$URL" || die "download failed"
mkdir -p "$TMP/app"
/usr/bin/unzip -q "$TMP/s4l.mcpb" -d "$TMP/app" || die "unzip failed"
[ -f "$TMP/app/dist/index.js" ] || die "bundle missing dist/index.js (bad download?)"
[ -x "$TMP/app/vendor/node-darwin-arm64/bin/node" ] || die "bundle missing vendored node"

mkdir -p "$STATE_DIR"
rm -rf "$APP_DIR.new"
mv "$TMP/app" "$APP_DIR.new"
if [ -d "$APP_DIR" ]; then rm -rf "$APP_DIR.old" && mv "$APP_DIR" "$APP_DIR.old"; fi
mv "$APP_DIR.new" "$APP_DIR"
rm -rf "$APP_DIR.old"
log "installed bundle to $APP_DIR"

NODE="$APP_DIR/vendor/node-darwin-arm64/bin/node"
SERVER="$APP_DIR/dist/index.js"
SAFE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# 2. Which host is driving this install?
HOST="${S4L_INSTALL_HOST:-}"
if [ -z "$HOST" ]; then
  if [ -n "${CODEX_THREAD_ID:-}" ]; then HOST="codex"
  elif [ -n "${CLAUDECODE:-}" ] || [ -n "${CLAUDE_CODE_ENTRYPOINT:-}" ]; then HOST="claude"
  fi
fi

HAVE_CODEX=0; [ -x "$CODEX_BIN" ] && HAVE_CODEX=1
HAVE_CLAUDE=0; command -v claude >/dev/null 2>&1 && HAVE_CLAUDE=1
[ "$HAVE_CODEX" = "1" ] || [ "$HAVE_CLAUDE" = "1" ] || die "neither the ChatGPT app nor the claude CLI was found; install one first (or double-click the .mcpb for Claude Desktop)."

# 3. Register the MCP server with every host present (registration is cheap and
# idempotent; the PROVIDER below is what decides who answers drafting turns).
if [ "$HAVE_CODEX" = "1" ]; then
  "$CODEX_BIN" mcp remove s4l >/dev/null 2>&1 || true
  "$CODEX_BIN" mcp add s4l --env "S4L_HOST=codex" --env "PATH=$SAFE_PATH" -- "$NODE" "$SERVER" \
    && log "registered MCP server with the ChatGPT app (codex)" \
    || log "WARN: codex mcp add failed; register manually with: codex mcp add s4l --env S4L_HOST=codex -- $NODE $SERVER"
fi
if [ "$HAVE_CLAUDE" = "1" ]; then
  claude mcp remove -s user s4l >/dev/null 2>&1 || true
  claude mcp add -s user s4l --env "PATH=$SAFE_PATH" -- "$NODE" "$SERVER" \
    && log "registered MCP server with Claude Code" \
    || log "WARN: claude mcp add failed; register manually with: claude mcp add -s user s4l -- $NODE $SERVER"
fi

# 4. Stamp the draft provider (single source of truth for which engine answers
# drafting turns; see scripts/draft_provider.py in the bundle).
PROVIDER=""
case "$HOST" in
  codex)  PROVIDER="codex-exec" ;;
  claude) PROVIDER="claude-p" ;;
  *) if [ "$HAVE_CLAUDE" = "1" ]; then PROVIDER="claude-p"; else PROVIDER="codex-exec"; fi ;;
esac
/usr/bin/python3 - "$STATE_DIR" "$PROVIDER" <<'PY'
import json, os, sys, time
sd, provider = sys.argv[1], sys.argv[2]
p = os.path.join(sd, "draft-provider.json")
tmp = p + ".tmp"
with open(tmp, "w") as f:
    json.dump({"provider": provider,
               "set_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "set_by": "install.sh"}, f, indent=2)
os.replace(tmp, p)
PY
log "draft provider: $PROVIDER"

# 5. Persistent server (codex-flavor boxes only). Claude Desktop keeps the MCP
# server process alive while the app runs, so the menu bar always has a live
# loopback to POST approvals to. The ChatGPT app spawns servers per chat and
# kills them after, which would strand menu-bar approvals between sessions.
# A KeepAlive LaunchAgent holds one server up; stdin is held open by tail
# because the stdio MCP transport exits on stdin EOF.
if [ "$PROVIDER" = "codex-exec" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.m13v.s4l-server.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$STATE_DIR/logs"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.m13v.s4l-server</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>-c</string>
    <string>tail -f /dev/null | exec "$NODE" "$SERVER"</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>S4L_HOST</key><string>codex</string>
    <key>S4L_STATE_DIR</key><string>$STATE_DIR</string>
    <key>PATH</key><string>$SAFE_PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$STATE_DIR/logs/s4l-server.log</string>
  <key>StandardErrorPath</key><string>$STATE_DIR/logs/s4l-server.err.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/com.m13v.s4l-server" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
    && log "persistent server LaunchAgent loaded (com.m13v.s4l-server)" \
    || log "WARN: could not load com.m13v.s4l-server; menu-bar approvals need the ChatGPT app open"
fi

log "done."
echo
echo "Next steps:"
if [ "$HOST" = "codex" ] || { [ -z "$HOST" ] && [ "$HAVE_CODEX" = "1" ]; }; then
  echo "  1. Fully quit and reopen the ChatGPT app so it picks up the new S4L connector."
  echo "  2. In a new chat, say: Set up S4L for my product."
else
  echo "  1. Restart your Claude session so it picks up the new S4L MCP server."
  echo "  2. In a new chat, say: Set up S4L for my product."
fi
