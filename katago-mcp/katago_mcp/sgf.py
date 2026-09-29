"""Minimal SGF parser for OGS game records (main line only).

Handles: FF[4] collections, escaped values, multi-valued properties (AB/AW),
setup stones, handicap, komi, rules, result, players and ranks, dates, the
OGS game id, time settings, and whether per-move clock properties exist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .board import BLACK, WHITE
from .coords import sgf_to_idx

# An online-go.com game link (game/N, game/view/N, api/v1/games/N): the game id in group 1.
OGS_GAME_RE = re.compile(r"online-go\.com/(?:api/v1/games/|game/)?(?:view/)?(\d+)")


class SgfError(ValueError):
    pass


# ----------------------------------------------------------------- tokenizer
class _Tree:
    __slots__ = ("nodes", "children")

    def __init__(self):
        self.nodes: list[dict] = []
        self.children: list["_Tree"] = []


def _parse_tree(text: str, i: int) -> tuple[_Tree, int]:
    """Parse a GameTree starting at text[i] == '('; return (tree, index after ')')."""
    n = len(text)
    assert text[i] == "("
    i += 1
    tree = _Tree()
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
        elif ch == ";":
            i += 1
            node: dict[str, list[str]] = {}
            while i < n:
                while i < n and text[i] in " \t\r\n":
                    i += 1
                if i >= n or text[i] in ";()":
                    break
                m = re.match(r"[A-Za-z]+", text[i:])
                if not m:
                    raise SgfError(f"bad property at offset {i}")
                ident = "".join(c for c in m.group(0) if c.isupper())
                i += m.end()
                while i < n and text[i] in " \t\r\n":
                    i += 1
                values: list[str] = []
                while i < n and text[i] == "[":
                    j = _skip_value(text, i)
                    values.append(_unescape(text[i + 1:j - 1]))
                    i = j
                    while i < n and text[i] in " \t\r\n":
                        i += 1
                node.setdefault(ident, []).extend(values)
            tree.nodes.append(node)
        elif ch == "(":
            child, i = _parse_tree(text, i)
            tree.children.append(child)
        elif ch == ")":
            return tree, i + 1
        else:
            raise SgfError(f"unexpected character {ch!r} at offset {i}")
    raise SgfError("unterminated game tree")


def _parse_collection(text: str) -> list[dict]:
    """Return the main line (first variation at every branch) as a list of nodes."""
    i = 0
    n = len(text)
    while i < n and text[i] in " \t\r\n\ufeff":
        i += 1
    if i >= n or text[i] != "(":
        raise SgfError("SGF must start with '('")
    tree, _ = _parse_tree(text, i)
    nodes: list[dict] = []
    t: _Tree | None = tree
    while t is not None:
        nodes.extend(t.nodes)
        t = t.children[0] if t.children else None
    if not nodes:
        raise SgfError("no nodes")
    return nodes


def _skip_value(text: str, i: int) -> int:
    """i points at '['; return index just past the matching ']'."""
    j = i + 1
    n = len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "]":
            return j + 1
        j += 1
    raise SgfError("unterminated property value")


def _unescape(v: str) -> str:
    return re.sub(r"\\(.)", r"\1", v)


# ----------------------------------------------------------------- model
OGS_RULES = {
    "japanese": "japanese", "chinese": "chinese-ogs", "korean": "korean", "aga": "aga",
    "nz": "new-zealand", "new zealand": "new-zealand", "ing": "chinese-ogs",
}


@dataclass
class SgfGame:
    size: int = 19
    setup_black: list[int] = field(default_factory=list)
    setup_white: list[int] = field(default_factory=list)
    moves: list[tuple[int, int | None]] = field(default_factory=list)   # (color, idx|None)
    handicap: int = 0
    komi: float | None = None
    rules_raw: str | None = None
    rules: str = "japanese"
    result_raw: str = ""
    players: dict = field(default_factory=dict)      # {"B": {"name","rank"}, "W": {...}}
    date: str | None = None
    place: str | None = None
    comment: str | None = None
    game_name: str | None = None
    ogs_game_id: str | None = None
    time_main: int | None = None
    overtime: str | None = None
    per_move_times: bool = False
    warnings: list[str] = field(default_factory=list)
    root: dict = field(default_factory=dict)

    # ---- derived
    @property
    def first_to_move(self) -> int:
        if self.moves:
            return self.moves[0][0]
        return WHITE if self.handicap >= 2 or self.setup_black else BLACK

    def result(self) -> dict:
        raw = (self.result_raw or "").strip()
        out = {"raw": raw, "winner": None, "margin": None, "method": "unknown"}
        if not raw or raw in ("?", "Void"):
            return out
        if raw in ("0", "Draw", "Jigo"):
            out["method"] = "score"
            out["margin"] = 0.0
            return out
        m = re.match(r"^([BW])\+(.*)$", raw)
        if not m:
            return out
        out["winner"] = m.group(1)
        rest = m.group(2).strip()
        if rest in ("R", "Resign"):
            out["method"] = "resign"
        elif rest in ("T", "Time"):
            out["method"] = "time"
        elif rest in ("F", "Forfeit"):
            out["method"] = "forfeit"
        else:
            try:
                out["margin"] = float(rest)
                out["method"] = "score"
            except ValueError:
                pass
        return out


def parse_rank(s: str | None) -> str | None:
    if not s:
        return None
    m = re.search(r"(\d+)\s*([kdpKDP])", s)
    if not m:
        return None
    return f"{int(m.group(1))}{m.group(2).lower()}"


def rank_to_profile(rank: str | None) -> str | None:
    """'10k' -> 'rank_10k', clamped to the human model's range; pro -> rank_9d."""
    if not rank:
        return None
    m = re.match(r"(\d+)([kdp])", rank)
    if not m:
        return None
    n, kind = int(m.group(1)), m.group(2)
    if kind == "k":
        return f"rank_{min(max(n, 1), 20)}k"
    if kind == "d":
        return f"rank_{min(max(n, 1), 9)}d"
    return "rank_9d"


def rank_stronger_by(rank: str, stones: int) -> str:
    """Move a kyu/dan rank `stones` stronger: 7k +3 -> 4k; 2k +3 -> 1d... (1k -> 1d is one step)."""
    m = re.match(r"(\d+)([kd])", rank)
    if not m:
        return rank
    n, kind = int(m.group(1)), m.group(2)
    level = (-n) if kind == "k" else (n - 1)      # 1k -> -1, 1d -> 0, 2d -> 1
    level += stones
    if level < 0:
        return f"{-level}k"
    return f"{min(level + 1, 9)}d"


def parse(text: str) -> SgfGame:
    nodes = _parse_collection(text)
    root = nodes[0]
    g = SgfGame(root=root)
    size_v = root.get("SZ", ["19"])[0]
    try:
        g.size = int(size_v.split(":")[0])
    except ValueError:
        raise SgfError(f"bad SZ {size_v!r}") from None
    for v in root.get("AB", []):
        idx = sgf_to_idx(v, g.size)
        if idx is not None:
            g.setup_black.append(idx)
    for v in root.get("AW", []):
        idx = sgf_to_idx(v, g.size)
        if idx is not None:
            g.setup_white.append(idx)
    if "HA" in root:
        try:
            g.handicap = int(root["HA"][0])
        except ValueError:
            g.warnings.append(f"unreadable HA {root['HA'][0]!r}")
    if g.handicap and not g.setup_black:
        g.warnings.append("HA present but no AB stones; handicap stones missing from the record")
    if "KM" in root:
        try:
            g.komi = float(root["KM"][0])
        except ValueError:
            g.warnings.append(f"unreadable KM {root['KM'][0]!r}")
    else:
        g.warnings.append("no KM property; komi assumed (confirm with the player)")
    if "RU" in root:
        g.rules_raw = root["RU"][0]
        g.rules = OGS_RULES.get(g.rules_raw.strip().lower(), "japanese")
        if g.rules_raw.strip().lower() not in OGS_RULES:
            g.warnings.append(f"unknown RU {g.rules_raw!r}; using japanese")
    g.result_raw = root.get("RE", [""])[0]
    g.players = {
        "B": {"name": root.get("PB", [""])[0], "rank": parse_rank(root.get("BR", [None])[0])},
        "W": {"name": root.get("PW", [""])[0], "rank": parse_rank(root.get("WR", [None])[0])},
    }
    g.date = root.get("DT", [None])[0]
    g.place = root.get("PC", [None])[0]
    g.comment = root.get("GC", [None])[0]
    g.game_name = root.get("GN", [None])[0]
    m = OGS_GAME_RE.search(text)
    if m:
        g.ogs_game_id = m.group(1)
    if "TM" in root:
        try:
            g.time_main = int(float(root["TM"][0]))
        except ValueError:
            pass
    g.overtime = root.get("OT", [None])[0]
    # moves along the main line; setup stones may also appear in later nodes (rare)
    for node in nodes[1:] if ("B" not in root and "W" not in root) else nodes:
        if "B" in node:
            g.moves.append((BLACK, sgf_to_idx(node["B"][0], g.size)))
        elif "W" in node:
            g.moves.append((WHITE, sgf_to_idx(node["W"][0], g.size)))
        if "BL" in node or "WL" in node:
            g.per_move_times = True
    if g.komi is None:
        g.komi = 0.5 if g.handicap >= 2 else 6.5
    return g
