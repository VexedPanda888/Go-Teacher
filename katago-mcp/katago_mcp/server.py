"""MCP server (stdio).  Thin layer: every tool delegates to `Tools` and converts ToolError to an error dict."""
from __future__ import annotations

import functools
import logging
import os
import sys

from .config import load_config
from .tools import ToolError, Tools

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
    log.info("katago-mcp %s serving; engine %s", __import__("katago_mcp").__version__,
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

    @server.tool()
    @guarded
    def engine_info(refresh_benchmark: bool = False) -> dict:
        """Machine, KataGo/network versions, human model, throughput (visits/s), active job and plan, student profiles."""
        return tools.engine_info(refresh_benchmark)

    @server.tool()
    @guarded
    def plan_budget(total_minutes: float | str, move_count: int | None = None, job_id: str | None = None,
                    self_review_minutes: float | None = None, episodes_requested: int | None = None,
                    expected_ld_episodes: int | None = None, selected: list | None = None) -> dict:
        """Turn the review time budget (minutes, or 'unlimited') into survey visits, episode count and per-episode search sizes. Re-plan with job_id + selected after triage."""
        return tools.plan_budget(total_minutes, move_count, job_id, self_review_minutes, episodes_requested,
                                 expected_ld_episodes, selected)

    @server.tool()
    @guarded
    def sgf_summary(sgf: str, student_username: str | None = None, boards_at: list | None = None,
                    ascii_options: dict | None = None) -> dict:
        """Parse a game without engine use: players, rules, komi, handicap, result, move count, captures, tension events, ASCII boards, position refs. `sgf` is an OGS game link or id (fetched from online-go.com), the path/name of an .sgf file on this machine (games/ folder), or raw SGF text; prefer the link or the path over pasting text."""
        return tools.sgf_summary(sgf, student_username, boards_at, ascii_options)

    @server.tool()
    @guarded
    def start_game_analysis(sgf: str, budget: dict | None = None, student_username: str | None = None,
                            game_id: str | None = None, options: dict | None = None) -> dict:
        """Start the asynchronous whole-game survey. `sgf`: OGS game link/id, .sgf file path, or SGF text (prefer link or path). budget: {visits_per_move} or {profile:'survey'} (from the active plan). Returns a job_id."""
        return tools.start_game_analysis(sgf, budget, student_username, game_id, options)

    @server.tool()
    @guarded
    def job_status(job_id: str, action: str = "status") -> dict:
        """Progress of a survey job (action 'status') or cancel it (action 'cancel')."""
        return tools.job_status(job_id, action)

    @server.tool()
    @guarded
    def job_results(job_id: str, detail: str = "digest", range: list | None = None, max_episodes: int = 10,
                    include_positives: bool = True) -> dict:
        """Survey digest: phases, points lost, episodes (chains) with signatures, candidate tags, human probabilities, decisive move, last chance. detail: digest | moves | full."""
        return tools.job_results(job_id, detail, range, max_episodes, include_positives)

    @server.tool()
    @guarded
    def get_position_ref(job_id: str | None = None, sgf: str | None = None, move_number: int = 0) -> dict:
        """Position reference for the position after move_number of a job, or of an SGF given as OGS link/id, file path, or text."""
        return tools.get_position_ref(job_id, sgf, move_number)

    @server.tool()
    @guarded
    def analyze_position(position: dict, budget: dict | None = None, options: dict | None = None) -> dict:
        """Search one position: root score/winrate, candidates with PV and human probabilities, acceptable set, policy top, groups. position: {ref}|{job_id,move_number}|{sgf,move_number}|{moves,...} plus optional then:[...]."""
        return tools.analyze_position(position, budget, options)

    @server.tool()
    @guarded
    def analyze_line(position: dict, line: list, budget: dict | None = None, follow_pv_plies: int | None = None,
                     options: dict | None = None) -> dict:
        """Play a line (forced moves and/or engine replies) and evaluate every node; then follow the PV. Returns per-ply evals, deltas, end ownership/groups, refutation probability."""
        return tools.analyze_line(position, line, budget, follow_pv_plies, options)

    @server.tool()
    @guarded
    def pass_probe(position: dict, player: str, move: str | None = None, budget: dict | None = None,
                   options: dict | None = None) -> dict:
        """Local value of a move: score if the player passes vs after the best move (and after `move`). options.rank_regions=true ranks the nine regions by the value of playing there."""
        return tools.pass_probe(position, player, move, budget, options)

    @server.tool()
    @guarded
    def swing_value(position: dict, points: list, budget: dict | None = None, options: dict | None = None) -> dict:
        """Swing (Black-first minus White-first) and sente/gote for up to six points, ranked."""
        return tools.swing_value(position, points, budget, options)

    @server.tool()
    @guarded
    def local_solve(position: dict, group_point: str, region: dict | None = None, budget: dict | None = None,
                    options: dict | None = None) -> dict:
        """Life-and-death of the group at group_point: attacker-first and defender-first playouts confined to a region -> alive | dead | unsettled | unclear with confidence."""
        return tools.local_solve(position, group_point, region, budget, options)

    @server.tool()
    @guarded
    def group_status(position: dict, options: dict | None = None) -> dict:
        """Every group with size, liberties, mean ownership and status (alive/unsettled/dead) from cached or quick ownership."""
        return tools.group_status(position, options)

    @server.tool()
    @guarded
    def ownership_diff(a: dict, b: dict, regions: list | None = None, budget: dict | None = None,
                       options: dict | None = None) -> dict:
        """Ownership change between two positions by region and by group; classifies the loss as local, mixed or global."""
        return tools.ownership_diff(a, b, regions, budget, options)

    @server.tool()
    @guarded
    def human_move_distribution(position: dict, profiles: list | None = None, moves_of_interest: list | None = None,
                                top_n: int = 8) -> dict:
        """Human-model move probabilities at a position for profiles (peer/target/horizon/opponent or rank_7k...), top moves and moves of interest."""
        return tools.human_move_distribution(position, profiles, moves_of_interest, top_n)

    @server.tool()
    @guarded
    def render_board(position: dict, options: dict | None = None) -> dict:
        """ASCII board with last move, highlights, region box, low-liberty groups; optional ownership/policy overlay."""
        return tools.render_board(position, options)

    @server.tool()
    @guarded
    def validate_variations(job_id: str, episodes: list, summary: dict | None = None, options: dict | None = None) -> dict:
        """Validate lesson branches and quizzes against the game (legality, colors, evaluations) and export the checksummed dashboard data blob."""
        return tools.validate_variations(job_id, episodes, summary, options)

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
