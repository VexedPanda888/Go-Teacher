"""Background verification (contract §1.22–§1.24), the stored probe results and sealing (§0.8)."""
import tempfile
import threading
import time
import unittest

from _helpers import make_tools  # noqa: E402  (also puts the package on sys.path)
from katago_mcp.coords import idx_to_gtp, neighbors  # noqa: E402
from katago_mcp.engine import MockEngine  # noqa: E402
from katago_mcp.tools import ToolError  # noqa: E402
from katago_mcp.verify import ResultStore, StoredResult  # noqa: E402
from test_tools_mock import synthetic_game  # noqa: E402


class RecordingEngine(MockEngine):
    """MockEngine that records the KataGo priority and thread of every search."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def analyze(self, spec, max_visits, *a, priority: int = 0, **kw):
        self.calls.append((threading.current_thread().name, priority))
        return super().analyze(spec, max_visits, *a, priority=priority, **kw)


def quiet_points(board, k: int) -> list[str]:
    """k empty points with no stone next to them (always legal), far apart enough to be distinct."""
    out = []
    for i in range(361):
        if board.cells[i] == 0 and all(board.cells[n] == 0 for n in neighbors(i, 19)):
            out.append(idx_to_gtp(i))
            if len(out) == k:
                break
    return out


class VerificationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = RecordingEngine()
        self.tools = make_tools(self.tmp.name, engine=self.engine)
        self.sgf = synthetic_game(70, seed=11)
        self.tools.plan_budget(40, move_count=70)
        self.job_id = self.tools.start_game_analysis(self.sgf, {"profile": "survey"})["job_id"]
        self.tools.wait_for_job(self.job_id, 0.05, timeout=30)
        self.digest = self.tools.job_results(self.job_id, "digest", max_episodes=10)
        self.eps = self.digest["episodes"]

    def tearDown(self):
        self.tools.close()
        self.tmp.cleanup()

    def wait_idle(self, timeout=60):
        return self.tools.verification_results(self.job_id, wait_seconds=timeout)

    def test_speculative_work_after_the_survey(self):
        view = self.wait_idle()
        ids = [e["episode"] for e in view["episodes"]]
        self.assertTrue(ids)
        self.assertLessEqual(len(ids), self.tools.cfg.verification.speculative_episodes)
        self.assertTrue(all(not e["selected"] and not e["sealed"] for e in view["episodes"]))
        # the background searches ran below Claude's priority, on the verification thread
        bg = [p for name, p in self.engine.calls if name == "katago-verify"]
        self.assertTrue(bg)
        self.assertTrue(all(p == self.tools.cfg.verification.background_priority for p in bg))
        # a speculative result serves Claude's identical call (not selected, so not sealed)
        ep = next(e for e in self.eps if e["id"] == ids[0])
        r = self.tools.intent_probe({"ref": ep["root"]["position_ref_before"]}, ep["root"]["played"])
        self.assertIn("precomputed", r)
        self.assertNotEqual(r["query_id"], r["precomputed"]["query_id"])

    def test_no_speculation_without_a_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = make_tools(tmp)
            try:
                jid = t.start_game_analysis(synthetic_game(60, seed=3), {"visits_per_move": 50})["job_id"]
                t.wait_for_job(jid, 0.05, timeout=30)
                time.sleep(0.2)
                with self.assertRaises(ToolError) as cm:
                    t.verification_results(jid)
                self.assertEqual(cm.exception.code, "verification_not_started")
            finally:
                t.close()

    def test_sealed_until_the_interview(self):
        self.wait_idle()
        e1, e2 = self.eps[0], self.eps[1]
        out = self.tools.start_verification(self.job_id, [{"id": e1["id"]}, {"id": e2["id"], "local_solve": True}])
        self.assertEqual(out["interview_order"], [e2["id"], e1["id"]])     # the local solve is interviewed first
        self.assertTrue(out["sealed"])
        before = e1["root"]["position_ref_before"]
        # nothing about a selected episode before its answer: results withheld, probes there refused
        v = self.tools.verification_results(self.job_id, e1["id"], wait_seconds=60)
        self.assertTrue(v["sealed"])
        self.assertNotIn("results", v)
        self.assertTrue(all("query_id" not in t and "note" not in t for t in v["tasks"]))
        with self.assertRaises(ToolError) as cm:
            self.tools.intent_probe({"ref": before}, e1["root"]["played"])
        self.assertEqual(cm.exception.code, "sealed")
        with self.assertRaises(ToolError):
            self.tools.analyze_position({"ref": before})
        # render_board stays available (no engine), but not with an engine overlay
        self.assertIn("ascii", self.tools.render_board({"ref": before}))
        with self.assertRaises(ToolError):
            self.tools.render_board({"ref": before}, {"overlay": "ownership"})
        # the interview unseals and queues the answer work
        after_board = self.tools._resolve_position({"ref": e1["root"]["position_ref_after"]}).board
        a, b = quiet_points(after_board, 2)
        r = self.tools.record_interview(self.job_id, e1["id"], answer="I wanted to secure the corner",
                                        expected_line=[a, b], confidence=4)
        self.assertFalse(r["sealed"])
        self.assertEqual(len(r["expected_line"]), 2)
        self.assertIn("expectation_probe", r["queued"])
        v = self.tools.verification_results(self.job_id, e1["id"], wait_seconds=60)
        self.assertEqual(v["state"], "done")
        res = v["results"]
        for name in ("intent_probe", "forced_line_best", "forced_line_played", "terminal_features", "root",
                     "expectation_probe"):
            self.assertIn(name, res, name)
        self.assertIn("comparison", res["terminal_features"])
        self.assertEqual(res["expectation_probe"]["nodes"][0]["source"], "stated")
        self.assertTrue(any(k.startswith("stability_x") for k in res))
        self.assertIsNotNone(v["stability_check"])
        self.assertEqual(v["interview"]["answers"][0]["confidence"], 4)
        # after the interview Claude's own calls return the stored results
        again = self.tools.intent_probe({"ref": before}, e1["root"]["played"])
        self.assertEqual(again["belief"], res["intent_probe"]["belief"])
        self.assertIn("precomputed", again)
        # parts limits the output
        only = self.tools.verification_results(self.job_id, e1["id"], parts=["intent_probe"])
        self.assertEqual(list(only["results"]), ["intent_probe"])

    def test_answer_work_runs_before_other_episodes(self):
        self.wait_idle()
        e1, e2, e3 = self.eps[0], self.eps[1], self.eps[2]
        self.tools.start_verification(self.job_id, [e1["id"], e2["id"], e3["id"]])
        self.tools.record_interview(self.job_id, e1["id"], answer="no idea")
        mgr = self.tools.verify
        with mgr._cv:
            order = [(ref[1], tk.name) for ref, tk in mgr._queue_order() if tk.state == "pending"]
        if order:                       # the answer work of E1 comes before E2's and E3's remaining work
            answer_pos = [i for i, (eid, name) in enumerate(order) if eid == e1["id"] and name == "expectation_probe"]
            other_pos = [i for i, (eid, _n) in enumerate(order) if eid != e1["id"]]
            if answer_pos and other_pos:
                self.assertLess(answer_pos[0], other_pos[0])
        v = self.tools.verification_results(self.job_id, e1["id"], wait_seconds=60)
        self.assertEqual(v["results"]["expectation_probe"]["expected_source"], "peer")
        self.assertTrue(self.tools.verification_results(self.job_id, e2["id"])["sealed"])

    def test_bad_lines_are_rejected_and_nothing_changes(self):
        self.wait_idle()
        e1 = self.eps[0]
        self.tools.start_verification(self.job_id, [e1["id"]])
        occupied = e1["root"]["played"]           # the played stone is on the board after the move
        with self.assertRaises(ToolError) as cm:
            self.tools.record_interview(self.job_id, e1["id"], expected_line=[occupied])
        self.assertEqual(cm.exception.code, "illegal_move")
        self.assertTrue(self.tools.verification_results(self.job_id, e1["id"])["sealed"])
        with self.assertRaises(ToolError) as cm:
            self.tools.record_interview(self.job_id, "E99", answer="?")
        self.assertEqual(cm.exception.code, "episode_not_selected")

    def test_fix_and_lines_from_later_answers(self):
        self.wait_idle()
        e1 = self.eps[0]
        self.tools.start_verification(self.job_id, [e1["id"]])
        self.tools.record_interview(self.job_id, e1["id"], answer="for the corner")
        before = self.tools._resolve_position({"ref": e1["root"]["position_ref_before"]}).board
        fix, a, b = quiet_points(before, 3)
        r = self.tools.record_interview(self.job_id, e1["id"], fix=fix,
                                        lines=[{"label": "resist", "moves": [a, b], "from": "before"}])
        self.assertEqual(r["fixes"], [fix])
        self.assertIn(f"fix:{fix}", r["queued"])
        v = self.tools.verification_results(self.job_id, e1["id"], wait_seconds=60)
        self.assertIn(f"fix:{fix}", v["results"])
        self.assertIn("line:resist", v["results"])
        self.assertEqual(v["results"]["line:resist"]["nodes"][0]["move"], a)

    def test_replan_keeps_sizes_when_results_exist(self):
        self.wait_idle()
        ids = [e["id"] for e in self.eps[:3]]
        p = self.tools.plan_budget(40, job_id=self.job_id, selected=[{"id": i, "needs_local_solve": False} for i in ids])
        self.assertTrue(any("kept the earlier" in n for n in p["notes"]))
        q = self.tools.plan_budget(40, job_id=self.job_id, selected=[{"id": i, "needs_local_solve": False} for i in ids],
                                   keep_sizes=False)
        self.assertFalse(any("kept the earlier" in n for n in q["notes"]))

    def test_release_forgets_the_verification(self):
        self.wait_idle()
        self.tools.job_status(self.job_id, "release")
        self.assertNotIn(self.job_id, self.tools.verify.episodes)


class ResultStoreTest(unittest.TestCase):
    def test_identical_call_waits_for_the_one_in_flight(self):
        rs = ResultStore()
        self.assertIsNone(rs.acquire("k", 1.0))           # claimed
        got = []
        th = threading.Thread(target=lambda: got.append(rs.acquire("k", 5.0)))
        th.start()
        time.sleep(0.1)
        self.assertEqual(got, [])                          # still waiting
        rs.put("k", StoredResult({"x": 1}, None, "g", "q1"))
        th.join(2)
        self.assertEqual(got[0].result, {"x": 1})
        self.assertEqual(rs.forget_game("g"), 1)

    def test_release_lets_the_waiter_compute(self):
        rs = ResultStore()
        rs.acquire("k", 1.0)
        rs.release("k")
        self.assertIsNone(rs.acquire("k", 1.0))


if __name__ == "__main__":
    unittest.main()
