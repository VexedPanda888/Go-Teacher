"""Positions, refs, cache and logging (tool contract §0.2, §0.8, §7)."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .board import Board
from .engine import Analysis, PositionSpec


@dataclass
class PositionRecord:
    spec: PositionSpec
    ref: str
    game_id: str | None = None
    move_number: int | None = None
    _board: Board | None = field(default=None, repr=False)

    @property
    def board(self) -> Board:
        if self._board is None:
            self._board = self.spec.board()
        return self._board

    @property
    def to_move(self) -> int:
        return self.spec.to_move

    def to_json(self) -> dict:
        s = self.spec
        return {"ref": self.ref, "game_id": self.game_id, "move_number": self.move_number,
                "size": s.size, "rules": s.rules, "komi": s.komi, "setup_black": s.setup_black,
                "setup_white": s.setup_white, "moves": [[c, i] for c, i in s.moves], "first_to_move": s.first_to_move}

    @staticmethod
    def from_json(d: dict) -> "PositionRecord":
        spec = PositionSpec(d["size"], d["rules"], d["komi"], d["setup_black"], d["setup_white"],
                            [(c, i) for c, i in d["moves"]], d["first_to_move"])
        return PositionRecord(spec, d["ref"], d.get("game_id"), d.get("move_number"))


def make_ref(spec: PositionSpec, board: Board | None = None) -> str:
    board = board or spec.board()
    h = hashlib.sha1()
    h.update(f"{spec.rules}|{spec.komi}|{spec.size}|{board.board_hash}|{board.to_move}|{board.ko_point}|"
             f"{board.history_hash}".encode())
    return "pos_" + h.hexdigest()[:16]


class Store:
    """In-memory maps with JSON persistence under reviews/<game_id>/."""

    def __init__(self, reviews_dir: str | Path):
        self.reviews_dir = Path(reviews_dir)
        self.reviews_dir.mkdir(parents=True, exist_ok=True)
        self.positions: dict[str, PositionRecord] = {}
        self.cache: "OrderedDict[tuple, Analysis]" = OrderedDict()
        self.by_ref: dict[str, set] = {}
        self.max_cache_entries = 4000        # ~70 KB each with ownership + human policies -> a few hundred MB at most
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}

    # ---------------------------------------------------------------- positions
    def put_position(self, spec: PositionSpec, game_id: str | None = None, move_number: int | None = None,
                     persist: bool = True) -> PositionRecord:
        board = spec.board()
        ref = make_ref(spec, board)
        with self._lock:
            rec = self.positions.get(ref)
            if rec is None:
                rec = PositionRecord(spec, ref, game_id, move_number, board)
                self.positions[ref] = rec
            elif rec.game_id is None and game_id is not None:
                rec.game_id, rec.move_number = game_id, move_number
        if persist:
            d = self.game_dir(rec.game_id) / "positions"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"{ref}.json"
            if not p.exists():
                p.write_text(json.dumps(rec.to_json()))
        return rec

    def get_position(self, ref: str) -> PositionRecord | None:
        rec = self.positions.get(ref)
        if rec is not None:
            return rec
        # search on disk
        for p in self.reviews_dir.glob(f"*/positions/{ref}.json"):
            rec = PositionRecord.from_json(json.loads(p.read_text()))
            with self._lock:
                self.positions[ref] = rec
            return rec
        return None

    # ---------------------------------------------------------------- cache
    @staticmethod
    def cache_key(ref: str, visits: int, **opts) -> tuple:
        return (ref, int(visits), tuple(sorted((k, json.dumps(v, sort_keys=True)) for k, v in opts.items())))

    def get_cached(self, ref: str, min_visits: int, **opts) -> Analysis | None:
        """Best cached analysis for this ref with at least min_visits and compatible options."""
        best = None
        with self._lock:
            for key in self.by_ref.get(ref, ()):
                (r, v, o) = key
                a = self.cache.get(key)
                if a is None or v < min_visits:
                    continue
                od = dict((k, json.loads(val)) for k, val in o)
                ok = True
                for k, want in opts.items():
                    if want in (True, "required") and not od.get(k):
                        ok = False
                        break
                    if k in ("wide_root_noise", "allow", "avoid") and od.get(k) != want:
                        ok = False
                        break
                if ok and (best is None or a.visits > best.visits):
                    best = a
        return best

    def put_cached(self, ref: str, a: Analysis, **opts) -> None:
        key = self.cache_key(ref, a.visits, **opts)
        with self._lock:
            self.cache[key] = a
            self.cache.move_to_end(key)
            self.by_ref.setdefault(ref, set()).add(key)
            while len(self.cache) > self.max_cache_entries:
                old_key, _ = self.cache.popitem(last=False)
                keys = self.by_ref.get(old_key[0])
                if keys:
                    keys.discard(old_key)
                    if not keys:
                        del self.by_ref[old_key[0]]

    def any_ownership(self, ref: str) -> list[float] | None:
        best = None
        with self._lock:
            for key in self.by_ref.get(ref, ()):
                a = self.cache.get(key)
                if a is not None and a.ownership is not None and (best is None or a.visits > best.visits):
                    best = a
        return best.ownership if best else None

    def forget_game(self, game_id: str) -> int:
        """Drop in-memory analyses and positions of a game (its files under reviews/ stay)."""
        n = 0
        with self._lock:
            refs = [r for r, rec in self.positions.items() if rec.game_id == game_id]
            for r in refs:
                for key in self.by_ref.pop(r, set()):
                    if self.cache.pop(key, None) is not None:
                        n += 1
                self.positions.pop(r, None)
        return n

    # ---------------------------------------------------------------- dirs, ids, logs
    def game_dir(self, game_id: str | None) -> Path:
        d = self.reviews_dir / (game_id or "_adhoc")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def next_query_id(self, game_id: str | None) -> str:
        gid = game_id or "adhoc"
        with self._lock:
            self._seq[gid] = self._seq.get(gid, 0) + 1
            n = self._seq[gid]
        return f"q_{gid}_{n:04d}"

    def log_query(self, game_id: str | None, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
        p = self.game_dir(game_id) / "queries.jsonl"
        with self._lock:
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write_json(self, game_id: str | None, name: str, obj) -> Path:
        p = self.game_dir(game_id) / name
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False))
        tmp.replace(p)
        return p

    def read_json(self, game_id: str | None, name: str):
        p = self.game_dir(game_id) / name
        if not p.exists():
            return None
        return json.loads(p.read_text())
