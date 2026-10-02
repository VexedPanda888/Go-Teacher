# Calibration / seeding pass (WS8)

Used when the student brings `seed/seed_summary.md` + `.json` (made by `katago-mcp-seed`; the plan is
`docs/plan.md` WS8, the tool-side notes are `katago-mcp/README.md` §5b). No self-review, no lessons.
1. Reconciliation per game (the flow skill's "When things go wrong" covers a mismatch with correct rules).
2. Tag frequencies and the top episodes: look for tags that never fire or fire on half the episodes,
   and test candidate rules on the JSON before proposing them. The server's rules live in
   `katago_mcp/metrics.py` `candidate_tags`; thresholds live in `[thresholds]` of `config/*.toml`.
3. Draft profile + a 20-episode sample (top episode per game) for the student to rate; the bar
   (`docs/plan.md` WS8) is ≥ 80 % tag accuracy. When the summary was made with `--probes`, also show the
   belief table and its sentence, and ask the student whether the belief matches what they were thinking
   on the sample episodes (bar: ≥ 6 of 8). Record everything in `seed/calibration.md` (local; `seed/` is not in git).
4. Seed memory only after the student agrees: `verdict: "SURVEY"`, beliefs with
   `belief_source: "probe_survey"`, `points_lost` = root loss (the chain
   sum double-counts swings), skip games already reviewed, never overwrite CONFIRMED episodes.
5. If tag rules change in code, rebuild tags from `reviews/<game_id>/analysis.json` (use
   `katago-mcp/.venv/bin/python`; system Python lacks `tomllib`), check they reproduce what was seeded,
   and update memory to match. The running server must be restarted to pick up code changes.
