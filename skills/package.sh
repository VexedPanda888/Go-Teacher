#!/usr/bin/env bash
# Zip each skill (every subdirectory holding a SKILL.md) into dist/<name>.zip.
# Each archive has the skill folder at its root, as Claude's skill upload expects.
# Uses `zip` when installed, else Python's zipfile (Git Bash on Windows has no zip).
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

# The archiver: zip, or the first Python that runs (python3 can be a Windows Store stub that does not).
python=""
if ! command -v zip >/dev/null 2>&1; then
  for p in python3 python py ../katago-mcp/.venv/bin/python ../katago-mcp/.venv/Scripts/python.exe; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import zipfile" >/dev/null 2>&1; then
      python="$p"
      break
    fi
  done
  if [ -z "$python" ]; then
    echo "package.sh: needs zip or Python 3" >&2
    exit 1
  fi
fi

pyzip() {   # pyzip <archive> <folder>: the same contents and exclusions as the zip command below
  "$python" - "$1" "$2" <<'EOF'
import fnmatch, os, sys, zipfile
archive, folder = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            if f == ".DS_Store" or fnmatch.fnmatch(f, "*.pyc"):
                continue
            p = os.path.join(root, f)
            z.write(p, p.replace(os.sep, "/"))
EOF
}

for name in "${names[@]}"; do
  if [ ! -f "$name/SKILL.md" ]; then
    echo "skip: $name has no SKILL.md" >&2
    continue
  fi
  rm -f "$out/$name.zip"
  if [ -z "$python" ]; then
    zip -qr "$out/$name.zip" "$name" -x '*.DS_Store' '*__pycache__*' '*.pyc'
  else
    pyzip "$out/$name.zip" "$name"
  fi
  echo "$out/$name.zip"
done
