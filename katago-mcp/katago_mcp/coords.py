"""Coordinate conversions.

Internal index: row-major from the top-left, i.e. index 0 is A19 and index
size*size-1 is T1 on a 19x19 board (tool contract §4.3).  GTP columns skip the
letter I.  SGF coordinates use letters a.. with 'aa' at the top-left.
"""
from __future__ import annotations

GTP_COLS = "ABCDEFGHJKLMNOPQRST"


class CoordError(ValueError):
    pass


def gtp_to_idx(pt: str | None, size: int = 19) -> int | None:
    """'D4' -> index; 'pass' / None -> None."""
    if pt is None:
        return None
    s = pt.strip().upper()
    if s == "PASS":
        return None
    if len(s) < 2:
        raise CoordError(f"bad point {pt!r}")
    col_ch, row_s = s[0], s[1:]
    if col_ch not in GTP_COLS[:size]:
        raise CoordError(f"bad column in {pt!r}")
    try:
        y = int(row_s)
    except ValueError:
        raise CoordError(f"bad row in {pt!r}") from None
    if not 1 <= y <= size:
        raise CoordError(f"row out of range in {pt!r}")
    return (size - y) * size + GTP_COLS.index(col_ch)


def idx_to_gtp(idx: int | None, size: int = 19) -> str:
    if idx is None:
        return "pass"
    r, c = divmod(idx, size)
    return f"{GTP_COLS[c]}{size - r}"


def sgf_to_idx(s: str, size: int = 19) -> int | None:
    """'pd' -> index; '' or 'tt' (size <= 19) -> None (pass)."""
    if s == "" or (s == "tt" and size <= 19):
        return None
    if len(s) != 2:
        raise CoordError(f"bad sgf point {s!r}")
    c = ord(s[0]) - ord("a")
    r = ord(s[1]) - ord("a")
    if not (0 <= c < size and 0 <= r < size):
        raise CoordError(f"sgf point out of range {s!r}")
    return r * size + c


def idx_to_sgf(idx: int | None, size: int = 19) -> str:
    if idx is None:
        return ""
    r, c = divmod(idx, size)
    return chr(ord("a") + c) + chr(ord("a") + r)


def rc(idx: int, size: int = 19) -> tuple[int, int]:
    return divmod(idx, size)


def chebyshev(a: int, b: int, size: int = 19) -> int:
    ra, ca = divmod(a, size)
    rb, cb = divmod(b, size)
    return max(abs(ra - rb), abs(ca - cb))


def neighbors(idx: int, size: int = 19) -> list[int]:
    r, c = divmod(idx, size)
    out = []
    if r > 0:
        out.append(idx - size)
    if r < size - 1:
        out.append(idx + size)
    if c > 0:
        out.append(idx - 1)
    if c < size - 1:
        out.append(idx + 1)
    return out


def star_points(size: int = 19) -> set[int]:
    if size == 19:
        lines = (3, 9, 15)
    elif size == 13:
        lines = (3, 6, 9)
    elif size == 9:
        lines = (2, 4, 6)
    else:
        return set()
    return {r * size + c for r in lines for c in lines}


def parse_move(m, size: int = 19) -> tuple[int, int | None]:
    """Accept ['B','Q7'], ('B', 'Q7'), 'BQ7', 'Bpass' -> (color_int, idx)."""
    from .board import CHAR_COLOR  # local import to avoid a cycle

    if isinstance(m, str):
        color_ch, pt = m[0], m[1:]
    else:
        color_ch, pt = m[0], m[1]
    if color_ch not in CHAR_COLOR:
        raise CoordError(f"bad color in move {m!r}")
    return CHAR_COLOR[color_ch], gtp_to_idx(pt, size)
