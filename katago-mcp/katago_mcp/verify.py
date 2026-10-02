"""Background verification (tool contract §1.22–§1.24) and the stored results of the probes (§0.8).

After triage, `start_verification` queues the probes of the belief protocol that do not need the student's
interview answer; they run on one worker thread at a KataGo priority below Claude's own calls while Claude
interviews the student. `record_interview` unseals an episode and queues the probes that need the answer.
Every probe goes through the `Tools` method Claude would call itself, so its result lands in the result
store and a later identical call returns it at once. Results of a selected episode are sealed (the store
refuses them to Claude, `verification_results` withholds them) until its interview is recorded.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .budget import INTENT_SEARCHES, expectation_searches, forced_line_searches, student_line_searches
from .store import timestamp

if TYPE_CHECKING:
    from .jobs import Job
    from .tools import Tools

log = logging.getLogger("katago_mcp")

# Task kinds in the order an episode's work runs; "answer" kinds need the interview.
TASK_ORDER = ("intent_probe", "forced_line_best", "forced_line_played", "terminal_features", "root", "stability",
              "supporting_test", "local_solve", "expectation_probe", "stability_misread", "fix", "line")
ANSWER_KINDS = {"expectation_probe", "stability_misread", "fix", "line"}
SPECULATIVE_KINDS = ("intent_probe", "forced_line_best", "forced_line_played", "terminal_features", "root", "stability")
TERMINAL = ("done", "failed", "skipped", "cancelled")


# ==================================================================== result store
@dataclass
class StoredResult:
    result: dict
    tag: tuple[str, str] | None      # (job_id, episode id) of the background task that computed it
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


# ==================================================================== verification
@dataclass
class Task:
    kind: str
    name: str                         # kind, or "fix:D6" / "line:<label>" / "stability_x16"
    run: Callable[["Task"], dict | None]
    deps: tuple[str, ...] = ()
    params: dict = field(default_factory=dict)
    state: str = "pending"            # pending | running | done | failed | skipped | cancelled
    result: dict | None = None
    error: dict | None = None
    note: str | None = None
    sizes: tuple | None = None        # the search sizes it ran at (a re-plan with other sizes re-queues it)
    seq: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    ep: "Episode | None" = field(default=None, repr=False)

    @property
    def answer(self) -> bool:
        return self.kind in ANSWER_KINDS


@dataclass
class Episode:
    job_id: str
    id: str
    move_number: int
    played: str
    teachable: str
    before: str                       # position refs before and after the played move
    after: str
    order: int = 0
    selected: bool = False            # False: speculative (started when the survey finished)
    interviewed: bool = False
    local_solve: dict | bool | None = None
    interview: dict = field(default_factory=lambda: {"answers": [], "expected_line": None, "fixes": [], "lines": []})
    tasks: "OrderedDict[str, Task]" = field(default_factory=OrderedDict)

    @property
    def sealed(self) -> bool:
        return self.selected and not self.interviewed

    def state(self) -> str:
        states = [t.state for t in self.tasks.values()]
        if any(s == "running" for s in states):
            return "running"
        if any(s == "pending" for s in states):
            return "queued"
        return "done"


class VerificationManager:
    def __init__(self, tools: "Tools"):
        self.tools = tools
        self.cfg = tools.cfg.verification
        self.episodes: dict[str, "OrderedDict[str, Episode]"] = {}     # job_id -> episodes
        self._cv = threading.Condition(threading.RLock())
        self._seq = 0
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.running_task: Task | None = None

    # ---------------------------------------------------------------- sealing
    def sealed(self, tag: tuple[str, str] | None) -> bool:
        if tag is None:
            return False
        with self._cv:
            ep = self.episodes.get(tag[0], {}).get(tag[1])
            return ep is not None and ep.sealed

    def sealed_at(self, ref: str, tag: tuple[str, str] | None) -> str | None:
        """The sealed episode whose position before or after the played move is `ref` (a probe there would
        reveal its verdict), unless the caller is that episode's own background task."""
        with self._cv:
            for job_id, run in self.episodes.items():
                for ep in run.values():
                    if ep.sealed and ref in (ep.before, ep.after) and tag != (job_id, ep.id):
                        return ep.id
        return None

    # ---------------------------------------------------------------- starting work
    def speculate(self, job: "Job") -> None:
        """Survey done: precompute the answer-free probes of the top episodes while the blind self-review
        goes on (nothing is shown). Needs a plan for the job, so batch seeding and the CLI never do this."""
        n = int(self.cfg.speculative_episodes)
        if n <= 0 or job.plan is None or job.ga is None:
            return
        with self._cv:
            if any(e.selected for e in self.episodes.get(job.job_id, {}).values()):
                return
        try:
            d = self.tools.jobs.digest(job.job_id, max_episodes=10, include_positives=False)
        except Exception:  # noqa: BLE001
            log.exception("speculative verification: no digest")
            return
        eps = sorted(d.get("episodes", []), key=lambda e: -(e["root"].get("points_lost") or 0))[:n]
        with self._cv:
            run = self.episodes.setdefault(job.job_id, OrderedDict())
            for k, e in enumerate(eps):
                if e["id"] in run or not e["root"].get("played"):
                    continue
                ep = self._episode_from_digest(job.job_id, e, None)
                ep.order = k
                run[ep.id] = ep
                for kind in SPECULATIVE_KINDS:
                    self._add_answer_free(ep, kind)
            self._wake()

    def start(self, job: "Job", specs: list) -> dict:
        if job.ga is None or job.state != "done":
            raise self._error("job_not_finished", "the survey must be complete before verification starts")
        if not specs:
            raise self._error("bad_request", "episodes must name at least one episode")
        digest = {e["id"]: e for e in self.tools.jobs.digest(job.job_id, max_episodes=10, include_positives=False)["episodes"]}
        wanted = []
        for spec in specs:
            spec = {"id": spec} if isinstance(spec, str) else dict(spec)
            eid = spec.get("id")
            if not eid:
                raise self._error("bad_request", "each episode needs an id")
            wanted.append((spec, digest.get(eid)))
        # interview order: the episodes with a local solve first (their long work starts first), else as given
        wanted.sort(key=lambda w: 0 if w[0].get("local_solve") else 1)
        out_eps = []
        with self._cv:
            run = self.episodes.setdefault(job.job_id, OrderedDict())
            selected_ids = set()
            for order, (spec, dig) in enumerate(wanted):
                new = self._episode_from_spec(job, spec, dig)
                old = run.get(new.id)
                if old is not None and (old.move_number, old.played) == (new.move_number, new.played):
                    ep = old
                    if ep.teachable != new.teachable:
                        for name in ("forced_line_best", "terminal_features"):
                            self._drop(ep, name)
                        ep.teachable = new.teachable
                    ep.local_solve = new.local_solve if new.local_solve is not None else ep.local_solve
                else:
                    if old is not None:
                        self._cancel_episode(old)
                    ep = new
                    run[ep.id] = ep
                if not ep.selected:
                    ep.selected = True
                ep.order = order
                selected_ids.add(ep.id)
                self._requeue_resized(ep)
                for kind in ("intent_probe", "forced_line_best", "forced_line_played", "terminal_features", "root",
                             "stability", "supporting_test", "local_solve"):
                    self._add_answer_free(ep, kind)
                out_eps.append(ep)
            for ep in run.values():
                if not ep.selected and ep.id not in selected_ids:
                    self._cancel_episode(ep)          # speculative episodes triage did not pick
            self._wake()
            eta = self._eta(job.job_id)
            reused = {ep.id: [t.name for t in ep.tasks.values() if t.state == "done"] for ep in out_eps}
        return {"job_id": job.job_id, "interview_order": [ep.id for ep in out_eps],
                "episodes": [self._episode_view(ep, with_results=False) for ep in out_eps],
                "already_computed": {k: v for k, v in reused.items() if v},
                "eta_seconds": eta, "sealed": True,
                "note": "results are sealed per episode until record_interview; interview in interview_order"}

    def interview(self, job: "Job", eid: str, answer: str | None, expected_line, fix, lines, confidence) -> dict:
        t = self.tools
        with self._cv:
            ep = self._selected(job.job_id, eid)
        before = t._resolve_position({"ref": ep.before})
        after = t._resolve_position({"ref": ep.after})
        # validate everything before changing state, so a bad line can be asked again
        exp = t._normalize_line(after, expected_line, "expected_line") if expected_line else None
        fixes = []
        for f in ([fix] if isinstance(fix, (str, dict)) else list(fix or [])):
            mv = t._normalize_line(before, [f], "fix")[0]
            fixes.append(mv[1:])
        line_specs = []
        for k, ln in enumerate(lines or [], 1):
            ln = {"moves": ln} if isinstance(ln, (list, str)) else dict(ln)
            frm = ln.get("from", "before")
            if frm not in ("before", "after"):
                raise self._error("bad_request", f"lines[{k}].from must be 'before' or 'after'")
            moves = t._normalize_line(before if frm == "before" else after, ln.get("moves"), f"lines[{k}]")
            line_specs.append({"label": str(ln.get("label") or f"L{len(ep.interview['lines']) + k}"), "from": frm,
                               "moves": moves})
        with self._cv:
            first = not ep.interviewed
            ep.interviewed = True
            if answer is not None or confidence is not None:
                ep.interview["answers"].append({"answer": answer, "confidence": confidence, "at": timestamp()})
            if first or exp is not None:
                if exp is not None and "expectation_probe" in ep.tasks:
                    old = ep.tasks["expectation_probe"]
                    if old.state == "done" and old.params.get("line") is None:
                        old.name = "expectation_probe_peer"          # keep the peer-read version
                        ep.tasks["expectation_probe_peer"] = ep.tasks.pop("expectation_probe")
                    else:
                        self._drop(ep, "expectation_probe")
                    self._drop(ep, "stability_misread")
                if exp is not None:
                    ep.interview["expected_line"] = exp
                if "expectation_probe" not in ep.tasks:
                    self._add(ep, Task("expectation_probe", "expectation_probe", self._run_expectation,
                                       params={"line": ep.interview["expected_line"]}))
                    self._add(ep, Task("stability_misread", "stability_misread", self._run_stability_misread,
                                       deps=("expectation_probe",)))
            for mv in fixes:
                if mv not in ep.interview["fixes"]:
                    ep.interview["fixes"].append(mv)
                    self._add(ep, Task("fix", f"fix:{mv}", self._run_fix, params={"move": mv}))
            for ls in line_specs:
                self._drop(ep, f"line:{ls['label']}")
                ep.interview["lines"] = [x for x in ep.interview["lines"] if x["label"] != ls["label"]] + [ls]
                self._add(ep, Task("line", f"line:{ls['label']}", self._run_line, params=ls))
            self._wake()
            queued = [x.name for x in ep.tasks.values() if x.state in ("pending", "running")]
            return {"job_id": job.job_id, "episode": ep.id, "sealed": False, "first_answer": first,
                    "expected_line": ep.interview["expected_line"], "fixes": list(ep.interview["fixes"]),
                    "lines": [{"label": x["label"], "from": x["from"], "moves": x["moves"]} for x in ep.interview["lines"]],
                    "queued": queued, "eta_seconds": self._eta(job.job_id, ep.id)}

    # ---------------------------------------------------------------- reading
    def results(self, job_id: str, eid: str | None, wait_seconds: float, parts: list | None) -> dict:
        wait = max(0.0, min(float(wait_seconds or 0), float(self.cfg.max_wait_seconds)))
        deadline = time.time() + wait
        with self._cv:
            run = self.episodes.get(job_id)
            if not run:
                raise self._error("verification_not_started", f"no background verification for {job_id}",
                                  suggestion="call start_verification after triage")
            if eid is None:
                def shown():
                    return [e for e in run.values() if e.selected] or list(run.values())
                while wait and any(e.state() != "done" for e in shown()) and time.time() < deadline:
                    self._cv.wait(max(0.0, deadline - time.time()))
                eps = shown()
                return {"job_id": job_id, "episodes": [self._episode_view(e, with_results=False) for e in eps],
                        "speculative": [e.id for e in run.values() if not e.selected and e.state() != "done"],
                        "running": None if self.running_task is None else self.running_task.name,
                        "eta_seconds": self._eta(job_id), "state": "done" if all(e.state() == "done" for e in eps) else "running"}
            ep = run.get(eid)
            if ep is None:
                raise self._error("episode_not_found", f"{eid} is not in the verification of {job_id}",
                                  suggestion="start_verification with this episode")
            while wait and ep.state() != "done" and time.time() < deadline:
                self._cv.wait(max(0.0, deadline - time.time()))
            view = self._episode_view(ep, with_results=not ep.sealed, parts=parts)
            view["eta_seconds"] = self._eta(job_id, ep.id)
            if ep.sealed:
                view["note"] = "sealed until record_interview: no engine result about this episode may be shown yet"
            return view

    def cancel(self, job_id: str) -> dict:
        with self._cv:
            n = 0
            for ep in self.episodes.get(job_id, {}).values():
                n += self._cancel_episode(ep)
            self._cv.notify_all()
        return {"job_id": job_id, "cancelled_tasks": n}

    def forget(self, job_id: str) -> None:
        with self._cv:
            for ep in self.episodes.pop(job_id, {}).values():
                self._cancel_episode(ep)
            self._cv.notify_all()

    def precomputed(self, job_id: str, ids: list[str]) -> int:
        """How many of these episodes have answer-free results already computed."""
        with self._cv:
            run = self.episodes.get(job_id, {})
            return sum(1 for i in ids if i in run and any(t.state == "done" for t in run[i].tasks.values()))

    def stop(self, timeout: float = 30.0) -> None:
        """Cancel queued work and wait for the running probe to finish (the engine stops after this)."""
        self._stop.set()
        with self._cv:
            for run in self.episodes.values():
                for ep in run.values():
                    self._cancel_episode(ep)
            self._cv.notify_all()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout)

    # ---------------------------------------------------------------- episodes and tasks
    def _episode_from_digest(self, job_id: str, e: dict, spec: dict | None) -> Episode:
        r = e["root"]
        teach = (spec or {}).get("teachable") or e.get("teachable_move_preliminary") or r["best"]
        return Episode(job_id, e["id"], int(r["move"]), r["played"], teach, r["position_ref_before"], r["position_ref_after"],
                       local_solve=(spec or {}).get("local_solve"))

    def _episode_from_spec(self, job: "Job", spec: dict, dig: dict | None) -> Episode:
        from .coords import idx_to_gtp
        t = self.tools
        if dig is not None and (spec.get("move_number") in (None, dig["root"]["move"])):
            ep = self._episode_from_digest(job.job_id, dig, spec)
        else:
            n = spec.get("move_number")
            if n is None:
                raise self._error("episode_not_found", f"{spec['id']} is not a digest episode; give its move_number")
            n = int(n)
            ga = job.ga
            if not 1 <= n <= ga.M:
                raise self._error("bad_request", f"move_number must be 1..{ga.M}")
            before = ga.positions[n - 1]
            played = idx_to_gtp(ga.moves[n - 1][1])
            best = idx_to_gtp(before.candidates[0].move) if before.candidates else played
            ep = Episode(job.job_id, spec["id"], n, played, spec.get("teachable") or best, ga.refs[n - 1], ga.refs[n],
                         local_solve=spec.get("local_solve"))
        if spec.get("teachable"):
            t._normalize_line(t._resolve_position({"ref": ep.before}), [spec["teachable"]], "teachable")
        ls = ep.local_solve
        if isinstance(ls, dict) and not ls.get("group_point"):
            raise self._error("bad_request", f"{ep.id}: local_solve needs group_point (or true for the automatic choice)")
        return ep

    def _selected(self, job_id: str, eid: str) -> Episode:
        ep = self.episodes.get(job_id, {}).get(eid)
        if ep is None or not ep.selected:
            raise self._error("episode_not_selected", f"{eid} was not started with start_verification for {job_id}",
                              suggestion="call start_verification with the triaged episodes first")
        return ep

    def _add(self, ep: Episode, task: Task) -> None:
        if task.name in ep.tasks:
            return
        self._seq += 1
        task.seq = self._seq
        task.ep = ep
        ep.tasks[task.name] = task

    def _add_answer_free(self, ep: Episode, kind: str) -> None:
        if kind == "intent_probe":
            self._add(ep, Task(kind, kind, lambda tk: self.tools.intent_probe({"ref": ep.before}, ep.played)))
        elif kind == "forced_line_best":
            self._add(ep, Task(kind, kind, lambda tk: self.tools.forced_line({"ref": ep.before}, ep.teachable)))
        elif kind == "forced_line_played":
            self._add(ep, Task(kind, kind, lambda tk: self.tools.forced_line({"ref": ep.before}, ep.played)))
        elif kind == "terminal_features":
            self._add(ep, Task(kind, kind, self._run_comparison, deps=("forced_line_played", "forced_line_best")))
        elif kind == "root":
            self._add(ep, Task(kind, kind, lambda tk: self.tools.analyze_position({"ref": ep.before}, {"profile": "root"})))
        elif kind == "stability":
            for m in self._multipliers(ep.job_id):
                self._add(ep, Task(kind, f"stability_x{m}", lambda tk: self.tools.analyze_position(
                    {"ref": ep.before}, {"profile": "stability", "multiplier": tk.params["multiplier"]}),
                    deps=("root",), params={"multiplier": m}))
        elif kind == "supporting_test":
            self._add(ep, Task(kind, kind, self._run_supporting, deps=("intent_probe",)))
        elif kind == "local_solve" and ep.local_solve:
            self._add(ep, Task(kind, kind, self._run_local_solve, deps=("intent_probe",)))

    def _drop(self, ep: Episode, name: str) -> None:
        tk = ep.tasks.pop(name, None)
        if tk is not None and tk.state == "pending":
            tk.state = "cancelled"

    def _cancel_episode(self, ep: Episode) -> int:
        n = 0
        for tk in ep.tasks.values():
            if tk.state == "pending":
                tk.state = "cancelled"
                n += 1
        return n

    def _requeue_resized(self, ep: Episode) -> None:
        """Done tasks searched at sizes the current plan no longer uses run again (their stored results stay)."""
        for tk in list(ep.tasks.values()):
            if tk.state == "cancelled":
                tk.state = "pending"
            if tk.state == "done" and tk.sizes is not None and tk.sizes != self._sizes(ep.job_id, tk):
                tk.state, tk.result = "pending", None
        mults = self._multipliers(ep.job_id)
        for name in [n for n, tk in ep.tasks.items() if tk.kind == "stability" and tk.state != "done"
                     and tk.params["multiplier"] not in mults]:
            self._drop(ep, name)

    def _multipliers(self, job_id: str) -> list[int]:
        plan = self.tools._active_plan(job_id)
        return list(plan["verification"]["per_episode"]["stability_multipliers"]) if plan else list(self.tools.cfg.budget.unit_base.stability)

    def _sizes(self, job_id: str, tk: Task) -> tuple:
        t = self.tools
        plan = t._active_plan(job_id)
        pe = plan["verification"]["per_episode"] if plan else {}
        if tk.kind in ("root", "stability", "supporting_test"):
            return (t._budget_visits({"profile": "root"}, job_id),)
        if tk.kind == "local_solve":
            return (t._budget_visits({"profile": "local_solve"}, job_id),)
        return (t._budget_visits({"profile": "line_node"}, job_id), pe.get("forced_line_plies"), pe.get("expectation_plies"))

    def _visits_estimate(self, job_id: str, tk: Task) -> float:
        t = self.tools
        plan = t._active_plan(job_id)
        pe = plan["verification"]["per_episode"] if plan else {}
        p = int(pe.get("forced_line_plies", t.cfg.budget.unit_base.plies))
        ln = t._budget_visits({"profile": "line_node"}, job_id)
        root = t._budget_visits({"profile": "root"}, job_id)
        k = tk.kind
        if k == "intent_probe":
            return INTENT_SEARCHES * ln
        if k in ("forced_line_best", "forced_line_played", "fix"):
            return forced_line_searches(p) * ln
        if k == "expectation_probe":
            return expectation_searches(p) * ln
        if k == "line":
            return (1 + len(tk.params.get("moves", [])) + p) * ln
        if k == "root":
            return root
        if k == "stability":
            return tk.params["multiplier"] * root
        if k == "stability_misread":
            return self._multipliers(job_id)[0] * root if self._multipliers(job_id) else root
        if k == "supporting_test":
            return 4 * root
        if k == "local_solve":
            return 2 * t._budget_visits({"profile": "local_solve"}, job_id)
        return 0.0

    def _eta(self, job_id: str, until: str | None = None) -> int | None:
        """Seconds until the queue (or one episode's work) is done, from the plan's search sizes."""
        vps = self.tools.vps
        if vps <= 0:
            return None
        total, last = 0.0, 0.0
        for ep_id, tk in self._queue_order():
            if tk.state not in ("pending", "running"):
                continue
            total += self._visits_estimate(job_id, tk)
            if until is None or ep_id == (job_id, until):
                last = total
        return int(round(last / vps))

    # ---------------------------------------------------------------- the worker
    def _wake(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, daemon=True, name="katago-verify")
            self._worker.start()
        self._cv.notify_all()

    def _queue_order(self) -> list[tuple[tuple[str, str], Task]]:
        """Pending work in run order: answer work of interviewed episodes, then the selected episodes' work in
        interview order, then speculative work breadth-first (every episode's intent_probe before any stability)."""
        items = []
        for job_id, run in self.episodes.items():
            for ep in run.values():
                for tk in ep.tasks.values():
                    rank = TASK_ORDER.index(tk.kind)
                    if ep.selected:
                        key = (0 if tk.answer else 1, ep.order, rank, tk.seq)
                    else:
                        key = (2, rank, ep.order, tk.seq)
                    items.append((key, (job_id, ep.id), tk))
        items.sort(key=lambda x: x[0])
        return [(ref, tk) for _k, ref, tk in items]

    def _next(self) -> tuple[Episode, Task] | None:
        for (job_id, eid), tk in self._queue_order():
            if tk.state != "pending":
                continue
            ep = self.episodes[job_id][eid]
            deps = [ep.tasks.get(d) for d in tk.deps]
            if any(d is None or d.state in ("failed", "skipped", "cancelled") for d in deps):
                tk.state, tk.note = "skipped", "a probe it depends on did not finish"
                continue
            if all(d.state == "done" for d in deps):
                return ep, tk
        return None

    def _loop(self) -> None:
        tls = self.tools._tls
        tls.priority = int(self.cfg.background_priority)
        while not self._stop.is_set():
            with self._cv:
                nxt = self._next()
                if nxt is None:
                    self._cv.wait(5.0)
                    continue
                ep, tk = nxt
                tk.state, tk.started_at = "running", time.time()
                self.running_task = tk
            tls.tag = (ep.job_id, ep.id)
            result, error, state, note = None, None, "done", None
            try:
                sizes = self._sizes(ep.job_id, tk)
                result = tk.run(tk)
                if result is None:
                    state, note = "skipped", tk.note
            except Exception as e:  # noqa: BLE001
                from .tools import ToolError
                state = "failed"
                error = e.to_dict()["error"] if isinstance(e, ToolError) else {"code": "internal", "message": f"{type(e).__name__}: {e}"}
                if not isinstance(e, ToolError):
                    log.exception("background task %s failed", tk.name)
            finally:
                tls.tag = None
            with self._cv:
                self.running_task = None
                if tk.state == "running":            # not dropped or replaced meanwhile
                    tk.state, tk.result, tk.error, tk.finished_at = state, result, error, time.time()
                    tk.sizes = sizes if state == "done" else None
                    if note:
                        tk.note = note
                self._cv.notify_all()

    # ---------------------------------------------------------------- task bodies
    def _done(self, ep: Episode, name: str) -> dict:
        return ep.tasks[name].result

    def _run_comparison(self, tk: Task) -> dict:
        ep = tk.ep
        a = self._done(ep, "forced_line_played")["end"]["position_ref"]
        b = self._done(ep, "forced_line_best")["end"]["position_ref"]
        return self.tools.terminal_features({"ref": a}, compare_to={"ref": b})

    def _run_supporting(self, tk: Task) -> dict | None:
        ep = tk.ep
        ip = self._done(ep, "intent_probe")
        belief = (ip.get("belief") or {}).get("id")
        if belief == "is_sente":
            pts = [ip["threat"].get("follow_up"), (ip.get("reply") or {}).get("move")]
        elif belief == "biggest_move":
            pts = [ep.played, ep.teachable]
        else:
            tk.note = f"no automatic supporting test for belief {belief!r} (local_solve covers the group beliefs)"
            return None
        pts = [p for p in dict.fromkeys(pts) if p and p != "pass"]
        if not pts:
            tk.note = "no points to compare"
            return None
        tk.params["belief"], tk.params["points"] = belief, pts
        return self.tools.swing_value({"ref": ep.before}, pts)

    def _run_local_solve(self, tk: Task) -> dict | None:
        ep = tk.ep
        ls = ep.local_solve
        if isinstance(ls, dict):
            point, at = ls["group_point"], ls.get("at", "before")
        else:
            ip = self._done(ep, "intent_probe")
            belief = (ip.get("belief") or {}).get("id")
            if belief == "needs_defending" and ip["defense"]["groups"]:
                point, at = ip["defense"]["groups"][0]["anchor"], "before"
            elif belief == "group_is_safe" and ip["left_behind"]:
                lb = min(ip["left_behind"], key=lambda g: g["ownership_after_reply"])
                point, at = lb["anchor"], "after"
            else:
                tk.note = (f"belief {belief!r} names no group; pass local_solve.group_point to start_verification "
                           "or call local_solve after the interview")
                return None
        tk.params.update({"group_point": point, "at": at})
        return self.tools.local_solve({"ref": ep.before if at == "before" else ep.after}, point)

    def _run_expectation(self, tk: Task) -> dict:
        ep = tk.ep
        line = tk.params.get("line")
        return self.tools.expectation_probe({"ref": ep.before}, ep.played, options={"expected_line": line} if line else None)

    def _run_stability_misread(self, tk: Task) -> dict | None:
        ep = tk.ep
        mr = self._done(ep, "expectation_probe").get("misread")
        if not mr:
            tk.note = "no misread: the reading held"
            return None
        then = [[m[0], m[1:]] for m in mr["line_to_here"]]
        mults = self._multipliers(ep.job_id)
        tk.params["then"] = mr["line_to_here"]
        return self.tools.analyze_position({"ref": ep.before, "then": then},
                                           {"profile": "stability", "multiplier": mults[0] if mults else 4})

    def _run_fix(self, tk: Task) -> dict:
        ep = tk.ep
        return self.tools.forced_line({"ref": ep.before}, tk.params["move"])

    def _run_line(self, tk: Task) -> dict:
        ep = tk.ep
        ref = ep.before if tk.params["from"] == "before" else ep.after
        return self.tools.analyze_line({"ref": ref}, [[m[0], m[1:]] for m in tk.params["moves"]])


    # ---------------------------------------------------------------- views
    def _episode_view(self, ep: Episode, with_results: bool, parts: list | None = None) -> dict:
        tasks = []
        for tk in ep.tasks.values():
            if tk.state == "cancelled":
                continue
            row = {"task": tk.name, "needs_answer": tk.answer, "state": tk.state}
            if tk.result is not None and not ep.sealed:
                row["query_id"] = tk.result.get("query_id")
            if tk.error and not ep.sealed:
                row["error"] = tk.error
            if tk.note and not ep.sealed:
                row["note"] = tk.note
            tasks.append(row)
        view = {"episode": ep.id, "move_number": ep.move_number, "played": ep.played, "teachable": ep.teachable,
                "position_ref_before": ep.before, "position_ref_after": ep.after, "selected": ep.selected,
                "sealed": ep.sealed, "interviewed": ep.interviewed, "state": ep.state(), "tasks": tasks}
        if not with_results:
            return view
        want = set(parts) if parts else None
        res = {}
        for tk in ep.tasks.values():
            if tk.result is None or (want is not None and tk.name not in want and tk.kind not in want):
                continue
            r = copy.deepcopy(tk.result)
            if tk.kind == "supporting_test":
                r = {"belief": tk.params.get("belief"), "points": tk.params.get("points"), "swing_value": r}
            if tk.kind == "local_solve":
                r = {"group_point": tk.params.get("group_point"), "at": tk.params.get("at"), **r}
            res[tk.name] = r
        view["results"] = res
        view["stability_check"] = self._stability_check(ep)
        view["interview"] = copy.deepcopy(ep.interview)
        return view

    def _stability_check(self, ep: Episode) -> dict | None:
        """katago-analysis §5: the top move unchanged and the score moved < stability_margin."""
        root = ep.tasks.get("root")
        if root is None or root.result is None or not root.result.get("candidates"):
            return None
        margin = self.tools.cfg.thresholds.stability_margin
        top, score = root.result["candidates"][0]["move"], root.result["root"]["score_lead"]
        runs = []
        for tk in ep.tasks.values():
            if tk.kind == "stability" and tk.result is not None and tk.result.get("candidates"):
                s = tk.result["candidates"][0]["move"]
                moved = round(abs(tk.result["root"]["score_lead"] - score), 2)
                runs.append({"multiplier": tk.params["multiplier"], "visits": tk.result["visits_used"], "top_move": s,
                             "score_moved": moved, "stable": s == top and moved < margin,
                             "query_id": tk.result.get("query_id")})
        out = {"root": {"top_move": top, "score_lead": score, "visits": root.result["visits_used"],
                        "query_id": root.result.get("query_id")},
               "runs": runs, "stable": all(r["stable"] for r in runs) if runs else None}
        sm, ex = ep.tasks.get("stability_misread"), ep.tasks.get("expectation_probe")
        if sm is not None and sm.result is not None and ex is not None and ex.result and ex.result.get("misread"):
            mr = ex.result["misread"]
            s_top = sm.result["candidates"][0]["move"] if sm.result.get("candidates") else None
            out["misread_node"] = {"never_considered": mr["never_considered"], "stability_top_move": s_top,
                                   "stable": s_top == mr["never_considered"], "query_id": sm.result.get("query_id")}
        return out

    @staticmethod
    def _error(code: str, message: str, suggestion: str | None = None):
        from .tools import ToolError
        return ToolError(code, message, suggestion=suggestion)
