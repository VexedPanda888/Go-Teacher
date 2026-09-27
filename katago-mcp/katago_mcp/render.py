"""ASCII rendering (tool contract §4)."""
from __future__ import annotations

from .board import BLACK, WHITE, EMPTY, COLOR_CHAR, Board
from .coords import GTP_COLS, idx_to_gtp, star_points


def render_board(board: Board, last_move: tuple[int, int | None] | None = None, mark_last: bool = True,
                 highlight: set[int] | None = None, region_box: set[int] | None = None,
                 coordinates: bool = True) -> str:
    size = board.size
    stars = star_points(size)
    highlight = highlight or set()
    last_idx = last_move[1] if (last_move and mark_last) else None
    header = f"{'Black' if board.to_move == BLACK else 'White'} to move"
    if last_move is not None:
        header += f" · last move: {COLOR_CHAR[last_move[0]]} {idx_to_gtp(last_move[1], size)}"
        if last_idx is not None and mark_last:
            header += " (@)"
    header += f" · captures B {board.captures[BLACK]}, W {board.captures[WHITE]}"
    lines = [header]
    col_line = "    " + " ".join(GTP_COLS[:size])
    if coordinates:
        lines.append(col_line)
    for r in range(size):
        cells = []
        for c in range(size):
            i = r * size + c
            v = board.cells[i]
            if i == last_idx:
                ch = "@"
            elif i in highlight:
                ch = "*" if v == EMPTY else ("X" if v == BLACK else "O")
            elif v == BLACK:
                ch = "X"
            elif v == WHITE:
                ch = "O"
            elif region_box is not None and i in region_box:
                ch = "-"
            elif i in stars:
                ch = ","
            else:
                ch = "."
            cells.append(ch)
        row_label = f"{size - r:>3} " if coordinates else ""
        tail = f" {size - r:>2}" if coordinates else ""
        lines.append(row_label + " ".join(cells) + tail)
    if coordinates:
        lines.append(col_line)
    return "\n".join(lines)


LEGEND = ("X Black · O White · . empty · , star point · @ last move · * highlighted · - region box; "
          "overlay: B/b Black (strong/weak), W/w White, . neutral")


def render_overlay(values: list[float], size: int, kind: str = "ownership", board: Board | None = None) -> str:
    """Render a 361-value array as a grid (Black-positive for ownership)."""
    rows = ["    " + " ".join(GTP_COLS[:size])]
    top = None
    if kind == "policy":
        order = sorted(range(size * size), key=lambda i: -values[i])[:9]
        top = {idx: str(9 - k) for k, idx in enumerate(order) if values[idx] > 0}
    for r in range(size):
        cells = []
        for c in range(size):
            i = r * size + c
            v = values[i]
            if kind == "ownership":
                ch = "B" if v >= 0.6 else "b" if v >= 0.2 else "W" if v <= -0.6 else "w" if v <= -0.2 else "."
            elif kind == "ownership_stdev":
                ch = str(min(9, max(0, int(round(v * 10)))))
            else:
                ch = top.get(i, ".") if top else "."
            cells.append(ch)
        rows.append(f"{size - r:>3} " + " ".join(cells) + f" {size - r:>2}")
    rows.append(rows[0])
    return "\n".join(rows)


def low_liberty_groups(board: Board, max_libs: int = 3, min_size: int = 1) -> list[dict]:
    out = []
    for g in board.groups():
        if len(g.liberties) <= max_libs and g.size >= min_size:
            out.append({
                "label": f"{COLOR_CHAR[g.color]} {idx_to_gtp(g.anchor, board.size)} group ({g.size})",
                "color": COLOR_CHAR[g.color],
                "anchor": idx_to_gtp(g.anchor, board.size),
                "size": g.size,
                "liberties": len(g.liberties),
                "liberty_points": [idx_to_gtp(l, board.size) for l in g.liberties],
            })
    out.sort(key=lambda d: (d["liberties"], -d["size"]))
    return out
