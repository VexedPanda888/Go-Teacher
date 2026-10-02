"""KataGo analysis engine wrapper and a deterministic mock.

All results are normalized to `Analysis` with Black-perspective winrate and
score lead and Black-positive ownership, regardless of KataGo's
reportAnalysisWinratesAs setting (tool contract §0.3).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Callable

from .board import BLACK, WHITE, COLOR_CHAR, Board, IllegalMove, opponent
from pathlib import Path

from .coords import gtp_to_idx, idx_to_gtp, star_points


class EngineError(Exception):
    def __init__(self, code: str, message: str, recoverable: bool = True, suggestion: str | None = None):
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.suggestion = suggestion


@dataclass
class Candidate:
    move: int | None
    order: int
    visits: int
    prior: float
    winrate: float          # Black perspective
    score_lead: float       # Black perspective
    score_stdev: float
    lcb: float              # Black perspective
    utility: float
    pv: list[int | None] = field(default_factory=list)


@dataclass
class Analysis:
    to_move: int
    visits: int
    winrate: float
    score_lead: float
    score_stdev: float
    candidates: list[Candidate]
    policy: list[float] | None = None            # length size*size+1 (last = pass), -1 for illegal
    ownership: list[float] | None = None         # Black-positive
    ownership_stdev: list[float] | None = None
    human: dict[str, list[float]] = field(default_factory=dict)   # profile -> policy array
    stopped_early: bool = False
    seconds: float = 0.0
    raw_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "to_move": self.to_move, "visits": self.visits, "winrate": self.winrate, "score_lead": self.score_lead,
            "score_stdev": self.score_stdev,
            "candidates": [{"move": c.move, "order": c.order, "visits": c.visits, "prior": c.prior, "winrate": c.winrate,
                            "score_lead": c.score_lead, "score_stdev": c.score_stdev, "lcb": c.lcb, "utility": c.utility,
                            "pv": list(c.pv)} for c in self.candidates],
            "policy": self.policy, "ownership": self.ownership, "ownership_stdev": self.ownership_stdev,
            "human": self.human, "stopped_early": self.stopped_early, "seconds": self.seconds, "raw_id": self.raw_id,
        }

    @staticmethod
    def from_dict(d: dict) -> "Analysis":
        cands = [Candidate(**c) for c in d.get("candidates", [])]
        d2 = dict(d)
        d2["candidates"] = cands
        return Analysis(**d2)


@dataclass
class PositionSpec:
    """What the engine needs to reproduce a position."""
    size: int
    rules: str
    komi: float
    setup_black: list[int]
    setup_white: list[int]
    moves: list[tuple[int, int | None]]
    first_to_move: int

    @property
    def to_move(self) -> int:
        if self.moves:
            return opponent(self.moves[-1][0])
        return self.first_to_move

    def board(self) -> Board:
        b = Board.from_setup(self.size, self.first_to_move, self.setup_black, self.setup_white)
        for color, idx in self.moves:
            b = b.play(color, idx, self.rules)
        return b


class KataGoEngine:
    """Thin, thread-safe wrapper around `katago analysis`."""

    def __init__(self, binary: str, analysis_config: str, model: str, human_model: str | None = None,
                 perspective: str = "BLACK", human_profile_key: str = "humanSLProfile",
                 startup_timeout: float = 120.0, query_timeout: float = 600.0, report_every: float = 1.0,
                 cwd: str | None = None, search_threads: int | None = None):
        self.binary = binary
        self.search_threads = search_threads    # per machine; overrides analysis.cfg via -override-config
        self.cwd = cwd
        self.echo_stderr = False        # CLI: mirror KataGo's own startup log to our stderr until ready
        self.ready = False
        self.starting = False
        self.start_error: str | None = None
        self.analysis_config = analysis_config
        self.model = model
        self.human_model = human_model
        self.perspective = perspective.upper()
        self.human_profile_key = human_profile_key
        self.startup_timeout = startup_timeout
        self.query_timeout = query_timeout
        self.report_every = report_every
        self._proc: subprocess.Popen | None = None
        self._queues: dict[str, Queue] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_tail: list[str] = []
        self.version: str = "unknown"
        self.backend: str = "unknown"

    # ------------------------------------------------------------- lifecycle
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self.running:
            return
        cmd = self.command()
        self.ready = False
        self.starting = True
        self.start_error = None
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True, bufsize=1, cwd=self.cwd or None)
        except FileNotFoundError:
            self.starting = False
            self.start_error = f"katago binary not found: {self.binary}"
            raise EngineError("engine_unavailable", self.start_error, False,
                              "install KataGo and set [katago].binary in the config")
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        # a trivial query proves the engine is up
        try:
            self.query({"id": "startup", "moves": [], "rules": "japanese", "komi": 6.5,
                        "boardXSize": 19, "boardYSize": 19, "maxVisits": 1}, timeout=self.startup_timeout)
        except EngineError as e:
            self.starting = False
            self.start_error = f"katago failed to start: {e}; stderr: " + " | ".join(self._stderr_tail[-5:])
            raise EngineError("engine_unavailable", self.start_error, False)
        self.starting = False
        self.ready = True

    def command(self) -> list[str]:
        cmd = [self.binary, "analysis", "-config", self.analysis_config, "-model", self.model]
        if self.human_model:
            cmd += ["-human-model", self.human_model]
        if self.search_threads:
            cmd += ["-override-config", f"numSearchThreadsPerAnalysisThread={int(self.search_threads)}"]
        return cmd

    def restart(self) -> None:
        """Stop and start again: drops KataGo's NN cache (its memory) and any stale state."""
        self.stop()
        self.start()

    def memory_mb(self) -> float | None:
        """KataGo's resident memory in MB, or None when unknown (not running, or no `ps`, e.g. on Windows)."""
        if not self.running:
            return None
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(self._proc.pid)],
                                 capture_output=True, text=True, timeout=5).stdout
            return int(out.strip()) / 1024      # ps reports KB
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    def stop(self) -> None:
        self.ready = False
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()
        self._proc = None

    def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            qid = msg.get("id")
            with self._lock:
                q = self._queues.get(qid)
            if q is not None:
                q.put(msg)
        # process ended: wake all waiters
        with self._lock:
            for q in self._queues.values():
                q.put({"error": "engine exited", "id": None})

    def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            line = line.rstrip()
            self._stderr_tail.append(line)
            if self.echo_stderr and not self.ready and line:
                print("  katago | " + line, file=sys.stderr, flush=True)
            if len(self._stderr_tail) > 200:
                del self._stderr_tail[:100]
            low = line.lower()
            if "katago v" in low and self.version == "unknown":
                self.version = line.split("v", 1)[-1].split()[0]
            if self.backend == "unknown":
                for b in ("metal", "opencl", "cuda", "tensorrt", "eigen"):
                    if f"{b} backend" in low or f"using {b}" in low or f"backend: {b}" in low or f"{b} device" in low:
                        self.backend = b
                        break

    # ------------------------------------------------------------- raw queries
    def _send(self, obj: dict) -> None:
        if not self.running:
            if self.starting:
                raise EngineError("engine_unavailable", "katago is still starting", True, "try again in a few seconds")
            msg = self.start_error or "katago is not running"
            raise EngineError("engine_unavailable", msg, True,
                              "check [katago] paths in the config and the server's stderr log; restart the server")
        line = json.dumps(obj)
        with self._lock:
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()

    def query(self, q: dict, timeout: float | None = None,
              on_report: Callable[[dict], bool] | None = None) -> dict:
        """Send a query; return the final response.  on_report(partial) may return True to terminate early."""
        qid = q.get("id") or f"q{uuid.uuid4().hex[:10]}"
        q["id"] = qid
        queue: Queue = Queue()
        with self._lock:
            self._queues[qid] = queue
        try:
            self._send(q)
            deadline = time.time() + (timeout or self.query_timeout)
            final = None
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.terminate(qid)
                    raise EngineError("timeout", f"katago query {qid} timed out")
                try:
                    msg = queue.get(timeout=min(remaining, 5.0))
                except Empty:
                    continue
                if "error" in msg:
                    raise EngineError("internal", f"katago error: {msg['error']}", True)
                if msg.get("isDuringSearch"):
                    if on_report and on_report(msg):
                        self.terminate(qid)
                        # the final response still arrives after termination
                        continue
                    continue
                final = msg
                break
            return final
        finally:
            with self._lock:
                self._queues.pop(qid, None)

    def terminate(self, qid: str) -> None:
        try:
            self._send({"id": f"t{uuid.uuid4().hex[:8]}", "action": "terminate", "terminateId": qid})
        except EngineError:
            pass

    # ------------------------------------------------------------- high level
    def _base_query(self, spec: PositionSpec) -> dict:
        q = {
            "rules": spec.rules, "komi": spec.komi,
            "boardXSize": spec.size, "boardYSize": spec.size,
            "initialStones": [["B", idx_to_gtp(i, spec.size)] for i in spec.setup_black]
            + [["W", idx_to_gtp(i, spec.size)] for i in spec.setup_white],
            "moves": [[COLOR_CHAR[c], idx_to_gtp(i, spec.size)] for c, i in spec.moves],
            "initialPlayer": COLOR_CHAR[spec.first_to_move],
        }
        return q

    def analyze(self, spec: PositionSpec, max_visits: int, include_ownership: bool = True,
                include_ownership_stdev: bool = False, include_policy: bool = True, pv_len: int | None = None,
                wide_root_noise: float | None = None, allow_moves: list[dict] | None = None,
                avoid_moves: list[dict] | None = None, priority: int = 0, stop_when_stable: bool = False,
                stable_delta: float = 0.5, human_profiles: list[str] | None = None,
                max_seconds: float | None = None) -> Analysis:
        q = self._base_query(spec)
        q.update({
            "maxVisits": int(max_visits), "includeOwnership": include_ownership,
            "includeOwnershipStdev": include_ownership_stdev, "includePolicy": include_policy,
            "includePVVisits": False, "priority": priority,
        })
        override = {}
        if wide_root_noise:
            override["wideRootNoise"] = wide_root_noise
        # analysisPVLen comes from analysis.cfg (15); PVs are truncated client-side to pv_len below
        if override:
            q["overrideSettings"] = override
        if allow_moves:
            q["allowMoves"] = allow_moves
        if avoid_moves:
            q["avoidMoves"] = avoid_moves
        reports: list[tuple[str, float]] = []
        t0 = time.time()
        if (stop_when_stable and max_visits >= 400) or max_seconds:
            q["reportDuringSearchEvery"] = self.report_every

            def on_report(msg: dict) -> bool:
                if max_seconds and time.time() - t0 >= max_seconds:
                    return True
                if not stop_when_stable:
                    return False
                infos = msg.get("moveInfos") or []
                if not infos:
                    return False
                top = infos[0].get("move")
                sl = msg.get("rootInfo", {}).get("scoreLead", 0.0)
                reports.append((top, sl))
                if len(reports) >= 3:
                    last = reports[-3:]
                    if all(r[0] == last[0][0] for r in last) and max(r[1] for r in last) - min(r[1] for r in last) < stable_delta:
                        return True
                return False
        else:
            on_report = None
        resp = self.query(q, on_report=on_report)
        a = self._normalize(resp, spec)
        if pv_len:
            for c in a.candidates:
                c.pv = c.pv[:pv_len]
        a.seconds = time.time() - t0
        a.stopped_early = bool(on_report) and a.visits < max_visits
        for prof in human_profiles or []:
            a.human[prof] = self.human_policy(spec, prof, priority=priority)
        return a

    def human_policy(self, spec: PositionSpec, profile: str, priority: int = 0) -> list[float]:
        if not self.human_model:
            raise EngineError("human_model_unavailable", "no human model configured", False,
                              "set [katago].human_model in the config")
        q = self._base_query(spec)
        q.update({"maxVisits": 1, "includePolicy": True, "includeOwnership": False, "priority": priority,
                  "overrideSettings": {self.human_profile_key: profile}})
        resp = self.query(q, timeout=60)
        hp = resp.get("humanPolicy")
        if hp is None:
            raise EngineError("human_model_unavailable", "katago returned no humanPolicy; is -human-model loaded?", False)
        return [float(x) for x in hp]

    def _flip_needed(self, resp: dict, spec: PositionSpec) -> bool:
        """True when values are side-to-move perspective and White is to move."""
        if self.perspective == "BLACK":
            return False
        if self.perspective == "WHITE":
            return True
        cur = resp.get("rootInfo", {}).get("currentPlayer", COLOR_CHAR[spec.to_move])
        return cur == "W"

    def _normalize(self, resp: dict, spec: PositionSpec) -> Analysis:
        flip = self._flip_needed(resp, spec)
        s = -1.0 if flip else 1.0
        root = resp.get("rootInfo", {})
        wr = root.get("winrate", 0.5)
        wr = 1 - wr if flip else wr
        cands = []
        for mi in resp.get("moveInfos", []):
            cwr = mi.get("winrate", 0.5)
            clcb = mi.get("lcb", cwr)
            cands.append(Candidate(
                move=gtp_to_idx(mi["move"], spec.size), order=int(mi.get("order", len(cands))),
                visits=int(mi.get("visits", 0)), prior=float(mi.get("prior", 0.0)),
                winrate=(1 - cwr) if flip else cwr, score_lead=s * float(mi.get("scoreLead", 0.0)),
                score_stdev=float(mi.get("scoreStdev", 0.0)), lcb=(1 - clcb) if flip else clcb,
                utility=s * float(mi.get("utility", 0.0)),
                pv=[gtp_to_idx(p, spec.size) for p in mi.get("pv", [])],
            ))
        cands.sort(key=lambda c: c.order)
        own = resp.get("ownership")
        if own is not None and flip:
            own = [-x for x in own]
        return Analysis(
            to_move=spec.to_move, visits=int(root.get("visits", 0)), winrate=float(wr),
            score_lead=s * float(root.get("scoreLead", 0.0)), score_stdev=float(root.get("scoreStdev", 0.0)),
            candidates=cands, policy=resp.get("policy"), ownership=own,
            ownership_stdev=resp.get("ownershipStdev"), raw_id=resp.get("id"),
        )

    def info(self) -> dict:
        return {"katago_version": self.version, "backend": self.backend, "running": self.running, "ready": self.ready,
                "start_error": self.start_error,
                "human_model": {"name": Path(self.human_model).name if self.human_model else "", "loaded": bool(self.human_model)}}


# ====================================================================== mock
class MockEngine:
    """Deterministic stand-in for tests and dry runs: influence-based ownership, plausible candidates.

    Not a Go engine.  Its only job is to return well-formed, self-consistent data quickly.
    """

    def __init__(self, human_model: bool = True, seconds_per_kvisit: float = 0.0):
        self.human_model = human_model
        self.seconds_per_kvisit = seconds_per_kvisit
        self.version = "mock"
        self.backend = "mock"

    @property
    def running(self) -> bool:
        return True

    fake_memory_mb: float | None = None     # tests set this to exercise the restart guard
    restarts = 0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def restart(self) -> None:
        self.restarts += 1
        self.fake_memory_mb = None

    def memory_mb(self) -> float | None:
        return self.fake_memory_mb

    def info(self) -> dict:
        return {"katago_version": "mock", "backend": "mock", "running": True, "ready": True, "start_error": None,
                "human_model": {"name": "mock-human", "loaded": self.human_model}}

    # -- heuristics
    @staticmethod
    def _influence(board: Board) -> list[float]:
        import numpy as np
        n = board.size
        grid = np.zeros((n, n), dtype=float)
        for i, c in enumerate(board.cells):
            if c == BLACK:
                grid[i // n, i % n] = 1.0
            elif c == WHITE:
                grid[i // n, i % n] = -1.0
        acc = np.zeros_like(grid)
        for dr in range(-3, 4):
            for dc in range(-3, 4):
                w = 1.0 / (1.0 + abs(dr) + abs(dc))
                src = grid[max(0, -dr):n - max(0, dr), max(0, -dc):n - max(0, dc)]
                acc[max(0, dr):n - max(0, -dr), max(0, dc):n - max(0, -dc)] += w * src
        own = np.tanh(acc * 1.2)
        own[grid == 1.0] = 0.9
        own[grid == -1.0] = -0.9
        return [float(x) for x in own.flatten()]

    def _score(self, board: Board, komi: float) -> float:
        own = self._influence(board)
        return sum(own) * 0.5 - komi

    def _noise(self, key: str, scale: float) -> float:
        h = int(hashlib.sha1(key.encode()).hexdigest()[:8], 16)
        return ((h % 10000) / 10000.0 - 0.5) * 2 * scale

    def analyze(self, spec: PositionSpec, max_visits: int, include_ownership: bool = True,
                include_ownership_stdev: bool = False, include_policy: bool = True, pv_len: int | None = None,
                wide_root_noise: float | None = None, allow_moves: list[dict] | None = None,
                avoid_moves: list[dict] | None = None, priority: int = 0, stop_when_stable: bool = False,
                stable_delta: float = 0.5, human_profiles: list[str] | None = None,
                max_seconds: float | None = None) -> Analysis:
        if max_seconds:
            max_visits = int(max(1, min(max_visits, max_seconds * 1000)))
        if self.seconds_per_kvisit:
            time.sleep(self.seconds_per_kvisit * max_visits / 1000.0)
        board = spec.board()
        to_move = spec.to_move
        n = spec.size
        base = self._score(board, spec.komi)
        key = f"{board.board_hash}:{to_move}"
        allowed: set[int] | None = None
        if allow_moves:
            for am in allow_moves:
                if am.get("player") == COLOR_CHAR[to_move]:
                    allowed = {gtp_to_idx(m, n) for m in am["moves"] if m.lower() != "pass"}
        avoided: set[int] = set()
        if avoid_moves:
            for av in avoid_moves:
                if av.get("player") == COLOR_CHAR[to_move]:
                    avoided |= {gtp_to_idx(m, n) for m in av["moves"] if m.lower() != "pass"}
        # candidate moves: empties near stones (or star points on an empty board), scored by heuristic delta
        stones = board.stones()
        cands_idx: list[int] = []
        empties = board.empties()
        if not stones:
            cands_idx = sorted(star_points(n))
        else:
            near = set()
            for s in stones:
                r, c = divmod(s, n)
                for rr in range(max(0, r - 2), min(n, r + 3)):
                    for cc in range(max(0, c - 2), min(n, c + 3)):
                        near.add(rr * n + cc)
            cands_idx = [i for i in empties if i in near]
            if len(cands_idx) < 8:
                cands_idx += [i for i in empties if i not in near][:8]
        if allowed is not None:
            cands_idx = [i for i in cands_idx if i in allowed] or list(allowed)
        cands_idx = [i for i in cands_idx if i not in avoided]
        scored = []
        sign = 1.0 if to_move == BLACK else -1.0
        for i in cands_idx[:40]:
            try:
                b2 = board.play(to_move, i, spec.rules)
            except IllegalMove:
                continue
            val = sign * (self._score(b2, spec.komi) - base) + self._noise(f"{key}:{i}", 1.5)
            scored.append((val, i))
        scored.sort(reverse=True)
        top = scored[:8]
        best_gain = top[0][0] if top else 0.0
        root_score = base + sign * best_gain * 0.5
        root_wr = 1 / (1 + math.exp(-root_score / 12.0))
        cands = []
        total_visits = max_visits
        for k, (val, i) in enumerate(top):
            sl = base + sign * val
            share = max(0.02, 0.5 ** k)
            visits = max(1, int(total_visits * share / sum(max(0.02, 0.5 ** j) for j in range(len(top)))))
            wr = 1 / (1 + math.exp(-sl / 12.0))
            prior = max(0.005, math.exp(-k * 0.7) / 2.5)
            cands.append(Candidate(move=i, order=k, visits=visits, prior=prior, winrate=wr, score_lead=sl,
                                   score_stdev=8.0 + abs(self._noise(f"{key}:sd:{i}", 4.0)), lcb=wr - 0.02,
                                   utility=(wr - 0.5) * 2, pv=[i]))
        policy = None
        if include_policy:
            policy = [-1.0] * (n * n + 1)
            tot = sum(c.prior for c in cands) or 1.0
            for e in empties:
                policy[e] = 0.0005
            for c in cands:
                policy[c.move] = c.prior / tot * 0.8
            policy[n * n] = 0.001
        own = self._influence(board) if include_ownership else None
        own_sd = None
        if include_ownership_stdev and own is not None:
            own_sd = [max(0.05, 0.5 - abs(x) * 0.45) for x in own]
        a = Analysis(to_move=to_move, visits=total_visits, winrate=root_wr, score_lead=root_score,
                     score_stdev=9.0, candidates=cands, policy=policy, ownership=own, ownership_stdev=own_sd)
        for prof in human_profiles or []:
            a.human[prof] = self.human_policy(spec, prof, a)
        return a

    def human_policy(self, spec: PositionSpec, profile: str, analysis: Analysis | None = None, priority: int = 0) -> list[float]:
        if not self.human_model:
            raise EngineError("human_model_unavailable", "mock human model disabled", False)
        if analysis is None:
            analysis = self.analyze(spec, 16, include_ownership=False, priority=priority)
        n = spec.size
        # weaker ranks -> flatter distribution
        m = re.match(r"rank_(\d+)([kd])", profile)
        strength = 0.0
        if m:
            v = int(m.group(1))
            strength = (-v) if m.group(2) == "k" else v
        temp = 1.6 - 0.05 * strength     # 7k -> 1.95, 3k -> 1.75, 1d -> 1.55
        pol = [-1.0] * (n * n + 1)
        board = spec.board()
        for e in board.empties():
            pol[e] = 0.0
        weights = [(c.move, math.exp((c.score_lead if analysis.to_move == BLACK else -c.score_lead) / (temp * 3.0)))
                   for c in analysis.candidates]
        tot = sum(w for _, w in weights) or 1.0
        for mv, w in weights:
            pol[mv] = w / tot * 0.9
        pol[n * n] = 0.001
        return pol
