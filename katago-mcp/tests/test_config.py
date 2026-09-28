import os
import sys
import tomllib
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from katago_mcp.config import load_config  # noqa: E402
from katago_mcp.engine import KataGoEngine  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
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


if __name__ == "__main__":
    unittest.main()
