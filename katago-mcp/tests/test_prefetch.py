"""explain_moment (contract §1.20), its background preparation of key moments, and the stored results (§0.8)."""
import tempfile
import threading
import time
import unittest

from _helpers import make_tools, quiet_points  # noqa: E402  (also puts the package on sys.path)
from katago_mcp.engine import MockEngine  # noqa: E402
from katago_mcp.prefetch import ResultStore, StoredResult  # noqa: E402
from katago_mcp.tools import ToolError  # noqa: E402
from test_tools_mock import synthetic_game  # noqa: E402


class RecordingEngine(MockEngine):
    """MockEngine that records the KataGo priority and thread of every search."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def analyze(self, spec, max_visits, *a, priority: int = 0, **kw):
        self.calls.append((threading.current_thread().name, priority))
        return super().analyze(spec, max_visits, *a, priority=priority, **kw)


def wait_prepared(tools, job_id, timeout=60.0) -> list[str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        states = [m["prepared"] for m in tools.job_results(job_id)["moments"][:tools.cfg.prefetch.moments]]
        if all(s in ("done", "failed") for s in states):
            return states
        time.sleep(0.1)
    return states


class ExplainMomentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.engine = RecordingEngine()
        cls.tools = make_tools(cls.tmp.name, engine=cls.engine)
        cls.job_id = cls.tools.start_game_analysis(synthetic_game(70, seed=11))["job_id"]
        cls.tools.wait_for_job(cls.job_id, 0.05, timeout=30)
        cls.moments = cls.tools.job_results(cls.job_id)["moments"]

    @classmethod
    def tearDownClass(cls):
        cls.tools.close()
        cls.tmp.cleanup()

    def test_key_moments_are_prepared_in_the_background(self):
        self.assertEqual(wait_prepared(self.tools, self.job_id), ["done"] * min(3, len(self.moments)))
        bg = [p for name, p in self.engine.calls if name == "katago-prefetch"]
        self.assertTrue(bg)
        self.assertTrue(all(p == self.tools.cfg.prefetch.priority for p in bg))
        m = self.moments[0]
        r = self.tools.explain_moment({"job_id": self.job_id, "move_number": m["move"] - 1})
        self.assertIn("precomputed", r)                        # Claude's identical call: the stored result
        self.assertNotEqual(r["query_id"], r["precomputed"]["query_id"])
        self.assertEqual(r["move"]["move"], m["played"])        # the move defaults to the one played

    def test_the_evidence(self):
        m = self.moments[0]
        r = self.tools.explain_moment({"job_id": self.job_id, "move_number": m["move"] - 1})
        self.assertEqual(r["perspective"], "B")
        self.assertIn(r["move"]["verdict"], ("best", "as_good", "mistake"))
        self.assertGreaterEqual(r["move"]["points_lost"], 0.0)
        self.assertEqual(set(r["best"]["human"]), {"peer", "target"})
        self.assertTrue(r["stability"]["visits"] >= self.tools.cfg.search.root)
        best_line = r["lines"]["best"]
        self.assertEqual(best_line["line"][0], "B" + r["best"]["move"])
        self.assertEqual(len(best_line["forced"]), len(best_line["line"]) - 1)
        self.assertIn(best_line["end"]["sente"], ("you", "opponent"))
        if r["move"]["move"] != r["best"]["move"]:
            self.assertEqual(r["lines"]["move"]["line"][0], "B" + r["move"]["move"])
            for key in ("score_diff", "groups_changed", "territory_changed", "sente", "tempo"):
                self.assertIn(key, r["comparison"])
            self.assertIn(r["purpose"]["move"]["reply"]["character"], ("tenuki", "local_calm", "local_sharp"))
        self.assertIn("threat", r["purpose"]["best"])
        self.assertTrue(all(q.startswith("q_") for q in r["query_ids"].values()))

    def test_what_about_another_move_and_the_students_reading(self):
        n = self.moments[0]["move"]
        pos = {"job_id": self.job_id, "move_number": n - 1}
        before = self.tools._resolve_position(pos).board
        x, a, b = quiet_points(before, 3)
        r = self.tools.explain_moment(pos, x, {"expected_line": [a, b], "deep": False})
        self.assertEqual(r["move"]["move"], x)
        self.assertIsNone(r["stability"])
        self.assertEqual(r["reading"]["source"], "stated")
        self.assertEqual(r["reading"]["line"][0], "B" + x)
        with self.assertRaises(ToolError) as cm:           # the expected line is checked before any search
            self.tools.explain_moment(pos, x, {"expected_line": ["B" + a]})
        self.assertEqual(cm.exception.code, "wrong_color")
        with self.assertRaises(ToolError) as cm:           # no game move to default to
            self.tools.explain_moment({"moves": ["BD4", "WQ16"]})
        self.assertEqual(cm.exception.code, "bad_request")

    def test_background_request(self):
        n = self.moments[-1]["move"]
        out = self.tools.explain_moment({"job_id": self.job_id, "move_number": n - 2}, background=True)
        self.assertTrue(out["queued"])
        self.assertIsInstance(out["estimate_seconds"], int)
        deadline = time.time() + 60
        while time.time() < deadline and self.tools.prefetch.state(self.job_id, n - 2, out["move"]) not in ("done", "failed"):
            time.sleep(0.1)
        self.assertEqual(self.tools.prefetch.state(self.job_id, n - 2, out["move"]), "done")
        self.assertIn("precomputed", self.tools.explain_moment({"job_id": self.job_id, "move_number": n - 2}))


class PrefetchOffTest(unittest.TestCase):
    def test_no_prefetch_when_turned_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = make_tools(tmp)
            try:
                jid = t.start_game_analysis(synthetic_game(60, seed=3), {"visits_per_move": 50}, options={"prefetch": False})["job_id"]
                t.wait_for_job(jid, 0.05, timeout=30)
                time.sleep(0.2)
                self.assertEqual(t.prefetch.pending(jid), [])
                self.assertTrue(all(m["prepared"] == "no" for m in t.job_results(jid)["moments"]))
            finally:
                t.close()

    def test_release_forgets_the_prefetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = make_tools(tmp)
            try:
                jid = t.start_game_analysis(synthetic_game(60, seed=5), {"visits_per_move": 50})["job_id"]
                t.wait_for_job(jid, 0.05, timeout=30)
                wait_prepared(t, jid)
                t.job_status(jid, "release")
                self.assertEqual(t.prefetch.pending(jid), [])
            finally:
                t.close()


class ResultStoreTest(unittest.TestCase):
    def test_identical_call_waits_for_the_one_in_flight(self):
        rs = ResultStore()
        self.assertIsNone(rs.acquire("k", 1.0))           # claimed
        self.assertEqual(rs.peek("k"), "running")
        got = []
        th = threading.Thread(target=lambda: got.append(rs.acquire("k", 5.0)))
        th.start()
        time.sleep(0.1)
        self.assertEqual(got, [])                          # still waiting
        rs.put("k", StoredResult({"x": 1}, "g", "q1"))
        th.join(2)
        self.assertEqual(got[0].result, {"x": 1})
        self.assertEqual(rs.peek("k"), "done")
        self.assertEqual(rs.forget_game("g"), 1)

    def test_release_lets_the_waiter_compute(self):
        rs = ResultStore()
        rs.acquire("k", 1.0)
        rs.release("k")
        self.assertIsNone(rs.acquire("k", 1.0))


if __name__ == "__main__":
    unittest.main()
