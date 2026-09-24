#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
example_dir="$(mktemp -d /tmp/insitu-quickstart.XXXXXX)"
trap 'rm -rf -- "$example_dir"' EXIT

cp -R "$repo_root/examples/quickstart/." "$example_dir/"
cd "$example_dir"

insitu sync
insitu check
grep -q 'Hello from Insitu!' notes.md
