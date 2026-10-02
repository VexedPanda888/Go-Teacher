#!/usr/bin/env bash
# Zip each skill (every subdirectory holding a SKILL.md) into dist/<name>.zip.
# Each archive has the skill folder at its root, as Claude's skill upload expects.
# Usage: skills/package.sh [skill-name ...]   (no arguments: all skills)
set -euo pipefail

cd "$(dirname "$0")"
out="dist"
mkdir -p "$out"

if [ $# -gt 0 ]; then
  names=("$@")
else
  names=()
  for dir in */; do
    [ -f "$dir/SKILL.md" ] && names+=("${dir%/}")
  done
fi

for name in "${names[@]}"; do
  if [ ! -f "$name/SKILL.md" ]; then
    echo "skip: $name has no SKILL.md" >&2
    continue
  fi
  rm -f "$out/$name.zip"
  zip -qr "$out/$name.zip" "$name" -x '*.DS_Store' '*__pycache__*' '*.pyc'
  echo "$out/$name.zip"
done
