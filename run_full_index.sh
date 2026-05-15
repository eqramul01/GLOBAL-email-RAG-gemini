#!/usr/bin/env bash
# Kick off the full Gemini Embedding 2 indexing run in the background,
# log everything to index.log, print the PID.
#
# Usage:
#   bash run_full_index.sh
#
# Status afterward:
#   tail -f index.log
#   pgrep -f 03_index_emails
#   python3 03_index_emails_gemini.py "$HOME/GLOBAL_E-mail_Outlook/Maildir_Archive" --status

set -euo pipefail

cd "$HOME/GLOBAL_E-mail_Outlook/Email_Archive_Toolkit_Gemini"

# Verify GEMINI_API_KEY is set
if [[ -z "${GEMINI_API_KEY:-}" ]]; then
  echo "ERROR: GEMINI_API_KEY is not set in this shell." >&2
  echo "Run: source ~/.zshrc" >&2
  exit 2
fi

# Verify Maildir exists
MAILDIR="$HOME/GLOBAL_E-mail_Outlook/Maildir_Archive"
if [[ ! -d "$MAILDIR" ]]; then
  echo "ERROR: Maildir not found at $MAILDIR" >&2
  exit 2
fi

# Kick off in background, fully detached.
# -u = unbuffered stdout/stderr so progress shows up in index.log live
# (without -u, Python block-buffers when stdout isn't a TTY)
nohup python3 -u 03_index_emails_gemini.py "$MAILDIR" > index.log 2>&1 &
PID=$!
disown

echo "Indexer started in background."
echo "  PID:      $PID"
echo "  Log:      $(pwd)/index.log"
echo
echo "Watch live:    tail -f index.log"
echo "Quick status:  python3 03_index_emails_gemini.py \"$MAILDIR\" --status"
echo "Stop:          kill $PID"
