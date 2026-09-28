"""Regions (tool contract §0.6): the standard 9-way partition and Region specs."""
from __future__ import annotations

from .coords import gtp_to_idx, CoordError

STANDARD_CODES = ["UL", "U", "UR", "L", "C", "R", "LL", "D", "LR"]
LABELS = {
    "UL": "upper left corner", "U": "upper side", "UR": "upper right corner",
    "L": "left side", "C": "center", "R": "right side",
    "LL": "lower left corner", "D": "lower side", "LR": "lower right corner",
}
_GRID = [["UL", "U", "UR"], ["L", "C", "R"], ["LL", "D", "LR"]]


class RegionError(ValueError):
    pass


def _band(i: int, size: int) -> int:
    lo = (size - 5) // 2            # 7 on 19x19: cols A-G / rows 19-13
    if i < lo:
        return 0
    if i < lo + 5:
        return 1
    return 2


def standard_code(idx: int, size: int = 19) -> str:
    r, c = divmod(idx, size)
    return _GRID[_band(r, size)][_band(c, size)]


def standard_partition(size: int = 19) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {c: set() for c in STANDARD_CODES}
    for i in range(size * size):
        out[standard_code(i, size)].add(i)
    return out


def region_indices(spec: dict | None, size: int = 19) -> set[int]:
    """Resolve a Region spec to a set of indices."""
    if spec is None:
        raise RegionError("region required")
    if "standard" in spec:
        code = spec["standard"]
        if code not in LABELS:
            raise RegionError(f"unknown standard region {code!r}")
        return {i for i in range(size * size) if standard_code(i, size) == code}
    if "rect" in spec:
        try:
            a = gtp_to_idx(spec["rect"][0], size)
            b = gtp_to_idx(spec["rect"][1], size)
        except (CoordError, IndexError, TypeError) as e:
            raise RegionError(f"bad rect: {e}") from None
        ra, ca = divmod(a, size)
        rb, cb = divmod(b, size)
        r0, r1 = sorted((ra, rb))
        c0, c1 = sorted((ca, cb))
        return {r * size + c for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)}
    if "near" in spec:
        try:
            center = gtp_to_idx(spec["near"], size)
        except CoordError as e:
            raise RegionError(f"bad near point: {e}") from None
        radius = int(spec.get("radius", 3))
        rc0, cc0 = divmod(center, size)
        return {r * size + c for r in range(max(0, rc0 - radius), min(size, rc0 + radius + 1))
                for c in range(max(0, cc0 - radius), min(size, cc0 + radius + 1))}
    if "points" in spec:
        try:
            return {gtp_to_idx(p, size) for p in spec["points"]}
        except CoordError as e:
            raise RegionError(f"bad point list: {e}") from None
    raise RegionError("region spec needs one of standard/rect/near/points")


def bbox(indices: set[int], size: int = 19) -> tuple[int, int]:
    """Return (top-left idx, bottom-right idx) of the bounding box."""
    rows = [i // size for i in indices]
    cols = [i % size for i in indices]
    return min(rows) * size + min(cols), max(rows) * size + max(cols)


def expand(indices: set[int], by: int, size: int = 19) -> set[int]:
    out = set()
    for i in indices:
        r, c = divmod(i, size)
        for rr in range(max(0, r - by), min(size, r + by + 1)):
            for cc in range(max(0, c - by), min(size, c + by + 1)):
                out.add(rr * size + cc)
    return out

