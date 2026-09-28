#!/usr/bin/env python3
"""Check that a dashboard data blob (or an export-N.json from reviews/) matches its SHA-256.

  python scripts/verify_export.py reviews/ogs_12345678/export-1.json
  python scripts/verify_export.py blob.json <sha256>
"""
import hashlib
import json
import sys


def canonical(data) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        obj = json.load(f)
    if "data" in obj and "sha256" in obj:
        expected, data = obj["sha256"], obj["data"]
    elif len(sys.argv) >= 3:
        expected, data = sys.argv[2], obj
    else:
        print("a bare blob needs the expected sha256 as the second argument", file=sys.stderr)
        return 2
    got = hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()
    ok = got == expected
    print(f"{'OK' if ok else 'MISMATCH'}  expected {expected[:16]}…  got {got[:16]}…  episodes={len(data.get('episodes', []))}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
