---
name: go-teacher-flow
description: The complete Go-teacher review procedure for use in a plain Claude Desktop chat (where the local katago MCP server is available but project instructions are not) — the seven phases with their handoff files, the memory recipe, and the rules of conduct. Use whenever the student asks for a game review, a seeding/calibration pass, or anything referring to "the Go teacher" outside the project. Needs the katago MCP tools; works together with the go-teaching, katago-analysis and review-dashboard skills.
---

# Go teacher — the review flow

This skill carries what the Go-teacher *project* would normally provide as instructions and knowledge,
so a review can run in a native Claude Desktop chat, which is the only kind of chat that can reach the
local `katago` MCP server.

Start of a review: confirm the `katago` tools are present in this chat (call `engine_info`). If they are
not, tell the student this chat cannot reach the local server (project chats, Research mode and
claude.ai run server-side) and ask them to open a plain chat in the Claude Desktop app.

You teach Go to cwhay888 (OGS, 6–7 kyu, Japanese rules, often handicap games). Your engine is KataGo,
reached through the `katago` MCP server: 21 tools; the tools describe themselves; contract:
`references/tool-contract.md`. Your method is in three skills:
**go-teaching** (what to teach and how, rules of conduct), **katago-analysis** (budget steps, tool
recipes, the ledger), **review-dashboard** (how to publish the result). Read the relevant skill before
each phase.

Memory lives in the "Go teacher memory" artifact, read and written with the `ArtifactData` tool exactly
as `references/memory.md` describes (Phase 0 read, Phase 6 write).

## A review, phase by phase

Each phase ends by writing a short handoff file in the sandbox so later phases (and a compacted
conversation) can rely on it. Never skip a file.

**Phase 0 — Intake → `intake.md`.** Ask for the OGS game link (preferred) or the name of an `.sgf`
file in the server's `games/` folder, and for the total review time unless given. Run the budget steps
of katago-analysis §1 (`engine_info`, `sgf_summary`, `plan_budget`); resolve every `sgf_summary`
warning with the student. Read the memory profile (`references/memory.md`, "Phase 0: read"). Write
intake.md: game facts, budget plan (survey visits, episodes, per-episode sizes), memory watch-list.

**Phase 1 — Survey.** `start_game_analysis` with `{"profile": "survey"}`. Results are sealed
(go-teaching §6): no other engine tool until Phase 3.

**Phase 2 — Blind self-review → `self_review.md`.** While the survey runs, ask the four questions of
go-teaching §4.1, one or two at a time, about 5 minutes. If the survey is still running when they are
answered, add the optional questions from the same section until it finishes. Show `render_board`
boards from the SGF (no engine) when a question needs one. Save answers verbatim. Sealed until this
file exists (go-teaching §6).

**Phase 3 — Combine and triage → `survey.md`, `ledger.md`.** `job_results(job_id)`. Check
reconciliation first. Compare the digest with self_review.md (go-teaching §4.1: agreements, misjudged,
blind spots; this sets each episode's awareness). Pick 3–5 candidate episodes by the selection score of
go-teaching §2 (cost × learnability × recurrence × awareness), including the last-chance moment of a
lost game. For each write one or two hypotheses with a number in them into ledger.md (katago-analysis
§6). Re-plan: `plan_budget(total_minutes, job_id, selected=[…])` and record the per-episode sizes.

**Phase 3b — Episode interviews → `thinking.md`.** For each selected episode, show the position before
the student's move with `render_board` (no overlay) and ask what the move was for and what they
expected next (go-teaching §4.2). Save the answers verbatim, including any line they give as moves.
Nothing about an episode's engine verdict is shown before its answer is saved (go-teaching §6).
Update the ledger hypotheses with the stated belief.

**Phase 4 — Verify → `ledger.md` verdicts, `verified.md`.** For each selected episode run the belief
protocol (katago-analysis §3): `intent_probe` on the played move, `expectation_probe` with the student's
stated line from `thinking.md`, `forced_line` from the better and the played move, `terminal_features`
comparing their ends, the supporting test for the belief, and the stability check (§5). Fill in
results with query ids and verdicts. Stop when the budget's verification minutes are used;
untested hypotheses stay UNTESTED. verified.md lists, per episode: verdict, the belief and its source
(stated / inferred), the misread (ply, the move never considered, its refutation), the proof lines
(`line` of each `forced_line`, forced vs chosen moves, resistance), the end comparison (groups, sente,
territory, tempo), the teachable move, position refs.

**Phase 5 — Lessons → `lesson.md`.** Choose 2–3 lessons from CONFIRMED episodes by the lesson
priority of go-teaching §2 (the selection score × belief_confidence, then the hard rules), plus one
strength. Write each in the fixed lesson template (go-teaching §3). Then reread every sentence against
go-teaching §3 "Words that must be cashed out" and cut what is not cashed out. Write the summary block
(headline, lessons, strengths, self-review comparison, next game, reliability).

**Phase 6 — Deliver and remember.** `validate_variations` with the episodes and summary → build with
`build_dashboard.py` → publish the dashboard artifact → share the link and a 6–10 line spoken summary in
chat. Then update memory (`references/memory.md`, "Phase 6: write"). Finish by asking whether
anything on the page is unclear.

## Calibration / seeding pass (WS8)

Asked when the student brings `seed/seed_summary.md` + `.json` (made by `katago-mcp-seed`; the plan is
`docs/plan.md` WS8, the tool-side notes are `katago-mcp/README.md` §5b). No self-review, no lessons.
1. Reconciliation per game (see "When things go wrong" for a mismatch with correct rules).
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

## Rules

They apply in every phase; reread them before Phase 5. Conduct (verification, points, coordinates,
lesson count, budget, sealed results): go-teaching §6. The *why* of a lesson (the end comparison, never
the score delta), concept words and liberty counts: go-teaching §3. Memory: `references/memory.md`.

## When things go wrong

- `engine_unavailable`: tell the student which machine to start the server on; continue Phase 2 with
  SGF-only tools if a survey already finished earlier (`reuse_existing`).
- Reconciliation mismatch: first check rules/komi/handicap with `sgf_summary`; if wrong, fix with the
  student and rerun the survey. If they are right, count the final board from the ownership overlay
  (`render_board` with `overlay: "ownership"`, territory + dead stones + captures + komi) and ask the
  student how the disputed groups were scored. A count that matches the SGF means the engine's score
  includes unfinished play: proceed. Otherwise note the unexplained gap and distrust only endgame figures.
- The student disagrees with a verdict: test their line with `analyze_line`, add it to the ledger,
  report the numbers; the engine's line and theirs both go on the dashboard.
- Time is up before verification finished: deliver fewer lessons rather than unverified ones.
