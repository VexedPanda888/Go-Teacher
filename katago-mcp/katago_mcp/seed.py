#!/usr/bin/env python3
"""Seed the teacher's memory: survey every SGF in a folder and write one compact summary.

  katago-mcp-seed --config config/m5pro.toml --sgf-dir seed/ [--visits 500] [--student cwhay888]
                  [--out seed/seed_summary.json] [--episodes 6] [--mock]

For each game: run (or reuse) the whole-game survey, take the digest, keep only what calibration and
memory need.  Writes <out>.json (machine-readable, one record per game) and <out>.md (a table for the
calibration conversation).  Re-running is cheap: finished surveys are reused from reviews/<game_id>/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from .config import load_config
from .engine import MockEngine
from .tools import ToolError, Tools


def survey_one(t: Tools, path: Path, visits: int, student: str | None, episodes: int) -> dict:
    sgf = path.read_text(encoding="utf-8", errors="replace")
    s = t.sgf_summary(sgf, student)
    r = t.start_game_analysis(sgf, {"visits_per_move": visits}, student)
    t0 = time.time()
    while True:
        st = t.job_status(r["job_id"])
        if st["state"] in ("done", "failed", "cancelled"):
            break
        print(f"\r  {path.name}: {st['positions_done']}/{st['positions_total']}  {st['elapsed_seconds']}s   ", end="", file=sys.stderr)
        time.sleep(1.0)
    print(file=sys.stderr)
    if st["state"] != "done":
        return {"file": path.name, "game_id": r["game_id"], "error": st.get("error") or st["state"]}
    d = t.job_results(r["job_id"], "digest", max_episodes=episodes)
    t.job_status(r["job_id"], "release")          # keep memory flat across a long run (files stay on disk)
    eps = []
    for e in d["episodes"]:
        eps.append({
            "id": e["id"], "moves": e["moves"], "student_moves": e["student_moves"], "points_lost": e["points_lost_total"],
            "root_points_lost": e["root"]["points_lost"], "played": e["root"]["played"], "best": e["root"]["best"],
            "teachable": e["teachable_move_preliminary"], "region": e["region"]["standard"], "phase": e["phase"],
            "game_state": e["game_state_before"]["label"], "candidate_tags": e["candidate_tags"],
            "style": e["style_axis"]["label"], "learnability": e["learnability"], "stability": e["stability"],
            "pattern_hash": e["pattern_hash"], "pattern_hash_5": e["pattern_hash_5"],
            "in_acceptable_set": e["acceptable_set"]["played_in_set"], "human_played": e["human"]["played"],
            "human_best": e["human"]["best"], "got_away": e["got_away_with_it"],
            "position_ref_before": e["root"]["position_ref_before"],
        })
    return {
        "file": path.name, "game_id": d["game_id"], "job_id": r["job_id"], "reused": r["reused"],
        "date": s["source"]["date"], "ogs_url": s["source"]["url"], "student_color": d["game"]["student_color"],
        "opponent": (s["opponent"] or {}), "players": s["players"], "handicap": d["game"]["handicap"], "komi": d["game"]["komi"],
        "result": d["game"]["result"], "reconciliation": d["game"]["reconciliation"], "moves": s["moves"]["count"],
        "visits_per_move": d["visits_per_move"], "phases": d["phases"], "game_type": d["game_type"],
        "decisive": d["decisive"], "last_chance": d["last_chance"], "points_lost": d["points_lost"],
        "positives": d["positives"], "reliability": d["reliability"], "episodes": eps, "survey_seconds": round(time.time() - t0),
    }


def markdown(records: list[dict]) -> str:
    ok = [r for r in records if "error" not in r]
    lines = ["# Seed surveys", "", f"{len(ok)} games surveyed, {len(records) - len(ok)} failed.", ""]
    lines += ["| game | date | you | hcp | result | recon | type | pts lost (o/m/e) | decisive | last chance |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in ok:
        pl = r["points_lost"]["student"] or {}
        lines.append(f"| {r['game_id']} | {r['date'] or ''} | {r['student_color']} | {r['handicap']} | {r['result']} | {r['reconciliation']['status']} | "
                     f"{r['game_type']['type']} | {pl.get('total', 0)} ({pl.get('opening', 0)}/{pl.get('middlegame', 0)}/{pl.get('endgame', 0)}) | "
                     f"{(r['decisive'] or {}).get('move', '')} | {(r['last_chance'] or {}).get('move', '')} |")
    tags = Counter()
    first_tags = Counter()
    styles = Counter()
    states = Counter()
    phases = Counter()
    hashes = Counter()
    unstable = 0
    total_eps = 0
    for r in ok:
        for e in r["episodes"]:
            total_eps += 1
            tags.update(e["candidate_tags"])
            if e["candidate_tags"]:
                first_tags[e["candidate_tags"][0]] += 1
            styles[e["style"]] += 1
            states[e["game_state"]] += 1
            phases[e["phase"]] += 1
            if e["pattern_hash"]:
                hashes[e["pattern_hash"]] += 1
            unstable += e["stability"] == "unstable"
    lines += ["", f"## Episodes: {total_eps} across {len(ok)} games ({unstable} unstable)", "",
              "| tag | as first tag | anywhere |", "|---|---|---|"]
    for tag, n in sorted(tags.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {tag} | {first_tags.get(tag, 0)} | {n} |")
    lines += ["", "Style: " + ", ".join(f"{k} {v}" for k, v in styles.most_common()),
              "Game state at the mistake: " + ", ".join(f"{k} {v}" for k, v in states.most_common()),
              "Phase: " + ", ".join(f"{k} {v}" for k, v in phases.most_common()), ""]
    rec = [(h, n) for h, n in hashes.items() if n >= 2]
    lines += [f"Recurring 7×7 patterns (≥ 2 games): {len(rec)}"] + [f"- {h}: {n}" for h, n in sorted(rec, key=lambda kv: -kv[1])]
    lines += ["", "## Top episodes per game (for the calibration pass)", ""]
    for r in ok:
        lines.append(f"### {r['game_id']} ({r['date'] or 'no date'}, {r['student_color']}, hcp {r['handicap']}, {r['result']})")
        for e in r["episodes"][:4]:
            hp = e["human_played"]
            lines.append(f"- {e['id']} moves {e['moves'][0]}–{e['moves'][1]}: {e['points_lost']} pts, {e['region']} {e['phase']} {e['game_state']}, "
                         f"played {e['played']} best {e['best']} teachable {e['teachable']}, tags {e['candidate_tags']}, style {e['style']}, "
                         f"learn {e['learnability']}, peer/target of played {hp.get('peer')}/{hp.get('target')}, stab {e['stability']}, {e['pattern_hash']}")
        lines.append("")
    for r in records:
        if "error" in r:
            lines.append(f"- FAILED {r['file']}: {r['error']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config")
    ap.add_argument("--sgf-dir", required=True)
    ap.add_argument("--visits", type=int, default=500)
    ap.add_argument("--student")
    ap.add_argument("--episodes", type=int, default=6)
    ap.add_argument("--out", default=None)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--restart-every", type=int, default=4, help="restart KataGo every N games to drop its NN cache (0 = never)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    t = Tools(cfg, engine=MockEngine() if args.mock else None, start_engine=not args.mock)
    if not args.mock:
        t.engine.start()
    files = sorted(p for p in Path(args.sgf_dir).iterdir() if p.suffix.lower() == ".sgf")
    if not files:
        sys.exit(f"no .sgf files in {args.sgf_dir}")
    out = Path(args.out or (Path(args.sgf_dir) / "seed_summary.json"))
    records = []
    try:
        for i, p in enumerate(files, 1):
            if args.restart_every and i > 1 and (i - 1) % args.restart_every == 0 and not args.mock:
                print("  restarting KataGo to clear its cache ...", file=sys.stderr)
                t.restart_engine()
            print(f"[{i}/{len(files)}] {p.name}", file=sys.stderr)
            try:
                records.append(survey_one(t, p, args.visits, args.student, args.episodes))
            except ToolError as e:
                records.append({"file": p.name, "error": e.to_dict()["error"]})
                print(f"  failed: {e.code}: {e.message}", file=sys.stderr)
            out.write_text(json.dumps(records, indent=1, ensure_ascii=False))
            out.with_suffix(".md").write_text(markdown(records))
    finally:
        t.close()
    print(f"wrote {out} and {out.with_suffix('.md')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
