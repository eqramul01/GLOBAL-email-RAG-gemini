#!/usr/bin/env bash
# setup_launchd.sh — Install bridge.py as a launchd LaunchAgent so it
# auto-starts at every login and restarts if it crashes.
#
# After running this once, you never type `python3 bridge.py` again.
#
# Usage:
#   bash setup_launchd.sh
#
# To uninstall later:
#   launchctl unload ~/Library/LaunchAgents/com.user.email-bridge.plist
#   rm ~/Library/LaunchAgents/com.user.email-bridge.plist

set -euo pipefail

# --- Validate environment --------------------------------------------------

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "ERROR: GEMINI_API_KEY is not set in this shell." >&2
  echo "Run 'source ~/.zshrc' first, or set the variable, then re-run this script." >&2
  exit 2
fi

TOOLKIT="$HOME/GLOBAL_E-mail_Outlook/Email_Archive_Toolkit_Gemini"
MAILDIR="$HOME/GLOBAL_E-mail_Outlook/Maildir_Archive"

if [[ ! -f "$TOOLKIT/bridge.py" ]]; then
  echo "ERROR: bridge.py not found at $TOOLKIT/bridge.py" >&2
  exit 2
fi

if [[ ! -d "$MAILDIR/.lancedb" ]]; then
  echo "WARN: $MAILDIR/.lancedb doesn't exist. Bridge will start but search will fail." >&2
fi

# --- Stop any existing bridge ---------------------------------------------

echo "Stopping any existing bridge process..."
pkill -f "python3.*bridge.py" 2>/dev/null || true
sleep 2
if pgrep -f "python3.*bridge.py" > /dev/null; then
  echo "ERROR: a bridge process is still running. Try kill -9 manually." >&2
  exit 2
fi
echo "  no bridge running ✓"

# --- Generate the plist ---------------------------------------------------

PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/com.user.email-bridge.plist"

mkdir -p "$PLIST_DIR"

# If a previous version is loaded, unload it first
if [[ -f "$PLIST" ]]; then
  echo "Unloading existing plist..."
  launchctl unload "$PLIST" 2>/dev/null || true
fi

echo "Writing plist to $PLIST ..."
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.user.email-bridge</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>python3</string>
    <string>$TOOLKIT/bridge.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$TOOLKIT</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>GEMINI_API_KEY</key>
    <string>$GEMINI_API_KEY</string>
    <key>MAILDIR_ROOT</key>
    <string>$MAILDIR</string>
  </dict>

  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>

  <key>StandardOutPath</key><string>/tmp/email-bridge.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/email-bridge.err.log</string>
</dict>
</plist>
EOF

# --- Load it --------------------------------------------------------------

echo "Loading plist (this starts the bridge AND registers it for future logins)..."
launchctl load "$PLIST"

# --- Verify ---------------------------------------------------------------

# launchd takes a moment to spawn the process; bridge takes a few more
# seconds to load LanceDB. Poll the /health endpoint for up to 30 seconds
# instead of doing a single pgrep at a fixed offset.

echo
echo "Waiting up to 30 seconds for bridge to respond on /health..."
ok=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -sf http://127.0.0.1:8765/health > /tmp/bridge_health.json 2>/dev/null; then
    ok=1
    echo "  bridge responding ✓ (after $((i*2))s)"
    break
  fi
  sleep 2
done

if [[ -z "$ok" ]]; then
  echo "  ERROR: bridge never came up. Check logs:"
  echo "    tail /tmp/email-bridge.err.log"
  echo "    tail /tmp/email-bridge.out.log"
  exit 1
fi

echo
echo "== /health response =="
python3 -m json.tool < /tmp/bridge_health.json
rm -f /tmp/bridge_health.json

echo
echo "== Process info =="
PID=$(pgrep -f "python3.*bridge.py" | head -1 || true)
if [[ -n "$PID" ]]; then
  echo "  PID: $PID"
fi

echo
echo "==============================================================="
echo "  bridge installed as a LaunchAgent ✓"
echo "==============================================================="
echo
echo "It will auto-start at every login and auto-restart if it crashes."
echo
echo "Useful commands:"
echo "  Status:   curl -s http://127.0.0.1:8765/health | python3 -m json.tool"
echo "  Stop:     launchctl unload $PLIST"
echo "  Start:    launchctl load $PLIST"
echo "  Restart:  launchctl unload $PLIST && launchctl load $PLIST"
echo "  Logs:     tail -f /tmp/email-bridge.{out,err}.log"
echo "  Uninstall: launchctl unload $PLIST && rm $PLIST"
