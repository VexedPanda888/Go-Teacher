import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from katago_mcp.budget import plan, unit_visits, solve_visits, line_node_searches, survey_visits, BudgetError  # noqa: E402
from katago_mcp.config import BudgetConfig  # noqa: E402


class TestUnits(unittest.TestCase):
    def test_base_and_cap_unit_visits(self):
        cfg = BudgetConfig()
        # intent 8 + expectation (2p + 5) + two forced lines 2 × (2p + 13) = 6p + 39 line-node searches
        self.assertEqual(line_node_searches(6), 75)
        self.assertEqual(unit_visits(cfg.unit_base), 1000 + 4 * 1000 + 75 * 250)      # 23,750
        self.assertEqual(solve_visits(cfg.unit_base), 3000)
        self.assertEqual(unit_visits(cfg.unit_cap), 6000 + 20 * 6000 + 87 * 1000)     # 213,000


class TestWorkedExamples(unittest.TestCase):
    def test_m5_pro_40_minutes(self):
        cfg = BudgetConfig()
        p = plan(cfg, vps=650, move_count=187, total_minutes=40)
        self.assertTrue(p["feasible"])
        # survey: 650*600/187 = 2085 -> capped at 1000 (approved change from 500)
        self.assertEqual(p["survey"]["visits_per_move"], 1000)
        self.assertAlmostEqual(p["survey"]["expected_minutes"], 187 * 1000 / 650 / 60, places=1)   # ~4.8 min, inside S
        self.assertFalse(p["survey"]["runs_past_self_review"])
        # 40 − 4 overhead − 5 blind self-review − 5 interviews
        self.assertEqual(p["verification"]["wall_minutes_available"], 26.0)
        self.assertEqual(p["reserved"], {"overhead_minutes": 4.0, "self_review_minutes": 5.0, "interview_minutes": 5.0})
        self.assertEqual(p["verification"]["episodes"], 5)
        steps = p["verification"]["ladder_steps_applied"]
        self.assertEqual(steps, ["root_3000", "line_600", "stability_16x", "plies_8", "solve_4000_all_ld"])
        pe = p["verification"]["per_episode"]
        self.assertEqual(pe["root_visits"], 3000)
        self.assertEqual(pe["line_node_visits"], 600)
        self.assertEqual(pe["forced_line_plies"], 8)
        self.assertEqual(pe["stability_multipliers"], [4, 16])
        self.assertEqual(pe["local_solve_visits"], 4000)
        self.assertEqual(p["profiles"]["stability"], [12000, 48000])
        self.assertLessEqual(p["verification"]["expected_minutes"], 26.0)
        self.assertLessEqual(p["expected_total_minutes"], 40.0)

    def test_m2_air_20_minutes_infeasible(self):
        cfg = BudgetConfig()
        p = plan(cfg, vps=30, move_count=187, total_minutes=20)
        self.assertFalse(p["feasible"])
        self.assertEqual(p["survey"]["visits_per_move"], 100)      # floor
        self.assertTrue(p["survey"]["runs_past_self_review"])
        self.assertAlmostEqual(p["survey"]["charged_minutes"], 187 * 100 / 30 / 60 - 5, places=1)   # past the blind review
        self.assertLess(p["verification"]["episodes"], 3)
        # the probes cost about twice the old three-line contrast: rigor is kept, episodes drop
        self.assertAlmostEqual(p["minimum_minutes_for_three_episodes"], 63.6, delta=0.6)
        self.assertTrue(any("three-episode" in n for n in p["notes"]))

    def test_short_blind_review_keeps_survey_visits(self):
        cfg = BudgetConfig()
        p = plan(cfg, vps=120, move_count=187, total_minutes=40)
        self.assertEqual(p["survey"]["visits_per_move"], int(120 * 10 * 60 / 187))   # sized by survey_minutes_target
        self.assertAlmostEqual(p["survey"]["charged_minutes"], 5.0, places=1)
        legacy = plan(cfg, vps=120, move_count=187, total_minutes=40, self_review_minutes=5)
        self.assertEqual(legacy["survey"]["visits_per_move"], int(120 * 5 * 60 / 187))

    def test_unlimited(self):
        cfg = BudgetConfig()
        p = plan(cfg, vps=650, move_count=187, total_minutes="unlimited")
        self.assertTrue(p["feasible"])
        self.assertEqual(p["mode"], "unlimited")
        self.assertEqual(p["survey"]["visits_per_move"], 1000)
        self.assertEqual(p["verification"]["episodes"], 5)
        self.assertEqual(p["verification"]["per_episode"]["root_visits"], 6000)
        self.assertGreater(p["expected_total_minutes"], 20)

    def test_replan_after_triage(self):
        cfg = BudgetConfig()
        selected = [{"id": "E1", "needs_local_solve": True}, {"id": "E2", "needs_local_solve": False},
                    {"id": "E3", "needs_local_solve": True}]
        p = plan(cfg, vps=650, move_count=187, total_minutes=40, selected=selected, elapsed_minutes=14.0,
                 survey_visits_existing=1000)
        self.assertEqual(p["mode"], "replan")
        self.assertTrue(p["feasible"])
        self.assertEqual(p["verification"]["episodes"], 3)
        self.assertEqual(p["verification"]["ld_episodes_budgeted"], 2)
        self.assertEqual(p["verification"]["wall_minutes_available"], 17.0)   # 40 − 4 − 14 elapsed − 5 interviews
        self.assertIn("solve_4000_all_ld", p["verification"]["ladder_steps_applied"])

    def test_replan_too_many_selected(self):
        cfg = BudgetConfig()
        selected = [{"id": f"E{i}", "needs_local_solve": True} for i in range(1, 6)]
        p = plan(cfg, vps=100, move_count=187, total_minutes=30, selected=selected, elapsed_minutes=15.0,
                 survey_visits_existing=300)
        self.assertFalse(p["feasible"])
        self.assertLess(p["verification"]["episodes"], 5)
        self.assertTrue(any("fit at base rigor" in n for n in p["notes"]))

    def test_survey_visits_and_quick_profile(self):
        cfg = BudgetConfig()
        self.assertEqual(survey_visits(cfg, 120, 187), int(120 * 10 * 60 / 187))
        self.assertEqual(survey_visits(cfg, 650, 187), cfg.survey_cap)
        self.assertEqual(survey_visits(cfg, 1, 187), cfg.survey_floor)
        self.assertEqual(plan(cfg, vps=650, move_count=187, total_minutes=40)["profiles"]["quick"], 200)
        self.assertEqual(plan(cfg, vps=650, move_count=187, total_minutes=40, quick_visits=300)["profiles"]["quick"], 300)

    def test_no_throughput(self):
        with self.assertRaises(BudgetError):
            plan(BudgetConfig(), vps=0, move_count=100, total_minutes=30)


if __name__ == "__main__":
    unittest.main()
