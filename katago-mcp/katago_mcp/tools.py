"""The tools of the contract, as plain Python (no MCP types here).

`Tools` owns the engine, the store and the job manager.  Every public method
takes JSON-like inputs and returns a JSON-like dict, or raises ToolError.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import urllib.error
import urllib.request
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import CONTRACT_VERSION, __version__
from .board import BLACK, WHITE, EMPTY, COLOR_CHAR, CHAR_COLOR, Board, IllegalMove, opponent
from .budget import (EXPECTATION_REFUTATION_PLIES, FORCED_REFUTATION_PLIES, FORCED_RESISTANCE_NODES, PUNISH_PLIES, BudgetError,
                     plan as plan_budget_fn, search_profiles, survey_visits)
from .config import Config
from .coords import CoordError, chebyshev, gtp_to_idx, idx_to_gtp, neighbors, parse_move
from .engine import Analysis, EngineError, KataGoEngine, PositionSpec
from .export import canonical, encode_ownership
from .jobs import Job, JobManager, game_id_of, resolve_profiles, student_color_of
from .metrics import (acceptable_set, best_candidate, candidate_for, capture_races, classify_local, decisive_and_last_chance,
                      group_changes, group_label, group_mean_ownership, group_records, group_status_label, groups_near, human_prob,
                      move_rows, near_empty, phases as phases_fn, prob_at, race_anchor_set, regional_attribution, reply_character,
                      sign_of, territory_by_region)
from .regions import LABELS, RegionError, STANDARD_CODES, expand, region_indices, standard_code, standard_partition
from .render import LEGEND, low_liberty_groups, render_board, render_overlay
from .sgf import OGS_GAME_RE, SgfError, parse, rank_to_profile
from .store import PositionRecord, Store, timestamp

log = logging.getLogger("katago_mcp")


class ToolError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None, recoverable: bool = True,
                 suggestion: str | None = None):
        super().__init__(message)
        self.code, self.message, self.details, self.recoverable, self.suggestion = code, message, details or {}, recoverable, suggestion

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details,
                          "recoverable": self.recoverable, "suggestion": self.suggestion}}


# The MCP tools, in registration order: server.py registers these `Tools` methods, docstring = description.
PUBLIC_TOOLS = ("engine_info", "plan_budget", "sgf_summary", "start_game_analysis", "job_status", "job_results",
                "get_position_ref", "analyze_position", "analyze_line", "pass_probe", "swing_value", "local_solve",
                "group_status", "ownership_diff", "human_move_distribution", "render_board", "terminal_features",
                "forced_line", "intent_probe", "expectation_probe", "validate_variations")

BELIEF_CATEGORIES = {
    "needs_defending": ["14", "2"], "group_is_safe": ["3", "2"], "is_sente": ["11", "15"],
    "behind_must_invade": ["9", "8"], "ahead_can_coast": ["9"], "sequence_works": ["5", "4", "3", "9"],
    "biggest_move": ["1", "15", "11"],
}


def _wrap_engine_error(e: EngineError) -> ToolError:
    return ToolError(e.code, str(e), recoverable=e.recoverable, suggestion=e.suggestion)


@contextmanager
def _engine_errors():
    """EngineError -> ToolError."""
    try:
        yield
    except EngineError as e:
        raise _wrap_engine_error(e)


def _gtp(pt, size: int = 19, where: str = "", details: dict | None = None) -> int | None:
    """A GTP point from a caller; a bad one is a bad_request (prefixed with `where`)."""
    try:
        return gtp_to_idx(pt, size)
    except CoordError as e:
        raise ToolError("bad_request", f"{where}: {e}" if where else str(e), details)


class _Spent:
    """Engine visits one tool call actually searched (cache hits are free); `_analyze(..., spent=)` adds to it."""
    __slots__ = ("visits",)

    def __init__(self):
        self.visits = 0


@dataclass
class _Validation:
    """State of one validate_variations call."""
    job: Job
    visits: int
    student: int
    evaluate: bool
    spent: _Spent = field(default_factory=_Spent)
    errors: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ownership: dict[str, str] = field(default_factory=dict)


def _r(x: float | None, nd: int = 2):
    return None if x is None else round(float(x), nd)


class Tools:
    def __init__(self, cfg: Config, engine=None, store: Store | None = None, start_engine: bool = False):
        self.cfg = cfg
        self.engine = engine or KataGoEngine(cfg.katago.binary, cfg.katago.analysis_config, cfg.katago.model,
                                            cfg.katago.human_model, cfg.katago.perspective,
                                            cfg.katago.human_profile_key, cfg.katago.startup_timeout,
                                            cfg.katago.query_timeout, cfg.katago.report_every, cwd=cfg.root_dir,
                                            search_threads=cfg.katago.search_threads)
        self.store = store or Store(cfg.reviews_dir)
        self.jobs = JobManager(cfg, self.engine, self.store)
        self.vps: float = cfg.vps
        self.plans: dict[str, dict] = {}          # job_id -> active plan
        self.last_plan: dict | None = None
        self.human_cache: dict[tuple[str, str], list[float]] = {}
        self.solve_results: dict[str, dict] = {}   # query_id -> local_solve result
        self._load_throughput_sidecar()
        self._start_thread: threading.Thread | None = None
        if start_engine:
            self.engine.start()

    # ---------------------------------------------------------------- engine lifecycle
    def start_engine_background(self) -> None:
        """Start KataGo in a daemon thread if it is not running, starting, or permanently failed."""
        eng = self.engine
        if eng.running or getattr(eng, "starting", False) or getattr(eng, "start_error", None):
            return
        if self._start_thread is not None and self._start_thread.is_alive():
            return

        def _run():
            try:
                eng.start()
            except EngineError:
                pass   # recorded in eng.start_error; tools report it

        self._start_thread = threading.Thread(target=_run, daemon=True, name="katago-start")
        self._start_thread.start()

    def _ensure_engine(self) -> None:
        """Called before any engine work: start if needed, wait a bounded time, else ask the caller to retry."""
        eng = self.engine
        if getattr(eng, "ready", True) and eng.running:
            return
        if getattr(eng, "start_error", None) and not eng.running:
            raise ToolError("engine_unavailable", eng.start_error, recoverable=False,
                            suggestion="fix [katago] paths in the config and restart the server")
        self.start_engine_background()
        deadline = time.time() + self.cfg.katago.first_call_wait_seconds
        while time.time() < deadline:
            if getattr(eng, "ready", False):
                return
            if getattr(eng, "start_error", None):
                raise ToolError("engine_unavailable", eng.start_error, recoverable=False,
                                suggestion="fix [katago] paths in the config and restart the server")
            time.sleep(0.5)
        raise ToolError("engine_unavailable", "katago is still starting (loading the network; the first OpenCL start "
                        "can take several minutes)", recoverable=True, suggestion="call the tool again in a minute")

    # ================================================================ infrastructure
    def _load_throughput_sidecar(self) -> None:
        p = self.cfg.throughput_path
        if p is not None and p.exists():
            try:
                d = json.loads(p.read_text())
                self.vps = float(d.get("visits_per_second_sustained") or self.vps)
                self.cfg.throughput.visits_per_second_sustained = self.vps
                self.cfg.throughput.visits_per_second_cold = float(d.get("visits_per_second_cold", 0.0))
                self.cfg.throughput.measured_at = d.get("measured_at", "")
            except (ValueError, OSError):
                pass

    def _save_throughput_sidecar(self) -> None:
        p = self.cfg.throughput_path
        if p is None:
            return
        p.write_text(json.dumps({"visits_per_second_cold": self.cfg.throughput.visits_per_second_cold,
                                 "visits_per_second_sustained": self.cfg.throughput.visits_per_second_sustained,
                                 "measured_at": self.cfg.throughput.measured_at}, indent=2))

    def _job_for_game(self, game_id: str | None) -> Job | None:
        if not game_id:
            return None
        for j in self.jobs.jobs.values():
            if j.game_id == game_id:
                return j
        return None

    def _log(self, game_id: str | None, tool: str, args: dict, visits: int = 0, seconds: float = 0.0,
             cached: bool = False, result: dict | None = None) -> str:
        qid = self.store.next_query_id(game_id)
        self.store.log_query(game_id, {"query_id": qid, "tool": tool, "args": _summarize(args), "visits": visits,
                                       "seconds": round(seconds, 2), "cached": cached, "result": result or {}})
        return qid

    # ---------------------------------------------------------------- SGF input
    def _resolve_sgf(self, sgf: str | None) -> tuple[str, str | None, dict]:
        """Accept SGF text, a path to an .sgf file on this machine, or an OGS game id / link.

        Returns (sgf_text, game_id_hint, source).  Pasting SGF through the chat is error-prone (the model
        retypes it into the tool call), so paths and OGS ids are the preferred forms.
        """
        s = (sgf or "").strip()
        if not s:
            raise ToolError("bad_request", "sgf is required: SGF text, a path to an .sgf file on this machine, "
                            "or an OGS game id or link")
        if s.startswith("(") or s.startswith("\ufeff("):
            return s.lstrip("\ufeff"), None, {"kind": "text", "chars": len(s)}
        m = OGS_GAME_RE.search(s) or re.fullmatch(r"(\d{3,12})", s)
        if m and not os.path.exists(s):
            return self._fetch_ogs(m.group(1))
        p = Path(s).expanduser()
        roots = [Path(self.cfg.games_dir), Path(self.cfg.root_dir or "."), Path.cwd(), Path.home()]
        candidates = [p] if p.is_absolute() else [r / p for r in roots]
        for c in candidates:
            if c.is_file():
                text = c.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
                mm = re.match(r"ogs_?(\d+)", c.stem)
                hint = f"ogs_{mm.group(1)}" if mm else None       # else the PC link, which parse() reads
                return text, hint, {"kind": "file", "path": str(c)}
        raise ToolError("bad_request", f"sgf is neither SGF text, an existing file, nor an OGS game id: {s[:80]!r}",
                        suggestion=f"put the .sgf file in {self.cfg.games_dir} and pass its file name, or pass the OGS game link")

    def _fetch_ogs(self, gid: str) -> tuple[str, str | None, dict]:
        games = Path(self.cfg.games_dir)
        games.mkdir(parents=True, exist_ok=True)
        cache = games / f"ogs_{gid}.sgf"
        if cache.is_file():
            return cache.read_text(encoding="utf-8", errors="replace"), f"ogs_{gid}", {"kind": "ogs", "cached": str(cache)}
        if not self.cfg.ogs.enabled:
            raise ToolError("bad_request", "OGS downloads are disabled in the config ([ogs].enabled = false)",
                            suggestion="download the SGF from online-go.com and pass its path")
        url = f"{self.cfg.ogs.base_url}/api/v1/games/{gid}/sgf"
        req = urllib.request.Request(url, headers={"User-Agent": f"katago-mcp/{__version__} (Go teacher review server)"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.ogs.timeout) as resp:
                data = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise ToolError("ogs_fetch_failed", f"online-go.com answered HTTP {e.code} for game {gid}",
                            suggestion="check the game id; private games must be downloaded by hand into games/")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ToolError("ogs_fetch_failed", f"could not reach online-go.com: {e}",
                            suggestion="check the network, or download the SGF and pass its path")
        if not data.lstrip("\ufeff").lstrip().startswith("("):
            raise ToolError("ogs_fetch_failed", f"online-go.com did not return an SGF for game {gid} (login required or wrong id)")
        cache.write_text(data, encoding="utf-8")
        return data.lstrip("\ufeff"), f"ogs_{gid}", {"kind": "ogs", "url": url, "saved_to": str(cache)}

    # ---------------------------------------------------------------- positions
    def _resolve_position(self, spec, persist: bool = True) -> PositionRecord:
        spec = norm_position(spec)
        then = norm_list(spec.get("then")) or []
        try:
            if "ref" in spec:
                rec = self.store.get_position(spec["ref"])
                if rec is None:
                    raise ToolError("unknown_ref", f"unknown position ref {spec['ref']}", suggestion="use get_position_ref")
                base = rec.spec
                game_id, mn = rec.game_id, rec.move_number
            elif "job_id" in spec:
                job = self.jobs.get(spec["job_id"])
                mn = int(spec.get("move_number", 0))
                if not 0 <= mn <= len(job.game.moves):
                    raise ToolError("bad_request", f"move_number must be 0..{len(job.game.moves)}")
                base = self.jobs.spec_at(job.game, mn)
                game_id = job.game_id
            elif "sgf" in spec:
                text, _hint, _src = self._resolve_sgf(spec["sgf"])
                game = parse(text)
                mn = int(spec.get("move_number", len(game.moves)))
                if not 0 <= mn <= len(game.moves):
                    raise ToolError("bad_request", f"move_number must be 0..{len(game.moves)}")
                base = self.jobs.spec_at(game, mn)
                game_id, mn = None, mn
            elif "moves" in spec or "setup" in spec:
                setup = spec.get("setup") or {}
                sb = [gtp_to_idx(p) for p in setup.get("B", [])]
                sw = [gtp_to_idx(p) for p in setup.get("W", [])]
                moves = [parse_move(m) for m in spec.get("moves", [])]
                to_move = CHAR_COLOR.get(spec.get("to_move", "B"), BLACK)
                first = moves[0][0] if moves else to_move
                if moves and "to_move" in spec and CHAR_COLOR[spec["to_move"]] != opponent(moves[-1][0]):
                    raise ToolError("bad_request", "to_move disagrees with the move list; add a pass to change the side to move")
                base = PositionSpec(19, spec.get("rules", "japanese"), float(spec.get("komi", 6.5)), sb, sw, moves, first)
                game_id, mn = None, None
            else:
                raise ToolError("bad_request", "position needs one of ref / job_id / sgf / moves")
        except EngineError as e:
            raise _wrap_engine_error(e)
        except (CoordError, SgfError) as e:
            raise ToolError("bad_request", str(e))
        if then:
            new = PositionSpec(base.size, base.rules, base.komi, list(base.setup_black), list(base.setup_white),
                               list(base.moves), base.first_to_move)
            board = new.board()
            for ply, m in enumerate(then, 1):
                try:
                    color, idx = parse_move(m, new.size)
                    board = board.play(color, idx, new.rules)
                except IllegalMove as e:
                    raise ToolError("illegal_move", f"then[{ply}] {e}", {"ply": ply, "move": m, "reason": e.reason})
                except CoordError as e:
                    raise ToolError("bad_request", f"then[{ply}]: {e}")
                new.moves.append((color, idx))
            base = new
            mn = None
        try:
            return self.store.put_position(base, game_id, mn, persist=persist)
        except IllegalMove as e:
            raise ToolError("illegal_move", str(e), {"reason": e.reason})

    def _perspective(self, requested: str | None, rec: PositionRecord) -> int:
        if requested in ("B", "W"):
            return CHAR_COLOR[requested]
        job = self._job_for_game(rec.game_id)
        if job is not None and job.student_color is not None:
            return job.student_color
        return BLACK

    @staticmethod
    def _pv_score(score_black: float, persp: int) -> float:
        return round(score_black * sign_of(persp), 2)

    @staticmethod
    def _pv_wr(wr_black: float, persp: int) -> float:
        return round(wr_black if persp == BLACK else 1 - wr_black, 4)

    # ---------------------------------------------------------------- budgets
    def _active_plan(self, job_id: str | None) -> dict | None:
        if job_id and job_id in self.plans:
            return self.plans[job_id]
        return self.last_plan

    def _visits_for(self, rec: PositionRecord, budget, default_profile: str) -> tuple[int, str | None]:
        """Visits for a search at rec under `budget`, and the job id of rec's game (for its plan)."""
        job = self._job_for_game(rec.game_id)
        jid = job.job_id if job else None
        return self._budget_visits(budget, jid, default_profile), jid

    def _budget_visits(self, budget, job_id: str | None = None, default_profile: str = "root") -> int:
        budget = norm_budget(budget) or {"profile": default_profile}
        if "visits" in budget:
            v = int(budget["visits"])
            if v < 1:
                raise ToolError("bad_request", "visits must be >= 1")
            return v
        if "seconds" in budget:
            if self.vps <= 0:
                raise ToolError("budget_infeasible", "throughput unknown; run engine_info with refresh_benchmark",
                                suggestion="katago-mcp benchmark --config <machine>.toml")
            return max(1, int(float(budget["seconds"]) * self.vps))
        prof = budget.get("profile", default_profile)
        quick = self.cfg.thresholds.quick_visits
        if prof == "quick":
            return quick
        plan = self._active_plan(job_id)
        u = self.cfg.budget.unit_base
        if plan:
            pr, mults = plan["profiles"], plan["verification"]["per_episode"]["stability_multipliers"]
        else:
            pr, mults = search_profiles(self._default_survey_visits(200), u, quick), u.stability
        if prof in ("survey", "root", "line_node", "local_solve"):
            return int(pr[prof])
        if prof == "stability":
            return int(pr["root"]) * int(budget.get("multiplier", mults[0] if mults else 4))
        raise ToolError("bad_request", f"unknown budget profile {prof!r}")

    def _default_survey_visits(self, move_count: int) -> int:
        return survey_visits(self.cfg.budget, self.vps, move_count) if self.vps > 0 else 300

    # ---------------------------------------------------------------- engine access
    def _analyze(self, rec: PositionRecord, visits: int, ownership: bool = True, ownership_stdev: bool = False,
                 policy: bool = True, wide_root_noise: float | None = None, allow: list[dict] | None = None,
                 avoid: list[dict] | None = None, pv_len: int | None = None, human_profiles: list[str] | None = None,
                 stop_when_stable: bool = False, priority: int = 10, spent: _Spent | None = None) -> tuple[Analysis, bool]:
        opts = dict(ownership=ownership, ownership_stdev=ownership_stdev, wide_root_noise=wide_root_noise or 0.0,
                    allow=allow, avoid=avoid)
        cached = self.store.get_cached(rec.ref, visits, **opts) if not allow and not avoid else None
        if cached is not None and (not ownership or cached.ownership is not None) \
                and (not ownership_stdev or cached.ownership_stdev is not None) and (not policy or cached.policy is not None):
            a = cached
            hit = True
        else:
            self._ensure_engine()
            with _engine_errors():
                a = self.engine.analyze(rec.spec, visits, include_ownership=ownership, include_ownership_stdev=ownership_stdev,
                                        include_policy=policy, pv_len=pv_len, wide_root_noise=wide_root_noise,
                                        allow_moves=allow, avoid_moves=avoid, priority=priority,
                                        stop_when_stable=stop_when_stable, stable_delta=self.cfg.thresholds.stable_stop_delta)
            if not allow and not avoid:
                self.store.put_cached(rec.ref, a, **opts)
            hit = False
            if spent is not None:
                spent.visits += a.visits
        for prof in human_profiles or []:
            if prof not in a.human:
                a.human[prof] = self._human_policy(rec, prof)
        return a, hit

    def _human_policy(self, rec: PositionRecord, profile: str) -> list[float]:
        key = (rec.ref, profile)
        if key in self.human_cache:
            return self.human_cache[key]
        self._ensure_engine()
        with _engine_errors():
            pol = self.engine.human_policy(rec.spec, profile)
        self.human_cache[key] = pol
        return pol

    def _ownership(self, rec: PositionRecord, budget: dict | None = None) -> tuple[list[float], float, int]:
        """Cached ownership if any, else a quick search.  Returns (ownership, score_black, visits)."""
        a = self.store.best_with_ownership(rec.ref)
        if a is not None:
            return a.ownership, a.score_lead, a.visits
        visits = self._budget_visits(budget, None, "quick")
        a, _ = self._analyze(rec, visits, ownership=True, policy=False)
        return a.ownership, a.score_lead, a.visits

    def _profiles(self, aliases: list[str] | None, rec: PositionRecord) -> dict[str, str]:
        job = self._job_for_game(rec.game_id)
        base = job.profiles if job else resolve_profiles(self.cfg, None, None)
        out = {}
        for a in aliases or ["peer", "target", "horizon"]:
            if a in base:
                out[a] = base[a]
            elif a.startswith("rank_"):
                out[a] = a
            else:
                prof = rank_to_profile(a)
                if not prof:
                    raise ToolError("bad_request", f"unknown human profile {a!r}")
                out[a] = prof
        return out

    def _candidate_dicts(self, a: Analysis, persp: int, profiles: dict[str, str], size: int, max_n: int, acc: list) -> list[dict]:
        out = []
        for c in a.candidates[:max_n]:
            hp = human_prob(a, profiles, c.move, size)
            out.append({"move": idx_to_gtp(c.move, size), "order": c.order, "visits": c.visits, "prior": round(c.prior, 4),
                        "winrate": self._pv_wr(c.winrate, persp), "score_lead": self._pv_score(c.score_lead, persp),
                        "score_stdev": round(c.score_stdev, 2), "lcb": self._pv_wr(c.lcb, persp),
                        "pv": [idx_to_gtp(p, size) for p in c.pv], "human": hp, "in_acceptable_set": c.move in acc})
        return out

    def _restart_if_heavy(self) -> None:
        """Restart KataGo before a survey if its memory is above [katago].restart_above_mb (0 disables).

        KataGo's NN cache is a fixed-size table (nnCacheSizePowerOfTwo in analysis.cfg), so its memory levels
        off and this normally never fires: it guards against an oversized cache setting or a leak. A restart
        costs a model reload (~30 s on Metal), so it only happens between jobs, never on a query count.
        """
        limit = self.cfg.katago.restart_above_mb
        if not limit or self.jobs.active is not None or not hasattr(self.engine, "memory_mb"):
            return
        mb = self.engine.memory_mb()
        if mb is None or mb <= limit:
            return
        log.warning("katago uses %.0f MB (limit %d); restarting it before the survey", mb, limit)
        with _engine_errors():
            self.engine.restart()

    def restart_engine(self) -> dict:
        if hasattr(self.engine, "restart"):
            with _engine_errors():
                self.engine.restart()
        return self.engine.info()

    # ================================================================ 1.1 engine_info
    def engine_info(self, refresh_benchmark: bool = False) -> dict:
        """Machine, KataGo/network versions, human model, throughput (visits/s), active job and plan, student profiles."""
        if refresh_benchmark:
            self.benchmark()
        else:
            self.start_engine_background()      # Phase 0 calls this first: get the model loading now
        info = self.engine.info()
        job = self.jobs.active
        prof = resolve_profiles(self.cfg, None, None)
        return {
            "machine": self.cfg.machine, "server_version": __version__, "contract_version": CONTRACT_VERSION,
            "katago_version": info.get("katago_version"), "backend": info.get("backend"),
            "network": {"name": Path(self.cfg.katago.model).name},
            "human_model": info.get("human_model"),
            "throughput": {"visits_per_second_cold": self.cfg.throughput.visits_per_second_cold,
                           "visits_per_second_sustained": self.vps, "measured_at": self.cfg.throughput.measured_at,
                           "method": "timed_query" if self.cfg.throughput.measured_at else "unmeasured"},
            "engine_status": "ready" if info.get("ready") else ("failed: " + info["start_error"]) if info.get("start_error")
            else ("starting (the first tool that needs the engine waits for it)" if info.get("running") or getattr(self.engine, "starting", False) else "not started"),
            "active_job": None if job is None or job.state not in ("queued", "running") else
            {"job_id": job.job_id, "progress": job.status()["progress"]},
            "active_plan": None if not self.last_plan else {"total_minutes": self.last_plan["total_minutes"],
                                                            "episodes": self.last_plan["verification"]["episodes"]},
            "student": {"username": self.cfg.student.username, "rank": self.cfg.student.rank, **prof},
            "thresholds": self.cfg.thresholds.__dict__,
        }

    def benchmark(self, seconds: float = 20.0) -> dict:
        """Timed query on a middlegame position; updates sustained visits/s."""
        moves = ["BQ16", "WD4", "BQ4", "WD16", "BR14", "WC14", "BF3", "WC6", "BO3", "WK17", "BF17", "WK3"]
        rec = self._resolve_position({"moves": moves, "rules": "japanese", "komi": 6.5}, persist=False)
        self._ensure_engine()
        with _engine_errors():
            a = self.engine.analyze(rec.spec, 10_000_000, include_ownership=False, include_policy=False,
                                    max_seconds=seconds, priority=20)
        secs = max(a.seconds, 1e-3)
        vps = a.visits / secs
        self.vps = vps
        self.cfg.throughput.visits_per_second_sustained = round(vps, 1)
        if not self.cfg.throughput.visits_per_second_cold:
            self.cfg.throughput.visits_per_second_cold = round(vps, 1)
        self.cfg.throughput.measured_at = timestamp()
        self._save_throughput_sidecar()
        return {"visits": a.visits, "seconds": round(secs, 2), "visits_per_second": round(vps, 1)}

    # ================================================================ 1.2 plan_budget
    def plan_budget(self, total_minutes: float | str, move_count: int | None = None, job_id: str | None = None,
                    self_review_minutes: float | None = None, episodes_requested: int | None = None,
                    expected_ld_episodes: int | None = None, selected: list | None = None,
                    interview_minutes: float | None = None) -> dict:
        """Turn the review time budget (minutes, or 'unlimited') into survey visits, episode count and per-episode search sizes (blind self-review and episode interviews reserved). Re-plan with job_id + selected after triage."""
        if isinstance(total_minutes, str):
            if total_minutes.strip().lower() not in ("unlimited", "unbounded", "as long as it needs"):
                try:
                    total_minutes = float(total_minutes)
                except ValueError:
                    raise ToolError("bad_request", "total_minutes must be a number or 'unlimited'")
            else:
                total_minutes = "unlimited"
        elapsed = 0.0
        existing_survey = None
        if job_id:
            job = self.jobs.get(job_id)
            move_count = len(job.game.moves)
            elapsed = job.elapsed / 60.0
            existing_survey = job.visits_per_move
        if not move_count:
            raise ToolError("bad_request", "move_count is required without job_id")
        try:
            p = plan_budget_fn(self.cfg.budget, self.vps, int(move_count), total_minutes,
                               self_review_minutes=self_review_minutes, episodes_requested=episodes_requested,
                               interview_minutes=interview_minutes, expected_ld_episodes=expected_ld_episodes, selected=selected, elapsed_minutes=elapsed,
                               survey_visits_existing=existing_survey, quick_visits=self.cfg.thresholds.quick_visits)
        except BudgetError as e:
            raise ToolError("budget_infeasible", str(e), suggestion="run engine_info with refresh_benchmark=true")
        p["move_count"] = int(move_count)
        p["machine"] = self.cfg.machine
        if job_id:
            self.plans[job_id] = p
            self.jobs.get(job_id).plan = p
        self.last_plan = p
        p["query_id"] = self._log(job.game_id if job_id else None, "plan_budget",
                                  {"total_minutes": total_minutes, "move_count": move_count, "job_id": job_id},
                                  result={"episodes": p["verification"]["episodes"], "feasible": p["feasible"]})
        return p

    # ================================================================ 1.3 sgf_summary
    def sgf_summary(self, sgf: str, student_username: str | None = None, boards_at: list | None = None,
                    ascii_options: dict | None = None) -> dict:
        """Parse a game without engine use: players, rules, komi, handicap, result, move count, captures, tension events, ASCII boards, position refs. `sgf` is an OGS game link or id (fetched from online-go.com), the path/name of an .sgf file on this machine (games/ folder), or raw SGF text; prefer the link or the path over pasting text."""
        sgf, gid_hint, source = self._resolve_sgf(sgf)
        try:
            game = parse(sgf)
        except SgfError as e:
            raise ToolError("invalid_sgf", str(e))
        if game.size != 19:
            raise ToolError("unsupported_board_size", f"board size {game.size} is not supported in v1", recoverable=False)
        username = student_username or self.cfg.student.username
        sc = student_color_of(game, username)
        warnings = list(game.warnings)
        if sc is None:
            warnings.append(f"student username {username!r} not found in PB/PW; confirm the color")
        gid = gid_hint or game_id_of(game, sgf)
        # replay for capture and tension events
        board = Board.from_setup(19, game.first_to_move, game.setup_black, game.setup_white)
        boards = [board]
        capture_events, tension_events = [], []
        reported: set[int] = set()
        for n, (color, idx) in enumerate(game.moves, 1):
            prev = board
            try:
                board = board.play(color, idx, game.rules)
            except IllegalMove as e:
                raise ToolError("invalid_sgf", f"move {n} ({COLOR_CHAR[color]} {idx_to_gtp(idx)}) is illegal: {e.reason}")
            boards.append(board)
            gained = board.captures[color] - prev.captures[color]
            if gained >= 2:
                pts = [i for i in range(361) if prev.cells[i] != EMPTY and board.cells[i] == EMPTY]
                capture_events.append({"move": n, "by": COLOR_CHAR[color], "stones": gained, "points": [idx_to_gtp(p) for p in pts]})
            for g in board.groups():
                if g.size >= 3 and len(g.liberties) <= 2 and g.anchor not in reported:
                    reported.add(g.anchor)
                    tension_events.append({"move": n, "color": COLOR_CHAR[g.color], "group_point": idx_to_gtp(g.anchor),
                                           "group_size": g.size})
        M = len(game.moves)
        wanted = boards_at if boards_at is not None else [50, 100, 150, "end"]
        board_out, refs = [], []
        for w in wanted:
            k = M if w == "end" else int(w)
            if 0 <= k <= M and not any(b["after_move"] == k for b in board_out):
                last = (game.moves[k - 1][0], game.moves[k - 1][1]) if k > 0 else None
                board_out.append({"after_move": k, "ascii": render_board(boards[k], last, **(ascii_options or {}))})
                rec = self.store.put_position(self.jobs.spec_at(game, k), gid, k)
                refs.append({"after_move": k, "ref": rec.ref})
        opp_color = None if sc is None else opponent(sc)
        res = game.result()
        last_mv = game.moves[-1] if game.moves else None
        ogs_id = game.ogs_game_id or (gid[4:] if gid.startswith("ogs_") else None)
        out = {
            "game_id": gid,
            "input": source,
            "source": {"ogs_game_id": ogs_id, "url": f"https://online-go.com/game/{ogs_id}" if ogs_id else None,
                       "date": game.date, "event": game.game_name},
            "board_size": 19,
            "players": game.players,
            "student": None if sc is None else {"color": COLOR_CHAR[sc], "rank": game.players[COLOR_CHAR[sc]].get("rank"), "matched_by": "PB" if sc == BLACK else "PW"},
            "opponent": None if opp_color is None else {"color": COLOR_CHAR[opp_color], "rank": game.players[COLOR_CHAR[opp_color]].get("rank"),
                                                        "human_profile": rank_to_profile(game.players[COLOR_CHAR[opp_color]].get("rank"))},
            "rules": {"sgf_ru": game.rules_raw, "katago_rules": game.rules, "komi": game.komi, "handicap": game.handicap,
                      "setup": {"B": [idx_to_gtp(i) for i in game.setup_black], "W": [idx_to_gtp(i) for i in game.setup_white]},
                      "warnings": [w for w in warnings if "komi" in w.lower() or "HA" in w or "RU" in w]},
            "result": res,
            "moves": {"count": M, "passes": [n for n, (c, i) in enumerate(game.moves, 1) if i is None],
                      "last_move": None if last_mv is None else {"number": M, "color": COLOR_CHAR[last_mv[0]], "point": idx_to_gtp(last_mv[1])},
                      "resign_after_move": M if res["method"] == "resign" else None},
            "time_settings": {"main": game.time_main, "overtime": game.overtime, "per_move_times_present": game.per_move_times},
            "phases_rough": {"opening_end": min(50, M), "middlegame_end": int(M * 0.75)},
            "capture_events": capture_events, "tension_events": tension_events[:12],
            "boards": board_out, "position_refs": refs, "warnings": warnings,
            "profiles": resolve_profiles(self.cfg, game, sc),
        }
        out["query_id"] = self._log(gid, "sgf_summary", {"moves": M, "student": out["student"]})
        return out

    # ================================================================ 1.4 – 1.6 jobs
    def start_game_analysis(self, sgf: str, budget: dict | None = None, student_username: str | None = None,
                            game_id: str | None = None, options: dict | None = None) -> dict:
        """Start the asynchronous whole-game survey. `sgf`: OGS game link/id, .sgf file path, or SGF text (prefer link or path). budget: {visits_per_move} or {profile:'survey'} (from the active plan). Returns a job_id."""
        options = options or {}
        sgf, gid_hint, _source = self._resolve_sgf(sgf)
        game_id = game_id or gid_hint
        try:
            game = parse(sgf)
        except SgfError as e:
            raise ToolError("invalid_sgf", str(e))
        budget = norm_budget(budget) or {"profile": "survey"}
        if "visits_per_move" in budget:
            visits = int(budget["visits_per_move"])
        elif "visits" in budget or "seconds" in budget:
            visits = self._budget_visits(budget)
        else:
            plan = self.last_plan
            visits = int(plan["profiles"]["survey"]) if plan else self._default_survey_visits(len(game.moves) or 1)
        self._ensure_engine()
        self._restart_if_heavy()
        with _engine_errors():
            job = self.jobs.start(sgf, visits, student_username, game_id, options)
        if self.last_plan and job.job_id not in self.plans:
            self.plans[job.job_id] = self.last_plan
            job.plan = self.last_plan
        expected = (job.positions_total * visits / self.vps) if self.vps > 0 else None
        out = {"job_id": job.job_id, "game_id": job.game_id, "positions_total": job.positions_total,
               "visits_per_move": visits, "expected_seconds": None if expected is None else round(expected),
               "started_at": timestamp(job.started_at), "reused": job.reused,
               "student_color": None if job.student_color is None else COLOR_CHAR[job.student_color],
               "profiles": job.profiles, "state": job.state}
        out["query_id"] = self._log(job.game_id, "start_game_analysis", {"visits_per_move": visits}, result={"job_id": job.job_id})
        return out

    def job_status(self, job_id: str, action: str = "status") -> dict:
        """Progress of a survey job (action 'status'), cancel it ('cancel'), or free a finished job's memory ('release'; its results stay on disk)."""
        with _engine_errors():
            if action == "cancel":
                return self.jobs.cancel(job_id)
            if action == "release":
                return self.jobs.release(job_id)
            return self.jobs.get(job_id).status()

    def job_results(self, job_id: str, detail: str = "digest", range: list | None = None, max_episodes: int = 10,
                    include_positives: bool = True) -> dict:
        """Survey digest: phases, points lost, episodes (chains) with signatures, candidate tags, human probabilities, decisive move, last chance. detail: digest | moves | full."""
        with _engine_errors():
            job = self.jobs.get(job_id)
            if detail == "moves":
                rows = self.jobs.rows(job_id, tuple(range) if range else None)
                return {"job_id": job_id, "game_id": job.game_id, "moves": [_row_out(r) for r in rows]}
            d = self.jobs.digest(job_id, max_episodes=max_episodes, include_positives=include_positives)
            if detail == "full":
                d["moves"] = [_row_out(r) for r in self.jobs.rows(job_id)]
        d["query_id"] = self._log(job.game_id, "job_results", {"detail": detail},
                                  result={"episodes": len(d.get("episodes", [])), "complete": d.get("complete")})
        return d

    # ================================================================ 1.7 get_position_ref
    def get_position_ref(self, job_id: str | None = None, sgf: str | None = None, move_number: int = 0) -> dict:
        """Position reference for the position after move_number of a job, or of an SGF given as OGS link/id, file path, or text."""
        spec = {"job_id": job_id, "move_number": move_number} if job_id else {"sgf": sgf, "move_number": move_number}
        if not job_id and not sgf:
            raise ToolError("bad_request", "job_id or sgf required")
        rec = self._resolve_position(spec)
        board = rec.board
        last = rec.spec.moves[-1] if rec.spec.moves else None
        cached = self.store.get_cached(rec.ref, 1)
        return {"position_ref": rec.ref, "game_id": rec.game_id, "move_number": len(rec.spec.moves),
                "to_move": COLOR_CHAR[rec.to_move],
                "last_move": None if last is None else [COLOR_CHAR[last[0]], idx_to_gtp(last[1])],
                "captures": {"B": board.captures[BLACK], "W": board.captures[WHITE]},
                "cached_analysis": None if cached is None else {"visits": cached.visits,
                                                                "score_lead": _r(cached.score_lead),
                                                                "top_move": idx_to_gtp(cached.candidates[0].move) if cached.candidates else None}}

    # ================================================================ 1.8 analyze_position
    def analyze_position(self, position: dict, budget: dict | None = None, options: dict | None = None) -> dict:
        """Search one position: root score/winrate, candidates with PV and human probabilities, acceptable set, policy top, groups. position: {ref}|{job_id,move_number}|{sgf,move_number}|{moves,...} plus optional then:[...]."""
        o = options or {}
        rec = self._resolve_position(position)
        visits, _ = self._visits_for(rec, budget, "root")
        persp = self._perspective(o.get("perspective"), rec)
        profiles = self._profiles(o.get("human_profiles"), rec)
        wrn = o.get("wide_root_noise", 0.04)
        t0 = time.time()
        a, hit = self._analyze(rec, visits, ownership=o.get("include_ownership", False) or o.get("include_groups", True),
                               ownership_stdev=o.get("include_ownership_stdev", False), policy=True,
                               wide_root_noise=wrn or None, pv_len=o.get("pv_len", 8), human_profiles=list(profiles.values()),
                               stop_when_stable=True)
        size = rec.spec.size
        acc = acceptable_set(a, a.to_move, self.cfg.thresholds)
        pol_top = []
        if a.policy:
            order = sorted(range(size * size), key=lambda i: -(a.policy[i] if a.policy[i] is not None else -1))[:8]
            pol_top = [{"move": idx_to_gtp(i), "prior": round(a.policy[i], 4)} for i in order if a.policy[i] and a.policy[i] > 0]
        out = {
            "position_ref": rec.ref, "to_move": COLOR_CHAR[a.to_move], "perspective": COLOR_CHAR[persp],
            "visits_used": a.visits, "seconds_used": round(a.seconds, 2), "cached": hit, "stopped_early": a.stopped_early,
            "root": {"score_lead": self._pv_score(a.score_lead, persp), "winrate": self._pv_wr(a.winrate, persp),
                     "score_stdev": round(a.score_stdev, 2), "visits": a.visits},
            "candidates": self._candidate_dicts(a, persp, profiles, size, int(o.get("max_candidates", 8)), acc),
            "acceptable_set": {"margin": self.cfg.thresholds.acceptable_margin, "moves": [idx_to_gtp(m) for m in acc]},
            "policy_top": pol_top,
            "resolved_profiles": profiles,
        }
        if o.get("include_ownership") and a.ownership is not None:
            out["ownership"] = [round(x, 2) for x in a.ownership]
        if o.get("include_ownership_stdev") and a.ownership_stdev is not None:
            out["ownership_stdev"] = [round(x, 2) for x in a.ownership_stdev]
        if o.get("include_groups", True) and a.ownership is not None:
            races = capture_races(rec.board, a.ownership, self.cfg.thresholds)
            out["groups"] = group_records(rec.board, a.ownership, self.cfg.thresholds, min_size=1,
                                          race_anchors=race_anchor_set(races, size))
            if races:
                out["capture_races"] = races
        out["query_id"] = self._log(rec.game_id, "analyze_position", {"ref": rec.ref, "visits": visits}, a.visits,
                                    time.time() - t0, hit, {"top": out["candidates"][0]["move"] if out["candidates"] else None,
                                                            "score_lead": out["root"]["score_lead"]})
        return out

    # ================================================================ 1.9 analyze_line
    def analyze_line(self, position: dict, line: list, budget: dict | None = None, follow_pv_plies: int | None = None,
                     options: dict | None = None) -> dict:
        """Play a line (forced moves and/or engine replies) and evaluate every node; then follow the PV. Returns per-ply evals, deltas, end ownership/groups, refutation probability."""
        o = options or {}
        if not line:
            raise ToolError("bad_request", "line must have at least one step")
        start = self._resolve_position(position)
        visits, jid = self._visits_for(start, budget, "line_node")
        persp = self._perspective(o.get("perspective"), start)
        plan = self._active_plan(jid)
        if follow_pv_plies is None:
            follow_pv_plies = plan["verification"]["per_episode"]["follow_pv_plies"] if plan else self.cfg.budget.unit_base.plies
        opp_alias = o.get("opponent_profile", "opponent")
        opp_profile = self._profiles([opp_alias], start)[opp_alias]
        punish_plies = int(o.get("punish_plies", PUNISH_PLIES))
        restrict = o.get("restrict")
        restrict_color = CHAR_COLOR[restrict["color"]] if restrict else None
        restrict_pts = region_indices(restrict["region"]) if restrict else None
        size = start.spec.size
        t0 = time.time()
        spent = _Spent()
        a0, _ = self._analyze(start, visits, ownership=False, policy=False, spent=spent)
        cur, cur_a = start, a0
        nodes: list[dict] = []
        punish: list[dict] = []
        opponent_color = opponent(persp)

        def engine_move(rec: PositionRecord, a: Analysis, ply: int) -> tuple[int | None, Analysis]:
            color = rec.to_move
            if restrict and color == restrict_color and ply <= int(restrict.get("until_ply", 99)):
                allowed = [idx_to_gtp(i) for i in restrict_pts if rec.board.cells[i] == EMPTY] + ["pass"]
                a2, _ = self._analyze(rec, visits, ownership=False, policy=False,
                                      allow=[{"player": COLOR_CHAR[color], "moves": allowed, "untilDepth": 1}], spent=spent)
                return best_candidate(a2), a2
            return best_candidate(a), a

        steps = list(norm_list(line) or []) + [{"engine": True}] * int(follow_pv_plies)
        for ply, raw_step in enumerate(steps, 1):
            color = cur.to_move
            step = norm_line_step(raw_step, color, size)
            if step.get("engine"):
                if step.get("color") and CHAR_COLOR.get(step["color"]) != color:
                    raise ToolError("bad_request", f"step {ply}: color {step['color']} is not to move", {"ply": ply})
                mv, cur_a = engine_move(cur, cur_a, ply)
                forced = False
                alternatives = [{"move": idx_to_gtp(c.move), "score_lead": self._pv_score(c.score_lead, persp), "visits": c.visits}
                                for c in cur_a.candidates[:3]]
            else:
                if CHAR_COLOR.get(step["color"]) != color:
                    raise ToolError("bad_request", f"step {ply}: {step['color']} is not to move ({COLOR_CHAR[color]} is)", {"ply": ply},
                                    suggestion="insert a pass, or start from the position before the opponent's reply")
                mv = _gtp(step["move"], size, f"step {ply}", {"ply": ply})
                forced = True
                alternatives = None
            human_p = None
            if color == opponent_color and not forced:
                try:
                    human_p = _r(prob_at(self._human_policy(cur, opp_profile), mv, size), 4)
                except ToolError:
                    human_p = None
                if human_p is not None and len(punish) < punish_plies:
                    punish.append({"ply": ply, "move": idx_to_gtp(mv), "probability": human_p})
            try:
                nxt = self._play(cur, mv, color)
            except ToolError as e:
                e.details["ply"] = ply
                raise
            is_last = ply == len(steps)
            na, _ = self._analyze(nxt, visits, ownership=is_last and o.get("ownership_at_end", True), policy=False, spent=spent)
            nodes.append({
                "ply": ply, "color": COLOR_CHAR[color], "move": idx_to_gtp(mv), "forced": forced, "position_ref": nxt.ref,
                "eval_after": {"score_lead": self._pv_score(na.score_lead, persp), "winrate": self._pv_wr(na.winrate, persp),
                               "score_stdev": round(na.score_stdev, 2), "visits": na.visits},
                "delta": self._pv_score(na.score_lead - cur_a.score_lead, persp),
                "alternatives": alternatives, "human_probability": human_p,
            })
            cur, cur_a = nxt, na
        end_board = cur.board
        end = {"position_ref": cur.ref, "score_lead": self._pv_score(cur_a.score_lead, persp), "winrate": self._pv_wr(cur_a.winrate, persp),
               "captures": {"B": end_board.captures[BLACK], "W": end_board.captures[WHITE]}}
        if cur_a.ownership is not None and o.get("ownership_at_end", True):
            end["ownership"] = [round(x, 2) for x in cur_a.ownership]
            if o.get("groups_at_end", True):
                races = capture_races(end_board, cur_a.ownership, self.cfg.thresholds)
                end["groups"] = group_records(end_board, cur_a.ownership, self.cfg.thresholds, min_size=2,
                                              race_anchors=race_anchor_set(races, size))
                if races:
                    end["capture_races"] = races
        largest = min(nodes, key=lambda nd: nd["delta"]) if nodes else None
        summary = {"score_start": self._pv_score(a0.score_lead, persp), "score_end": end["score_lead"],
                   "total_change": round(end["score_lead"] - self._pv_score(a0.score_lead, persp), 2),
                   "largest_drop": None if largest is None else {"ply": largest["ply"], "color": largest["color"],
                                                                 "move": largest["move"], "delta": largest["delta"]}}
        if o.get("compare_to_best", True) and a0.candidates:
            best = a0.candidates[0]
            first = nodes[0] if nodes else None
            summary["vs_best"] = {"best_first_move": idx_to_gtp(best.move),
                                  "score_after_best_root_estimate": self._pv_score(best.score_lead, persp),
                                  "gap_vs_first_step": None if first is None else round(self._pv_score(best.score_lead, persp) - first["eval_after"]["score_lead"], 2),
                                  "note": "root estimate of the best move, not a played-out line; run analyze_line on it for the contrast"}
        out = {"start": {"position_ref": start.ref, "score_lead": summary["score_start"], "to_move": COLOR_CHAR[start.to_move]},
               "perspective": COLOR_CHAR[persp], "nodes": nodes, "end": end, "summary": summary, "legality": "ok",
               "visits_used": spent.visits, "seconds_used": round(time.time() - t0, 2)}
        if punish:
            prod = 1.0
            for p in punish:
                prod *= p["probability"]
            out["refutation_probability"] = {"by_profile": opp_profile, "per_ply": punish, "product": round(prod, 4)}
        out["query_id"] = self._log(start.game_id, "analyze_line", {"ref": start.ref, "line": line, "follow": follow_pv_plies},
                                    spent.visits, time.time() - t0, False, {"score_start": summary["score_start"], "score_end": summary["score_end"]})
        return out

    # ================================================================ 1.10 pass_probe
    def pass_probe(self, position: dict, player: str, move: str | None = None, budget: dict | None = None,
                   options: dict | None = None) -> dict:
        """Local value of a move: score if the player passes vs after the best move (and after `move`). options.rank_regions=true ranks the nine regions by the value of playing there."""
        o = options or {}
        rec = self._resolve_position(position)
        player = norm_color(player)
        color = CHAR_COLOR.get(player)
        if color is None:
            raise ToolError("bad_request", "player must be 'B' or 'W'")
        if rec.to_move != color:
            raise ToolError("bad_request", f"{player} is not to move at this position ({COLOR_CHAR[rec.to_move]} is)",
                            suggestion="pass the position before the move in question")
        visits, _ = self._visits_for(rec, budget, "root")
        persp = CHAR_COLOR.get(o.get("perspective"), color)
        s = sign_of(persp)
        t0 = time.time()
        spent = _Spent()
        a0, _ = self._analyze(rec, visits, ownership=False, policy=False, stop_when_stable=True, spent=spent)
        pass_rec = self._play(rec, None, color)
        ap, _ = self._analyze(pass_rec, visits, ownership=False, policy=False, stop_when_stable=True, spent=spent)
        score_if_pass = ap.score_lead
        reply = best_candidate(ap)
        out = {"position_ref": rec.ref, "player": player, "perspective": COLOR_CHAR[persp],
               "score_if_pass": self._pv_score(score_if_pass, persp),
               "opponent_reply_to_pass": {"move": idx_to_gtp(reply), "region": LABELS[standard_code(reply)] if reply is not None else None},
               "score_after_best": {"move": idx_to_gtp(a0.candidates[0].move) if a0.candidates else None,
                                    "score": self._pv_score(a0.candidates[0].score_lead if a0.candidates else a0.score_lead, persp)},
               "score_after_move": None, "local_value": {"played": None,
                                                         "best": round(s * ((a0.candidates[0].score_lead if a0.candidates else a0.score_lead) - score_if_pass), 2)}}
        if move:
            mv_rec = self._resolve_position({"ref": rec.ref, "then": [[player, move]]})
            am, _ = self._analyze(mv_rec, visits, ownership=False, policy=False, spent=spent)
            out["score_after_move"] = {"move": move, "score": self._pv_score(am.score_lead, persp)}
            out["local_value"]["played"] = round(s * (am.score_lead - score_if_pass), 2)
        if o.get("rank_regions"):
            urgency = []
            parts = standard_partition(rec.spec.size)
            for code in STANDARD_CODES:
                allowed = [idx_to_gtp(i) for i in parts[code] if rec.board.cells[i] == EMPTY]
                if not allowed:
                    continue
                ar, _ = self._analyze(rec, max(200, visits // 3), ownership=False, policy=False,
                                      allow=[{"player": player, "moves": allowed, "untilDepth": 1}], spent=spent)
                if ar.candidates:
                    urgency.append({"region": code, "label": LABELS[code], "best_move_there": idx_to_gtp(ar.candidates[0].move),
                                    "value": round(s * (ar.candidates[0].score_lead - score_if_pass), 2)})
            urgency.sort(key=lambda u: -u["value"])
            out["urgency"] = urgency
        out["visits_used"] = spent.visits
        out["seconds_used"] = round(time.time() - t0, 2)
        out["query_id"] = self._log(rec.game_id, "pass_probe", {"ref": rec.ref, "player": player, "move": move}, spent.visits,
                                    time.time() - t0, False, {"local_value": out["local_value"]})
        return out

    # ================================================================ 1.11 swing_value
    def swing_value(self, position: dict, points: list, budget: dict | None = None, options: dict | None = None) -> dict:
        """Swing (Black-first minus White-first) and sente/gote for up to six points, ranked."""
        o = options or {}
        points = norm_list(points) or []
        if not points or len(points) > 6:
            raise ToolError("bad_request", "points must have 1..6 entries")
        rec = self._resolve_position(position)
        visits, _ = self._visits_for(rec, budget, "root")
        persp = self._perspective(o.get("perspective"), rec)
        radius = int(o.get("local_radius", 4))
        t0 = time.time()
        spent = _Spent()
        results = []
        for pt in points:
            idx = _gtp(pt, rec.spec.size)
            if idx is None or rec.board.cells[idx] != EMPTY:
                results.append({"point": pt, "skipped": "occupied"})
                continue
            entry = {"point": pt}
            scores = {}
            for color_ch in ("B", "W"):
                color = CHAR_COLOR[color_ch]
                then = []
                if rec.to_move != color:
                    then.append([COLOR_CHAR[rec.to_move], "pass"])
                then.append([color_ch, pt])
                try:
                    r2 = self._resolve_position({"ref": rec.ref, "then": then})
                except ToolError as e:
                    entry[f"{'black' if color_ch == 'B' else 'white'}_first"] = {"skipped": e.message}
                    continue
                a, _ = self._analyze(r2, visits, ownership=False, policy=False, spent=spent)
                reply = best_candidate(a)
                local = reply is not None and chebyshev(reply, idx, rec.spec.size) <= radius
                gap = None
                if a.candidates and len(a.candidates) > 1:
                    s2 = sign_of(a.to_move)
                    non_local = [c for c in a.candidates[1:] if c.move is None or chebyshev(c.move, idx, rec.spec.size) > radius]
                    if non_local:
                        gap = s2 * (a.candidates[0].score_lead - non_local[0].score_lead)
                key = "black_first" if color_ch == "B" else "white_first"
                entry[key] = {"score_after": self._pv_score(a.score_lead, persp), "best_reply": idx_to_gtp(reply),
                              "reply_is_local": local, "reply_gap": _r(gap)}
                scores[color_ch] = a.score_lead
                entry.setdefault("_sente", {})[color_ch] = "unclear" if (gap is not None and gap < 0.5) else ("sente" if local else "gote")
            if "B" in scores and "W" in scores:
                entry["swing"] = round(abs(scores["B"] - scores["W"]), 2)
            sente = entry.pop("_sente", {})
            entry["sente_gote"] = {"for_black": sente.get("B", "unclear"), "for_white": sente.get("W", "unclear")}
            results.append(entry)
        ranked = [r["point"] for r in sorted((r for r in results if "swing" in r), key=lambda r: -r["swing"])]
        out = {"position_ref": rec.ref, "to_move": COLOR_CHAR[rec.to_move], "perspective": COLOR_CHAR[persp],
               "results": results, "ranked": ranked, "visits_used": spent.visits, "seconds_used": round(time.time() - t0, 2)}
        out["query_id"] = self._log(rec.game_id, "swing_value", {"ref": rec.ref, "points": points}, spent.visits, time.time() - t0, False,
                                    {"ranked": ranked})
        return out

    # ================================================================ 1.12 local_solve
    def local_solve(self, position: dict, group_point: str, region: dict | None = None, budget: dict | None = None,
                    options: dict | None = None) -> dict:
        """Life-and-death of the group at group_point: attacker-first and defender-first playouts confined to a region -> alive | dead | unsettled | unclear with confidence."""
        o = options or {}
        rec = self._resolve_position(position)
        visits, _ = self._visits_for(rec, budget, "local_solve")
        th = self.cfg.thresholds
        size = rec.spec.size
        gp = _gtp(group_point, size)
        g = rec.board.group_at(gp) if gp is not None else None
        if g is None:
            raise ToolError("no_group_at_point", f"no stone at {group_point}")
        target_stones = set(g.stones)
        if region is not None:
            try:
                reg = region_indices(region, size)
            except RegionError as e:
                raise ToolError("bad_region", str(e))
        else:
            reg = expand(target_stones, 2, size)
        reg |= set(g.liberties)
        max_plies = int(o.get("max_plies", 20))
        allow_tenuki = bool(o.get("allow_tenuki", True))
        defender, attacker = g.color, opponent(g.color)
        caveats = []
        if any(any(nb not in reg for nb in neighbors(l, size)) for l in g.liberties):
            caveats.append("group's liberties touch the region boundary: outside liberties may matter")
        if rec.board.ko_capture_available(rec.spec.rules):
            caveats.append("ko present in the position")
        t0 = time.time()
        spent = _Spent()

        def own_of(a: Analysis, board: Board) -> float:
            return group_mean_ownership(g, a.ownership, board)     # captured stones count as lost for the defender

        def run(first: int) -> dict:
            cur = rec
            seq = []
            if cur.to_move != first:
                cur = self._play(cur, None)
                seq.append([COLOR_CHAR[opponent(first)], "pass"])
            hist: list[float] = []
            reason = "max_plies"
            passes = 0
            a = None
            for ply in range(1, max_plies + 1):
                color = cur.to_move
                allowed = [idx_to_gtp(i) for i in reg if cur.board.cells[i] == EMPTY]
                if allow_tenuki or not allowed:
                    allowed.append("pass")
                a, _ = self._analyze(cur, visits, ownership=True, policy=False,
                                     allow=[{"player": COLOR_CHAR[color], "moves": allowed, "untilDepth": 1}], spent=spent)
                if not target_stones & set(cur.board.stones(defender)):
                    reason = "group_captured"
                    break
                mv = best_candidate(a)
                seq.append([COLOR_CHAR[color], idx_to_gtp(mv)])
                cur = self._play(cur, mv, color)
                passes = passes + 1 if mv is None else 0
                if passes >= 2:
                    reason = "both_passed"
                    break
                if not target_stones & set(cur.board.stones(defender)):
                    reason = "group_captured"
                    a = None
                    break
                hist.append(own_of(a, cur.board))
                if len(hist) >= 3 and max(hist[-3:]) - min(hist[-3:]) <= 0.1:
                    reason = "stable"
                    break
            if a is None or reason == "group_captured":
                final_own = -1.0
                a_end = None
            else:
                a_end, _ = self._analyze(cur, max(200, visits // 2), ownership=True, policy=False, spent=spent)
                final_own = own_of(a_end, cur.board)
            return {"sequence": seq, "final_position_ref": cur.ref, "final_group_ownership": round(final_own, 3),
                    "final_status": group_status_label(final_own, th), "plies": len([m for m in seq if m[1] != "pass"]),
                    "stopped_reason": reason}

        att = run(attacker)
        dfn = run(defender)
        sa, sd = att["final_status"], dfn["final_status"]
        if sa == "alive" and sd == "alive":
            status = "alive"
        elif sa == "dead" and sd == "dead":
            status = "dead"
        elif sa == "dead" and sd == "alive":
            status = "unsettled"
        else:
            status = "unclear"
        margins = [abs(att["final_group_ownership"]) - th.alive, abs(dfn["final_group_ownership"]) - th.alive]
        conf = "high" if all(m >= 0.2 for m in margins) else "low" if any(abs(v) < 0.1 for v in (att["final_group_ownership"], dfn["final_group_ownership"])) or status == "unclear" else "medium"
        if abs(att["final_group_ownership"]) < 0.2 or abs(dfn["final_group_ownership"]) < 0.2:
            caveats.append("seki-like ownership near zero in at least one run")
        out = {"target": {"color": COLOR_CHAR[g.color], "stones": [idx_to_gtp(i) for i in g.stones],
                          "label": f"{COLOR_CHAR[g.color]} {LABELS[standard_code(g.anchor, size)]} ({g.size})"},
               "region_used": {"points": [idx_to_gtp(i) for i in sorted(reg)]},
               "attacker_first": att, "defender_first": dfn, "status": status, "confidence": conf, "caveats": caveats,
               "visits_used": spent.visits, "seconds_used": round(time.time() - t0, 2)}
        qid = self._log(rec.game_id, "local_solve", {"ref": rec.ref, "group_point": group_point}, spent.visits, time.time() - t0, False,
                        {"status": status, "confidence": conf})
        out["query_id"] = qid
        self.solve_results[qid] = out
        return out

    # ================================================================ 1.13 group_status
    def group_status(self, position: dict, options: dict | None = None) -> dict:
        """Every group with size, liberties, mean ownership and status (alive/unsettled/dead) from cached or quick ownership."""
        o = options or {}
        rec = self._resolve_position(position)
        t0 = time.time()
        own, _score, visits = self._ownership(rec, o.get("budget"))
        races = capture_races(rec.board, own, self.cfg.thresholds)
        groups = group_records(rec.board, own, self.cfg.thresholds, int(o.get("min_size", 1)), bool(o.get("include_liberty_points", False)),
                               race_anchors=race_anchor_set(races, rec.spec.size))
        summary = {"B": {"alive": 0, "unsettled": 0, "dead": 0}, "W": {"alive": 0, "unsettled": 0, "dead": 0}}
        for g in groups:
            summary[g["color"]][g["status"]] += 1
        out = {"position_ref": rec.ref, "groups": groups, "unsettled": [g["id"] for g in groups if g["status"] == "unsettled"],
               "summary": summary, "ownership_visits": visits}
        if races:
            out["capture_races"] = races
        out["query_id"] = self._log(rec.game_id, "group_status", {"ref": rec.ref}, 0, time.time() - t0, True, {"unsettled": len(out["unsettled"])})
        return out

    # ================================================================ 1.14 ownership_diff
    def ownership_diff(self, a: dict, b: dict, regions: list | None = None, budget: dict | None = None, options: dict | None = None) -> dict:
        """Ownership change between two positions by region and by group; classifies the loss as local, mixed or global."""
        o = options or {}
        ra = self._resolve_position(a)
        rb = self._resolve_position(b)
        persp = self._perspective(o.get("perspective"), ra)
        t0 = time.time()
        own_a, sa, _ = self._ownership(ra, budget)
        own_b, sb, _ = self._ownership(rb, budget)
        s = sign_of(persp)
        delta = s * (sb - sa)
        attr = regional_attribution(own_a, own_b, persp, ra.spec.size, delta)
        changes = group_changes(ra.board, own_a, own_b, self.cfg.thresholds, min_size=2, top=5)
        out = {"a": {"position_ref": ra.ref, "score_lead": round(s * sa, 2)}, "b": {"position_ref": rb.ref, "score_lead": round(s * sb, 2)},
               "perspective": COLOR_CHAR[persp], "score_delta": round(delta, 2), "regional": attr["regional"],
               "explained_share": attr["explained_share"],
               "groups_changed": [{"group": c["group"], "before": c["before_value"], "after": c["after_value"],
                                   "status_before": c["status_before"], "status_after": c["status_after"], "points": c["points"]} for c in changes],
               "local_vs_global": {"primary_region": attr["primary_region"], "primary_label": LABELS[attr["primary_region"]],
                                   "local_share": attr["local_share"], "classification": classify_local(attr["local_share"], self.cfg.thresholds)}}
        if regions:
            custom = []
            for spec in regions:
                try:
                    idxs = region_indices(spec, ra.spec.size)
                except RegionError as e:
                    raise ToolError("bad_region", str(e))
                custom.append({"region": spec, "ownership_delta_points": round(s * sum(own_b[i] - own_a[i] for i in idxs), 2)})
            out["custom_regions"] = custom
        out["query_id"] = self._log(ra.game_id, "ownership_diff", {"a": ra.ref, "b": rb.ref}, 0, time.time() - t0, True,
                                    {"score_delta": out["score_delta"], "classification": out["local_vs_global"]["classification"]})
        return out

    # ================================================================ 1.15 human_move_distribution
    def human_move_distribution(self, position: dict, profiles: list | None = None, moves_of_interest: list | None = None,
                                top_n: int = 8) -> dict:
        """Human-model move probabilities at a position for profiles (peer/target/horizon/opponent or rank_7k...), top moves and moves of interest."""
        rec = self._resolve_position(position)
        prof = self._profiles(norm_list(profiles) or ["peer", "target", "horizon", "opponent"], rec)
        moves_of_interest = norm_list(moves_of_interest)
        size = rec.spec.size
        t0 = time.time()
        out_p = {}
        for alias, p in prof.items():
            pol = self._human_policy(rec, p)
            order = sorted((i for i in range(size * size) if pol[i] > 0), key=lambda i: -pol[i])[:top_n]
            ent = -sum(v * math.log(v) for v in pol[:size * size] if v and v > 0)
            moi = {m: _r(prob_at(pol, _gtp(m, size), size), 4) for m in moves_of_interest or []}
            out_p[alias] = {"profile": p, "top": [{"move": idx_to_gtp(i), "probability": round(pol[i], 4)} for i in order],
                            "moves_of_interest": moi, "entropy": round(ent, 3)}
        out = {"position_ref": rec.ref, "to_move": COLOR_CHAR[rec.to_move], "profiles": out_p, "resolved_profiles": prof}
        out["query_id"] = self._log(rec.game_id, "human_move_distribution", {"ref": rec.ref, "profiles": list(prof.values())}, 0,
                                    time.time() - t0, True)
        return out

    # ================================================================ 1.16 render_board
    def render_board(self, position: dict, options: dict | None = None) -> dict:
        """ASCII board with last move, highlights, region box; optional ownership/policy overlay and low-liberty groups."""
        o = options or {}
        rec = self._resolve_position(position)
        size = rec.spec.size
        last = rec.spec.moves[-1] if rec.spec.moves else None
        hl = {_gtp(p, size) for p in (norm_list(o.get("highlight")) or [])}
        box = None
        if o.get("region_box"):
            try:
                box = region_indices(o["region_box"], size)
            except RegionError as e:
                raise ToolError("bad_region", str(e))
        ascii_board = render_board(rec.board, last, o.get("mark_last", True), hl, box, o.get("coordinates", True))
        out = {"position_ref": rec.ref, "to_move": COLOR_CHAR[rec.to_move],
               "last_move": None if last is None else [COLOR_CHAR[last[0]], idx_to_gtp(last[1])],
               "captures": {"B": rec.board.captures[BLACK], "W": rec.board.captures[WHITE]}, "ascii": ascii_board, "legend": LEGEND}
        overlay = o.get("overlay")
        if overlay in ("ownership", "ownership_stdev"):
            if overlay == "ownership":
                own, _, _ = self._ownership(rec)
                out["overlay_ascii"] = render_overlay(own, size, "ownership")
            else:
                a, _ = self._analyze(rec, self.cfg.thresholds.quick_visits, ownership=True, ownership_stdev=True, policy=False)
                out["overlay_ascii"] = render_overlay(a.ownership_stdev, size, "ownership_stdev")
        elif overlay == "policy":
            a, _ = self._analyze(rec, self.cfg.thresholds.quick_visits, ownership=False, policy=True)
            out["overlay_ascii"] = render_overlay(a.policy[:size * size], size, "policy")
        if o.get("label_low_liberties", False):
            out["low_liberty_groups"] = low_liberty_groups(rec.board, 3, 1)[:8]
        return out

    # ================================================================ causal evidence: shared helpers
    def _human_top(self, rec: PositionRecord, profile: str, exclude: set | None = None) -> tuple[int | None, float | None]:
        """The legal move a player of `profile` most likely plays here (never pass), and its probability."""
        pol = self._human_policy(rec, profile)
        size = rec.spec.size
        for i in sorted(range(size * size), key=lambda k: -pol[k]):
            if pol[i] <= 0:
                break
            if exclude and i in exclude:
                continue
            try:
                rec.board.play(rec.to_move, i, rec.spec.rules)
            except IllegalMove:
                continue
            return i, round(pol[i], 4)
        return None, None

    def _second_best_gap(self, rec: PositionRecord, a: Analysis, visits: int, spent: _Spent) -> tuple[float | None, str | None]:
        """Re-search with the top move avoided so the second-best has real visits; returns (how much the
        best beats it for the side to move, the second-best move)."""
        top = a.candidates[0]
        x = rec.to_move
        a2, _ = self._analyze(rec, visits, ownership=False, policy=False,
                              avoid=[{"player": COLOR_CHAR[x], "moves": [idx_to_gtp(top.move)], "untilDepth": 1}], spent=spent)
        if not a2.candidates:
            return None, None
        return round(sign_of(x) * (a.score_lead - a2.score_lead), 2), idx_to_gtp(a2.candidates[0].move)

    def _own_move(self, move, rec: PositionRecord) -> int | None:
        """A move for the side to move at rec, given as 'Q7', 'BQ7', {'color','move'} …; checks the colour."""
        step = norm_line_step(move, rec.to_move, rec.spec.size)
        if step.get("engine"):
            raise ToolError("bad_request", "move must be a point, not 'engine'")
        if CHAR_COLOR.get(step["color"]) != rec.to_move:
            raise ToolError("bad_request", f"{step['color']} is not to move at this position ({COLOR_CHAR[rec.to_move]} is)",
                            suggestion="pass the position before the move in question")
        return _gtp(step["move"], rec.spec.size)

    def _play(self, rec: PositionRecord, move: int | None, color: int | None = None) -> PositionRecord:
        return self._resolve_position({"ref": rec.ref, "then": [[COLOR_CHAR[color if color is not None else rec.to_move], idx_to_gtp(move)]]})

    def _engine_continuation(self, rec: PositionRecord, plies: int, visits: int,
                             spent: _Spent) -> tuple[list[str], PositionRecord, Analysis | None]:
        """Engine-vs-engine for `plies` moves from rec. Returns (moves as 'BQ7', end record, end analysis)."""
        moves, cur, a = [], rec, None
        for _ in range(plies):
            a, _ = self._analyze(cur, visits, ownership=False, policy=False, spent=spent)
            if not a.candidates or a.candidates[0].move is None:
                break
            mv = a.candidates[0].move
            moves.append(f"{COLOR_CHAR[cur.to_move]}{idx_to_gtp(mv)}")
            cur = self._play(cur, mv)
            a = None
        if a is None:
            a, _ = self._analyze(cur, visits, ownership=False, policy=False, spent=spent)
        return moves, cur, a

    def _features(self, rec: PositionRecord, visits: int, persp: int, spent: _Spent,
                  must_answer: bool = False) -> tuple[dict, list[dict]]:
        """Terminal features of one position (contract §1.18). Returns (public dict, groups with stones)."""
        th = self.cfg.thresholds
        size = rec.spec.size
        a, _ = self._analyze(rec, visits, ownership=True, policy=False, spent=spent)
        sp = sign_of(persp)
        races = capture_races(rec.board, a.ownership, th)
        groups = group_records(rec.board, a.ownership, th, min_size=2, race_anchors=race_anchor_set(races, size))
        you, opp = COLOR_CHAR[persp], COLOR_CHAR[opponent(persp)]
        weak = {"you": sum(1 for g in groups if g["color"] == you and g["status"] == "unsettled"),
                "opponent": sum(1 for g in groups if g["color"] == opp and g["status"] == "unsettled")}
        terr = territory_by_region(rec.board, a.ownership, persp)
        tot = {"you": round(sum(r["you"] for r in terr.values()), 1), "opponent": round(sum(r["opponent"] for r in terr.values()), 1)}
        # tempo price: what the side to move gains by playing its best move instead of passing
        mover = rec.to_move
        pass_rec = self._play(rec, None, mover)
        ap, _ = self._analyze(pass_rec, visits, ownership=False, policy=False, spent=spent)
        best = a.candidates[0] if a.candidates else None
        tempo = {"side_to_move": COLOR_CHAR[mover], "best_move": idx_to_gtp(best.move) if best else None,
                 "value": round(sign_of(mover) * (a.score_lead - ap.score_lead), 2),
                 "region": LABELS[standard_code(best.move, size)] if best and best.move is not None else None}
        # sente: the side to move chooses freely, unless the caller knows it still has to answer
        # (a forced line cut off by max_plies). A big local best move is not an answer: it may be an attack.
        holder = opponent(mover) if must_answer else mover
        sente = {"holder": "you" if holder == persp else "opponent", "holder_color": COLOR_CHAR[holder],
                 "side_to_move_must_answer": must_answer}
        pub = {"position_ref": rec.ref, "to_move": COLOR_CHAR[mover], "perspective": you,
               "score_lead": round(sp * a.score_lead, 2),
               "groups": [{k: g[k] for k in ("label", "anchor", "color", "size", "status", "mean_ownership", "liberties") if k in g}
                          for g in groups],
               "weak_groups": weak, "territory": terr, "territory_total": tot, "sente": sente, "tempo": tempo}
        if races:
            pub["capture_races"] = races
        return pub, groups

    @staticmethod
    def _compare_features(fa: dict, ga_: list[dict], fb: dict, gb: list[dict], th) -> dict:
        """What is concretely different between two endpoints (a = e.g. after the played line, b = after
        the better line), from the perspective both were computed in."""
        def owner_group(groups, color, stone):
            for g in groups:
                if g["color"] == color and stone in g["stones"]:
                    return g
            return None

        changes, seen_b = [], set()
        for g in ga_:
            h = next((h for s in g["stones"] if (h := owner_group(gb, g["color"], s)) is not None), None)
            if h is not None:
                seen_b.add(h["id"])
            st_b = h["status"] if h else "captured"
            moved = h is None or abs(h["mean_ownership"] - g["mean_ownership"]) >= th.group_change_min
            if (st_b != g["status"] and moved) or h is None:
                changes.append({"group": g["label"], "anchor": g["anchor"], "in_a": g["status"], "in_b": st_b,
                                "ownership_a": g["mean_ownership"], "ownership_b": h["mean_ownership"] if h else None})
        for h in gb:
            if h["id"] not in seen_b and not any(owner_group(ga_, h["color"], s) for s in h["stones"]) and h["size"] >= 3:
                changes.append({"group": h["label"], "anchor": h["anchor"], "in_a": "absent", "in_b": h["status"],
                                "ownership_a": None, "ownership_b": h["mean_ownership"]})
        regions = []
        for code, ra in fa["territory"].items():
            rb = fb["territory"][code]
            dy, do = round(rb["you"] - ra["you"], 1), round(rb["opponent"] - ra["opponent"], 1)
            if abs(dy) >= th.territory_diff_min or abs(do) >= th.territory_diff_min:
                regions.append({"region": code, "label": ra["label"], "a": {"you": ra["you"], "opponent": ra["opponent"]},
                                "b": {"you": rb["you"], "opponent": rb["opponent"]}, "you_diff": dy, "opponent_diff": do})
        regions.sort(key=lambda r: -(abs(r["you_diff"]) + abs(r["opponent_diff"])))
        return {"score_diff": round(fb["score_lead"] - fa["score_lead"], 2),
                "groups_changed": changes, "territory_changed": regions,
                "territory_total": {"a": fa["territory_total"], "b": fb["territory_total"]},
                "sente": {"a": fa["sente"]["holder"], "b": fb["sente"]["holder"], "changed": fa["sente"]["holder"] != fb["sente"]["holder"]},
                "tempo": {"a": fa["tempo"], "b": fb["tempo"]},
                "weak_groups": {"a": fa["weak_groups"], "b": fb["weak_groups"]}}

    # ================================================================ 1.18 terminal_features
    def terminal_features(self, position: dict, compare_to: dict | None = None, budget: dict | None = None,
                          options: dict | None = None) -> dict:
        """What an end position looks like (group statuses, weak groups, territory by region, who holds sente, what the next move is worth) and, with compare_to, what is concretely different between two end positions."""
        o = options or {}
        rec = self._resolve_position(position)
        visits, _ = self._visits_for(rec, budget, "line_node")
        persp = self._perspective(o.get("perspective"), rec)
        t0 = time.time()
        spent = _Spent()
        fa, ga_ = self._features(rec, visits, persp, spent)
        out = {"a": fa}
        if compare_to is not None:
            rb = self._resolve_position(compare_to)
            fb, gb = self._features(rb, visits, persp, spent)
            out["b"] = fb
            out["comparison"] = self._compare_features(fa, ga_, fb, gb, self.cfg.thresholds)
        out["visits_used"] = spent.visits
        out["seconds_used"] = round(time.time() - t0, 2)
        out["query_id"] = self._log(rec.game_id, "terminal_features", {"ref": rec.ref, "compare_to": compare_to}, spent.visits,
                                    time.time() - t0, False, {"score_a": fa["score_lead"],
                                                              "score_b": out.get("b", {}).get("score_lead")})
        return out

    # ================================================================ 1.19 forced_line
    def forced_line(self, position: dict, move: str, budget: dict | None = None, options: dict | None = None) -> dict:
        """Play a move and extend the line while each reply is forced (the second-best loses more than forced_margin), preferring human-legible moves; adds the opponent's natural resistance with its refutation and the terminal features of the end position."""
        o = options or {}
        th = self.cfg.thresholds
        start = self._resolve_position(position)
        visits, jid = self._visits_for(start, budget, "line_node")
        persp = self._perspective(o.get("perspective"), start)
        sp = sign_of(persp)
        plan = self._active_plan(jid)
        default_plies = plan["verification"]["per_episode"].get("forced_line_plies", 8) if plan else 8
        max_plies = int(o.get("max_plies", default_plies))
        forced_margin = float(o.get("forced_margin", th.forced_margin))
        human_margin = float(o.get("human_margin", th.human_margin))
        resistance_nodes = int(o.get("resistance_nodes", FORCED_RESISTANCE_NODES))
        refute_plies = int(o.get("refutation_plies", FORCED_REFUTATION_PLIES))
        extend = o.get("extend", "local")
        if extend not in ("forced", "local"):
            raise ToolError("bad_request", "extend must be 'forced' or 'local'")
        prof = self._profiles([o.get("legible_profile", "target"), o.get("resistance_profile", "opponent")], start)
        legible_prof = prof[o.get("legible_profile", "target")]
        resist_prof = prof[o.get("resistance_profile", "opponent")]
        size = start.spec.size
        t0 = time.time()
        spent = _Spent()
        a0, _ = self._analyze(start, visits, ownership=False, policy=False, spent=spent)
        mover = start.to_move
        mv0 = self._own_move(move, start)
        cur = self._play(start, mv0, mover)
        line = [f"{COLOR_CHAR[mover]}{idx_to_gtp(mv0)}"]
        played_idx = [mv0]
        nodes: list[dict] = []
        stop_reason, free = "max_plies", None
        resist_used = 0
        first_eval = None
        for ply in range(1, max_plies + 1):
            x = cur.to_move
            sx = sign_of(x)
            a, _ = self._analyze(cur, visits, ownership=False, policy=False, spent=spent)
            if first_eval is None:
                first_eval = a.score_lead
            if not a.candidates or a.candidates[0].move is None:
                stop_reason = "pass"
                break
            top = a.candidates[0]
            gap, alt = self._second_best_gap(cur, a, visits, spent)
            is_forced = gap is None or gap > forced_margin
            recent = [m for m in played_idx[-3:] if m is not None]
            local = any(chebyshev(top.move, m, size) <= th.local_radius for m in recent)
            if not is_forced and not (extend == "local" and local):
                stop_reason = "not_forced" if extend == "forced" else "quiet"
                free = {"side": COLOR_CHAR[x], "who": "you" if x == persp else "opponent", "best_move": idx_to_gtp(top.move),
                        "second_best": alt, "gap": gap, "best_is_local": local}
                break
            # the legible choice: the human-profile move among near-best candidates
            pol = self._human_policy(cur, legible_prof)
            chosen, by = top, "engine"
            floor = sx * top.score_lead - human_margin
            for c in a.candidates[1:]:
                if c.move is not None and c.visits >= th.acceptable_min_visit_share * max(1, a.visits) \
                        and sx * c.score_lead >= floor and pol[c.move] > pol[chosen.move]:
                    chosen, by = c, "human"
            node = {"ply": ply, "color": COLOR_CHAR[x], "move": idx_to_gtp(chosen.move), "forced": is_forced, "chosen_by": by,
                    "engine_best": idx_to_gtp(top.move), "gap_to_second": gap, "second_best": alt,
                    "score_after": round(sp * chosen.score_lead, 2), "legible_probability": round(pol[chosen.move], 4)}
            if x != persp and resist_used < resistance_nodes:
                r, rp = self._human_top(cur, resist_prof)
                if r is not None and r != chosen.move:
                    resist_used += 1
                    rrec = self._play(cur, r)
                    ar, _ = self._analyze(rrec, visits, ownership=False, policy=False, spent=spent)
                    ref_moves, _end, aend = self._engine_continuation(rrec, refute_plies, visits, spent)
                    node["resistance"] = {"move": idx_to_gtp(r), "probability": rp,
                                          "loss_for_resister": round(sx * (a.score_lead - ar.score_lead), 2),
                                          "refutation": ref_moves, "score_end": round(sp * aend.score_lead, 2) if aend else None}
                elif r is not None:
                    node["resistance"] = {"move": idx_to_gtp(r), "probability": rp, "note": "the natural reply is the forced one"}
            nodes.append(node)
            line.append(f"{COLOR_CHAR[x]}{idx_to_gtp(chosen.move)}")
            played_idx.append(chosen.move)
            cur = self._play(cur, chosen.move, x)
        must_answer = False
        if stop_reason == "max_plies":
            ae, _ = self._analyze(cur, visits, ownership=False, policy=False, spent=spent)
            if ae.candidates and ae.candidates[0].move is not None:
                g_end, _alt = self._second_best_gap(cur, ae, visits, spent)
                must_answer = g_end is None or g_end > forced_margin
        end, _g = self._features(cur, visits, persp, spent, must_answer=must_answer)
        best0 = a0.candidates[0] if a0.candidates else None
        out = {"start": {"position_ref": start.ref, "to_move": COLOR_CHAR[mover], "score_lead": round(sp * a0.score_lead, 2),
                         "best_move": idx_to_gtp(best0.move) if best0 else None},
               "move": {"color": COLOR_CHAR[mover], "move": idx_to_gtp(mv0),
                        "score_after": None if first_eval is None else round(sp * first_eval, 2),
                        "loss_vs_best": None if first_eval is None or best0 is None else
                        round(max(0.0, sign_of(mover) * (best0.score_lead - first_eval)), 2)},
               "perspective": COLOR_CHAR[persp], "line": line, "nodes": nodes, "stop_reason": stop_reason, "free_at_end": free,
               "end": end, "visits_used": spent.visits, "seconds_used": round(time.time() - t0, 2)}
        out["query_id"] = self._log(start.game_id, "forced_line", {"ref": start.ref, "move": move, "max_plies": max_plies}, spent.visits,
                                    time.time() - t0, False, {"line": line, "stop": stop_reason, "score_end": end["score_lead"]})
        return out

    # ================================================================ 1.20 intent_probe
    def intent_probe(self, position: dict, move: str, budget: dict | None = None, options: dict | None = None) -> dict:
        """What a move was for: what it threatened if ignored, what it prevented, the character of the best reply, and the belief it implies (needs_defending, group_is_safe, is_sente, behind_must_invade, ahead_can_coast, attack_works, biggest_move) with its evidence."""
        o = options or {}
        th = self.cfg.thresholds
        rec = self._resolve_position(position)
        visits, _ = self._visits_for(rec, budget, "line_node")
        size = rec.spec.size
        x = rec.to_move
        opp = opponent(x)
        sx = sign_of(x)
        g_idx = self._own_move(move, rec)
        if g_idx is None:
            raise ToolError("bad_request", "intent_probe needs a board move, not a pass")
        nr = int(o.get("neighborhood_radius", th.neighborhood_radius))
        t0 = time.time()
        spent = _Spent()

        def search(r: PositionRecord, ownership: bool = False, allow: list | None = None) -> Analysis:
            return self._analyze(r, visits, ownership=ownership, policy=False, allow=allow, spent=spent)[0]

        def top(a: Analysis):
            return a.candidates[0] if a.candidates else None

        def gown(gr, board: Board, own: list[float]) -> float:
            return round(group_mean_ownership(gr, own, board), 3)

        a_p = search(rec, ownership=True)
        e = top(a_p)
        e_idx = e.move if e else None
        rec_g = self._play(rec, g_idx, x)
        a_g = search(rec_g, ownership=True)
        s_g = a_g.score_lead
        r_idx = best_candidate(a_g)
        rc = reply_character(a_g, g_idx, size, th)
        # the opponent's best local answer, and X's best local follow-up if the opponent ignores the move:
        # the difference is the local swing, i.e. the size of what G threatened
        loc = near_empty(rec_g.board, g_idx, nr) + ["pass"]
        a_gloc = search(rec_g, allow=[{"player": COLOR_CHAR[opp], "moves": loc, "untilDepth": 1}])
        s_gloc = a_gloc.score_lead
        rec_gpass = self._play(rec_g, None, opp)
        a_gpass = search(rec_gpass, allow=[{"player": COLOR_CHAR[x], "moves": near_empty(rec_gpass.board, g_idx, nr) + ["pass"],
                                            "untilDepth": 1}])
        f = top(a_gpass)
        v_f = round(sx * (a_gpass.score_lead - s_gloc), 2) + 0.0
        tenuki_value = round(sx * (s_gloc - s_g), 2) + 0.0
        # what G prevented: X passes, the opponent plays its strongest move near G
        rec_ppass = self._play(rec, None, x)
        a_ppass = search(rec_ppass, allow=[{"player": COLOR_CHAR[opp], "moves": near_empty(rec.board, g_idx, nr) + ["pass"],
                                            "untilDepth": 1}])
        d_idx = best_candidate(a_ppass)
        defended = groups_near(rec.board, x, g_idx, th.defend_radius)
        groups_def, v_d = [], None
        if d_idx is not None:
            rec_pd = self._play(rec_ppass, d_idx, opp)
            a_pd = search(rec_pd, ownership=True)
            v_d = round(sx * (s_g - a_pd.score_lead), 2) + 0.0
            for gr in defended:
                groups_def.append({"label": group_label(gr, size), "anchor": idx_to_gtp(gr.anchor),
                                   "ownership_before": gown(gr, rec.board, a_p.ownership),
                                   "ownership_after_move": gown(gr, rec_g.board, a_g.ownership),
                                   "ownership_if_attacked": gown(gr, rec_pd.board, a_pd.ownership)})
        # groups left behind when the opponent's best reply is elsewhere
        left = []
        if r_idx is not None and rc and not rc["local"]:
            rec_gr = self._play(rec_g, r_idx, opp)
            a_gr = search(rec_gr, ownership=True)
            for gr in groups_near(rec.board, x, r_idx, th.defend_radius):
                left.append({"label": group_label(gr, size), "anchor": idx_to_gtp(gr.anchor),
                             "ownership_before": gown(gr, rec.board, a_p.ownership),
                             "ownership_after_reply": gown(gr, rec_gr.board, a_gr.ownership)})
        # was the better move gote too, and did anything change status between the two?
        e_gote, status_changes = None, None
        if e_idx is not None and e_idx != g_idx:
            rec_e = self._play(rec, e_idx, x)
            a_e = search(rec_e, ownership=True)
            rce = reply_character(a_e, e_idx, size, th)
            e_gote = bool(rce and not rce["local"])
            status_changes = [group_label(gr, size) for gr in rec.board.groups() if gr.size >= 3
                              and abs(gown(gr, rec_e.board, a_e.ownership) - gown(gr, rec_g.board, a_g.ownership))
                              >= th.group_change_min]
        # risk against the score
        cand_g = candidate_for(a_p, g_idx)
        stdev_g = round(cand_g.score_stdev if cand_g else a_g.score_stdev, 2)
        stdev_e = round(e.score_stdev, 2) if e else None
        lead = round(sx * a_p.score_lead, 2)
        profiles = self._profiles(["peer", "horizon"], rec)
        hp = {}
        for alias, prof in profiles.items():
            try:
                pol = self._human_policy(rec, prof)
                hp[alias] = {"move": round(pol[g_idx], 4), "best": round(pol[e_idx], 4) if e_idx is not None else None}
            except ToolError:
                pass
        # beliefs, most specific first
        matches: list[dict] = []
        if groups_def and all(gd["ownership_if_attacked"] > th.needs_defending_alive for gd in groups_def):
            matches.append({"id": "needs_defending", "evidence": f"after a pass, the opponent's strongest local move {idx_to_gtp(d_idx)} "
                            f"leaves {', '.join(gd['label'] for gd in groups_def)} owned "
                            f"{min(gd['ownership_if_attacked'] for gd in groups_def):.2f}; the move was worth {v_d} locally"})
        fallen = [lg for lg in left if lg["ownership_before"] >= th.safe_group_dead and lg["ownership_after_reply"] < th.safe_group_dead]
        if fallen:
            matches.append({"id": "group_is_safe", "evidence": f"the opponent's best reply {idx_to_gtp(r_idx)} attacks "
                            f"{', '.join(lg['label'] for lg in fallen)}: ownership falls to "
                            f"{min(lg['ownership_after_reply'] for lg in fallen):.2f}"})
        if rc and not rc["local"] and v_f >= th.sente_threat_min and tenuki_value >= th.tenuki_min:
            matches.append({"id": "is_sente", "evidence": f"the move threatened {idx_to_gtp(f.move) if f else '?'} (worth {v_f}), but "
                            f"the opponent gains {tenuki_value} more by playing {idx_to_gtp(r_idx)} instead of answering"})
        if stdev_e:
            if lead >= th.game_state_close and stdev_g >= th.risk_stdev_ratio * stdev_e:
                matches.append({"id": "behind_must_invade", "evidence": f"you were {lead} ahead and chose a move with score stdev "
                                f"{stdev_g} against {stdev_e} for the best move"})
            elif lead <= -th.game_state_close and stdev_g * th.risk_stdev_ratio <= stdev_e:
                matches.append({"id": "ahead_can_coast", "evidence": f"you were {-lead} behind and chose a move with score stdev "
                                f"{stdev_g} against {stdev_e} for the best move"})
        if rc and rc["character"] == "local_sharp" and not any(m["id"] == "needs_defending" for m in matches):
            matches.append({"id": "sequence_works", "evidence": f"the reply {idx_to_gtp(r_idx)} is local and sharp (worth "
                            f"{rc['gap']} more than playing elsewhere): the local sequence you read does not work; "
                            "expectation_probe finds the move you did not consider"})
        if rc and not rc["local"] and e_gote and not status_changes:
            matches.append({"id": "biggest_move", "evidence": f"both {idx_to_gtp(g_idx)} and {idx_to_gtp(e_idx)} are gote and no group "
                            f"changes status; {idx_to_gtp(e_idx)} is simply larger"})
        for m in matches:
            m["categories"] = BELIEF_CATEGORIES[m["id"]]
        cand_e_score = e.score_lead if e else a_p.score_lead
        out = {
            "position_ref": rec.ref, "player": COLOR_CHAR[x], "perspective": COLOR_CHAR[x],
            "move": idx_to_gtp(g_idx), "best_move": idx_to_gtp(e_idx) if e_idx is not None else None,
            "score": {"before": lead, "after_move": round(sx * s_g, 2), "after_best": round(sx * cand_e_score, 2),
                      "loss": round(max(0.0, sx * (cand_e_score - s_g)), 2)},
            "reply": rc,
            "threat": {"follow_up": idx_to_gtp(f.move) if f else None, "value": v_f,
                       "opponent_local_answer": idx_to_gtp(a_gloc.candidates[0].move) if a_gloc.candidates else None},
            "tenuki_value": tenuki_value,
            "defense": {"opponent_local_move": idx_to_gtp(d_idx) if d_idx is not None else None, "value": v_d, "groups": groups_def},
            "left_behind": left,
            "better_move": {"gote": e_gote, "groups_that_differ": status_changes},
            "risk": {"score_lead_before": lead, "stdev_move": stdev_g, "stdev_best": stdev_e, "human": hp},
            "belief": matches[0] if matches else None, "matches": [m["id"] for m in matches],
            "visits_used": spent.visits, "seconds_used": round(time.time() - t0, 2),
        }
        out["query_id"] = self._log(rec.game_id, "intent_probe", {"ref": rec.ref, "move": move}, spent.visits, time.time() - t0, False,
                                    {"belief": out["belief"]["id"] if out["belief"] else None, "matches": out["matches"]})
        return out

    # ================================================================ 1.21 expectation_probe
    def expectation_probe(self, position: dict, move: str, budget: dict | None = None, options: dict | None = None) -> dict:
        """Play the line the student expected (options.expected_line) or the one a player of their rank reads, check every move with the engine, and report the first move that loses more than misread_margin: the misread, the move never considered, and its refutation."""
        o = options or {}
        th = self.cfg.thresholds
        rec = self._resolve_position(position)
        visits, jid = self._visits_for(rec, budget, "line_node")
        size = rec.spec.size
        x = rec.to_move
        persp = self._perspective(o.get("perspective"), rec)
        sp = sign_of(persp)
        plan = self._active_plan(jid)
        default_plies = plan["verification"]["per_episode"].get("expectation_plies", 6) if plan else 6
        plies = int(o.get("plies", default_plies))
        margin = float(o.get("misread_margin", th.misread_margin))
        refute_plies = int(o.get("refutation_plies", EXPECTATION_REFUTATION_PLIES))
        prof_alias = o.get("profile", "peer")
        prof = self._profiles([prof_alias], rec)[prof_alias]
        expected = list(norm_list(o.get("expected_line")) or [])
        g_idx = self._own_move(move, rec)
        t0 = time.time()
        spent = _Spent()

        def search(r: PositionRecord, ownership: bool = False) -> Analysis:
            return self._analyze(r, visits, ownership=ownership, policy=False, spent=spent)[0]

        def score_of(a: Analysis, r: PositionRecord, mv: int | None) -> float:
            c = next((c for c in a.candidates if c.move == mv and c.visits >= th.acceptable_min_visit_share * max(1, a.visits)), None)
            if c is not None:
                return c.score_lead
            return search(self._play(r, mv)).score_lead

        a0 = search(rec)
        g_loss = round(max(0.0, sign_of(x) * (a0.candidates[0].score_lead - score_of(a0, rec, g_idx))), 2) if a0.candidates else None
        cur = self._play(rec, g_idx, x)
        nodes, misread, line = [], None, [f"{COLOR_CHAR[x]}{idx_to_gtp(g_idx)}"]
        for ply in range(1, plies + 1):
            c = cur.to_move
            sc = sign_of(c)
            if ply <= len(expected):
                step = norm_line_step(expected[ply - 1], c, size)
                if step.get("engine") or CHAR_COLOR.get(step["color"]) != c:
                    raise ToolError("bad_request", f"expected_line step {ply}: {expected[ply - 1]!r} is not a move for "
                                    f"{COLOR_CHAR[c]}, who is to move", {"ply": ply})
                mv = _gtp(step["move"], size, f"expected_line step {ply}", {"ply": ply})
                source = "stated"
            else:
                mv, _p = self._human_top(cur, prof)
                if mv is None:
                    break
                source = prof_alias
            a = search(cur)
            if not a.candidates:
                break
            best = a.candidates[0]
            try:
                s_m = score_of(a, cur, mv)
            except ToolError as e:
                e.details["ply"] = ply
                raise
            loss = round(max(0.0, sc * (best.score_lead - s_m)), 2)
            try:
                hprob = round(self._human_policy(cur, prof)[size * size if mv is None else mv], 4)
            except ToolError:
                hprob = None
            node = {"ply": ply, "color": COLOR_CHAR[c], "move": idx_to_gtp(mv), "source": source,
                    "engine_best": idx_to_gtp(best.move), "loss": loss, "human_probability": hprob,
                    "score_after": round(sp * s_m, 2)}
            nodes.append(node)
            if loss > margin and best.move != mv:
                ref_start = self._play(cur, best.move, c)
                ref_moves, ref_end, _a = self._engine_continuation(ref_start, refute_plies, visits, spent)
                a_end = search(ref_end, ownership=True)
                races = capture_races(ref_end.board, a_end.ownership, th)
                misread = {"ply": ply, "color": COLOR_CHAR[c], "whose": "you" if c == x else "opponent",
                           "expected": idx_to_gtp(mv), "never_considered": idx_to_gtp(best.move), "loss": loss,
                           "refutation": [f"{COLOR_CHAR[c]}{idx_to_gtp(best.move)}"] + ref_moves,
                           "refutation_end": {"position_ref": ref_end.ref, "score_lead": round(sp * a_end.score_lead, 2),
                                              "capture_races": races},
                           "line_to_here": list(line)}
                break
            line.append(f"{COLOR_CHAR[c]}{idx_to_gtp(mv)}")
            cur = self._play(cur, mv, c)
        out = {"position_ref": rec.ref, "perspective": COLOR_CHAR[persp], "profile": prof,
               "move": {"color": COLOR_CHAR[x], "move": idx_to_gtp(g_idx), "loss_vs_best": g_loss},
               "expected_source": "stated" if expected and len(expected) >= len(nodes) else ("mixed" if expected else prof_alias),
               "nodes": nodes, "line": line, "misread": misread,
               "note": None if misread else f"the line a {prof} player reads holds for {len(nodes)} plies; look for the belief "
                                            "in intent_probe (value, not reading)",
               "visits_used": spent.visits, "seconds_used": round(time.time() - t0, 2)}
        out["query_id"] = self._log(rec.game_id, "expectation_probe", {"ref": rec.ref, "move": move, "expected_line": expected},
                                    spent.visits, time.time() - t0, False,
                                    {"misread_ply": misread["ply"] if misread else None,
                                     "never_considered": misread["never_considered"] if misread else None})
        return out

    # ================================================================ 1.17 validate_variations
    def validate_variations(self, job_id: str, episodes: list, summary: dict | None = None, options: dict | None = None) -> dict:
        """Validate lesson branches and quizzes against the game (legality, colors, evaluations) and export the checksummed dashboard data blob."""
        o = options or {}
        with _engine_errors():
            job = self.jobs.get(job_id)
        if job.ga is None or job.state != "done":
            raise ToolError("job_not_finished", "the survey must be complete before exporting")
        ga = job.ga
        ga.build_boards()
        student = ga.student_color if ga.student_color is not None else BLACK
        v = _Validation(job, self._budget_visits(o.get("budget"), job_id, "line_node"), student, o.get("evaluate_missing", True))
        t0 = time.time()
        eps_out = []
        for ep in episodes:
            eid = ep.get("id", "E?")
            root_n = int(ep["moves"][0]) if ep.get("moves") else None
            if root_n is None or not 1 <= root_n <= ga.M:
                v.errors.append({"episode_id": eid, "code": "bad_from_move", "message": "episode moves range missing or out of game"})
                continue
            for n in (root_n - 1, root_n):
                if ga.positions[n].ownership is not None:
                    v.ownership[f"m{n}"] = encode_ownership(ga.positions[n].ownership)
            branches_out, branch_ends = [], {}
            for br in ep.get("branches", []):
                done = self._vv_branch(v, br, eid, root_n, branches_out)
                if done is not None:
                    branches_out.append(done[0])
                    branch_ends[done[0]["id"]] = done[1]
            comparison_out = self._vv_comparison(v, ep["comparison"], eid, branches_out, branch_ends) if ep.get("comparison") else None
            quiz_out = self._vv_quiz(v, ep["quiz"], eid, root_n) if ep.get("quiz") else None
            eps_out.append({"id": eid, "moves": ep.get("moves"), "title": ep.get("title", ""), "category": ep.get("category", ""),
                            "tags": ep.get("tags", []), "pointsLost": ep.get("points_lost"),
                            "commentary": [{"atMove": int(c["at_move"]), "text": c["text"]} for c in ep.get("commentary", [])],
                            "branches": branches_out, "quiz": quiz_out, "principle": ep.get("principle", ""), "cue": ep.get("cue", ""),
                            "ruleCheck": ep.get("rule_check", ""), "belief": ep.get("belief"), "comparison": comparison_out})
        valid = not v.errors
        out = {"valid": valid, "errors": v.errors, "warnings": v.warnings, "visits_used": v.spent.visits,
               "seconds_used": round(time.time() - t0, 2)}
        if valid:
            data = self._vv_data(v, job_id, eps_out, summary)
            blob = canonical(data)
            out["dashboard_data"] = blob
            out["sha256"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()
            out["size_bytes"] = len(blob.encode("utf-8"))
            n = len(list(self.store.game_dir(job.game_id).glob("export-*.json"))) + 1
            self.store.write_json(job.game_id, f"export-{n}.json", {"sha256": out["sha256"], "data": data})
        out["query_id"] = self._log(job.game_id, "validate_variations", {"episodes": len(episodes)}, v.spent.visits, time.time() - t0, False,
                                    {"valid": valid, "errors": len(v.errors)})
        return out

    def _vv_branch(self, v: "_Validation", br: dict, eid: str, root_n: int, earlier: list[dict]) -> tuple[dict, PositionRecord] | None:
        """One lesson branch replayed and evaluated: (exported branch, end position), or None after recording its error."""
        job, ga, errors = v.job, v.job.ga, v.errors
        size, M = ga.size, ga.M
        bid = br.get("id", "B?")
        parent, at_ply, prefix = None, None, []
        if br.get("from_branch"):
            parent = next((b for b in earlier if b["id"] == br["from_branch"]), None)
            if parent is None:
                errors.append({"episode_id": eid, "branch_id": bid, "code": "bad_branch_parent",
                               "message": f"from_branch {br['from_branch']!r} is not an earlier valid branch of {eid}"})
                return None
            at_ply = int(br.get("at_ply", 0))
            if not 0 <= at_ply <= len(parent["moves"]):
                errors.append({"episode_id": eid, "branch_id": bid, "code": "bad_branch_parent",
                               "message": f"at_ply {at_ply} outside 0..{len(parent['moves'])} of {parent['id']}"})
                return None
            fm = parent["fromMove"]
            prefix = parent["moves"][:at_ply]
        else:
            fm = int(br.get("from_move", root_n - 1))
        if not 0 <= fm <= M:
            errors.append({"episode_id": eid, "branch_id": bid, "code": "bad_from_move", "message": f"from_move {fm} outside 0..{M}"})
            return None
        spec = self.jobs.spec_at(job.game, fm)
        board = ga.boards[fm]
        cur = self.store.put_position(spec, job.game_id, fm, persist=False)
        evals, moves_out, a = [], [], None
        s = sign_of(v.student)
        for ply, m in enumerate(prefix + list(br.get("moves", [])), 1):
            try:
                color, idx = parse_move(m, size)
            except CoordError as e:
                errors.append({"episode_id": eid, "branch_id": bid, "ply": ply, "move": str(m), "code": "illegal_move", "message": str(e)})
                return None
            if color != board.to_move:
                errors.append({"episode_id": eid, "branch_id": bid, "ply": ply, "move": str(m), "code": "wrong_color",
                               "message": f"{COLOR_CHAR[color]} played but {COLOR_CHAR[board.to_move]} is to move"})
                return None
            try:
                board = board.play(color, idx, spec.rules)
            except IllegalMove as e:
                errors.append({"episode_id": eid, "branch_id": bid, "ply": ply, "move": str(m), "code": "illegal_move", "message": e.reason})
                return None
            spec = PositionSpec(spec.size, spec.rules, spec.komi, spec.setup_black, spec.setup_white,
                                spec.moves + [(color, idx)], spec.first_to_move)
            cur = self.store.put_position(spec, job.game_id, None, persist=False)
            a = self._analyze(cur, v.visits, ownership=True, policy=False, spent=v.spent)[0] if v.evaluate \
                else self.store.get_cached(cur.ref, 1)
            if a is None:
                errors.append({"episode_id": eid, "branch_id": bid, "ply": ply, "move": str(m), "code": "unknown_query",
                               "message": "no cached evaluation and evaluate_missing is false"})
                return None
            evals.append(round(s * a.score_lead, 1))
            moves_out.append(f"{COLOR_CHAR[color]}{idx_to_gtp(idx, size)}")
        if len(moves_out) <= len(prefix):
            errors.append({"episode_id": eid, "branch_id": bid, "code": "illegal_move", "message": "branch has no moves"})
            return None
        game_cont = [f"{COLOR_CHAR[c]}{idx_to_gtp(i, size)}" for c, i in ga.moves[fm:fm + len(moves_out)]]
        if game_cont == moves_out and parent is None:
            v.warnings.append(f"branch {bid} of {eid} never diverges from the game")
        if a.ownership is not None:
            v.ownership[f"{eid}:{bid}:end"] = encode_ownership(a.ownership)
        bo = {"id": bid, "label": br.get("label", bid), "fromMove": fm, "moves": moves_out, "evals": evals,
              "ownershipAtEnd": f"{eid}:{bid}:end" if a.ownership is not None else None,
              "ledgerRef": br.get("ledger_ref"), "kind": br.get("kind")}
        if parent is not None:
            bo["parentBranch"], bo["branchPly"] = parent["id"], at_ply
        return bo, cur

    def _vv_comparison(self, v: "_Validation", cmp_spec: dict, eid: str, branches_out: list[dict],
                       branch_ends: dict[str, PositionRecord]) -> dict | None:
        """The end-position comparison of two branches of an episode (dashboard shape)."""
        ids = (cmp_spec.get("a"), cmp_spec.get("b"))
        missing = [i for i in ids if i not in branch_ends]
        if missing:
            v.errors.append({"episode_id": eid, "code": "bad_comparison",
                             "message": f"comparison names {missing}, which are not valid branches of {eid}"})
            return None
        fa, ga_g = self._features(branch_ends[ids[0]], v.visits, v.student, v.spent)
        fb, gb_g = self._features(branch_ends[ids[1]], v.visits, v.student, v.spent)
        c = self._compare_features(fa, ga_g, fb, gb_g, self.cfg.thresholds)
        labels = {b["id"]: b["label"] for b in branches_out}
        return {"a": ids[0], "b": ids[1], "aLabel": labels[ids[0]], "bLabel": labels[ids[1]],
                "scoreDiff": c["score_diff"],
                "groups": [{"group": g["group"], "a": g["in_a"], "b": g["in_b"]} for g in c["groups_changed"]],
                "territory": [{"label": r["label"], "a": r["a"], "b": r["b"]} for r in c["territory_changed"]],
                "territoryTotal": c["territory_total"], "sente": {"a": c["sente"]["a"], "b": c["sente"]["b"]},
                "nextMove": {k: {"side": t["side_to_move"], "move": t["best_move"], "value": t["value"]} for k, t in c["tempo"].items()},
                "weakGroups": c["weak_groups"]}

    def _vv_quiz(self, v: "_Validation", q: dict, eid: str, root_n: int) -> dict | None:
        """A quiz with the points lost of every candidate (the actual move by the survey's own definition)."""
        job, ga, errors = v.job, v.job.ga, v.errors
        size, M = ga.size, ga.M
        at = int(q.get("at_move", root_n))
        if not 1 <= at <= M:
            errors.append({"episode_id": eid, "code": "bad_quiz", "message": f"quiz at_move {at} outside 1..{M}"})
            return None
        before = ga.positions[at - 1]
        mover, actual_idx = ga.moves[at - 1]
        sm = sign_of(mover)
        best = before.candidates[0] if before.candidates else None
        best_score = best.score_lead if best else before.score_lead
        peer_prof = ga.profiles.get("peer")
        peer_idx = None
        if peer_prof and peer_prof in before.human:
            pol = before.human[peer_prof]
            peer_idx = max(range(size * size), key=lambda i: pol[i])
        cand_pts = list(dict.fromkeys(list(q.get("candidates", [])) + [idx_to_gtp(actual_idx, size)] +
                                      ([idx_to_gtp(peer_idx, size)] if peer_idx is not None else [])))
        cands = []
        for pt in cand_pts:
            try:
                ci = gtp_to_idx(pt, size)
            except CoordError:
                errors.append({"episode_id": eid, "code": "bad_quiz", "message": f"bad candidate {pt}"})
                continue
            c = candidate_for(before, ci)
            if ci == actual_idx:
                sc = ga.positions[at].score_lead          # same definition as the survey's points lost
            elif c is not None:
                sc = c.score_lead
            else:
                spec = self.jobs.spec_at(job.game, at - 1)
                spec.moves.append((mover, ci))
                try:
                    rec = self.store.put_position(spec, job.game_id, None, persist=False)
                except IllegalMove as e:
                    errors.append({"episode_id": eid, "code": "bad_quiz", "message": f"candidate {pt} illegal: {e.reason}"})
                    continue
                sc = self._analyze(rec, v.visits, ownership=False, policy=False, spent=v.spent)[0].score_lead
            labels = []
            if ci == actual_idx:
                labels.append("actual")
            if ci == peer_idx:
                labels.append("peer")
            if best and ci == best.move:
                labels.append("best")
            cands.append({"move": pt, "pointsLost": round(max(0.0, sm * (best_score - sc)), 1), "labels": labels, "note": ""})
        quiz_out = {"atMove": at, "type": q.get("type", "move"), "candidates": cands,
                    "actual": idx_to_gtp(actual_idx, size), "peerMove": idx_to_gtp(peer_idx, size) if peer_idx is not None else None}
        if q.get("type") == "status":
            st = q.get("status") or {}
            res = self.solve_results.get(st.get("solve_query_id", ""))
            if res is None:
                errors.append({"episode_id": eid, "code": "unknown_query", "message": f"solve_query_id {st.get('solve_query_id')!r} not found"})
            else:
                quiz_out["status"] = {"groupPoint": st.get("group_point"), "answer": res["status"], "confidence": res["confidence"]}
        return quiz_out

    def _vv_data(self, v: "_Validation", job_id: str, eps_out: list[dict], summary: dict | None) -> dict:
        """The dashboard data blob (checksummed as canonical JSON by the caller)."""
        th = self.cfg.thresholds
        game, ga = v.job.game, v.job.ga
        size = ga.size
        s = sign_of(v.student)
        you = COLOR_CHAR[v.student]
        opp = COLOR_CHAR[opponent(v.student)]
        rows = move_rows(ga, th)
        dec, lc = decisive_and_last_chance(ga, rows, th)
        return {
            "meta": {"game_id": ga.game_id, "job_id": job_id, "visits_per_move": ga.visits_per_move, "server_version": __version__,
                     "contract_version": CONTRACT_VERSION, "exported_at": timestamp()},
            "game": {"id": ga.game_id, "date": game.date, "you": you, "opponent": game.players[opp]["name"],
                     "opponentRank": game.players[opp].get("rank"), "yourRank": game.players[you].get("rank"),
                     "handicap": ga.handicap, "komi": ga.komi, "rules": ga.rules, "result": ga.result.get("raw", ""),
                     "players": game.players},
            "setup": {"AB": [idx_to_gtp(i, size) for i in ga.setup_black], "AW": [idx_to_gtp(i, size) for i in ga.setup_white]},
            "moves": [f"{COLOR_CHAR[c]}{idx_to_gtp(i, size)}" for c, i in ga.moves],
            "scoreSeries": [round(s * a.score_lead, 1) for a in ga.positions],
            "phases": phases_fn(ga, th), "decisive": dec, "lastChance": lc,
            "episodes": eps_out, "ownership": v.ownership, "summary": summary or {},
        }

    def wait_for_job(self, job_id: str, poll: float = 0.5, on_progress=None, timeout: float | None = None) -> dict:
        """Block until a survey job is done, failed or cancelled (or `timeout` passes); returns its last status.
        For the CLI, seeding and tests; `on_progress(status)` sees every status polled."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            st = self.job_status(job_id)
            if on_progress is not None:
                on_progress(st)
            if st["state"] in ("done", "failed", "cancelled") or (deadline is not None and time.time() >= deadline):
                return st
            time.sleep(poll)

    def close(self) -> None:
        try:
            self.engine.stop()
        except Exception:
            pass


# ==================================================================== lenient inputs
def norm_color(c) -> str | None:
    """'B'/'W' from 'b', 'black', 'White', 1/2, etc."""
    if c is None:
        return None
    if isinstance(c, int):
        return {1: "B", 2: "W"}.get(c)
    t = str(c).strip().lower()
    return {"b": "B", "black": "B", "w": "W", "white": "W"}.get(t)


def norm_position(spec) -> dict:
    """Accept a dict, a 'pos_…' ref string, an OGS id/link or file path (-> sgf), or raw SGF text."""
    if isinstance(spec, dict):
        return spec
    if isinstance(spec, str):
        t = spec.strip()
        if t.startswith("pos_"):
            return {"ref": t}
        if t.startswith("job_"):
            return {"job_id": t, "move_number": 0}
        return {"sgf": t}
    raise ToolError("bad_request", "position must be an object such as {\"ref\": …} or {\"job_id\": …, \"move_number\": n}")


def norm_budget(b) -> dict | None:
    """Accept {visits}/{seconds}/{profile}, a bare number (visits), or a profile name string."""
    if b is None or isinstance(b, dict):
        return b
    if isinstance(b, bool):
        return None
    if isinstance(b, (int, float)):
        return {"visits": int(b)}
    if isinstance(b, str):
        t = b.strip()
        if t.isdigit():
            return {"visits": int(t)}
        return {"profile": t.lower()}
    raise ToolError("bad_request", "budget must be an object like {\"profile\": \"root\"} or {\"visits\": 1000}")


def norm_list(x) -> list | None:
    if x is None:
        return None
    if isinstance(x, (list, tuple)):
        return list(x)
    if isinstance(x, str):
        parts = [p for p in re.split(r"[,\s]+", x.strip()) if p]
        return parts
    return [x]


def norm_line_step(step, to_move: int, size: int = 19) -> dict:
    """A line step may be {'color','move'}, {'engine': true}, 'engine', 'BQ7', 'WQ7', 'Q7', 'pass', ['B','Q7']."""
    if isinstance(step, dict):
        if step.get("engine") or step.get("type") == "engine":
            return {"engine": True, **({"color": norm_color(step["color"])} if step.get("color") else {})}
        color = norm_color(step.get("color")) or COLOR_CHAR[to_move]
        mv = step.get("move") or step.get("point")
        if mv is None:
            raise ToolError("bad_request", f"line step needs a move: {step!r}")
        return {"color": color, "move": str(mv).strip()}
    if isinstance(step, (list, tuple)) and len(step) == 2:
        return {"color": norm_color(step[0]) or COLOR_CHAR[to_move], "move": str(step[1]).strip()}
    if isinstance(step, str):
        t = step.strip()
        if t.lower() in ("engine", "*", "?", "best", "engine_reply", "reply"):
            return {"engine": True}
        m = re.fullmatch(r"([BbWw])\s*[:\-]?\s*([A-Ta-t](?:1[0-9]|[1-9])|pass)", t)
        if m:
            return {"color": norm_color(m.group(1)), "move": m.group(2)}
        if re.fullmatch(r"[A-Ta-t](?:1[0-9]|[1-9])|pass", t, re.I):
            return {"color": COLOR_CHAR[to_move], "move": t}
    raise ToolError("bad_request", f"cannot read line step {step!r}; use {{\"color\": \"B\", \"move\": \"Q7\"}} or {{\"engine\": true}}")


# ==================================================================== helpers
def _row_out(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in ("idx", "best_idx", "acceptable")}


def _summarize(args: dict) -> dict:
    out = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 200:
            out[k] = v[:80] + f"...({len(v)} chars)"
        elif isinstance(v, list) and len(v) > 20:
            out[k] = f"list[{len(v)}]"
        else:
            out[k] = v
    return out
