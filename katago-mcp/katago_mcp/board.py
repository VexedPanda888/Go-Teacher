"""Go board: stones, groups, liberties, captures, ko/superko, legality.

Colors are ints: EMPTY=0, BLACK=1, WHITE=2.  Boards are immutable in use:
`play()` returns a new Board.  Hashes are Zobrist (deterministic seed) so
position references are stable across machines and restarts.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable

from .coords import idx_to_gtp, neighbors

EMPTY, BLACK, WHITE = 0, 1, 2
COLOR_CHAR = {BLACK: "B", WHITE: "W"}
CHAR_COLOR = {"B": BLACK, "W": WHITE, "b": BLACK, "w": WHITE}


def opponent(color: int) -> int:
    return BLACK if color == WHITE else WHITE


# KataGo rule presets that matter for legality (suicide, superko) and for the
# reconciliation check (area vs territory, handicap bonus).
RULESETS: dict[str, dict] = {
    "japanese": dict(suicide=False, superko=False, area=False, handicap_bonus=False),
    "korean": dict(suicide=False, superko=False, area=False, handicap_bonus=False),
    "chinese": dict(suicide=False, superko=True, area=True, handicap_bonus=True),
    "chinese-ogs": dict(suicide=False, superko=True, area=True, handicap_bonus=True),
    "chinese-kgs": dict(suicide=False, superko=True, area=True, handicap_bonus=True),
    "aga": dict(suicide=False, superko=True, area=True, handicap_bonus=True),
    "bga": dict(suicide=False, superko=True, area=True, handicap_bonus=True),
    "tromp-taylor": dict(suicide=True, superko=True, area=True, handicap_bonus=False),
    "new-zealand": dict(suicide=True, superko=True, area=True, handicap_bonus=False),
}

_MAX = 19
_rng = random.Random(20260927)
_Z_STONE = [[0] * (_MAX * _MAX), [_rng.getrandbits(64) for _ in range(_MAX * _MAX)],
            [_rng.getrandbits(64) for _ in range(_MAX * _MAX)]]
_Z_KO = [_rng.getrandbits(64) for _ in range(_MAX * _MAX)]
_Z_WHITE_TO_MOVE = _rng.getrandbits(64)
_MASK64 = (1 << 64) - 1


class IllegalMove(Exception):
    def __init__(self, reason: str, idx: int | None = None, size: int = 19):
        self.reason = reason
        self.idx = idx
        pt = idx_to_gtp(idx, size) if idx is not None else "pass"
        super().__init__(f"illegal move at {pt}: {reason}")


@dataclass(frozen=True)
class Group:
    color: int
    stones: tuple[int, ...]
    liberties: tuple[int, ...]

    @property
    def size(self) -> int:
        return len(self.stones)

    @property
    def anchor(self) -> int:
        return min(self.stones)


class Board:
    __slots__ = ("size", "cells", "to_move", "ko_point", "captures", "board_hash",
                 "history", "history_hash", "move_count")

    def __init__(self, size: int = 19, to_move: int = BLACK):
        if size > _MAX or size < 5:
            raise ValueError("board size must be 5..19")
        self.size = size
        self.cells = [EMPTY] * (size * size)
        self.to_move = to_move
        self.ko_point: int | None = None
        self.captures = {BLACK: 0, WHITE: 0}   # stones captured BY that color
        self.board_hash = 0
        self.history: frozenset[int] = frozenset()   # board hashes seen (positional superko)
        self.history_hash = 0
        self.move_count = 0

    @classmethod
    def from_setup(cls, size: int, to_move: int, setup_black=(), setup_white=()) -> "Board":
        """Empty board with setup stones (AB/AW), before any move."""
        b = cls(size, to_move=to_move)
        for i in setup_black:
            b.place(BLACK, i)
        for i in setup_white:
            b.place(WHITE, i)
        return b

    # ------------------------------------------------------------------ basics
    def copy(self) -> "Board":
        b = Board.__new__(Board)
        b.size = self.size
        b.cells = self.cells[:]
        b.to_move = self.to_move
        b.ko_point = self.ko_point
        b.captures = dict(self.captures)
        b.board_hash = self.board_hash
        b.history = self.history
        b.history_hash = self.history_hash
        b.move_count = self.move_count
        return b

    def __getitem__(self, idx: int) -> int:
        return self.cells[idx]

    def state_hash(self) -> int:
        h = self.board_hash
        if self.to_move == WHITE:
            h ^= _Z_WHITE_TO_MOVE
        if self.ko_point is not None:
            h ^= _Z_KO[self.ko_point]
        return h

    def stones(self, color: int | None = None) -> list[int]:
        if color is None:
            return [i for i, c in enumerate(self.cells) if c != EMPTY]
        return [i for i, c in enumerate(self.cells) if c == color]

    def empties(self) -> list[int]:
        return [i for i, c in enumerate(self.cells) if c == EMPTY]

    # ------------------------------------------------------------------ groups
    def group_at(self, idx: int) -> Group | None:
        color = self.cells[idx]
        if color == EMPTY:
            return None
        stones = []
        libs = set()
        seen = {idx}
        stack = [idx]
        while stack:
            p = stack.pop()
            stones.append(p)
            for n in neighbors(p, self.size):
                c = self.cells[n]
                if c == EMPTY:
                    libs.add(n)
                elif c == color and n not in seen:
                    seen.add(n)
                    stack.append(n)
        return Group(color, tuple(sorted(stones)), tuple(sorted(libs)))

    def groups(self) -> list[Group]:
        seen = set()
        out = []
        for i, c in enumerate(self.cells):
            if c != EMPTY and i not in seen:
                g = self.group_at(i)
                seen.update(g.stones)
                out.append(g)
        return out

    # ------------------------------------------------------------------ setup
    def place(self, color: int, idx: int) -> None:
        """Setup stone (AB/AW).  Mutates in place; no captures, no legality."""
        old = self.cells[idx]
        if old != EMPTY:
            self.board_hash ^= _Z_STONE[old][idx]
        self.cells[idx] = color
        if color != EMPTY:
            self.board_hash ^= _Z_STONE[color][idx]

    def _remove(self, stones: Iterable[int]) -> int:
        n = 0
        for s in stones:
            self.board_hash ^= _Z_STONE[self.cells[s]][s]
            self.cells[s] = EMPTY
            n += 1
        return n

    # ------------------------------------------------------------------ play
    def play(self, color: int, idx: int | None, rules: str = "japanese") -> "Board":
        rs = RULESETS.get(rules, RULESETS["japanese"])
        b = self.copy()
        b.move_count += 1
        if idx is None:  # pass
            b.ko_point = None
            b.to_move = opponent(color)
            b.history = b.history | {b.board_hash}
            b.history_hash = ((b.history_hash * 1000003) ^ b.board_hash) & _MASK64
            return b
        if not 0 <= idx < self.size * self.size:
            raise IllegalMove("off board", idx, self.size)
        if b.cells[idx] != EMPTY:
            raise IllegalMove("occupied", idx, self.size)
        if b.ko_point == idx and color == b.to_move:
            raise IllegalMove("ko", idx, self.size)
        b.cells[idx] = color
        b.board_hash ^= _Z_STONE[color][idx]
        captured = 0
        captured_point: int | None = None
        opp = opponent(color)
        for n in neighbors(idx, self.size):
            if b.cells[n] == opp:
                g = b.group_at(n)
                if not g.liberties:
                    captured += b._remove(g.stones)
                    captured_point = g.stones[0] if len(g.stones) == 1 else None
        if captured:
            b.captures[color] += captured
        own = b.group_at(idx)
        if not own.liberties:
            if rs["suicide"]:
                n = b._remove(own.stones)
                b.captures[opp] += n
            else:
                raise IllegalMove("suicide", idx, self.size)
        # simple ko: single capture by a single stone that now has exactly one liberty
        b.ko_point = None
        if captured == 1 and captured_point is not None and own.size == 1 and len(own.liberties) == 1:
            # after the capture the placed stone's only liberty is the captured point
            b.ko_point = captured_point
        if rs["superko"] and b.board_hash in self.history:
            raise IllegalMove("superko", idx, self.size)
        b.to_move = opponent(color)
        b.history = b.history | {b.board_hash}
        b.history_hash = ((b.history_hash * 1000003) ^ b.board_hash) & _MASK64
        return b

    # ------------------------------------------------------------------ misc
    def ko_capture_available(self, rules: str = "japanese") -> bool:
        """True if either side could capture a single stone in a ko shape right now."""
        for color in (BLACK, WHITE):
            opp = opponent(color)
            for i in self.empties():
                for n in neighbors(i, self.size):
                    if self.cells[n] == opp:
                        g = self.group_at(n)
                        if g.size == 1 and len(g.liberties) == 1 and g.liberties[0] == i:
                            # capturing would leave our stone with one liberty (the captured point)?
                            try:
                                after = self.play(color, i, rules)
                            except IllegalMove:
                                continue
                            if after.ko_point is not None:
                                return True
        return False

