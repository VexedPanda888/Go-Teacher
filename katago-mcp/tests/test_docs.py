import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from katago_mcp import CONTRACT_VERSION, __version__  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
CONTRACT = os.path.join(REPO, "skills", "go-teacher-flow", "references", "tool-contract.md")


class TestToolContractDoc(unittest.TestCase):
    def test_header_names_the_code_versions(self):
        with open(CONTRACT, encoding="utf-8") as f:
            header = f.readline()
        self.assertIn(f"v{CONTRACT_VERSION}", header)
        self.assertIn(f"katago-mcp {__version__}", header)


if __name__ == "__main__":
    unittest.main()
