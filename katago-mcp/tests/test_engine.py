import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from katago_mcp.engine import EngineError, KataGoEngine  # noqa: E402

FAKE = os.path.join(os.path.dirname(__file__), "fake_katago.py")


class FakeKataGo(KataGoEngine):
    def command(self) -> list[str]:
        return [sys.executable, FAKE]


class TestKataGoProtocol(unittest.TestCase):
    def setUp(self):
        self.engine = FakeKataGo("katago", "analysis.cfg", "model.bin.gz", startup_timeout=10, query_timeout=10)
        self.engine.start()
        self.addCleanup(self.engine.stop)

    def _query(self, qid):
        return self.engine.query({"id": qid, "moves": [], "rules": "japanese", "komi": 6.5,
                                  "boardXSize": 19, "boardYSize": 19, "maxVisits": 100})

    def test_a_warning_is_not_the_answer(self):
        with self.assertLogs("katago_mcp", level="WARNING"):
            res = self._query("warn1")
        self.assertEqual(res["rootInfo"]["visits"], 100)
        self.assertNotIn("warning", res)

    def test_no_results_is_an_error(self):
        with self.assertRaises(EngineError):
            self._query("nores1")

    def test_plain_result(self):
        self.assertEqual(self._query("q1")["moveInfos"][0]["move"], "D4")


if __name__ == "__main__":
    unittest.main()
