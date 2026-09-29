"""The dashboard data blob: canonical JSON (what the SHA-256 is taken of) and the ownership codec.

skills/review-dashboard/scripts/build_dashboard.py keeps its own copy of `canonical` (the skill is
standalone); the two must stay identical.
"""
from __future__ import annotations

import json


def canonical(data) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def encode_ownership(own: list[float]) -> str:
    """Black-positive ownership as one letter per point: 'a' = -1.0 … 'k' = 0 … 'u' = +1.0."""
    out = []
    for o in own:
        k = int(round((max(-1.0, min(1.0, o)) + 1.0) * 10))
        out.append(chr(ord("a") + max(0, min(20, k))))
    return "".join(out)


def decode_ownership(s: str) -> list[float]:
    return [(ord(ch) - ord("a")) / 10.0 - 1.0 for ch in s]
