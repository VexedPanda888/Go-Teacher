#!/usr/bin/env bash
# katago-mcp install helper for Apple-silicon Macs (M2 Air, M5 Pro).
# Installs KataGo, downloads the two b18 networks, creates a venv, writes the thread count.
# Re-run safely; every step is idempotent.  Verify afterwards with:  katago-mcp selfcheck --config config/<machine>.toml
set -euo pipefail
cd "$(dirname "$0")/.."
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

if [ ! -d .venv ]; then
  echo ">> creating venv"; python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -e ".[dev]"

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
