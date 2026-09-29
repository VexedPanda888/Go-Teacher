import os
import tempfile
import tomllib
import unittest

from _helpers import ROOT  # noqa: E402  (also puts the package on sys.path)
from katago_mcp.config import load_config  # noqa: E402
from katago_mcp.engine import KataGoEngine  # noqa: E402

MACHINES = ("m5pro", "m2air", "r5700xt")


def machine_toml(name: str) -> str:
    return os.path.join(ROOT, "config", f"{name}.toml")


class TestMachineConfigs(unittest.TestCase):
    def test_search_threads_reach_the_engine_command(self):
        for name in MACHINES:
            cfg = load_config(machine_toml(name))
            self.assertIsInstance(cfg.katago.search_threads, int, name)
            eng = KataGoEngine(cfg.katago.binary, cfg.katago.analysis_config, cfg.katago.model,
                               cfg.katago.human_model, search_threads=cfg.katago.search_threads)
            cmd = eng.command()
            i = cmd.index("-override-config")
            self.assertEqual(cmd[i + 1], f"numSearchThreadsPerAnalysisThread={cfg.katago.search_threads}")

    def test_no_override_without_search_threads(self):
        self.assertNotIn("-override-config", KataGoEngine("katago", "a.cfg", "m.bin.gz").command())

    def test_shared_sections_are_identical_across_machines(self):
        """Calibration edits [thresholds] (and budget/student) once per machine; keep the three in sync."""
        data = {}
        for name in MACHINES:
            with open(machine_toml(name), "rb") as f:
                data[name] = tomllib.load(f)
        for section in ("student", "budget", "thresholds", "paths"):
            first = data[MACHINES[0]].get(section)
            for name in MACHINES[1:]:
                self.assertEqual(data[name].get(section), first, f"[{section}] differs in {name}.toml")


class TestLoadConfig(unittest.TestCase):
    def test_budget_units_and_throughput_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "box.toml")
            with open(p, "w") as f:
                f.write("[budget]\nsurvey_cap = 800\n[budget.unit_base]\nroot = 1234\n[budget.unit_cap]\nplies = 10\n")
            cfg = load_config(p)
            self.assertEqual(cfg.budget.survey_cap, 800)
            self.assertEqual((cfg.budget.unit_base.root, cfg.budget.unit_base.line_node), (1234, 250))
            self.assertEqual((cfg.budget.unit_cap.plies, cfg.budget.unit_cap.root), (10, 6000))
            self.assertEqual(str(cfg.throughput_path), os.path.join(tmp, "box.throughput.json"))
        self.assertIsNone(load_config(None).throughput_path)


if __name__ == "__main__":
    unittest.main()
