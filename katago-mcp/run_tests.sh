#!/usr/bin/env bash
# The repo's venv when there is one (it has mcp installed), else python3.
cd "$(dirname "$0")"
py=python3
for p in .venv/bin/python .venv/Scripts/python.exe; do [ -x "$p" ] && py="$p" && break; done
"$py" -m unittest discover -s tests -v "$@"
