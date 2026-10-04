"""Shared test setup: the package on sys.path, and Tools on the mock engine in a temporary directory."""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from katago_mcp.config import Config  # noqa: E402
from katago_mcp.coords import idx_to_gtp, neighbors  # noqa: E402
from katago_mcp.engine import MockEngine  # noqa: E402
from katago_mcp.tools import Tools  # noqa: E402


def quiet_points(board, k: int) -> list[str]:
    """k empty points with no stone next to them (always legal), far apart enough to be distinct."""
    out = []
    for i in range(361):
        if board.cells[i] == 0 and all(board.cells[n] == 0 for n in neighbors(i, 19)):
            out.append(idx_to_gtp(i))
            if len(out) == k:
                break
    return out


def make_tools(tmp: str, engine=None, **overrides) -> Tools:
    """Tools on a MockEngine (or `engine`) with reviews/ and games/ under tmp and 650 visits/s.
    Overrides set config fields, nested with '__': make_tools(tmp, katago__restart_above_mb=0)."""
    cfg = Config()
    cfg.reviews_dir = os.path.join(tmp, "reviews")
    cfg.games_dir = os.path.join(tmp, "games")
    cfg.throughput.visits_per_second_sustained = 650.0
    cfg.student.username = "student1"
    for key, value in overrides.items():
        *path, name = key.split("__")
        obj = cfg
        for p in path:
            obj = getattr(obj, p)
        if not hasattr(obj, name):
            raise AttributeError(f"no config field {key}")
        setattr(obj, name, value)
    return Tools(cfg, engine=engine or MockEngine(), start_engine=True)
