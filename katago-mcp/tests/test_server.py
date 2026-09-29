import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from katago_mcp.engine import MockEngine  # noqa: E402
from katago_mcp.tools import PUBLIC_TOOLS  # noqa: E402


class ServerRegistrationTest(unittest.TestCase):
    def test_tools_registered_with_descriptions(self):
        from katago_mcp.server import build_server
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "test.toml")
            with open(cfg, "w") as f:
                f.write('[paths]\nreviews_dir = "reviews"\n[throughput]\nvisits_per_second_sustained = 650.0\n')
            server = build_server(cfg, engine=MockEngine(), start_engine=False)
            try:
                tools = asyncio.run(server.list_tools())
                self.assertEqual(len(PUBLIC_TOOLS), 21)
                self.assertEqual([t.name for t in tools], list(PUBLIC_TOOLS))
                self.assertTrue(all(t.description for t in tools))
                pb = next(t for t in tools if t.name == "plan_budget")
                self.assertEqual(pb.inputSchema["required"], ["total_minutes"])
                res = asyncio.run(server.call_tool("plan_budget", {"total_minutes": 40, "move_count": 70}))
                content = res[0] if isinstance(res, tuple) else res
                self.assertTrue(json.loads(content[0].text)["feasible"])
                res = asyncio.run(server.call_tool("job_status", {"job_id": "job_nope"}))
                content = res[0] if isinstance(res, tuple) else res
                self.assertEqual(json.loads(content[0].text)["error"]["code"], "job_not_found")
            finally:
                server._katago_tools.close()


if __name__ == "__main__":
    unittest.main()
