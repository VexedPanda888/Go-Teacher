#!/usr/bin/env python3
"""Seed the teacher's memory: survey every SGF in a folder and write one compact summary.

  katago-mcp-seed --config config/m5pro.toml --sgf-dir seed/ [--visits 500] [--student <ogs-name>]
                  [--out seed/seed_summary.json] [--episodes 6] [--mock]
                  [--probes] [--probe-episodes 5] [--probe-visits 200]

For each game: run (or reuse) the whole-game survey, take the digest, keep only what calibration and
memory need.  Writes <out>.json (machine-readable, one record per game) and <out>.md (a table for the
calibration conversation).  Re-running is cheap: finished surveys are reused from reviews/<game_id>/.

With --probes, intent_probe runs on the top episodes of each game (by root loss) at a small budget and
each episode gets its inferred belief, so beliefs can be clustered across games after one batch
("three of your five biggest losses were defences of groups that were already alive"). These beliefs
are survey grade: inferred at low visits, never stated by the student, never verified.
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


def probe_one(t: Tools, e: dict, visits: int) -> dict:
    """The compact intent_probe result kept in the seed record."""
    try:
        r = t.intent_probe({"ref": e["root"]["position_ref_before"]}, e["root"]["played"], {"visits": visits})
    except ToolError as err:
        return {"error": err.code}
    return {"belief": r["belief"]["id"] if r["belief"] else None, "matches": r["matches"],
            "evidence": r["belief"]["evidence"] if r["belief"] else None,
            "reply": (r["reply"] or {}).get("character"), "threat": r["threat"]["value"], "tenuki_value": r["tenuki_value"],
            "defense": r["defense"]["value"], "loss": r["score"]["loss"], "query_id": r["query_id"], "visits": visits}


def survey_one(t: Tools, path: Path, visits: int, student: str | None, episodes: int,
               probes: bool = False, probe_episodes: int = 5, probe_visits: int = 200) -> dict:
    sgf = path.read_text(encoding="utf-8", errors="replace")
    s = t.sgf_summary(sgf, student)
    r = t.start_game_analysis(sgf, {"visits_per_move": visits}, student)
    t0 = time.time()
    st = t.wait_for_job(r["job_id"], 1.0, lambda st: print(f"\r  {path.name}: {st['positions_done']}/{st['positions_total']}  "
                                                             f"{st['elapsed_seconds']}s   ", end="", file=sys.stderr))
    print(file=sys.stderr)
    if st["state"] != "done":
        return {"file": path.name, "game_id": r["game_id"], "error": st.get("error") or st["state"]}
    d = t.job_results(r["job_id"], "digest", max_episodes=episodes)
    probed = {}
    if probes:
        for e in sorted(d["episodes"], key=lambda e: -e["root"]["points_lost"])[:probe_episodes]:
            if e["root"]["played"] not in (None, "pass"):
                probed[e["id"]] = probe_one(t, e, probe_visits)
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
            "persistent_best": e.get("persistent_best", []), "best_reply": e.get("best_reply"),
            "position_ref_before": e["root"]["position_ref_before"],
            **({"probe": probed[e["id"]]} if e["id"] in probed else {}),
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


def belief_clusters(records: list[dict], top: int = 5) -> dict:
    """Beliefs among the probed episodes: counts overall, and among each game's `top` biggest root losses
    pooled across games (the insight a coach gives after a month)."""
    ok = [r for r in records if "error" not in r]
    probed = [(e, r["game_id"]) for r in ok for e in r["episodes"] if e.get("probe") and "error" not in e["probe"]]
    allc = Counter(e["probe"]["belief"] or "none" for e, _ in probed)
    biggest = sorted(probed, key=lambda eg: -eg[0]["root_points_lost"])[:top]
    topc = Counter(e["probe"]["belief"] or "none" for e, _ in biggest)
    sentence = None
    if biggest:
        b, n = topc.most_common(1)[0]
        if b != "none" and n >= 2:
            sentence = f"{n} of your {len(biggest)} biggest probed losses share the belief {b!r}: " + \
                       ", ".join(f"{g} move {e['moves'][0]}" for e, g in biggest if e["probe"]["belief"] == b)
    return {"probed": len(probed), "all": dict(allc), "top": dict(topc), "top_n": len(biggest), "sentence": sentence}


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
    bc = belief_clusters(records)
    if bc["probed"]:
        lines += [f"## Beliefs ({bc['probed']} probed episodes, survey grade)", "",
                  "| belief | all probed | among the " + str(bc["top_n"]) + " biggest |", "|---|---|---|"]
        for b, n in sorted(bc["all"].items(), key=lambda kv: -kv[1]):
            lines.append(f"| {b} | {n} | {bc['top'].get(b, 0)} |")
        lines += ["", bc["sentence"] or "No belief repeats among the biggest losses.", ""]
    rec = [(h, n) for h, n in hashes.items() if n >= 2]
    lines += [f"Recurring 7×7 patterns (≥ 2 games): {len(rec)}"] + [f"- {h}: {n}" for h, n in sorted(rec, key=lambda kv: -kv[1])]
    lines += ["", "## Top episodes per game (for the calibration pass)", ""]
    for r in ok:
        lines.append(f"### {r['game_id']} ({r['date'] or 'no date'}, {r['student_color']}, hcp {r['handicap']}, {r['result']})")
        for e in r["episodes"][:4]:
            hp = e["human_played"]
            lines.append(f"- {e['id']} moves {e['moves'][0]}–{e['moves'][1]}: {e['points_lost']} pts, {e['region']} {e['phase']} {e['game_state']}, "
                         f"played {e['played']} best {e['best']} teachable {e['teachable']}, tags {e['candidate_tags']}, style {e['style']}, "
                         f"learn {e['learnability']}, peer/target of played {hp.get('peer')}/{hp.get('target')}, stab {e['stability']}, {e['pattern_hash']}"
                         + (f", belief {e['probe'].get('belief')}" if e.get("probe") else ""))
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
    ap.add_argument("--probes", action="store_true", help="run intent_probe on each game's top episodes (beliefs)")
    ap.add_argument("--probe-episodes", type=int, default=5)
    ap.add_argument("--probe-visits", type=int, default=200)
    ap.add_argument("--restart-every", type=int, default=0,
                    help="also restart KataGo every N games (0 = never; the default). Not needed normally: each survey "
                         "already restarts KataGo if its memory is above [katago].restart_above_mb")
    args = ap.parse_args()
    cfg = load_config(args.config)
    t = Tools(cfg, engine=MockEngine() if args.mock else None, start_engine=not args.mock)
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
                records.append(survey_one(t, p, args.visits, args.student, args.episodes,
                                          args.probes, args.probe_episodes, args.probe_visits))
            except ToolError as e:
                records.append({"file": p.name, "error": e.to_dict()["error"]})
                print(f"  failed: {e.code}: {e.message}", file=sys.stderr)
            out.write_text(json.dumps(records, indent=1, ensure_ascii=False), encoding="utf-8")
            out.with_suffix(".md").write_text(markdown(records), encoding="utf-8")
    finally:
        t.close()
    print(f"wrote {out} and {out.with_suffix('.md')}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
