"""katago-mcp command line.

  katago-mcp serve --config config/m5pro.toml
  katago-mcp benchmark --config config/m5pro.toml [--seconds 20]
  katago-mcp selfcheck --config config/m5pro.toml [--sgf game.sgf]
  katago-mcp sgf-summary game.sgf [--student <ogs-name>]
  katago-mcp survey game.sgf --config ... [--visits 300] [--mock]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .config import load_config
from .coords import gtp_to_idx
from .engine import MockEngine
from .tools import ToolError, Tools


def _tools(args, start_engine: bool) -> Tools:
    cfg = load_config(getattr(args, "config", None))
    engine = MockEngine() if getattr(args, "mock", False) else None
    t = Tools(cfg, engine=engine, start_engine=False)
    if start_engine and engine is None:
        t.engine.echo_stderr = True
        print(f"starting KataGo ({cfg.katago.binary}) with {os.path.basename(cfg.katago.model)}"
              + (f" and {os.path.basename(cfg.katago.human_model)}" if cfg.katago.human_model else "")
              + "\n  loading takes seconds on Metal; the first OpenCL start can tune kernels for several minutes."
              + "\n  KataGo's own log follows; the check continues when the engine answers.", file=sys.stderr, flush=True)
        t0 = time.time()
        t.engine.start()
        print(f"  engine ready after {time.time() - t0:.0f}s ({t.engine.info().get('backend')} backend, "
              f"KataGo {t.engine.info().get('katago_version')})", file=sys.stderr, flush=True)
    elif start_engine:
        t.engine.start()
    return t


def cmd_serve(args) -> int:
    from .server import main as serve_main
    serve_main(args.config)
    return 0


def cmd_benchmark(args) -> int:
    t = _tools(args, start_engine=True)
    try:
        r = t.benchmark(args.seconds)
        print(json.dumps(r, indent=2))
        if t.cfg.throughput_path:
            print(f"saved to {t.cfg.throughput_path}", file=sys.stderr)
        return 0
    finally:
        t.close()


def cmd_sgf_summary(args) -> int:
    t = Tools(load_config(args.config), engine=MockEngine(), start_engine=False)
    text = open(args.sgf, encoding="utf-8").read()
    try:
        r = t.sgf_summary(text, args.student)
    except ToolError as e:
        print(json.dumps(e.to_dict(), indent=2))
        return 1
    print(json.dumps(r, indent=2, ensure_ascii=False))
    return 0


def cmd_selfcheck(args) -> int:
    """Start the engine, check the perspective convention with a Black corner, optionally reconcile a game."""
    t = _tools(args, start_engine=True)
    rc = 0
    try:
        print(json.dumps(t.engine_info(), indent=2))
        # Black owns a solid lower-left corner; ownership there must be positive (Black-positive convention)
        pos = {"setup": {"B": ["C3", "D3", "E3", "F3", "F2", "F1", "C4", "D4", "E4"], "W": ["Q16"]},
               "moves": [], "to_move": "W", "rules": "japanese", "komi": 6.5}
        r = t.analyze_position(pos, {"visits": 100}, {"include_ownership": True, "include_groups": False, "perspective": "B"})
        own = r["ownership"]
        corner = sum(own[gtp_to_idx(p)] for p in ("A1", "B1", "B2", "A2", "C2", "D2")) / 6
        print(f"corner ownership (should be clearly positive): {corner:+.2f}")
        if corner < 0.3:
            print("PERSPECTIVE MISMATCH: set [katago].perspective to match reportAnalysisWinratesAs in analysis.cfg", file=sys.stderr)
            rc = 2
        hp = t.human_move_distribution(pos, ["rank_7k"], ["Q4"], 3)
        print("human model ok:", hp["profiles"]["rank_7k"]["top"][:3])
        if args.sgf:
            text = open(args.sgf, encoding="utf-8").read()
            s = t.start_game_analysis(text, {"visits_per_move": args.visits}, args.student, options={"prefetch": False})
            print(f"survey {s['job_id']} started ({s['positions_total']} positions @ {s['visits_per_move']} visits)")
            t.wait_for_job(s["job_id"], 2, lambda st: print(f"  {st['positions_done']}/{st['positions_total']} "
                                                            f"elapsed {st['elapsed_seconds']}s", end="\r"))
            print()
            d = t.job_results(s["job_id"], "story", max_moments=5)
            print(json.dumps({k: d[k] for k in ("game", "phases", "lead", "decisive", "last_chance", "points_lost")}, indent=2))
            print("group events:", [(e["move"], e["group"], e["from"], e["to"]) for e in d["group_events"]])
            print("key moments:", [(m["id"], m["moves"], m["played"], m["best"], m["points_lost"]) for m in d["moments"]])
            if d["game"]["reconciliation"]["status"] == "mismatch":
                print("RECONCILIATION MISMATCH:", d["game"]["reconciliation"], file=sys.stderr)
                rc = max(rc, 3)
    except ToolError as e:
        print(json.dumps(e.to_dict(), indent=2), file=sys.stderr)
        rc = 1
    finally:
        t.close()
    return rc


def cmd_survey(args) -> int:
    t = _tools(args, start_engine=not args.mock)
    try:
        text = open(args.sgf, encoding="utf-8").read()
        s = t.start_game_analysis(text, {"visits_per_move": args.visits}, args.student, options={"prefetch": False})
        t.wait_for_job(s["job_id"], 0.5 if args.mock else 2)
        print(json.dumps(t.job_results(s["job_id"], "story", max_moments=args.moments), indent=2, ensure_ascii=False))
        return 0
    except ToolError as e:
        print(json.dumps(e.to_dict(), indent=2), file=sys.stderr)
        return 1
    finally:
        t.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="katago-mcp")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the MCP server on stdio")
    s.add_argument("--config", required=False)
    s.set_defaults(fn=cmd_serve)
    b = sub.add_parser("benchmark", help="measure visits/second and save the throughput sidecar")
    b.add_argument("--config", required=True)
    b.add_argument("--seconds", type=float, default=20.0)
    b.set_defaults(fn=cmd_benchmark)
    g = sub.add_parser("sgf-summary", help="parse an SGF (no engine)")
    g.add_argument("sgf")
    g.add_argument("--config")
    g.add_argument("--student")
    g.set_defaults(fn=cmd_sgf_summary)
    c = sub.add_parser("selfcheck", help="start the engine and verify conventions")
    c.add_argument("--config", required=True)
    c.add_argument("--sgf")
    c.add_argument("--student")
    c.add_argument("--visits", type=int, default=200)
    c.set_defaults(fn=cmd_selfcheck)
    v = sub.add_parser("survey", help="run a whole-game survey and print its story")
    v.add_argument("sgf")
    v.add_argument("--config")
    v.add_argument("--student")
    v.add_argument("--visits", type=int, default=300)
    v.add_argument("--moments", type=int, default=6)
    v.add_argument("--mock", action="store_true", help="use the mock engine (no KataGo)")
    v.set_defaults(fn=cmd_survey)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
