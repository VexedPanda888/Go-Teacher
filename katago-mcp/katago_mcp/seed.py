"""Seeding (tool contract §1.20–§1.24): a brief review of past games, one at a time, that seeds the memory.

The student goes through recent games with Claude on one page. For each game Claude tells the survey's story,
the student says what is wrong or missing, and Claude proposes one key lesson for the student to confirm.
The server keeps the session on disk (reviews/_seed/session.json), surveys the games in the background ahead
of the student, prepares each game's top key moment, makes the page rows, and at the end turns what the
student confirmed into memory documents. Memory itself is never touched here: only Claude reaches the
artifact, and it writes them.

Game states: queued (no survey on disk yet) -> surveying -> ready (loaded, key moment being prepared) or
stored (a finished survey on disk, not loaded); failed. A recorded game is released back to stored.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .board import CHAR_COLOR, COLOR_CHAR, IllegalMove, opponent
from .coords import CoordError, gtp_to_idx, idx_to_gtp
from .jobs import game_id_of, student_color_of
from .metrics import move_rows, sign_of
from .sgf import OGS_GAME_RE, SgfError, parse
from .store import timestamp

if TYPE_CHECKING:
    from .tools import Tools

log = logging.getLogger("katago_mcp")

SESSION = "session.json"
STATUSES = ("confirmed", "no_lesson", "skipped")
LOG_ID = "_seed"                     # queries of the seeding itself go to reviews/_seed/queries.jsonl


def _error(code: str, message: str, **kw):
    from .tools import ToolError      # tools imports this module
    return ToolError(code, message, **kw)


class _Skip(Exception):
    """A game that cannot be seeded, with the reason in plain words."""


def norm_game_id(x) -> str:
    """'ogs_123', '123', an OGS link -> 'ogs_123'; anything else as given."""
    s = str(x).strip()
    m = OGS_GAME_RE.search(s) or re.fullmatch(r"(?:ogs_)?(\d{3,12})", s)
    return f"ogs_{m.group(1)}" if m else s


def _text(v, field: str, required: bool = True) -> str | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise _error("bad_request", f"{field} is required")
        return None
    if not isinstance(v, str):
        raise _error("bad_request", f"{field} must be text")
    return v.strip()


# ==================================================================== page rows
def _signed(data: dict) -> dict:
    from .tools import dashboard_row_sha
    row = dict(data)
    row["sha256"] = dashboard_row_sha(data)
    return row


def index_row(s: dict, current: str | None) -> dict:
    """seed/index: the games of the seeding and the one Claude is on. Statuses come from the record rows."""
    return _signed({"kind": "seed_index", "seed_id": s["seed_id"], "current": current,
                    "games": [{"n": g["n"], "game_id": g["game_id"], "date": g["date"], "opponent": g["opponent"],
                               "you": g["you"], "result": g["result"]} for g in s["games"]]})


def game_row(tools: "Tools", g: dict, of: int, job, story: dict) -> dict:
    """seedgames/<game_id>: the game with the survey's engine data, for the seed page. Numbers are integers
    (tenths of a point), so the page's checksum cannot disagree with Python's float formatting."""
    ga, game = job.ga, job.game
    size = ga.size
    you = job.student_color
    sgn = sign_of(you)
    rows = move_rows(ga, tools.cfg.thresholds)
    dec, lc = story.get("decisive"), story.get("last_chance")
    data = {
        "kind": "seed_game", "game_id": g["game_id"], "n": g["n"], "of": of,
        "you": COLOR_CHAR[you], "players": game.players, "handicap": int(game.handicap), "komi": f"{game.komi:g}",
        "rules": game.rules, "result": game.result().get("raw", "") or "", "date": game.date,
        "first_to_move": COLOR_CHAR[game.first_to_move],
        "setup": {"AB": [idx_to_gtp(i, size) for i in ga.setup_black], "AW": [idx_to_gtp(i, size) for i in ga.setup_white]},
        "moves": [f"{COLOR_CHAR[c]}{idx_to_gtp(i, size)}" for c, i in ga.moves],
        # the student's score lead after each move, in tenths of a point (index 0 = after setup)
        "scores": [int(round(sgn * a.score_lead * 10)) for a in ga.positions],
        # per move n (index n - 1): the survey's best move in the position before it
        "best": [r["best"] if ga.positions[r["n"] - 1].candidates else None for r in rows],
        "phases": {k: story["phases"].get(k) for k in ("opening", "middlegame", "endgame")},
        "decisive": {"move": dec["move"], "by": dec["by"]} if dec else None,
        "last_chance": {"move": lc["move"], "played": lc["played"], "best": lc["best"]} if lc else None,
        "moments": [{"id": m["id"], "moves": list(m["moves"]), "move": m["move"], "played": m["played"], "best": m["best"],
                     "lost10": int(round(m["points_lost"] * 10))} for m in story["moments"]],
        "events": [{"move": e["move"], "group": e["group"], "whose": e["whose"], "from": e["from"], "to": e["to"]}
                   for e in story["group_events"]],
        "visits_per_move": int(ga.visits_per_move),
    }
    row = _signed(data)
    # free text, outside the checksum: Claude writes its story into `text`
    row["title"] = f"Game {g['n']} of {of}"
    row["text"] = ""
    return row


def record_row(g: dict, rec: dict) -> dict:
    """seedrecords/<game_id>: what the student agreed for this game, shown on the seed page."""
    L = rec.get("lesson")
    return _signed({"kind": "seed_record", "game_id": g["game_id"], "status": rec["status"], "story": rec.get("story"),
                    "feedback": rec.get("feedback"),
                    "lesson": None if not L else {k: L.get(k) for k in ("move", "played", "better", "title", "takeaway",
                                                                         "theme", "cue")}})


# ==================================================================== the session
class Seeder:
    def __init__(self, tools: "Tools"):
        self.tools = tools
        self.dir = Path(tools.cfg.reviews_dir) / "_seed"
        self._cv = threading.Condition(threading.RLock())
        self._session: dict | None = None
        self._loaded = False
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def cfg(self):
        return self.tools.cfg.seed

    # ---------------------------------------------------------------- persistence
    def _path(self) -> Path:
        return self.dir / SESSION

    def _load(self) -> dict | None:
        if self._loaded:
            return self._session
        self._loaded = True
        p = self._path()
        if not p.exists():
            return None
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            log.warning("seed: %s is unreadable; starting without it", p)
            return None
        for g in s.get("games", []):
            # jobs do not survive a server restart: the games come back from disk
            if g["state"] in ("surveying", "ready") and g.get("job_id") not in self.tools.jobs.jobs:
                g["state"], g["job_id"] = self._disk_state(g), None
            if g["state"] == "failed" and not s.get("finished_at"):
                g["state"], g["error"] = self._disk_state(g), None     # try again
        self._session = s
        return s

    def _save(self) -> None:
        s = self._session
        if s is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        s["updated_at"] = timestamp()
        tmp = self._path().with_suffix(".tmp")
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._path())

    def _archive(self) -> None:
        """Keep the current session as reviews/_seed/<seed_id>.json and clear it."""
        s = self._session
        if s is None:
            return
        self._release_all(s)
        if not s.get("finished_at"):
            s["cancelled_at"] = timestamp()
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{s['seed_id']}.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        self._path().unlink(missing_ok=True)
        self._session = None

    def previous(self) -> list[dict]:
        """Earlier seedings on this machine (finished or abandoned), oldest first."""
        out = []
        for p in sorted(self.dir.glob("seed_*.json")):
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            recs = [g.get("record") or {} for g in s.get("games", [])]
            out.append({"seed_id": s.get("seed_id"), "created_at": s.get("created_at"), "finished_at": s.get("finished_at"),
                        "cancelled_at": s.get("cancelled_at"), "games": len(recs),
                        "lessons": sum(1 for r in recs if r.get("status") == "confirmed")})
        return out

    def _require(self) -> dict:
        s = self._load()
        if s is None:
            raise _error("seed_not_started", "no seeding on this machine", suggestion="call seed_start")
        return s

    def _find(self, s: dict, ref) -> dict:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit() and len(ref.strip()) <= 3):
            n = int(ref)
            for g in s["games"]:
                if g["n"] == n:
                    return g
            raise _error("bad_request", f"there is no game {n} in this seeding (1..{len(s['games'])})")
        gid = norm_game_id(ref)
        for g in s["games"]:
            if g["game_id"] == gid:
                return g
        raise _error("bad_request", f"{ref!r} is not in this seeding",
                     details={"games": [g["game_id"] for g in s["games"]]})

    # ---------------------------------------------------------------- choosing the games
    def _stored_visits(self, game_id: str) -> int:
        """Visits per move of a finished survey on disk (0: none). Reads only the head of analysis.json."""
        p = Path(self.tools.cfg.reviews_dir) / game_id / "analysis.json"
        if not p.exists():
            return 0
        try:
            with p.open(encoding="utf-8") as f:
                head = f.read(600)
        except OSError:
            return 0
        c = re.search(r'"complete":\s*(true|false)', head)
        v = re.search(r'"visits_per_move":\s*(\d+)', head)
        return int(v.group(1)) if c and c.group(1) == "true" and v else 0

    def _disk_state(self, g: dict) -> str:
        return "stored" if self._stored_visits(g["game_id"]) >= int(self.cfg.survey_visits) else "queued"

    def _candidate(self, ref: str) -> dict:
        try:
            text, hint, src = self.tools._resolve_sgf(ref)
        except Exception as e:  # noqa: BLE001  (a ToolError: fetch failed, not a file, …)
            raise _Skip(getattr(e, "message", str(e)))
        try:
            game = parse(text)
        except SgfError as e:
            raise _Skip(f"the SGF could not be read ({e})")
        gid = hint or game_id_of(game, text)
        if game.size != 19:
            raise _Skip(f"{game.size}x{game.size}, only 19x19 is supported")
        username = self.tools.cfg.student.username
        student = student_color_of(game, username)
        if student is None:
            raise _Skip(f"{username or 'the student'} did not play in it")
        if len(game.moves) < int(self.cfg.min_moves):
            raise _Skip(f"only {len(game.moves)} moves")
        # how to find the SGF again: the OGS cache, the file, or a copy in games/ for pasted text
        if src.get("kind") == "ogs":
            sgf_ref = gid
        elif src.get("kind") == "file":
            sgf_ref = src["path"]
        else:
            d = Path(self.tools.cfg.games_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{gid}.sgf").write_text(text, encoding="utf-8")
            sgf_ref = str(d / f"{gid}.sgf")
        you, opp = COLOR_CHAR[student], COLOR_CHAR[opponent(student)]
        g = {"n": 0, "game_id": gid, "sgf": sgf_ref, "date": game.date, "you": you,
             "opponent": game.players[opp]["name"], "opponent_rank": game.players[opp].get("rank"),
             "your_rank": game.players[you].get("rank"), "handicap": int(game.handicap), "komi": f"{game.komi:g}",
             "result": game.result().get("raw", "") or "", "moves": len(game.moves),
             "state": "queued", "job_id": None, "keep": False, "error": None, "record": None}
        g["state"] = self._disk_state(g)
        return g

    def _get_json(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": f"katago-mcp/{__version__} (Go teacher review server)",
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.tools.cfg.ogs.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise _error("ogs_fetch_failed", f"online-go.com answered HTTP {e.code} for {url}",
                         suggestion="pass the games as OGS links instead")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            raise _error("ogs_fetch_failed", f"could not read {url}: {e}",
                         suggestion="check the network, or pass the games as .sgf file names in games/")

    @staticmethod
    def _listing_ignored(r: dict) -> str | None:
        """Why a game in the OGS list is not a candidate at all (not reported one by one)."""
        if not r.get("ended"):
            return "unfinished"
        if r.get("annulled") or str(r.get("outcome") or "").lower() in ("cancellation", "cancelled"):
            return "annulled or cancelled"
        if r.get("width") != 19 or r.get("height") != 19:
            return "not 19x19"
        if r.get("rengo"):
            return "rengo"
        return None

    def _from_ogs(self, count: int, excl: set[str]) -> tuple[list[dict], list[dict], dict]:
        ogs = self.tools.cfg.ogs
        user = self.tools.cfg.student.username
        if not user:
            raise _error("bad_request", "the config has no [student].username to look up on OGS",
                         suggestion="set it in the machine's TOML, or pass the games as links")
        if not ogs.enabled:
            raise _error("bad_request", "OGS downloads are disabled in the config ([ogs].enabled = false)",
                         suggestion="pass the games as .sgf file names in games/")
        base = ogs.base_url.rstrip("/")
        found = self._get_json(f"{base}/api/v1/players/?username={urllib.parse.quote(user)}")
        hit = next((r for r in found.get("results", []) if str(r.get("username", "")).lower() == user.lower()), None)
        if hit is None:
            raise _error("ogs_fetch_failed", f"online-go.com has no player named {user}")
        url = f"{base}/api/v1/players/{hit['id']}/games/?ordering=-ended&page_size=50"
        picked, skipped, ignored, seen, pages = [], [], {}, set(), 0
        while url and len(picked) < count and pages < int(self.cfg.ogs_pages):
            pages += 1
            page = self._get_json(url)
            for r in page.get("results", []):
                if len(picked) >= count:
                    break
                gid = f"ogs_{r.get('id')}"
                if gid in seen:
                    continue
                seen.add(gid)
                why = self._listing_ignored(r)
                if why:
                    ignored[why] = ignored.get(why, 0) + 1
                    continue
                if gid in excl:
                    skipped.append({"game": gid, "reason": "reviewed already"})
                    continue
                try:
                    picked.append(self._candidate(gid))
                except _Skip as e:
                    skipped.append({"game": gid, "reason": str(e)})
            url = page.get("next")
        picked.reverse()                    # oldest first: the games in the order they were played
        return picked, skipped, ignored

    def _from_list(self, games: list, excl: set[str]) -> tuple[list[dict], list[dict]]:
        picked, skipped, seen = [], [], set()
        for ref in games:
            try:
                g = self._candidate(str(ref).strip())
            except _Skip as e:
                skipped.append({"game": str(ref), "reason": str(e)})
                continue
            if g["game_id"] in excl:
                skipped.append({"game": g["game_id"], "reason": "reviewed already"})
                continue
            if g["game_id"] in seen:
                continue
            seen.add(g["game_id"])
            picked.append(g)
        return picked, skipped

    # ---------------------------------------------------------------- start / status
    def start(self, games: list | None = None, count: int | None = None, exclude: list | None = None,
              restart: bool = False) -> dict:
        with self._cv:
            s = self._load()
            if s is not None and not s.get("finished_at") and not restart:
                self._wake()
                out = self.status()
                out["resumed"] = True
                if games or count:
                    out["note"] = "an unfinished seeding exists and was resumed; pass restart: true to start a new one"
                return out
        excl = {norm_game_id(x) for x in (exclude or [])}
        ignored: dict = {}
        if games:
            picked, skipped = self._from_list(list(games), excl)
            source = "list"
        else:
            n = int(count or self.cfg.games)
            if n < 1:
                raise _error("bad_request", "count must be at least 1")
            picked, skipped, ignored = self._from_ogs(n, excl)
            source = "ogs"
        if not picked:
            raise _error("bad_request", "no game left to seed", details={"skipped": skipped, "ignored": ignored},
                         suggestion="name the games (OGS links or .sgf names), or include reviewed games")
        for n, g in enumerate(picked, 1):
            g["n"] = n
        with self._cv:
            self._load()
            if self._session is not None:
                self._archive()
            self._session = {"seed_id": "seed_" + time.strftime("%Y%m%d-%H%M%S"), "created_at": timestamp(),
                             "updated_at": None, "finished_at": None, "source": source,
                             "student": self.tools.cfg.student.username, "survey_visits": int(self.cfg.survey_visits),
                             "current": None, "page_url": None, "games": picked}
            self._loaded = True
            self._save()
            self._wake()
            out = self.status()
        out["resumed"] = False
        out["skipped"] = skipped
        if ignored:
            out["ignored"] = ignored
        return out

    def _prepared(self, g: dict) -> str:
        tasks = self.tools.prefetch.pending(g["job_id"]) if g.get("job_id") else []
        return tasks[0]["state"] if tasks else "no"

    def _survey_seconds(self, g: dict) -> float | None:
        vps = self.tools.vps
        return (g["moves"] + 1) * int(self.cfg.survey_visits) / vps if vps > 0 else None

    def status(self) -> dict:
        with self._cv:
            s = self._load()
            if s is None:
                return {"session": None, "previous": self.previous()}
            out_games, ahead = [], 0.0
            for g in s["games"]:
                rec = g.get("record") or {}
                e = {"n": g["n"], "game_id": g["game_id"], "date": g["date"], "opponent": g["opponent"], "you": g["you"],
                     "result": g["result"], "handicap": g["handicap"], "moves": g["moves"], "state": g["state"],
                     "recorded": rec.get("status")}
                job = self.tools.jobs.jobs.get(g.get("job_id")) if g.get("job_id") else None
                if g["state"] == "surveying" and job is not None:
                    st = job.status()
                    e["progress"] = st["progress"]
                    ahead += st["eta_seconds"] or 0
                    e["eta_seconds"] = round(ahead)
                elif g["state"] == "queued" and not rec:
                    secs = self._survey_seconds(g)
                    if secs is not None:
                        ahead += secs
                        e["eta_seconds"] = round(ahead)
                if g["state"] == "ready":
                    e["job_id"] = g["job_id"]
                    e["lesson_prepared"] = self._prepared(g)
                if g.get("error"):
                    e["error"] = g["error"]
                out_games.append(e)
            nxt = next((g for g in s["games"] if not g.get("record")), None)
            recorded = sum(1 for g in s["games"] if g.get("record"))
            return {"seed_id": s["seed_id"], "created_at": s["created_at"], "finished_at": s.get("finished_at"),
                    "source": s["source"], "survey_visits": s["survey_visits"], "current": s.get("current"),
                    "recorded": recorded, "of": len(s["games"]), "games": out_games,
                    "next": None if nxt is None else {"n": nxt["n"], "game_id": nxt["game_id"], "state": nxt["state"]},
                    "engine_seconds_ahead": round(ahead), "previous": self.previous()}

    # ---------------------------------------------------------------- the background work
    def _wake(self) -> None:
        with self._cv:
            if self._worker is None or not self._worker.is_alive():
                self._stop.clear()
                self._worker = threading.Thread(target=self._loop, daemon=True, name="katago-seed")
                self._worker.start()
            self._cv.notify_all()

    def wake_if_active(self) -> None:
        """Resume the background work of an unfinished seeding (after a server restart, on the first seed call)."""
        with self._cv:
            s = self._load()
            if s is not None and not s.get("finished_at"):
                self._wake()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                pending = self._tick()
            except Exception:  # noqa: BLE001
                log.exception("seed: background step failed")
                pending = True
            if not pending:
                return                   # idle; seed_start, seed_game and seed_record wake it again
            with self._cv:
                self._cv.wait(1.0)

    def _engine_ready(self) -> bool:
        eng = self.tools.engine
        if getattr(eng, "ready", True) and eng.running:
            return True
        self.tools.start_engine_background()
        return False

    def _release(self, g: dict) -> None:
        jid = g.get("job_id")
        g["job_id"] = None
        if jid and jid in self.tools.jobs.jobs:
            try:
                self.tools.job_status(jid, "release")
            except Exception:  # noqa: BLE001
                log.exception("seed: could not release %s", jid)

    def _release_all(self, s: dict) -> None:
        for g in s["games"]:
            if g["state"] == "ready":
                self._release(g)
                g["state"] = "stored"

    def _launch(self, g: dict, keep: bool) -> bool:
        """Start (or load from disk) the survey of one game. False: the engine cannot take it now."""
        if not self._engine_ready():          # start_game_analysis would wait for it inside our lock
            return False
        opts = {"prefetch": bool(keep), "prefetch_moments": int(self.cfg.prepare_moments)}
        try:
            r = self.tools.start_game_analysis(g["sgf"], {"visits_per_move": int(self.cfg.survey_visits)}, options=opts)
        except Exception as e:  # noqa: BLE001
            code = getattr(e, "code", "internal")
            if code in ("engine_busy", "engine_unavailable"):
                return False
            g["state"], g["error"] = "failed", getattr(e, "message", str(e))
            return True
        g["job_id"], g["keep"], g["error"] = r["job_id"], bool(keep), None
        if r["state"] == "done":                    # a stored survey, loaded at once
            if keep:
                g["state"] = "ready"
            else:
                self._release(g)
                g["state"] = "stored"
        else:
            g["state"] = "surveying"
        return True

    def _tick(self) -> bool:
        """One step of the background work. True while there is something left to do."""
        with self._cv:
            s = self._session
            if s is None or s.get("finished_at"):
                return False
            jobs = self.tools.jobs
            changed = False
            for g in s["games"]:                     # surveys that ended
                if g["state"] != "surveying":
                    continue
                job = jobs.jobs.get(g.get("job_id"))
                if job is None:
                    g["state"], g["job_id"], changed = self._disk_state(g), None, True
                elif job.state == "done":
                    if g.get("keep") and not g.get("record"):
                        g["state"] = "ready"
                    else:
                        self._release(g)
                        g["state"] = "stored"
                    changed = True
                elif job.state in ("failed", "cancelled"):
                    g["state"], g["job_id"] = "failed", None
                    g["error"] = (job.error or {}).get("message") or job.state
                    changed = True
            open_ = [g for g in s["games"] if not g.get("record")]
            window = int(self.cfg.ready_ahead)
            held = sum(1 for g in open_ if g["state"] == "ready" or (g["state"] == "surveying" and g.get("keep")))
            for g in open_:                          # stored games into the window, in order (no engine needed)
                if window and held >= window:
                    break
                if g["state"] == "stored" and self._launch(g, keep=True):
                    held += g["state"] == "ready"
                    changed = True
            if jobs.active is None:                  # the next survey, in order
                nxt = next((g for g in open_ if g["state"] == "queued"), None)
                if nxt is not None and self._launch(nxt, keep=not window or held < window):
                    changed = True
            if changed:
                self._save()
            surveying = any(g["state"] == "surveying" for g in s["games"])
            queued = any(g["state"] == "queued" for g in open_)
            loadable = any(g["state"] == "stored" for g in open_) and (not window or held < window)
            return surveying or queued or loadable

    # ---------------------------------------------------------------- one game
    def game(self, ref=None) -> dict:
        with self._cv:
            s = self._require()
            if s.get("finished_at"):
                raise _error("seed_finished", "this seeding is finished", suggestion="seed_start starts a new one")
            g = self._find(s, ref) if ref not in (None, "") else next((x for x in s["games"] if not x.get("record")), None)
            if g is None:
                return {"ready": False, "done": True, "note": "every game has been gone through: call seed_finish"}
            if g["state"] in ("stored", "queued", "failed") and (g["state"] == "stored" or self.tools.jobs.active is None):
                self._launch(g, keep=True)          # on demand, out of order if need be
                self._save()
            if g["state"] != "ready":
                self._wake()
                e = next(x for x in self.status()["games"] if x["game_id"] == g["game_id"])
                return {"ready": False, "n": g["n"], "game_id": g["game_id"], "state": g["state"],
                        "progress": e.get("progress"), "eta_seconds": e.get("eta_seconds"), "error": g.get("error"),
                        "note": "its survey is not finished; call seed_game again when seed_status shows it ready"}
            s["current"] = g["game_id"]
            self._save()
            job_id, of = g["job_id"], len(s["games"])
        story = self.tools.job_results(job_id)
        job = self.tools.jobs.get(job_id)
        top = story["moments"][0] if story["moments"] else None
        with self._cv:
            rows = [{"collection": "seed", "doc_id": "index", "row": index_row(s, g["game_id"])},
                    {"collection": "seedgames", "doc_id": g["game_id"], "row": game_row(self.tools, g, of, job, story)}]
        return {
            "ready": True, "n": g["n"], "of": of, "game_id": g["game_id"], "job_id": job_id,
            "game": {k: g[k] for k in ("date", "you", "opponent", "opponent_rank", "your_rank", "handicap", "komi", "result", "moves")},
            "recorded": (g.get("record") or {}).get("status"),
            "story": story,
            "top_moment": None if top is None else {"id": top["id"], "move": top["move"], "played": top["played"],
                                                    "best": top["best"], "points_lost": top["points_lost"],
                                                    "prepared": top["prepared"]},
            "page_rows": rows,
            "write": "ArtifactData set on the seed page, each row to its collection and doc_id, data = row exactly as "
                     "given; put your story in the seedgames row's text (title and text are free text)",
        }

    # ---------------------------------------------------------------- recording
    def _lesson(self, g: dict, lesson) -> dict:
        if not isinstance(lesson, dict):
            raise _error("bad_request", "lesson must be an object {move, better, title, takeaway, theme, cue?, student_words?, moment?}")
        try:
            n = int(lesson.get("move"))
        except (TypeError, ValueError):
            raise _error("bad_request", "lesson.move must be the move number of the student's move")
        text, _hint, _src = self.tools._resolve_sgf(g["sgf"])
        game = parse(text)
        if not 1 <= n <= len(game.moves):
            raise _error("bad_request", f"lesson.move must be 1..{len(game.moves)}")
        color, idx = game.moves[n - 1]
        student = CHAR_COLOR[g["you"]]
        if color != student:
            raise _error("bad_request", f"move {n} was your opponent's; a lesson is about one of the student's moves")
        if idx is None:
            raise _error("bad_request", f"move {n} was a pass")
        try:
            bi = gtp_to_idx(str(lesson.get("better") or ""))
        except CoordError as e:
            raise _error("bad_request", f"lesson.better: {e}")
        if bi is None:
            raise _error("bad_request", "lesson.better must be a point, not a pass")
        if bi == idx:
            raise _error("bad_request", "lesson.better is the move that was played")
        try:
            self.tools.jobs.spec_at(game, n - 1).board().play(student, bi, game.rules)
        except IllegalMove as e:
            raise _error("illegal_move", f"lesson.better {idx_to_gtp(bi)} is not legal before move {n}: {e}")
        moment = lesson.get("moment")
        if moment is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,8}", str(moment)):
            raise _error("bad_request", "lesson.moment must be a key moment id such as 'M1'")
        out = {"move": n, "played": idx_to_gtp(idx), "better": idx_to_gtp(bi),
               "title": _text(lesson.get("title"), "lesson.title"), "takeaway": _text(lesson.get("takeaway"), "lesson.takeaway"),
               "theme": _text(lesson.get("theme"), "lesson.theme"), "cue": _text(lesson.get("cue"), "lesson.cue", False),
               "student_words": _text(lesson.get("student_words"), "lesson.student_words", False),
               "moment": str(moment) if moment is not None else None, "points_lost": None}
        job = self.tools.jobs.jobs.get(g.get("job_id")) if g.get("job_id") else None
        if job is not None and job.ga is not None and n <= job.ga.M:
            out["points_lost"] = round(move_rows(job.ga, self.tools.cfg.thresholds)[n - 1]["points_lost"], 1)
        return out

    def record(self, game, status: str, story: str | None = None, feedback: str | None = None,
               lesson: dict | None = None) -> dict:
        if status not in STATUSES:
            raise _error("bad_request", "status must be 'confirmed', 'no_lesson' or 'skipped'")
        with self._cv:
            s = self._require()
            if s.get("finished_at"):
                raise _error("seed_finished", "this seeding is finished", suggestion="seed_start starts a new one")
            if game in (None, ""):
                raise _error("bad_request", "game is required (its number or game id)")
            g = self._find(s, game)
        story_t = _text(story, "story", required=status != "skipped")
        if status == "confirmed":
            if lesson is None:
                raise _error("bad_request", "a confirmed game needs its lesson")
            L = self._lesson(g, lesson)
        elif lesson:
            raise _error("bad_request", "a lesson is recorded only with status 'confirmed'")
        else:
            L = None
        rec = {"status": status, "story": story_t, "feedback": _text(feedback, "feedback", False), "lesson": L,
               "recorded_at": timestamp()}
        with self._cv:
            g["record"] = rec
            if g["state"] == "ready":
                self._release(g)
                g["state"] = "stored"
            self._save()
            nxt = next((x for x in s["games"] if not x.get("record")), None)
            recorded = sum(1 for x in s["games"] if x.get("record"))
            out = {"recorded": {"n": g["n"], "game_id": g["game_id"], **rec},
                   "page_row": {"collection": "seedrecords", "doc_id": g["game_id"], "row": record_row(g, rec)},
                   "progress": {"recorded": recorded, "of": len(s["games"])},
                   "next": None if nxt is None else {"n": nxt["n"], "game_id": nxt["game_id"], "state": nxt["state"]}}
        self._wake()
        return out

    # ---------------------------------------------------------------- the end
    def finish(self, page_url: str | None = None, leave_out_unrecorded: bool = False) -> dict:
        with self._cv:
            s = self._require()
            left = [g for g in s["games"] if not g.get("record")]
            if left and not leave_out_unrecorded and not s.get("finished_at"):
                raise _error("seed_incomplete", f"{len(left)} of {len(s['games'])} games have not been gone through",
                             details={"games": [{"n": g["n"], "game_id": g["game_id"]} for g in left]},
                             suggestion="go through them (seed_game, seed_record), or pass leave_out_unrecorded: true "
                                        "when the student wants to stop here")
            if page_url:
                s["page_url"] = str(page_url)
            ops, lesson_ids, game_ids, themes = [], [], [], OrderedDict()
            for g in s["games"]:
                r = g.get("record")
                if not r or r["status"] == "skipped":
                    continue
                gid, L = g["game_id"], r.get("lesson")
                lid = f"{gid}-S1" if L else None
                moment = None if not L else {"id": L.get("moment") or "M1", "move": L["move"], "played": L["played"],
                                             "better": L["better"], "points_lost": L.get("points_lost")}
                ops.append({"op": "set", "collection": "games", "doc_id": gid, "data": {
                    "game_id": gid, "date": g["date"], "color": g["you"], "handicap": g["handicap"], "komi": g["komi"],
                    "opponent": g["opponent"], "opponent_rank": g["opponent_rank"], "result": g["result"],
                    "reviewed_at": r["recorded_at"][:10], "seeded": True, "seed_id": s["seed_id"],
                    "story": r["story"], "story_feedback": r.get("feedback"),
                    "moments": [moment] if moment else [], "lesson_ids": [lid] if lid else [],
                    "dashboard_url": s.get("page_url")}})
                game_ids.append(gid)
                if L:
                    ops.append({"op": "set", "collection": "lessons", "doc_id": lid, "data": {
                        "game_id": gid, "date": g["date"], "moments": [moment["id"]], "move": L["move"],
                        "played": L["played"], "better": L["better"], "title": L["title"], "takeaway": L["takeaway"],
                        "student_words": L.get("student_words"), "cue": L.get("cue"), "theme": L["theme"],
                        "status": "open", "recall": [], "source": "seed", "seed_id": s["seed_id"]}})
                    lesson_ids.append(lid)
                    themes.setdefault(L["theme"], []).append(lid)
            if not s.get("finished_at"):
                s["finished_at"] = timestamp()
                self._release_all(s)
            self._save()
            out = {"seed_id": s["seed_id"], "finished_at": s["finished_at"],
                   "games_written": len(game_ids), "lessons_written": len(lesson_ids),
                   "left_out": [{"n": g["n"], "game_id": g["game_id"],
                                 "why": "skipped" if g.get("record") else "not gone through"}
                                for g in s["games"] if not g.get("record") or g["record"]["status"] == "skipped"],
                   "game_ids": game_ids, "lesson_ids": lesson_ids,
                   "themes": [{"theme": t, "count": len(ids), "lesson_ids": ids}
                              for t, ids in sorted(themes.items(), key=lambda kv: -len(kv[1]))],
                   "batches": [ops[i:i + 50] for i in range(0, len(ops), 50)],
                   "profile_seed": {"seed_id": s["seed_id"], "date": s["finished_at"][:10], "games": len(game_ids),
                                    "lessons": len(lesson_ids), "page_url": s.get("page_url")}}
        self.stop(timeout=2.0)
        return out
