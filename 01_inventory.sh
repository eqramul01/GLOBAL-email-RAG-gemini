#!/usr/bin/env bash
# 01_inventory.sh — read-only survey of the source mbox archive.
# Lists every *.mbox under the given root with size + message count
# (counted by "From - " separator lines, the mbox-rd convention).
#
# Usage: bash 01_inventory.sh /path/to/Mails
#
# Modifies nothing. Output is suitable for diffing against post-conversion
# manifest counts to confirm no messages were dropped.

set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <path-to-mails-root>" >&2
  exit 1
fi

ROOT="$1"
if [[ ! -d "$ROOT" ]]; then
  echo "Not a directory: $ROOT" >&2
  exit 1
fi

printf "%-12s  %-12s  %s\n" "SIZE" "MESSAGES" "PATH"
printf "%-12s  %-12s  %s\n" "----" "--------" "----"

total_msgs=0
total_files=0

# Use null-delimited find/read so paths with spaces survive
while IFS= read -r -d '' mbox; do
  size_bytes=$(stat -f%z "$mbox" 2>/dev/null || stat -c%s "$mbox")
  # Human-readable size
  size_h=$(awk -v b="$size_bytes" 'BEGIN{
    s="BKMGT"; for(i=1; b>=1024 && i<5; i++){b/=1024} printf "%.1f%s", b, substr(s,i,1)
  }')
  # Count "From - " message separators
  count=$(grep -c '^From - ' "$mbox" || true)
  total_msgs=$((total_msgs + count))
  total_files=$((total_files + 1))
  rel="${mbox#"$ROOT"/}"
  printf "%-12s  %-12s  %s\n" "$size_h" "$count" "$rel"
done < <(find "$ROOT" -type f -name '*.mbox' -print0 | sort -z)

printf "\nTotal: %d mbox files, %d messages\n" "$total_files" "$total_msgs"
