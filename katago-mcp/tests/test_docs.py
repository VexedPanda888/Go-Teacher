import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from katago_mcp import CONTRACT_VERSION, __version__  # noqa: E402

REPO = os.path.join(os.path.dirname(__file__), "..", "..")
CANONICAL = os.path.join(REPO, "skills", "go-teacher-flow", "references", "tool-contract.md")
DOCS_COPY = os.path.join(REPO, "docs", "tool-contract.md")


class TestToolContractDoc(unittest.TestCase):
    def test_docs_copy_matches_skill_copy(self):
        with open(CANONICAL, encoding="utf-8") as a, open(DOCS_COPY, encoding="utf-8") as b:
            self.assertEqual(a.read(), b.read(),
                             "docs/tool-contract.md differs from the skill copy; copy the skill copy over it")

    def test_header_names_the_code_versions(self):
        with open(CANONICAL, encoding="utf-8") as f:
            header = f.readline()
        self.assertIn(f"v{CONTRACT_VERSION}", header)
        self.assertIn(f"katago-mcp {__version__}", header)


if __name__ == "__main__":
    unittest.main()
