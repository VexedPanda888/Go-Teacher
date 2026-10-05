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
    def test_slower_machines_search_less(self):
        sizes = {n: load_config(machine_toml(n)).search for n in MACHINES}
        self.assertGreater(sizes["m5pro"].root, sizes["m2air"].root)
        self.assertGreaterEqual(sizes["m5pro"].line_node, sizes["r5700xt"].line_node)

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
        """[thresholds], [prefetch], [student], [paths] and most of [seed] are the same on every machine; [search] differs."""
        data = {}
        for name in MACHINES:
            with open(machine_toml(name), "rb") as f:
                data[name] = tomllib.load(f)
        for section in ("student", "thresholds", "prefetch", "paths"):
            first = data[MACHINES[0]].get(section)
            for name in MACHINES[1:]:
                self.assertEqual(data[name].get(section), first, f"[{section}] differs in {name}.toml")
        # [seed] is shared except the survey size, which follows the machine's speed
        seed = {n: {k: v for k, v in data[n]["seed"].items() if k != "survey_visits"} for n in MACHINES}
        for name in MACHINES[1:]:
            self.assertEqual(seed[name], seed[MACHINES[0]], f"[seed] differs in {name}.toml")
        for name in MACHINES:
            self.assertEqual(load_config(machine_toml(name)).seed.survey_visits, data[name]["seed"]["survey_visits"])


class TestLoadConfig(unittest.TestCase):
    def test_search_sizes_and_throughput_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "box.toml")
            with open(p, "w") as f:
                f.write("[search]\nsurvey_cap = 800\nroot = 1234\n[budget]\nmin_episodes = 3\n[prefetch]\nmoments = 0\n")
            cfg = load_config(p)             # an old [budget] section is ignored
            self.assertEqual((cfg.search.survey_cap, cfg.search.root, cfg.search.line_node), (800, 1234, 600))
            self.assertEqual(cfg.prefetch.moments, 0)
            self.assertFalse(hasattr(cfg, "budget"))
            self.assertEqual(str(cfg.throughput_path), os.path.join(tmp, "box.throughput.json"))
        self.assertIsNone(load_config(None).throughput_path)


if __name__ == "__main__":
    unittest.main()
