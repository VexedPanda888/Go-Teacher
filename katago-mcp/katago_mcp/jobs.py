"""Survey jobs (tool contract §1.4–§1.6)."""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field

from .board import BLACK, WHITE, COLOR_CHAR, CHAR_COLOR, opponent
from .config import Config
from .engine import Analysis, PositionSpec, EngineError
from .metrics import GameAnalysis, move_rows, story
from .sgf import SgfGame, parse, rank_to_profile, rank_stronger_by
from .store import Store, timestamp

log = logging.getLogger("katago_mcp")


def resolve_profiles(cfg: Config, game: SgfGame | None, student_color: int | None) -> dict[str, str]:
    student_rank = cfg.student.rank
    opp_rank = None
    if game is not None and student_color is not None:
        sc, oc = COLOR_CHAR[student_color], COLOR_CHAR[opponent(student_color)]
        student_rank = game.players[sc].get("rank") or cfg.student.rank
        opp_rank = game.players[oc].get("rank")
    peer = rank_to_profile(student_rank) or "rank_7k"
    target = rank_to_profile(rank_stronger_by(student_rank, cfg.student.target_offset_stones)) or "rank_4k"
    horizon = cfg.student.horizon_rank if cfg.student.horizon_rank.startswith("rank_") else rank_to_profile(cfg.student.horizon_rank)
    return {"peer": peer, "target": target, "horizon": horizon or "rank_1d",
            "opponent": rank_to_profile(opp_rank) or peer}


def student_color_of(game: SgfGame, username: str) -> int | None:
    u = (username or "").strip().lower()
    if not u:
        return None
    if game.players["B"]["name"].strip().lower() == u:
        return BLACK
    if game.players["W"]["name"].strip().lower() == u:
        return WHITE
    return None


def game_id_of(game: SgfGame, text: str) -> str:
    if game.ogs_game_id:
        return f"ogs_{game.ogs_game_id}"
    return "sgf_" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def game_analysis(game_id: str, game: SgfGame, student_color: int | None, positions: list[Analysis], refs: list[str],
                  visits_per_move: int, profiles: dict[str, str]) -> GameAnalysis:
    return GameAnalysis(
        game_id=game_id, size=game.size, rules=game.rules, komi=game.komi, handicap=game.handicap,
        student_color=student_color, moves=list(game.moves), setup_black=list(game.setup_black),
        setup_white=list(game.setup_white), first_to_move=game.first_to_move, positions=positions, refs=refs,
        visits_per_move=visits_per_move, result=game.result(), profiles=profiles)


@dataclass
class Job:
    job_id: str
    game_id: str
    game: SgfGame
    sgf_text: str
    visits_per_move: int
    student_color: int | None
    profiles: dict[str, str]
    options: dict = field(default_factory=dict)
    state: str = "queued"
    positions_done: int = 0
    positions_total: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: dict | None = None
    reused: bool = False
    ga: GameAnalysis | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def status(self) -> dict:
        eta = None
        if self.state == "running" and self.positions_done:
            per = self.elapsed / self.positions_done
            eta = per * (self.positions_total - self.positions_done)
        return {"job_id": self.job_id, "game_id": self.game_id, "state": self.state,
                "positions_done": self.positions_done, "positions_total": self.positions_total,
                "progress": round(self.positions_done / self.positions_total, 3) if self.positions_total else 0.0,
                "current_move": self.positions_done, "elapsed_seconds": round(self.elapsed, 1),
                "eta_seconds": None if eta is None else round(eta), "partial_results_available": self.positions_done >= 2,
                "reused": self.reused, "error": self.error}


class JobManager:
    def __init__(self, cfg: Config, engine, store: Store):
        self.cfg = cfg
        self.engine = engine
        self.store = store
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.active: Job | None = None
        self.on_done = None          # called with the job when a survey is done (Tools: prefetch of key moments)

    # ---------------------------------------------------------------- start
    def start(self, sgf_text: str, visits_per_move: int, student_username: str | None = None,
              game_id: str | None = None, options: dict | None = None) -> Job:
        options = options or {}
        game = parse(sgf_text)
        if game.size != 19:
            raise EngineError("unsupported_board_size", f"board size {game.size} not supported in v1", False)
        student = student_color_of(game, student_username or self.cfg.student.username)
        gid = game_id or game_id_of(game, sgf_text)

        def new_job() -> Job:
            j = Job(job_id=f"job_{uuid.uuid4().hex[:8]}", game_id=gid, game=game, sgf_text=sgf_text,
                    visits_per_move=int(visits_per_move), student_color=student,
                    profiles=resolve_profiles(self.cfg, game, student), options=options)
            j.positions_total = len(game.moves) + 1
            return j

        # A finished survey on disk needs no engine: it is reused even while another survey runs (seeding
        # loads stored games while the next one is surveyed).
        (self.store.game_dir(gid) / "game.sgf").write_text(sgf_text, encoding="utf-8")
        if options.get("reuse_existing", True):
            existing = self.load_game(gid)
            if existing is not None and existing.visits_per_move >= visits_per_move and existing.M == len(game.moves):
                job = new_job()
                job.ga = existing
                job.reused = True
                job.positions_done = job.positions_total
                job.state = "done"
                job.finished_at = time.time()
                with self._lock:
                    self.jobs[job.job_id] = job
                self._done(job)
                return job
        with self._lock:
            if self.active is not None and self.active.state in ("queued", "running"):
                raise EngineError("engine_busy", f"job {self.active.job_id} is running",
                                  True, f"call job_status with job_id {self.active.job_id}, or cancel it")
            job = new_job()
            self.jobs[job.job_id] = job
            self.active = job
        t = threading.Thread(target=self._run, args=(job,), daemon=True)
        t.start()
        return job

    def spec_at(self, game: SgfGame, k: int) -> PositionSpec:
        """The position after move k of a game (a fresh spec the caller may extend)."""
        return PositionSpec(game.size, game.rules, game.komi, list(game.setup_black), list(game.setup_white),
                            list(game.moves[:k]), game.first_to_move)

    def _run(self, job: Job) -> None:
        job.state = "running"
        game = job.game
        profiles = list(dict.fromkeys(job.profiles.values()))
        analyses: list[Analysis] = []
        refs: list[str] = []
        ga = game_analysis(job.game_id, game, job.student_color, analyses, refs, job.visits_per_move, dict(job.profiles))
        job.ga = ga
        try:
            for k in range(job.positions_total):
                if job._cancel.is_set():
                    job.state = "cancelled"
                    break
                spec = self.spec_at(game, k)
                rec = self.store.put_position(spec, job.game_id, k, persist=False)
                a = self.engine.analyze(spec, job.visits_per_move, include_ownership=True, include_ownership_stdev=True,
                                        include_policy=True, human_profiles=profiles, priority=0)
                self.store.put_cached(rec.ref, a, ownership=True)
                analyses.append(a)
                refs.append(rec.ref)
                job.positions_done = k + 1
                if (k + 1) % 20 == 0:
                    self._persist(job, complete=False)
            if job.state != "cancelled":
                job.finished_at = time.time()
                self._persist(job, complete=True)      # persist before announcing "done" (no partial-file race)
                job.state = "done"
                self._done(job)
            else:
                job.finished_at = time.time()
                self._persist(job, complete=False)
        except Exception as e:  # noqa: BLE001
            job.state = "failed"
            job.finished_at = time.time()
            job.error = {"code": getattr(e, "code", "internal"), "message": str(e), "trace": traceback.format_exc()[-800:]}
            self._persist(job, complete=False)
        finally:
            with self._lock:
                if self.active is job:
                    self.active = None

    def _done(self, job: Job) -> None:
        if self.on_done is None:
            return
        try:
            self.on_done(job)
        except Exception:  # noqa: BLE001
            log.exception("on_done hook failed for %s", job.job_id)

    def _persist(self, job: Job, complete: bool) -> None:
        ga = job.ga
        obj = {
            "job_id": job.job_id, "game_id": job.game_id, "complete": complete, "visits_per_move": job.visits_per_move,
            "student_color": None if ga.student_color is None else COLOR_CHAR[ga.student_color],
            "profiles": ga.profiles, "positions": [a.to_dict() for a in ga.positions], "refs": list(ga.refs),
            "written_at": timestamp(),
        }
        self.store.write_json(job.game_id, "analysis.json", obj)

    def load_game(self, game_id: str) -> GameAnalysis | None:
        obj = self.store.read_json(game_id, "analysis.json")
        if not obj or not obj.get("complete"):
            return None
        sgf_path = self.store.game_dir(game_id) / "game.sgf"
        if not sgf_path.exists():
            return None
        game = parse(sgf_path.read_text(encoding="utf-8"))
        sc = obj.get("student_color")
        ga = game_analysis(game_id, game, CHAR_COLOR[sc] if sc else None, [Analysis.from_dict(d) for d in obj["positions"]],
                           list(obj["refs"]), obj["visits_per_move"], obj.get("profiles", {}))
        # rebuild the position store and cache so refs resolve
        for k, a in enumerate(ga.positions):
            spec = self.spec_at(game, k)
            rec = self.store.put_position(spec, game_id, k, persist=False)
            self.store.put_cached(rec.ref, a, ownership=a.ownership is not None)
        return ga

    def release(self, job_id: str) -> dict:
        """Free a finished job's memory (analysis.json on disk remains; a new start_game_analysis reuses it)."""
        job = self.get(job_id)
        if job.state in ("queued", "running"):
            raise EngineError("engine_busy", f"job {job_id} is still running", True, "cancel it first")
        n = self.store.forget_game(job.game_id)
        job.ga = None
        self.jobs.pop(job_id, None)
        return {"job_id": job_id, "game_id": job.game_id, "analyses_dropped": n}

    # ---------------------------------------------------------------- access
    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise EngineError("job_not_found", f"unknown job {job_id}", True, "start_game_analysis first")
        return job

    def cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        job._cancel.set()
        return job.status()

    def story(self, job_id: str, max_moments: int = 6) -> dict:
        job = self.get(job_id)
        if job.ga is None or job.positions_done < 2:
            raise EngineError("job_not_finished", f"job {job_id} has no results yet", True,
                              "call job_status and wait")
        complete = job.state == "done"
        ga = job.ga
        if not complete:
            # analyze the prefix that exists
            ga = GameAnalysis(**{**ga.__dict__, "positions": ga.positions[:job.positions_done],
                                 "refs": ga.refs[:job.positions_done], "moves": ga.moves[:max(0, job.positions_done - 1)],
                                 "boards": []})
        d = story(ga, self.cfg.thresholds, max_moments=max_moments, complete=complete, job_id=job_id)
        d["state"] = job.state
        return d

    def rows(self, job_id: str, rng: tuple[int, int] | None = None) -> list[dict]:
        job = self.get(job_id)
        if job.ga is None:
            raise EngineError("job_not_finished", f"job {job_id} has no results yet", True)
        rows = move_rows(job.ga, self.cfg.thresholds)
        if rng:
            rows = [r for r in rows if rng[0] <= r["n"] <= rng[1]]
        return rows
