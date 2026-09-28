#!/usr/bin/env python3
"""Register katago-mcp in Claude Desktop's config (macOS or Windows), merging with what is already there.

  python3 install/register_claude_desktop.py --config config/m5pro.toml
  python3 install/register_claude_desktop.py --config config/m5pro.toml --dest /path/to/claude_desktop_config.json

Writes an entry named "katago" (change with --name) that runs the venv's katago-mcp with absolute paths.
Quit Claude Desktop completely (Cmd+Q / exit from the tray) and relaunch it afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def default_dest() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="machine TOML, e.g. config/m5pro.toml")
    ap.add_argument("--name", default="katago")
    ap.add_argument("--dest", help="claude_desktop_config.json path (default: the platform's)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    if ".Trash" in repo.parts or "Trash" in repo.parts:
        sys.exit(f"this copy of the script lives in the Trash ({repo}); run it from the live repo:\n"
                 f"  cd <repo>/katago-mcp && python3 install/register_claude_desktop.py --config config/m5pro.toml")
    cfg = Path(args.config).resolve()
    if not cfg.exists():
        sys.exit(f"config not found: {cfg}")
    exe = repo / (".venv/Scripts/katago-mcp.exe" if os.name == "nt" else ".venv/bin/katago-mcp")
    if not exe.exists():
        sys.exit(f"{exe} not found: run the installer first (install/macos.sh or install/windows.ps1)")
    entry = {"command": str(exe), "args": ["serve", "--config", str(cfg)]}
    if sys.platform == "darwin":
        home = Path.home()
        for prot in ("Desktop", "Documents", "Downloads"):
            if str(repo).startswith(str(home / prot) + "/"):
                print(f"WARNING: the repo is under ~/{prot}, a folder macOS privacy protection guards. Claude Desktop "
                      f"cannot run anything there unless it has been granted access to that folder (System Settings > "
                      f"Privacy & Security > Files and Folders). On managed Macs that grant may not take effect; "
                      f"moving the repo to e.g. ~/GitHub and rebuilding the venv avoids it.", file=sys.stderr)

    dest = Path(args.dest) if args.dest else default_dest()
    data: dict = {}
    if dest.exists():
        try:
            data = json.loads(dest.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            sys.exit(f"{dest} is not valid JSON ({e}); fix or move it first")
    servers = data.setdefault("mcpServers", {})
    servers[args.name] = entry
    text = json.dumps(data, indent=2)
    if args.dry_run:
        print(f"would write {dest}:\n{text}")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.with_suffix(".json.bak").write_text(dest.read_text(encoding="utf-8"), encoding="utf-8")
    dest.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {dest}\n{text}\n\nNow quit Claude Desktop completely and start it again; the tools menu should list '{args.name}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
