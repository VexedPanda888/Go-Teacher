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
    path = sys.argv[1]
    obj = json.load(open(path, encoding="utf-8"))
    if "data" in obj and "sha256" in obj:
        expected, data = obj["sha256"], obj["data"]
    else:
        expected, data = sys.argv[2], obj
    got = hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()
    ok = got == expected
    print(f"{'OK' if ok else 'MISMATCH'}  expected {expected[:16]}…  got {got[:16]}…  episodes={len(data.get('episodes', []))}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
