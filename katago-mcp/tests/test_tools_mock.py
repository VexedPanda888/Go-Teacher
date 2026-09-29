import hashlib
import json
import os
import random
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from katago_mcp import CONTRACT_VERSION  # noqa: E402
from katago_mcp.board import BLACK, WHITE, Board, IllegalMove  # noqa: E402
from katago_mcp.config import Config  # noqa: E402
from katago_mcp.coords import idx_to_gtp, idx_to_sgf, gtp_to_idx, star_points  # noqa: E402
from katago_mcp.engine import MockEngine  # noqa: E402
from katago_mcp.tools import Tools, ToolError, encode_ownership, decode_ownership  # noqa: E402


def synthetic_game(n_moves: int = 70, seed: int = 7, handicap: int = 0) -> str:
    """Random-but-legal game biased toward the corners and existing stones."""
    rng = random.Random(seed)
    board = Board()
    setup = []
    if handicap:
        hs = ["Q16", "D4", "Q4", "D16", "K10", "D10", "Q10", "K16", "K4"][:handicap]
        for p in hs:
            board.place(BLACK, gtp_to_idx(p))
            setup.append(idx_to_sgf(gtp_to_idx(p)))
    color = WHITE if handicap >= 2 else BLACK
    moves = []
    stars = list(star_points())
    for n in range(n_moves):
        cands = []
        if n < 6 and not handicap:
            cands = [s for s in stars if board.cells[s] == 0]
        else:
            stones = board.stones()
            near = set()
            for s in stones:
                r, c = divmod(s, 19)
                for rr in range(max(0, r - 2), min(19, r + 3)):
                    for cc in range(max(0, c - 2), min(19, c + 3)):
                        i = rr * 19 + cc
                        if board.cells[i] == 0:
                            near.add(i)
            cands = list(near)
        rng.shuffle(cands)
        for idx in cands:
            try:
                board = board.play(color, idx, "japanese")
                moves.append((color, idx))
                break
            except IllegalMove:
                continue
        color = BLACK if color == WHITE else WHITE
    body = "".join(f";{'B' if c == BLACK else 'W'}[{idx_to_sgf(i)}]" for c, i in moves)
    ab = f"HA[{handicap}]AB" + "".join(f"[{s}]" for s in setup) if handicap else ""
    komi = "0.5" if handicap else "6.5"
    return (f"(;GM[1]FF[4]SZ[19]{ab}KM[{komi}]RU[Japanese]PB[cwhay888]BR[7k]PW[rival]WR[6k]RE[W+R]"
            f"PC[OGS: https://online-go.com/game/{1000 + seed}]{body})")


class MockToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = Config()
        cfg.reviews_dir = os.path.join(cls.tmp.name, "reviews")
        cfg.throughput.visits_per_second_sustained = 650.0
        cls.tools = Tools(cfg, engine=MockEngine(), start_engine=True)
        cls.sgf = synthetic_game(70, seed=7)
        cls.summary = cls.tools.sgf_summary(cls.sgf)
        cls.plan = cls.tools.plan_budget(40, move_count=70)
        cls.started = cls.tools.start_game_analysis(cls.sgf, {"profile": "survey"})
        cls.job_id = cls.started["job_id"]
        for _ in range(600):
            st = cls.tools.job_status(cls.job_id)
            if st["state"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.05)
        cls.status = st
        cls.digest = cls.tools.job_results(cls.job_id, "digest", max_episodes=6)

    @classmethod
    def tearDownClass(cls):
        cls.tools.close()
        cls.tmp.cleanup()

    # ---------------------------------------------------------------- summary / plan / job
    def test_sgf_summary(self):
        s = self.summary
        self.assertEqual(s["student"]["color"], "B")
        self.assertEqual(s["opponent"]["human_profile"], "rank_6k")
        self.assertEqual(s["moves"]["count"], 70)
        self.assertEqual(s["rules"]["katago_rules"], "japanese")
        self.assertEqual(s["result"]["method"], "resign")
        self.assertEqual(s["moves"]["resign_after_move"], 70)
        self.assertEqual([b["after_move"] for b in s["boards"]], [50, 70])
        self.assertTrue(s["boards"][0]["ascii"].startswith("Black to move" if 50 % 2 == 0 else "White to move"))
        self.assertEqual(s["profiles"], {"peer": "rank_7k", "target": "rank_4k", "horizon": "rank_1d", "opponent": "rank_6k"})

    def test_plan_and_job(self):
        self.assertTrue(self.plan["feasible"])
        self.assertEqual(self.started["visits_per_move"], self.plan["profiles"]["survey"])
        self.assertEqual(self.status["state"], "done", self.status.get("error"))
        self.assertEqual(self.status["positions_done"], 71)
        d = self.digest
        self.assertTrue(d["complete"])
        self.assertEqual(d["game"]["student_color"], "B")
        self.assertEqual(d["phases"]["opening"][0], 1)
        self.assertGreaterEqual(d["points_lost"]["student"]["total"], 0.0)
        self.assertEqual(d["points_lost"]["student"]["moves"], 35)
        eps = d["episodes"]
        self.assertTrue(eps, "mock game should produce at least one episode")
        totals = [e["points_lost_total"] for e in eps]
        self.assertEqual(totals, sorted(totals, reverse=True))
        e = eps[0]
        for key in ("root", "region", "phase", "acceptable_set", "signature", "style_axis", "human", "learnability",
                    "candidate_tags", "pattern_hash", "stability", "group_status_change"):
            self.assertIn(key, e)
        self.assertIn(e["region"]["standard"], ("UL", "U", "UR", "L", "C", "R", "LL", "D", "LR"))
        self.assertTrue(e["pattern_hash"].startswith("ph_"))
        self.assertIn("peer", e["human"]["played"])
        self.assertLessEqual(len(e["candidate_tags"]), 3)
        self.assertIn(e["best_reply"]["character"], ("tenuki", "local_calm", "local_sharp"))
        self.assertEqual(e["best_reply"]["local"], e["best_reply"]["character"] != "tenuki")
        self.assertIn(d["game_type"]["type"], ("single_blunder", "accumulation", "mixed"))
        rows = self.tools.job_results(self.job_id, "moves", range=[10, 12])["moves"]
        self.assertEqual([r["n"] for r in rows], [10, 11, 12])
        self.assertNotIn("idx", rows[0])

    def test_reuse_existing(self):
        r = self.tools.start_game_analysis(self.sgf, {"visits_per_move": self.started["visits_per_move"]})
        self.assertTrue(r["reused"])
        self.assertEqual(r["state"], "done")

    def test_budget_replan_with_job(self):
        sel = [{"id": "E1", "needs_local_solve": True}, {"id": "E2", "needs_local_solve": False}]
        p = self.tools.plan_budget(40, job_id=self.job_id, selected=sel)
        self.assertEqual(p["mode"], "replan")
        self.assertEqual(p["verification"]["episodes"], 2)
        self.assertEqual(p["survey"]["visits_per_move"], self.started["visits_per_move"])

    # ---------------------------------------------------------------- position tools
    def test_position_ref_and_analyze(self):
        ref = self.tools.get_position_ref(job_id=self.job_id, move_number=30)
        self.assertTrue(ref["position_ref"].startswith("pos_"))
        self.assertEqual(ref["move_number"], 30)
        self.assertEqual(ref["to_move"], "B")
        self.assertIsNotNone(ref["cached_analysis"])
        a = self.tools.analyze_position({"ref": ref["position_ref"]}, {"profile": "root"}, {"include_ownership": True})
        self.assertEqual(a["perspective"], "B")   # student color from the job
        self.assertEqual(len(a["ownership"]), 361)
        self.assertTrue(a["candidates"])
        self.assertIn("peer", a["candidates"][0]["human"])
        self.assertTrue(a["acceptable_set"]["moves"])
        self.assertTrue(a["groups"])
        in_race = {g["anchor"] for r in a.get("capture_races", []) for g in r["groups"]}
        self.assertTrue(all(("liberties" in g) == (g["anchor"] in in_race) for g in a["groups"]))
        # same position via job_id resolves to the same ref, and via `then` to a different one
        a2 = self.tools.analyze_position({"job_id": self.job_id, "move_number": 30}, {"visits": 50}, {"perspective": "W"})
        self.assertEqual(a2["position_ref"], ref["position_ref"])
        self.assertAlmostEqual(a2["root"]["score_lead"], -a["root"]["score_lead"], places=1)
        best = a["candidates"][0]["move"]
        a3 = self.tools.analyze_position({"ref": ref["position_ref"], "then": [["B", best]]}, {"visits": 50})
        self.assertNotEqual(a3["position_ref"], ref["position_ref"])
        self.assertEqual(a3["to_move"], "W")

    def test_analyze_position_errors(self):
        with self.assertRaises(ToolError) as cm:
            self.tools.analyze_position({"ref": "pos_doesnotexist0000"})
        self.assertEqual(cm.exception.code, "unknown_ref")
        with self.assertRaises(ToolError) as cm:
            self.tools.analyze_position({"job_id": self.job_id, "move_number": 5, "then": [["W", "pass"], ["B", "A1"], ["W", "A1"]]})
        self.assertEqual(cm.exception.code, "illegal_move")
        self.assertEqual(cm.exception.details["ply"], 3)

    def test_analyze_line_three_line_contrast(self):
        pos = {"job_id": self.job_id, "move_number": 40}
        a = self.tools.analyze_position(pos, {"visits": 100})
        line = self.tools.analyze_line(pos, [{"color": "B", "move": a["candidates"][1]["move"] if len(a["candidates"]) > 1 else a["candidates"][0]["move"]},
                                             {"engine": True}], {"visits": 60}, follow_pv_plies=3)
        self.assertEqual(len(line["nodes"]), 5)
        self.assertTrue(line["nodes"][0]["forced"])
        self.assertFalse(line["nodes"][1]["forced"])
        self.assertEqual(line["nodes"][1]["color"], "W")
        self.assertIn("refutation_probability", line)
        self.assertIn("ownership", line["end"])
        self.assertIn("vs_best", line["summary"])
        self.assertEqual(line["legality"], "ok")
        with self.assertRaises(ToolError) as cm:
            self.tools.analyze_line(pos, [{"color": "W", "move": "K10"}], {"visits": 20})
        self.assertEqual(cm.exception.code, "bad_request")

    def test_forced_line_and_terminal_features(self):
        pos = {"job_id": self.job_id, "move_number": 40}
        a = self.tools.analyze_position(pos, {"visits": 100})
        best = a["candidates"][0]["move"]
        # every reply counts as forced: the line runs to max_plies, with legible choices and resistance
        fl = self.tools.forced_line(pos, best, {"visits": 60}, {"max_plies": 4, "forced_margin": -100})
        self.assertEqual(fl["line"][0], "B" + best)
        self.assertEqual(fl["stop_reason"], "max_plies")
        self.assertEqual(len(fl["nodes"]), 4)
        self.assertEqual(len(fl["line"]), 5)
        self.assertEqual([n["color"] for n in fl["nodes"]], ["W", "B", "W", "B"])
        self.assertTrue(all(n["chosen_by"] in ("engine", "human") for n in fl["nodes"]))
        self.assertTrue(any("resistance" in n for n in fl["nodes"] if n["color"] == "W"))
        end = fl["end"]
        self.assertEqual(len(end["territory"]), 9)
        self.assertIn(end["sente"]["holder"], ("you", "opponent"))
        self.assertIsInstance(end["tempo"]["value"], float)
        self.assertTrue(all("stones" not in g for g in end["groups"]))
        # extend "forced" stops at the first reply that is not forced
        fl2 = self.tools.forced_line(pos, a["candidates"][-1]["move"], {"visits": 60},
                                     {"max_plies": 4, "forced_margin": 100, "extend": "forced"})
        self.assertEqual(fl2["stop_reason"], "not_forced")
        self.assertEqual(fl2["nodes"], [])
        self.assertEqual(fl2["free_at_end"]["side"], "W")
        self.assertEqual(fl2["end"]["sente"]["holder"], "opponent")      # W to move and free
        # extend "local" (default) keeps following local best moves, marked not forced, until a quiet move
        fl3 = self.tools.forced_line(pos, best, {"visits": 60}, {"max_plies": 3, "forced_margin": 100})
        self.assertIn(fl3["stop_reason"], ("quiet", "max_plies", "pass"))
        self.assertTrue(all(n["forced"] is False for n in fl3["nodes"]))
        # endpoint comparison
        tf = self.tools.terminal_features({"ref": fl2["end"]["position_ref"]}, {"ref": end["position_ref"]}, {"visits": 60})
        c = tf["comparison"]
        for key in ("score_diff", "groups_changed", "territory_changed", "sente", "tempo", "weak_groups"):
            self.assertIn(key, c)
        self.assertAlmostEqual(c["score_diff"], tf["b"]["score_lead"] - tf["a"]["score_lead"], places=1)
        with self.assertRaises(ToolError) as cm:
            self.tools.forced_line(pos, "W" + best, {"visits": 20})
        self.assertEqual(cm.exception.code, "bad_request")

    def test_intent_probe(self):
        pos = {"job_id": self.job_id, "move_number": 40}
        a = self.tools.analyze_position(pos, {"visits": 100})
        mv = a["candidates"][-1]["move"]
        r = self.tools.intent_probe(pos, mv, {"visits": 60})
        self.assertEqual(r["player"], "B")
        self.assertEqual(r["move"], mv)
        for key in ("score", "reply", "threat", "tenuki_value", "defense", "left_behind", "better_move", "risk", "matches"):
            self.assertIn(key, r)
        self.assertIn(r["reply"]["character"], ("tenuki", "local_calm", "local_sharp"))
        self.assertGreaterEqual(r["score"]["loss"], 0.0)
        ids = {"needs_defending", "group_is_safe", "is_sente", "behind_must_invade", "ahead_can_coast", "sequence_works", "biggest_move"}
        self.assertTrue(set(r["matches"]) <= ids)
        if r["belief"]:
            self.assertEqual(r["belief"]["id"], r["matches"][0])
            self.assertTrue(r["belief"]["categories"])
        with self.assertRaises(ToolError):
            self.tools.intent_probe(pos, "pass", {"visits": 20})

    def test_expectation_probe(self):
        pos = {"job_id": self.job_id, "move_number": 40}
        a = self.tools.analyze_position(pos, {"visits": 100})
        mv = a["candidates"][0]["move"]
        # a negative margin makes the first imagined move that is not the engine's the misread
        r = self.tools.expectation_probe(pos, mv, {"visits": 60}, {"plies": 4, "misread_margin": -1})
        self.assertEqual(r["move"]["move"], mv)
        m = r["misread"]
        if m is None:
            self.assertEqual(len(r["nodes"]), 4)
            self.assertTrue(all(n["move"] == n["engine_best"] for n in r["nodes"]))
        else:
            self.assertEqual(len(r["nodes"]), m["ply"])
            self.assertEqual(m["whose"], "opponent" if m["color"] == "W" else "you")
            self.assertEqual(m["refutation"][0], m["color"] + m["never_considered"])
            self.assertEqual(len(m["line_to_here"]), m["ply"])
        # a stated line is followed move for move
        board_after = self.tools._resolve_position({**pos, "then": [["B", mv]]}).board
        reply = idx_to_gtp(board_after.empties()[100])
        r2 = self.tools.expectation_probe(pos, mv, {"visits": 60}, {"plies": 1, "expected_line": [f"W{reply}"], "misread_margin": 999})
        self.assertEqual(r2["nodes"][0]["move"], reply)
        self.assertEqual(r2["nodes"][0]["source"], "stated")
        self.assertIsNone(r2["misread"])
        with self.assertRaises(ToolError) as cm:
            self.tools.expectation_probe(pos, mv, {"visits": 20}, {"expected_line": [f"B{reply}"]})
        self.assertEqual(cm.exception.code, "bad_request")

    def test_pass_probe_and_regions(self):
        pos = {"job_id": self.job_id, "move_number": 40}
        r = self.tools.pass_probe(pos, "B", None, {"visits": 60}, {"rank_regions": True})
        self.assertIn("score_if_pass", r)
        self.assertIsNotNone(r["local_value"]["best"])
        self.assertEqual(len(r["urgency"]), 9)
        self.assertGreaterEqual(r["urgency"][0]["value"], r["urgency"][-1]["value"])
        with self.assertRaises(ToolError):
            self.tools.pass_probe(pos, "W", None, {"visits": 20})

    def test_swing_value(self):
        pos = {"job_id": self.job_id, "move_number": 40}
        board = self.tools._resolve_position(pos).board
        pts = [idx_to_gtp(i) for i in board.empties()[:3]]
        r = self.tools.swing_value(pos, pts, {"visits": 40})
        self.assertEqual(len(r["results"]), 3)
        self.assertEqual(len(r["ranked"]), 3)
        self.assertIn(r["results"][0]["sente_gote"]["for_black"], ("sente", "gote", "unclear"))

    def test_local_solve_and_status_quiz(self):
        pos = {"job_id": self.job_id, "move_number": 60}
        board = self.tools._resolve_position(pos).board
        groups = sorted(board.groups(), key=lambda g: -g.size)
        gp = idx_to_gtp(groups[0].anchor)
        r = self.tools.local_solve(pos, gp, None, {"visits": 30}, {"max_plies": 6})
        self.assertIn(r["status"], ("alive", "dead", "unsettled", "unclear"))
        self.assertIn(r["confidence"], ("high", "medium", "low"))
        self.assertTrue(r["attacker_first"]["sequence"])
        self.assertIn(r["query_id"], self.tools.solve_results)
        with self.assertRaises(ToolError) as cm:
            self.tools.local_solve(pos, idx_to_gtp(board.empties()[0]), None, {"visits": 10})
        self.assertEqual(cm.exception.code, "no_group_at_point")

    def test_group_status_ownership_diff_human_render(self):
        a = {"job_id": self.job_id, "move_number": 44}
        b = {"job_id": self.job_id, "move_number": 45}
        gs = self.tools.group_status(a)
        self.assertTrue(gs["groups"])
        self.assertEqual(set(gs["summary"].keys()), {"B", "W"})
        od = self.tools.ownership_diff(a, b)
        self.assertEqual(len(od["regional"]), 9)
        self.assertIn(od["local_vs_global"]["classification"], ("local", "mixed", "global"))
        hm = self.tools.human_move_distribution(a, ["peer", "rank_2d"], ["K10"])
        self.assertEqual(hm["profiles"]["peer"]["profile"], "rank_7k")
        self.assertEqual(hm["profiles"]["rank_2d"]["profile"], "rank_2d")
        self.assertIn("K10", hm["profiles"]["peer"]["moves_of_interest"])
        rb = self.tools.render_board(a, {"overlay": "ownership", "highlight": ["K10"], "region_box": {"standard": "UL"}})
        self.assertIn("legend", rb)
        self.assertEqual(len(rb["ascii"].splitlines()), 22)
        self.assertEqual(len(rb["overlay_ascii"].splitlines()), 21)
        self.assertNotIn("low_liberty_groups", rb)             # off by default since contract v0.3
        self.assertIn("low_liberty_groups", self.tools.render_board(a, {"label_low_liberties": True}))

    # ---------------------------------------------------------------- export
    def test_validate_variations_export(self):
        d = self.digest
        ep = d["episodes"][0]
        n = ep["root"]["move"]
        a = self.tools.analyze_position({"job_id": self.job_id, "move_number": n - 1}, {"visits": 80})
        best = a["candidates"][0]["move"]
        branch_moves = [f"B{best}"]
        episodes = [{
            "id": "E1", "moves": ep["moves"], "title": "Test episode", "category": "5", "tags": ep["candidate_tags"],
            "points_lost": ep["points_lost_total"],
            "commentary": [{"at_move": n, "text": "The played move loses points."}],
            "branches": [
                {"id": "B1", "label": "Engine's move", "from_move": n - 1, "moves": branch_moves, "ledger_ref": "H1"},
                {"id": "B2", "label": "As played", "from_move": n - 1, "moves": [f"B{ep['root']['played']}"]},
            ],
            "quiz": {"at_move": n, "type": "move", "candidates": [best]},
            "principle": "Check liberties before extending.", "cue": "Two-liberty group nearby.",
        }]
        r = self.tools.validate_variations(self.job_id, episodes, {"headline": "x"})
        self.assertTrue(r["valid"], r["errors"])
        self.assertTrue(any("never diverges" in w for w in r["warnings"]))
        blob = r["dashboard_data"]
        self.assertEqual(hashlib.sha256(blob.encode("utf-8")).hexdigest(), r["sha256"])
        data = json.loads(blob)
        self.assertEqual(data["game"]["you"], "B")
        self.assertEqual(len(data["moves"]), 70)
        self.assertEqual(len(data["scoreSeries"]), 71)
        e1 = data["episodes"][0]
        self.assertEqual(len(e1["branches"]), 2)
        self.assertEqual(e1["branches"][0]["moves"], branch_moves)
        self.assertEqual(len(e1["branches"][0]["evals"]), 1)
        self.assertIn(e1["branches"][0]["ownershipAtEnd"], data["ownership"])
        self.assertEqual(len(data["ownership"][f"m{n}"]), 361)
        labels = {c["move"]: c["labels"] for c in e1["quiz"]["candidates"]}
        self.assertIn("actual", labels[ep["root"]["played"]])
        self.assertTrue(any("peer" in l for l in labels.values()))
        # a branch off another branch, a comparison of two branch ends, rule_check and belief
        fl = self.tools.forced_line({"job_id": self.job_id, "move_number": n - 1}, best, {"visits": 40},
                                    {"max_plies": 3, "forced_margin": -100})
        nested = [{
            "id": "E1", "moves": ep["moves"], "title": "Nested", "category": "14", "tags": [], "points_lost": 1.0,
            "commentary": [], "rule_check": "Name the attack you fear and read your answer.",
            "belief": {"id": "needs_defending", "source": "stated"},
            "branches": [
                {"id": "B1", "label": "Better: forced line", "kind": "better", "from_move": n - 1, "moves": fl["line"]},
                {"id": "B2", "label": "As played", "kind": "as_played", "from_move": n - 1, "moves": [f"B{ep['root']['played']}"]},
                {"id": "B3", "label": "If White resists", "kind": "resistance", "from_branch": "B1", "at_ply": 1,
                 "moves": fl["line"][1:2]},
            ],
            "comparison": {"a": "B2", "b": "B1"},
        }]
        r3 = self.tools.validate_variations(self.job_id, nested, {"headline": "x"})
        self.assertTrue(r3["valid"], r3["errors"])
        e3 = json.loads(r3["dashboard_data"])["episodes"][0]
        b3 = e3["branches"][2]
        self.assertEqual(b3["parentBranch"], "B1")
        self.assertEqual(b3["branchPly"], 1)
        self.assertEqual(b3["fromMove"], n - 1)
        self.assertEqual(b3["moves"], fl["line"][:2])
        self.assertEqual(len(b3["evals"]), 2)
        self.assertEqual(e3["branches"][0]["kind"], "better")
        self.assertEqual(e3["ruleCheck"], "Name the attack you fear and read your answer.")
        self.assertEqual(e3["belief"]["id"], "needs_defending")
        cmp_ = e3["comparison"]
        self.assertEqual((cmp_["a"], cmp_["b"], cmp_["aLabel"]), ("B2", "B1", "As played"))
        for key in ("scoreDiff", "groups", "territory", "sente", "nextMove", "weakGroups"):
            self.assertIn(key, cmp_)
        bad_nested = [{**nested[0], "branches": [nested[0]["branches"][2]], "comparison": {"a": "B1", "b": "B9"}}]
        r4 = self.tools.validate_variations(self.job_id, bad_nested)
        self.assertEqual({e["code"] for e in r4["errors"]}, {"bad_branch_parent", "bad_comparison"})
        # illegal branch and wrong color are reported, not exported
        bad = [{"id": "E2", "moves": ep["moves"], "branches": [
            {"id": "B1", "from_move": n - 1, "moves": ["WK10"]},
            {"id": "B2", "from_move": n - 1, "moves": [f"B{ep['root']['played']}", f"W{ep['root']['played']}"]}]}]
        r2 = self.tools.validate_variations(self.job_id, bad)
        self.assertFalse(r2["valid"])
        self.assertEqual({e["code"] for e in r2["errors"]}, {"wrong_color", "illegal_move"})
        self.assertNotIn("dashboard_data", r2)

    def test_ownership_codec(self):
        vals = [-1.0, -0.55, 0.0, 0.37, 1.0]
        s = encode_ownership(vals)
        self.assertEqual(s, "a" + chr(ord("a") + 4) + "k" + chr(ord("a") + 14) + "u")
        back = decode_ownership(s)
        for v, b in zip(vals, back):
            self.assertLessEqual(abs(v - b), 0.05)

    def test_engine_info_and_logs(self):
        info = self.tools.engine_info()
        self.assertEqual(info["contract_version"], CONTRACT_VERSION)
        self.assertEqual(info["backend"], "mock")
        self.assertEqual(info["student"]["peer"], "rank_7k")
        log_path = os.path.join(self.tools.cfg.reviews_dir, self.started["game_id"], "queries.jsonl")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path) as f:
            lines = f.read().splitlines()
        self.assertTrue(all(json.loads(l)["query_id"].startswith("q_ogs_1007_") for l in lines))
        self.assertTrue(os.path.exists(os.path.join(self.tools.cfg.reviews_dir, self.started["game_id"], "analysis.json")))


class HandicapDigestTest(unittest.TestCase):
    def test_handicap_game_uses_score_basis(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.reviews_dir = os.path.join(tmp, "reviews")
            cfg.throughput.visits_per_second_sustained = 650.0
            t = Tools(cfg, engine=MockEngine(), start_engine=True)
            sgf = synthetic_game(40, seed=3, handicap=4)
            s = t.sgf_summary(sgf)
            self.assertEqual(s["rules"]["handicap"], 4)
            self.assertEqual(len(s["rules"]["setup"]["B"]), 4)
            r = t.start_game_analysis(sgf, {"visits_per_move": 60})
            for _ in range(400):
                if t.job_status(r["job_id"])["state"] in ("done", "failed"):
                    break
                time.sleep(0.05)
            d = t.job_results(r["job_id"])
            self.assertTrue(d["complete"])
            self.assertEqual(d["game"]["handicap"], 4)
            self.assertEqual(d["game"]["reconciliation"]["status"], "n/a")
            if d["decisive"]:
                self.assertEqual(d["decisive"]["basis"], "score")
            t.close()


class SgfInputTest(unittest.TestCase):
    """sgf inputs: text, file path (games dir / absolute), OGS id or link (fetched, cached)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config()
        cfg.reviews_dir = os.path.join(self.tmp.name, "reviews")
        cfg.games_dir = os.path.join(self.tmp.name, "games")
        cfg.throughput.visits_per_second_sustained = 650.0
        self.tools = Tools(cfg, engine=MockEngine())
        self.sgf = synthetic_game(30, seed=41)

    def tearDown(self):
        self.tools.close()
        self.tmp.cleanup()

    def test_file_by_name_in_games_dir_and_absolute(self):
        os.makedirs(self.tools.cfg.games_dir, exist_ok=True)
        p = os.path.join(self.tools.cfg.games_dir, "ogs_555001.sgf")
        with open(p, "w") as f:
            f.write(self.sgf.replace("PC[OGS: https://online-go.com/game/1041]", ""))   # no PC: id comes from the file name
        s = self.tools.sgf_summary("ogs_555001.sgf")
        self.assertEqual(s["game_id"], "ogs_555001")
        self.assertEqual(s["input"]["kind"], "file")
        self.assertEqual(s["source"]["ogs_game_id"], "555001")
        s2 = self.tools.sgf_summary(p)
        self.assertEqual(s2["game_id"], "ogs_555001")
        r = self.tools.start_game_analysis(p, {"visits_per_move": 30})
        self.assertEqual(r["game_id"], "ogs_555001")
        # let the background job finish before tearDown deletes the folder it writes into
        for _ in range(400):
            if self.tools.job_status(r["job_id"])["state"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.02)

    def test_ogs_fetch_is_cached_and_used_by_all_entry_points(self):
        import urllib.request
        calls = []

        class FakeResp:
            def __init__(self, data): self.data = data
            def read(self): return self.data
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=0):
            calls.append(req.full_url)
            return FakeResp(self.sgf.encode("utf-8"))

        real = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        try:
            s = self.tools.sgf_summary("https://online-go.com/game/78123456")
            self.assertEqual(s["game_id"], "ogs_78123456")
            self.assertEqual(s["input"]["kind"], "ogs")
            self.assertTrue(calls[0].endswith("/api/v1/games/78123456/sgf"))
            self.assertTrue(os.path.exists(os.path.join(self.tools.cfg.games_dir, "ogs_78123456.sgf")))
            s2 = self.tools.sgf_summary("78123456")          # bare id, served from the cache
            self.assertEqual(len(calls), 1)
            self.assertIn("cached", s2["input"])
            ref = self.tools.get_position_ref(sgf="78123456", move_number=5)
            self.assertEqual(ref["move_number"], 5)
        finally:
            urllib.request.urlopen = real

    def test_ogs_http_error_and_bad_input(self):
        import urllib.error, urllib.request
        real = urllib.request.urlopen

        def failing(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

        urllib.request.urlopen = failing
        try:
            with self.assertRaises(ToolError) as cm:
                self.tools.sgf_summary("online-go.com/game/99999999")
            self.assertEqual(cm.exception.code, "ogs_fetch_failed")
        finally:
            urllib.request.urlopen = real
        with self.assertRaises(ToolError) as cm:
            self.tools.sgf_summary("not-a-game")
        self.assertEqual(cm.exception.code, "bad_request")
        s = self.tools.sgf_summary(self.sgf)                 # raw text still works
        self.assertEqual(s["input"]["kind"], "text")


class LenientInputsTest(unittest.TestCase):
    """Argument shapes a model plausibly sends: string steps, bare refs, bare visit counts, colour words."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = Config()
        cfg.reviews_dir = os.path.join(cls.tmp.name, "reviews")
        cfg.throughput.visits_per_second_sustained = 650.0
        cls.tools = Tools(cfg, engine=MockEngine())
        cls.sgf = synthetic_game(40, seed=61)
        r = cls.tools.start_game_analysis(cls.sgf, {"visits_per_move": 40})
        cls.job = r["job_id"]
        for _ in range(400):
            if cls.tools.job_status(cls.job)["state"] == "done":
                break
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.tools.close()
        cls.tmp.cleanup()

    def test_line_steps_as_strings(self):
        pos = {"job_id": self.job, "move_number": 20}
        a = self.tools.analyze_position(pos, 60)                    # bare number = visits
        best = a["candidates"][0]["move"]
        pv = a["candidates"][0]["pv"]
        third = "W" + pv[1] if len(pv) > 1 else "engine"
        r = self.tools.analyze_line(pos, [f"B{best}", "engine", third], "line_node", follow_pv_plies=1)
        self.assertGreaterEqual(len(r["nodes"]), 3)
        self.assertTrue(r["nodes"][0]["forced"])
        self.assertFalse(r["nodes"][1]["forced"])
        r2 = self.tools.analyze_line(pos, [best], 30, follow_pv_plies=0)   # bare point: colour = side to move
        self.assertEqual(r2["nodes"][0]["color"], "B")
        with self.assertRaises(ToolError) as cm:
            self.tools.analyze_line(pos, ["W" + best], 30, follow_pv_plies=0)
        self.assertEqual(cm.exception.code, "bad_request")

    def test_bare_ref_and_colour_words(self):
        ref = self.tools.get_position_ref(job_id=self.job, move_number=20)["position_ref"]
        a = self.tools.analyze_position(ref, "quick")                # bare ref string, profile string
        self.assertEqual(a["position_ref"], ref)
        p = self.tools.pass_probe(ref, "black", None, 40)
        self.assertEqual(p["player"], "B")
        hm = self.tools.human_move_distribution(ref, "peer, target", "K10 D4")
        self.assertEqual(set(hm["profiles"]), {"peer", "target"})
        self.assertIn("D4", hm["profiles"]["peer"]["moves_of_interest"])
        with self.assertRaises(ToolError):
            self.tools.analyze_position(12345)


if __name__ == "__main__":
    unittest.main()
