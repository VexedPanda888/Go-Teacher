"""Stored results (tool contract §0.8) and the background preparation of key moments (§1.20).

A tool call blocks Claude's turn, so slow work runs while Claude talks to the student: when a survey
finishes, the server prepares `explain_moment` for its top key moments, and Claude may queue more with
`explain_moment(..., background: true)`. The work runs on one worker thread at a KataGo priority below
Claude's own calls, through the same `Tools` methods Claude calls, so every result lands in the result store
and Claude's identical later call returns it at once (or waits for it while it is being computed). Nothing
is shown to the student until Claude asks for it: the review asks what the student was thinking first.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jobs import Job
    from .tools import Tools

log = logging.getLogger("katago_mcp")


# ==================================================================== result store
@dataclass
class StoredResult:
    result: dict
    game_id: str | None
    query_id: str | None


class ResultStore:
    """Whole tool results keyed by (tool, position ref, visits, normalized arguments). A key being computed
    is in flight: an identical call waits for it instead of searching again."""

    def __init__(self, max_entries: int = 2000):
        self._entries: "OrderedDict[str, StoredResult]" = OrderedDict()
        self._inflight: set[str] = set()
        self._cv = threading.Condition(threading.RLock())
        self.max_entries = max_entries

    def acquire(self, key: str, timeout: float) -> StoredResult | None:
        """The stored result, or None after claiming the key (the caller computes, then put() or release())."""
        deadline = time.time() + timeout
        with self._cv:
            while key in self._inflight:
                left = deadline - time.time()
                if left <= 0:
                    return None          # compute alongside; the later put() wins
                self._cv.wait(left)
            e = self._entries.get(key)
            if e is not None:
                self._entries.move_to_end(key)
                return e
            self._inflight.add(key)
            return None

    def peek(self, key: str) -> str:
        """'done', 'running' (in flight) or 'none', without waiting or claiming."""
        with self._cv:
            if key in self._entries:
                return "done"
            return "running" if key in self._inflight else "none"

    def put(self, key: str, entry: StoredResult) -> None:
        with self._cv:
            self._inflight.discard(key)
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
            self._cv.notify_all()

    def release(self, key: str) -> None:
        with self._cv:
            self._inflight.discard(key)
            self._cv.notify_all()

    def forget_game(self, game_id: str) -> int:
        with self._cv:
            keys = [k for k, e in self._entries.items() if e.game_id == game_id]
            for k in keys:
                del self._entries[k]
            return len(keys)


# ==================================================================== prefetch
@dataclass
class Task:
    job_id: str
    move_number: int                  # the position after this move (the moment is the next move)
    move: str                         # the move explained against the best one
    options: dict = field(default_factory=dict)
    state: str = "queued"             # queued | running | done | failed | cancelled
    error: dict | None = None

    @property
    def key(self) -> tuple:
        return (self.job_id, self.move_number, self.move, json.dumps(self.options, sort_keys=True))


class Prefetcher:
    def __init__(self, tools: "Tools"):
        self.tools = tools
        self.cfg = tools.cfg.prefetch
        self.tasks: "OrderedDict[tuple, Task]" = OrderedDict()
        self._cv = threading.Condition(threading.RLock())
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.running: Task | None = None

    def after_survey(self, job: "Job") -> None:
        """Survey done: prepare its top key moments while Claude tells the story and asks about them."""
        n = int(self.cfg.moments)
        if n <= 0 or job.ga is None or not job.options.get("prefetch", True):
            return
        try:
            moments = self.tools.jobs.story(job.job_id, max_moments=n)["moments"]
        except Exception:  # noqa: BLE001
            log.exception("prefetch: no story for %s", job.job_id)
            return
        for m in moments:
            self.add(job.job_id, m["move"] - 1, m["played"])

    def add(self, job_id: str, move_number: int, move: str, options: dict | None = None) -> Task:
        t = Task(job_id, int(move_number), move, dict(options or {}))
        with self._cv:
            old = self.tasks.get(t.key)
            if old is not None and old.state != "cancelled":
                return old
            self.tasks[t.key] = t
            self._wake()
        return t

    def state(self, job_id: str, move_number: int, move: str, options: dict | None = None) -> str | None:
        with self._cv:
            t = self.tasks.get(Task(job_id, int(move_number), move, dict(options or {})).key)
            return None if t is None else t.state

    def pending(self, job_id: str) -> list[dict]:
        with self._cv:
            return [{"move_number": t.move_number, "move": t.move, "state": t.state, **({"error": t.error} if t.error else {})}
                    for t in self.tasks.values() if t.job_id == job_id and t.state != "cancelled"]

    def forget(self, job_id: str) -> None:
        with self._cv:
            for k in [k for k, t in self.tasks.items() if t.job_id == job_id]:
                t = self.tasks.pop(k)
                if t.state == "queued":
                    t.state = "cancelled"
            self._cv.notify_all()

    def stop(self, timeout: float = 30.0) -> None:
        """Cancel queued work and wait for the running task (the engine stops after this)."""
        self._stop.set()
        with self._cv:
            for t in self.tasks.values():
                if t.state == "queued":
                    t.state = "cancelled"
            self._cv.notify_all()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout)

    def _wake(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, daemon=True, name="katago-prefetch")
            self._worker.start()
        self._cv.notify_all()

    def _loop(self) -> None:
        self.tools._tls.priority = int(self.cfg.priority)
        while not self._stop.is_set():
            with self._cv:
                t = next((x for x in self.tasks.values() if x.state == "queued"), None)
                if t is None:
                    self._cv.wait(5.0)
                    continue
                t.state = "running"
                self.running = t
            state, error = "done", None
            try:
                self.tools.explain_moment({"job_id": t.job_id, "move_number": t.move_number}, t.move,
                                          options=t.options or None)
            except Exception as e:  # noqa: BLE001
                from .tools import ToolError
                state = "failed"
                error = e.to_dict()["error"] if isinstance(e, ToolError) else {"code": "internal", "message": f"{type(e).__name__}: {e}"}
                if not isinstance(e, ToolError):
                    log.exception("prefetch of %s move %d failed", t.job_id, t.move_number + 1)
            with self._cv:
                self.running = None
                if t.state == "running":
                    t.state, t.error = state, error
                self._cv.notify_all()
