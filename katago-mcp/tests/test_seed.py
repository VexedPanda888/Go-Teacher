"""Seeding (contract §1.20–§1.24): the session, the background surveys, the page rows, the records, the memory
documents, the OGS game list, and the CLI that prepares a seeding ahead of the chat."""
import json
import os
import tempfile
import time
import unittest
import urllib.request

from _helpers import make_tools  # noqa: E402  (also puts the package on sys.path)
from katago_mcp import cli  # noqa: E402
from katago_mcp.engine import MockEngine  # noqa: E402
from katago_mcp.tools import ToolError, dashboard_row_sha  # noqa: E402
from test_tools_mock import synthetic_game  # noqa: E402


def write_game(games_dir: str, gid: int, n_moves: int, seed: int, student_white: bool = False) -> str:
    sgf = synthetic_game(n_moves, seed=seed).replace(f"game/{1000 + seed}", f"game/{gid}")
    if student_white:
        sgf = sgf.replace("PB[student1]", "PB[tmp]").replace("PW[rival]", "PW[student1]").replace("PB[tmp]", "PB[rival]")
    os.makedirs(games_dir, exist_ok=True)
    name = f"ogs_{gid}.sgf"
    with open(os.path.join(games_dir, name), "w") as f:
        f.write(sgf)
    return name


def floats(x) -> int:
    if isinstance(x, float):
        return 1
    if isinstance(x, dict):
        return sum(floats(v) for v in x.values())
    if isinstance(x, list):
        return sum(floats(v) for v in x)
    return 0


def wait_until(tools, pred, timeout=30.0) -> dict:
    deadline = time.time() + timeout
    while True:
        st = tools.seed_status()
        if pred(st) or time.time() > deadline:
            return st
        time.sleep(0.05)


def settled(st) -> bool:
    return all(g["state"] in ("ready", "stored") or g["recorded"] for g in st["games"])


class SeedFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tools = make_tools(self.tmp.name, seed__ready_ahead=2, seed__survey_visits=40, seed__min_moves=40)
        gd = self.tools.cfg.games_dir
        self.names = [write_game(gd, 9001, 70, 21), write_game(gd, 9002, 64, 22, student_white=True),
                      write_game(gd, 9003, 60, 23), write_game(gd, 9004, 30, 24)]

    def tearDown(self):
        self.tools.close()
        self.tmp.cleanup()

    def start(self, **kw):
        return self.tools.seed_start(games=self.names, **kw)

    def test_no_session(self):
        st = self.tools.seed_status()
        self.assertIsNone(st["session"])
        with self.assertRaises(ToolError) as cm:
            self.tools.seed_game()
        self.assertEqual(cm.exception.code, "seed_not_started")

    def test_start_window_and_game_rows(self):
        r = self.start(exclude=["9003"])
        self.assertFalse(r["resumed"])
        self.assertEqual([g["game_id"] for g in r["games"]], ["ogs_9001", "ogs_9002"])
        self.assertEqual({k["game"]: k["reason"] for k in r["skipped"]},
                         {"ogs_9003": "reviewed already", "ogs_9004.sgf": "only 30 moves"})
        st = wait_until(self.tools, settled)
        self.assertEqual([g["state"] for g in st["games"]], ["ready", "ready"])
        self.assertTrue(all(g["lesson_prepared"] in ("queued", "running", "done") for g in st["games"]))
        g = self.tools.seed_game()
        self.assertTrue(g["ready"])
        self.assertEqual((g["n"], g["of"], g["game_id"]), (1, 2, "ogs_9001"))
        self.assertEqual(g["story"]["job_id"], g["job_id"])
        self.assertEqual(g["top_moment"]["id"], "M1")
        idx, game = (pr["row"] for pr in g["page_rows"])
        self.assertEqual([(pr["collection"], pr["doc_id"]) for pr in g["page_rows"]],
                         [("seed", "index"), ("seedgames", "ogs_9001")])
        self.assertEqual(idx["current"], "ogs_9001")
        for row in (idx, game):
            self.assertEqual(row["sha256"], dashboard_row_sha(row))
            self.assertEqual(floats(row), 0, "page rows carry integers only")
        self.assertEqual((len(game["moves"]), len(game["scores"]), len(game["best"])), (70, 71, 70))
        self.assertEqual(game["you"], "B")
        self.assertEqual(dashboard_row_sha({**game, "text": "The story.", "title": "x"}), game["sha256"])
        self.assertEqual(self.tools.seed_status()["current"], "ogs_9001")
        # the second game: the student was White
        g2 = self.tools.seed_game(2)
        self.assertEqual(g2["page_rows"][1]["row"]["you"], "W")

    def test_records_and_finish(self):
        self.start()
        wait_until(self.tools, settled)
        g = self.tools.seed_game()
        tm = g["top_moment"]
        lesson = {"move": tm["move"], "better": tm["best"], "title": "T", "takeaway": "Check the cut first.",
                  "theme": "own weaknesses before big points", "moment": "M1"}
        played = g["story"]["moments"][0]["played"]
        for bad, code in (({**lesson, "move": tm["move"] + 1}, "bad_request"),       # the opponent's move
                          ({**lesson, "better": played}, "bad_request"),            # the move that was played
                          ({**lesson, "better": "Z9"}, "bad_request"),
                          ({k: v for k, v in lesson.items() if k != "theme"}, "bad_request")):
            with self.assertRaises(ToolError) as cm:
                self.tools.seed_record(1, "confirmed", story="S.", lesson=bad)
            self.assertEqual(cm.exception.code, code, bad)
        occupied = self.tools.dashboard_row("game", g["job_id"])["row"]["moves"][0][1:]
        with self.assertRaises(ToolError) as cm:
            self.tools.seed_record(1, "confirmed", story="S.", lesson={**lesson, "better": occupied})
        self.assertEqual(cm.exception.code, "illegal_move")
        with self.assertRaises(ToolError):
            self.tools.seed_record(1, "confirmed", story="S.")                        # no lesson
        with self.assertRaises(ToolError):
            self.tools.seed_record(1, "no_lesson", story="S.", lesson=lesson)         # a lesson needs 'confirmed'
        with self.assertRaises(ToolError):
            self.tools.seed_record(1, "no_lesson")                                    # no story
        r = self.tools.seed_record(1, "confirmed", story="You led; 33 was the slip.", feedback="Agreed.", lesson=lesson)
        self.assertEqual(r["recorded"]["lesson"]["played"], played)
        self.assertIsNotNone(r["recorded"]["lesson"]["points_lost"])
        row = r["page_row"]["row"]
        self.assertEqual((r["page_row"]["collection"], r["page_row"]["doc_id"]), ("seedrecords", "ogs_9001"))
        self.assertEqual(row["sha256"], dashboard_row_sha(row))
        self.assertEqual(floats(row), 0)
        self.assertEqual(r["progress"], {"recorded": 1, "of": 3})
        self.assertEqual(r["next"]["game_id"], "ogs_9002")
        st = wait_until(self.tools, lambda s: s["games"][2]["state"] == "ready")   # the window moved on
        self.assertEqual([g["state"] for g in st["games"]], ["stored", "ready", "ready"])
        with self.assertRaises(ToolError) as cm:
            self.tools.seed_finish()
        self.assertEqual(cm.exception.code, "seed_incomplete")
        self.tools.seed_record("ogs_9002", "no_lesson", story="A close game.")
        fin = self.tools.seed_finish(page_url="https://claude.ai/artifact/seed", leave_out_unrecorded=True)
        self.assertEqual((fin["games_written"], fin["lessons_written"]), (2, 1))
        self.assertEqual(fin["left_out"], [{"n": 3, "game_id": "ogs_9003", "why": "not gone through"}])
        self.assertEqual(fin["lesson_ids"], ["ogs_9001-S1"])
        self.assertEqual(fin["themes"], [{"theme": "own weaknesses before big points", "count": 1, "lesson_ids": ["ogs_9001-S1"]}])
        ops = [op for b in fin["batches"] for op in b]
        self.assertEqual([(o["collection"], o["doc_id"]) for o in ops],
                         [("games", "ogs_9001"), ("lessons", "ogs_9001-S1"), ("games", "ogs_9002")])
        g1, l1, g2 = (o["data"] for o in ops)
        self.assertTrue(g1["seeded"] and g2["seeded"])
        self.assertEqual(g1["lesson_ids"], ["ogs_9001-S1"])
        self.assertEqual(g2["lesson_ids"], [])
        self.assertEqual(g1["dashboard_url"], "https://claude.ai/artifact/seed")
        self.assertEqual((l1["source"], l1["status"], l1["recall"], l1["better"]), ("seed", "open", [], tm["best"]))
        self.assertEqual(fin["profile_seed"]["lessons"], 1)
        # finished: the same documents again, and no more recording
        self.assertEqual(self.tools.seed_finish()["batches"], fin["batches"])
        with self.assertRaises(ToolError) as cm:
            self.tools.seed_record(3, "skipped")
        self.assertEqual(cm.exception.code, "seed_finished")
        # a new seeding archives the finished one
        r2 = self.start()
        self.assertFalse(r2["resumed"])
        self.assertEqual([p["finished_at"] is not None for p in r2["previous"]], [True])

    def test_batches_hold_at_most_50_writes(self):
        names = [write_game(self.tools.cfg.games_dir, 9100 + k, 40, 30 + k) for k in range(30)]
        self.tools.cfg.seed.ready_ahead = 0
        self.tools.seed_start(games=names)
        wait_until(self.tools, settled, timeout=60)
        for k in range(30):
            g = self.tools.seed_game(k + 1)
            m = g["story"]["moments"]
            if m:
                self.tools.seed_record(k + 1, "confirmed", story="S.", lesson={"move": m[0]["move"], "better": m[0]["best"],
                                                                                "title": "T", "takeaway": "X.", "theme": "t"})
            else:
                self.tools.seed_record(k + 1, "no_lesson", story="S.")
        fin = self.tools.seed_finish()
        self.assertTrue(all(len(b) <= 50 for b in fin["batches"]))
        self.assertEqual(sum(len(b) for b in fin["batches"]), fin["games_written"] + fin["lessons_written"])

    def test_resume_after_a_server_restart(self):
        self.start()
        wait_until(self.tools, settled)
        self.tools.seed_record(1, "skipped")
        self.tools.close()
        self.tools = make_tools(self.tmp.name, seed__ready_ahead=2, seed__survey_visits=40, seed__min_moves=40)
        st = self.tools.seed_status()                     # the jobs are gone; the surveys are on disk
        self.assertEqual(st["recorded"], 1)
        self.assertEqual(st["next"]["game_id"], "ogs_9002")
        self.assertTrue(all(g["state"] in ("stored", "ready") for g in st["games"]))
        r = self.start(restart=False)
        self.assertTrue(r["resumed"])
        self.assertIn("note", r)                          # games were given, but the unfinished seeding was resumed
        self.assertTrue(self.tools.seed_game()["ready"])
        r = self.start(restart=True)
        self.assertFalse(r["resumed"])
        self.assertEqual(r["previous"][0]["games"], 3)
        self.assertIsNotNone(r["previous"][0]["cancelled_at"])

    def test_seed_page_boards(self):
        self.start()
        wait_until(self.tools, settled)
        g = self.tools.seed_game()
        b = self.tools.dashboard_row("board", g["job_id"], {"title": "As played", "at_move": 10}, page="seed")
        self.assertEqual(b["doc_id"], "ogs_9001-b01")
        self.assertEqual(b["row"]["id"], b["doc_id"])
        self.assertEqual(b["row"]["sha256"], dashboard_row_sha(b["row"]))
        with self.assertRaises(ToolError):
            self.tools.dashboard_row("game", g["job_id"], page="seed")
        with self.assertRaises(ToolError):
            self.tools.dashboard_row("board", g["job_id"], {"title": "x", "at_move": 1}, page="other")


class StoredSurveyWhileBusyTest(unittest.TestCase):
    def test_a_stored_survey_loads_while_another_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = make_tools(tmp, engine=MockEngine(seconds_per_kvisit=0.01))
            try:
                a, b = synthetic_game(40, seed=51), synthetic_game(60, seed=52)
                t.wait_for_job(t.start_game_analysis(a, {"visits_per_move": 20}, options={"prefetch": False})["job_id"], 0.02, timeout=30)
                running = t.start_game_analysis(b, {"visits_per_move": 400}, options={"prefetch": False})
                self.assertFalse(running["reused"])
                again = t.start_game_analysis(a, {"visits_per_move": 20}, options={"prefetch": False})
                self.assertTrue(again["reused"])
                with self.assertRaises(ToolError) as cm:          # a new survey still waits for the engine
                    t.start_game_analysis(synthetic_game(30, seed=53), {"visits_per_move": 20})
                self.assertEqual(cm.exception.code, "engine_busy")
                t.job_status(running["job_id"], "cancel")
                t.wait_for_job(running["job_id"], 0.02, timeout=30)
            finally:
                t.close()


class OgsListTest(unittest.TestCase):
    """The student's recent games from OGS: the player lookup, pages of games, what is left out and why."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tools = make_tools(self.tmp.name, seed__survey_visits=20, seed__min_moves=40)
        self.sgfs = {gid: synthetic_game(n, seed=60 + k).replace(f"game/{1000 + 60 + k}", f"game/{gid}")
                     for k, (gid, n) in enumerate(((7001, 70), (7002, 30), (7003, 60), (7004, 64), (7005, 50)))}
        listed = [{"id": 7005, "ended": "2026-10-05T10:00:00Z", "width": 19, "height": 19},
                  {"id": 7099, "ended": "2026-10-05T09:00:00Z", "width": 19, "height": 19, "annulled": True, "outcome": "Cancellation"},
                  {"id": 7004, "ended": "2026-10-04T10:00:00Z", "width": 19, "height": 19},
                  {"id": 7098, "ended": None, "width": 19, "height": 19}]
        listed2 = [{"id": 7003, "ended": "2026-10-03T10:00:00Z", "width": 19, "height": 19},
                   {"id": 7097, "ended": "2026-10-02T10:00:00Z", "width": 9, "height": 9},
                   {"id": 7002, "ended": "2026-10-01T10:00:00Z", "width": 19, "height": 19},
                   {"id": 7001, "ended": "2026-09-30T10:00:00Z", "width": 19, "height": 19}]
        self.calls = []
        base = self.tools.cfg.ogs.base_url

        def fake(req, timeout=0):
            url = req.full_url
            self.calls.append(url)
            if "/players/?username=" in url:
                body = {"results": [{"id": 42, "username": "Student1"}]}
            elif "/players/42/games/" in url:
                body = {"results": listed2, "next": None} if "page=2" in url else {"results": listed, "next": url + "&page=2"}
            elif url.endswith("/sgf"):
                return FakeResp(self.sgfs[int(url.split("/")[-2])].encode())
            else:
                raise AssertionError(url)
            return FakeResp(json.dumps(body).encode())

        self.base = base
        self.real = urllib.request.urlopen
        urllib.request.urlopen = fake

    def tearDown(self):
        urllib.request.urlopen = self.real
        self.tools.close()
        self.tmp.cleanup()

    def test_recent_games(self):
        r = self.tools.seed_start(count=3, exclude=["https://online-go.com/game/7004"])
        self.assertEqual(r["source"], "ogs")
        # newest first on OGS; the seeding takes them in the order they were played
        self.assertEqual([g["game_id"] for g in r["games"]], ["ogs_7001", "ogs_7003", "ogs_7005"])
        self.assertEqual({k["game"]: k["reason"] for k in r["skipped"]},
                         {"ogs_7004": "reviewed already", "ogs_7002": "only 30 moves"})
        self.assertEqual(r["ignored"], {"annulled or cancelled": 1, "unfinished": 1, "not 19x19": 1})
        self.assertTrue(self.calls[0].startswith(self.base + "/api/v1/players/?username=student1"))
        self.assertFalse(any("/7099/" in c or "/7098/" in c for c in self.calls))     # never downloaded
        wait_until(self.tools, settled)

    def test_unknown_player(self):
        self.tools.cfg.student.username = "nobody"
        with self.assertRaises(ToolError) as cm:
            self.tools.seed_start()
        self.assertEqual(cm.exception.code, "ogs_fetch_failed")


class FakeResp:
    def __init__(self, data): self.data = data
    def read(self): return self.data
    def __enter__(self): return self
    def __exit__(self, *a): return False


class CliSeedTest(unittest.TestCase):
    def test_prepare_then_resume_in_the_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "config"))
            toml = os.path.join(tmp, "config", "box.toml")
            with open(toml, "w") as f:
                f.write('[student]\nusername = "student1"\n[throughput]\nvisits_per_second_sustained = 650\n'
                        '[seed]\nsurvey_visits = 20\nmin_moves = 40\n')
            gd = os.path.join(tmp, "games")
            names = [write_game(gd, 9201, 50, 71), write_game(gd, 9202, 45, 72)]
            self.assertEqual(cli.main(["seed", "--config", toml, "--mock", "--game", names[0], "--game", names[1]]), 0)
            t = make_tools(tmp, seed__survey_visits=20, seed__min_moves=40)
            try:
                st = t.seed_status()
                self.assertEqual([g["state"] for g in st["games"]], ["stored", "stored"])
                self.assertTrue(t.seed_start()["resumed"])
                self.assertTrue(t.seed_game()["ready"])
            finally:
                t.close()


if __name__ == "__main__":
    unittest.main()
