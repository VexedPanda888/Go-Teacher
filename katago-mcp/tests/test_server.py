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
                self.assertEqual(len(PUBLIC_TOOLS), 24)
                self.assertEqual([t.name for t in tools], list(PUBLIC_TOOLS))
                self.assertTrue(all(t.description for t in tools))
                em = next(t for t in tools if t.name == "explain_moment")
                self.assertEqual(em.inputSchema["required"], ["position"])
                for gone in ("plan_budget", "start_verification", "record_interview", "verification_results"):
                    self.assertNotIn(gone, PUBLIC_TOOLS)
                sr = next(t for t in tools if t.name == "seed_record")
                self.assertEqual(sr.inputSchema["required"], ["game", "status"])
                res = asyncio.run(server.call_tool("seed_status", {}))
                content = res[0] if isinstance(res, tuple) else res
                self.assertIsNone(json.loads(content[0].text)["session"])
                res = asyncio.run(server.call_tool("engine_info", {}))
                content = res[0] if isinstance(res, tuple) else res
                self.assertEqual(json.loads(content[0].text)["search"]["root"], 3000)
                res = asyncio.run(server.call_tool("job_status", {"job_id": "job_nope"}))
                content = res[0] if isinstance(res, tuple) else res
                self.assertEqual(json.loads(content[0].text)["error"]["code"], "job_not_found")
            finally:
                server._katago_tools.close()


if __name__ == "__main__":
    unittest.main()
