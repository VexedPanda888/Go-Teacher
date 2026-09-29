"""MCP server (stdio).  Thin layer: registers the public `Tools` methods (their docstrings are the tool
descriptions) and converts ToolError to an error dict."""
from __future__ import annotations

import functools
import inspect
import logging
import os
import sys

from . import __version__
from .config import load_config
from .tools import PUBLIC_TOOLS, ToolError, Tools

log = logging.getLogger("katago_mcp")


def build_server(config_path: str | None = None, engine=None, start_engine: bool = True):
    try:
        from mcp.server.fastmcp import FastMCP   # mcp 1.x (pinned in pyproject: mcp>=1.2,<2)
    except ImportError:
        try:
            from mcp.server.mcpserver import MCPServer as FastMCP   # mcp 2.x renamed the class; API may differ
            print("katago-mcp: running on mcp 2.x via the MCPServer fallback; if tools misbehave run "
                  "`pip install 'mcp<2'`", file=sys.stderr)
        except ImportError:
            raise SystemExit("katago-mcp needs the mcp package (1.x): run `pip install 'mcp>=1.2,<2'` in the venv")

    cfg = load_config(config_path or os.environ.get("KATAGO_MCP_CONFIG"))
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    tools = Tools(cfg, engine=engine, start_engine=False)
    if start_engine and cfg.katago.start_on_boot:
        # Optional eager start. Default is lazy: Claude Desktop launches two server instances (chat and the
        # Cowork/Code pool) and only the one that is used should load a model.
        tools.start_engine_background()
    log.info("katago-mcp %s serving; engine %s", __version__,
             "starting" if cfg.katago.start_on_boot else "starts on first use")
    server = FastMCP("katago-mcp", instructions=(
        "KataGo analysis for Go teaching. Coordinates are GTP (A1..T19, no I). Scores are points from the "
        "stated perspective; ownership is Black-positive. Call plan_budget before verification work."))

    def guarded(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return fn(*a, **kw)
            except ToolError as e:
                return e.to_dict()
            except Exception as e:  # noqa: BLE001
                log.exception("tool failure")
                return ToolError("internal", f"{type(e).__name__}: {e}", recoverable=True).to_dict()
        return wrapper

    for name in PUBLIC_TOOLS:
        fn = getattr(tools, name)
        server.tool(name=name, description=inspect.getdoc(fn))(guarded(fn))

    server._katago_tools = tools   # for tests and the CLI
    return server


def main(config_path: str | None = None) -> None:
    server = build_server(config_path)
    try:
        server.run(transport="stdio")
    finally:
        server._katago_tools.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
