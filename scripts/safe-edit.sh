#!/usr/bin/env bash
set -euo pipefail

# Usage: safe-edit.sh <file> <command...>
# Makes a timestamped backup of <file>, runs <command...>, and commits changes if any.

FILE="${1:-}"
shift || true
if [[ -z "$FILE" ]]; then
  echo "usage: $0 <file> <command...>" >&2
  exit 2
fi

if [[ ! -f "$FILE" ]]; then
  echo "safe-edit: file not found: $FILE" >&2
  exit 2
fi

TS=$(date +%Y%m%d-%H%M%S)
BKDIR="/Users/MacBookAir/clawd/backups"
mkdir -p "$BKDIR"
BASENAME=$(basename "$FILE")
cp "$FILE" "$BKDIR/${BASENAME}.${TS}.bak"

# Run provided command (if any)
if [[ "$#" -gt 0 ]]; then
  "$@"
fi

# Commit if the file changed
cd /Users/MacBookAir/clawd
if ! git diff --quiet -- "$FILE"; then
  git add "$FILE" "$BKDIR/${BASENAME}.${TS}.bak" >/dev/null 2>&1 || true
  git commit -m "Auto backup + edit: $BASENAME ($TS)" >/dev/null 2>&1 || true
fi
