#!/usr/bin/env bash
set -euo pipefail
for file in "$@"; do
  [[ -f "$file" ]] || continue
  awk '/[ \t\r]+$/ { printf "%s:%d: trailing whitespace\n", FILENAME, FNR; bad=1 } END { exit bad }' "$file"
  if [[ -s "$file" && -n $(tail -c 1 "$file") ]]; then
    printf '%s: missing final newline\n' "$file" >&2
    exit 1
  fi
done
