#!/usr/bin/env bash
# Check existing tracked and untracked source without treating deletions as files.
set -euo pipefail
files=()
while IFS= read -r -d '' file; do
  if [[ -f "$file" || -L "$file" ]]; then files+=("$file"); fi
done < <(git ls-files --cached --others --exclude-standard -z)
if (( ${#files[@]} )); then prek run --files "${files[@]}"; fi
