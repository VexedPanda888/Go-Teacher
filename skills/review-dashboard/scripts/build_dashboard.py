#!/usr/bin/env python3
"""Build a review dashboard from the checksummed data blob produced by katago-mcp's validate_variations.

Usage
  build_dashboard.py --export reviews/<game_id>/export-1.json --out review.html
  build_dashboard.py --blob blob.json --sha <sha256 from validate_variations> --out review.html
  (add --template path/to/dashboard.html to use another template; default: ../template/dashboard.html)

The script refuses to build if the SHA-256 of the canonical JSON does not match: the dashboard only ever
shows data that came out of the engine unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def canonical(data) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def load(args) -> tuple[dict, str]:
    if args.export:
        obj = json.loads(Path(args.export).read_text(encoding="utf-8"))
        if "data" not in obj or "sha256" not in obj:
            sys.exit("export file must contain {sha256, data}")
        return obj["data"], obj["sha256"]
    if not args.blob or not args.sha:
        sys.exit("give --export, or --blob together with --sha")
    text = Path(args.blob).read_text(encoding="utf-8")
    return json.loads(text), args.sha


def check_shape(data: dict) -> list[str]:
    problems = []
    for key in ("game", "setup", "moves", "scoreSeries", "episodes", "ownership"):
        if key not in data:
            problems.append(f"missing top-level key {key!r}")
    if problems:
        return problems
    if len(data["scoreSeries"]) != len(data["moves"]) + 1:
        problems.append("scoreSeries must have one entry per position (moves + 1)")
    M = len(data["moves"])
    for e in data["episodes"]:
        if not (isinstance(e.get("moves"), list) and len(e["moves"]) == 2 and 1 <= e["moves"][0] <= e["moves"][1] <= M):
            problems.append(f"episode {e.get('id')} has a bad moves range")
        for br in e.get("branches", []):
            if len(br.get("evals", [])) != len(br.get("moves", [])):
                problems.append(f"branch {br.get('id')} of {e.get('id')}: evals and moves differ in length")
            if br.get("ownershipAtEnd") and br["ownershipAtEnd"] not in data["ownership"]:
                problems.append(f"branch {br.get('id')} of {e.get('id')}: ownershipAtEnd key not in ownership")
    for k, v in data["ownership"].items():
        if len(v) != 361:
            problems.append(f"ownership {k} is not 361 characters")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export")
    ap.add_argument("--blob")
    ap.add_argument("--sha")
    ap.add_argument("--out", required=True)
    ap.add_argument("--template", default=str(HERE.parent / "template" / "dashboard.html"))
    ap.add_argument("--allow-mismatch", action="store_true", help="build anyway (for template work only)")
    args = ap.parse_args()
    data, expected = load(args)
    got = hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()
    if got != expected:
        msg = f"checksum mismatch: expected {expected}, got {got}"
        if not args.allow_mismatch:
            sys.exit(msg + "\nThe blob was edited after validate_variations. Re-run validate_variations instead of editing.")
        print("WARNING " + msg, file=sys.stderr)
    problems = check_shape(data)
    if problems:
        sys.exit("data shape problems:\n  " + "\n  ".join(problems))
    template = Path(args.template).read_text(encoding="utf-8")
    marker = '<script id="data" type="application/json">{"__placeholder__": true}</script>'
    if marker not in template:
        sys.exit("template has no data placeholder")
    payload = canonical(data).replace("</", "<\\/").replace("<!--", "<\\!--")
    html = template.replace(marker, f'<script id="data" type="application/json">{payload}</script>')
    opp = data["game"].get("opponent") or "opponent"
    html = html.replace("<title>Go review</title>", f"<title>Go review: vs {opp}</title>", 1)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size:,} bytes), data sha256 {got[:16]}…, {len(data['episodes'])} episodes, {len(data['moves'])} moves")
    return 0


if __name__ == "__main__":
    sys.exit(main())
