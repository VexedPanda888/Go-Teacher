#!/usr/bin/env bash
# katago-mcp install helper for Apple-silicon Macs (M2 Air, M5 Pro).
# Installs KataGo, downloads the two b18 networks, creates a venv, writes the thread count.
# Re-run safely; every step is idempotent.  Verify afterwards with:  katago-mcp selfcheck --config config/<machine>.toml
set -euo pipefail
cd "$(dirname "$0")/.."
case "$(pwd)" in *"/.Trash/"*) echo "This folder is inside the Trash ($(pwd)). Move the repo out first."; exit 1;; esac
MACHINE="${1:-m5pro}"                 # m5pro | m2air
THREADS="${2:-}"                      # override numSearchThreadsPerAnalysisThread

if ! command -v katago >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo ">> installing KataGo with Homebrew"
    brew install katago
  else
    echo "Homebrew not found. Install it (https://brew.sh) or build KataGo from source:"
    echo "  git clone https://github.com/lightvector/KataGo && cd KataGo/cpp && cmake . -DUSE_BACKEND=METAL && make -j"
    exit 1
  fi
fi
echo ">> katago: $(command -v katago)"; katago version | head -3 || true
echo "   (Homebrew builds may use the OpenCL backend; a Metal build from source is usually faster."
echo "    The benchmark below measures whatever you have — the plan adapts to the measured visits/s.)"

mkdir -p models
if [ ! -f models/kata1-b18c384nbt-latest.bin.gz ]; then
  echo ">> download the latest kata1 b18c384nbt network from https://katagotraining.org/networks/"
  echo "   and save it as models/kata1-b18c384nbt-latest.bin.gz (or edit [katago].model in config/${MACHINE}.toml)"
fi
if [ ! -f models/b18c384nbt-humanv0.bin.gz ]; then
  echo ">> download b18c384nbt-humanv0.bin.gz from the KataGo releases page (v1.15.0 or later):"
  echo "   https://github.com/lightvector/KataGo/releases  -> save as models/b18c384nbt-humanv0.bin.gz"
fi

# Python >= 3.11 (tomllib).  Xcode's python3 is often 3.9; Homebrew's is fine.
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11+ not found. Install one:  brew install python@3.12   then re-run."; exit 1
fi
echo ">> python: $($PY --version) ($PY)"
if [ ! -d .venv ]; then
  echo ">> creating venv"; "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
pip install -q -e ".[dev]"
echo ">> installed: $(katago-mcp --help 2>/dev/null | head -1 || echo 'katago-mcp entry point missing')"

if [ -z "$THREADS" ]; then
  case "$MACHINE" in
    m2air) THREADS=8 ;;
    *)     THREADS=24 ;;
  esac
fi
sed -i.bak -E "s/^numSearchThreadsPerAnalysisThread = .*/numSearchThreadsPerAnalysisThread = ${THREADS}/" config/analysis.cfg && rm -f config/analysis.cfg.bak
echo ">> numSearchThreadsPerAnalysisThread = ${THREADS}"

BIN="$(command -v katago)"
python3 - "$MACHINE" "$BIN" <<'PY'
import re, sys
machine, binary = sys.argv[1], sys.argv[2]
p = f"config/{machine}.toml"
s = open(p).read()
s = re.sub(r'^binary = .*$', f'binary = "{binary}"', s, flags=re.M)
open(p, "w").write(s)
print(f">> wrote binary path into {p}")
PY

cat <<MSG

Next steps
  1. Put the two networks in models/ (see above), then:
       source .venv/bin/activate
       katago-mcp benchmark --config config/${MACHINE}.toml        # 20 s; saves visits/s
       katago-mcp selfcheck --config config/${MACHINE}.toml --sgf path/to/a/game.sgf
  2. Register the server in Claude Desktop (README §4) or Claude Code:
       claude mcp add katago -- $(pwd)/.venv/bin/katago-mcp serve --config $(pwd)/config/${MACHINE}.toml
MSG
