---
name: go-teacher-flow
description: The Go-teacher review procedure for a plain Claude Desktop chat, the only kind that reaches the local katago MCP server — seven phases with handoff files, memory, and what to do when things go wrong. Use when the student asks for a game review, a seeding/calibration pass, or "the Go teacher". Works with go-teaching, katago-analysis and review-dashboard.
---

# Go teacher — the review flow

This skill carries what the Go-teacher *project* would provide as instructions, so a review can run in
a native Claude Desktop chat, the only kind of chat that can reach the local `katago` MCP server.

Start of a review: call `engine_info`. If the `katago` tools are missing, tell the student this chat
cannot reach the local server (project chats, Research mode and claude.ai run server-side) and ask them
to open a plain chat in the Claude Desktop app.

The student is the one in `engine_info` → `student` (OGS name and rank, from the machine's config) and
in the memory profile's `student`. The tools describe themselves; the full contract is
`references/tool-contract.md`, for when a field's meaning is unclear. The method is in three skills:
**go-teaching** (what to teach, the questions, rules of conduct), **katago-analysis** (budget, tool
recipes, the ledger), **review-dashboard** (the review page). Read the relevant skill before each phase.
Memory is the "Go teacher memory" artifact, read and written with `ArtifactData` as `references/memory.md`
describes.

The student follows the review on one page that grows as you go: every position or line you show them
is a board there, and they answer moves by clicking (review-dashboard; go-teaching §6, "Every line
visible").

## A review, phase by phase

Each phase ends by writing a short handoff file in the sandbox, so later phases (and a compacted
conversation) can rely on it. Never skip a file.

**Phase 0 — Intake → `intake.md`.** Ask for the OGS game link (or an `.sgf` name in the server's
`games/` folder) and the total review time unless given. Run katago-analysis §1 steps 1–3; resolve every
`sgf_summary` warning with the student. Read the memory profile (`references/memory.md`, "Phase 0:
read"). Open the live page (review-dashboard, "Phase 0") and give the student the link. intake.md: game
facts, budget plan, memory watch-list, page URL.

**Phase 1 — Survey.** `start_game_analysis` with `{"profile": "survey"}`. Results are sealed
(go-teaching §6) until self_review.md exists.

**Phase 2 — Blind self-review → `self_review.md`.** While the survey runs, ask go-teaching §4.1 (about
5 minutes). Save answers verbatim.

**Phase 3 — Triage → `survey.md`, `ledger.md`.** `job_results(job_id)`; check reconciliation first
(katago-analysis §2). Compare with self_review.md (go-teaching §4.1) and pick **one** episode by the
selection score (go-teaching §2): a review teaches a single lesson. Write one or two hypotheses with a
number for it into ledger.md (katago-analysis §6); note the next two by score in survey.md as reserves. Re-plan and start the background
verification **before the first interview question** (katago-analysis §1 steps 5–6).

**Phase 3b — Episode interview → `thinking.md`.** go-teaching §4.2 for the selected episode;
`record_interview` after the answer. Update the ledger with the stated belief. If `verification_results(job_id)` still shows minutes of work
afterwards, ask go-teaching §4.3, then recall quizzes (§4.4).

**Phase 4 — Verify → ledger verdicts, `verified.md`.** Read the episodes as they finish:
`verification_results(job_id)` shows each one's `state`; `verification_results(job_id, episode)` on a
`done` one returns the belief protocol already run (katago-analysis §3, §5). A tool call blocks your
turn, so while nothing is done and the next result is more than about a minute away, ask go-teaching
§4.3, then a recall quiz (§4.4), instead of waiting; use `wait_seconds` only for the last short
stretch. Add what a stated belief needs beyond the protocol. Fill in query ids and verdicts.
Stop when the verification minutes are used; what is left stays UNTESTED. verified.md, per episode:
verdict, belief and source (stated / inferred), the misread (ply, move never considered, refutation),
the proof lines (forced vs chosen moves, resistance), the end comparison, the teachable move, refs.

**Phase 5 — Lesson → `lesson.md`.** One lesson, from the selected episode once it is CONFIRMED, plus one
strength (go-teaching §2). Write it in the lesson template, then reread every sentence against
"Words that must be cashed out" (go-teaching §3). Write the commentary and the summary block as review-dashboard
`references/dashboard-input.md` describes.

**Phase 6 — Deliver and remember.** Build and republish the dashboard to the live page's URL
(review-dashboard, "Phase 6"). Tell the student the page now holds the lesson and, at every move of the
game, the engine's best move in blue next to the move played, so they can ask about any of them. Give
a 6–10 line summary in chat: the lesson first, then the moves the survey ranks as the biggest losses
after it (move number, played, best, points lost, from the digest), as starting points for questions. Update memory (`references/memory.md`, "Phase 6: write"). Ask whether anything is
unclear; follow-ups go on the same page (review-dashboard, "Follow-up questions").

A seeding / calibration pass (the student brings `seed/seed_summary.*`) follows
`references/calibration.md` instead.

## When things go wrong

- `engine_unavailable`: tell the student which machine to start the server on; with a survey that
  finished earlier, continue from it (`reuse_existing`).
- Reconciliation mismatch: check rules, komi and handicap with `sgf_summary`; if wrong, fix them with
  the student and rerun the survey. If right, count the final board from the ownership overlay
  (`render_board`, `overlay: "ownership"`: territory + dead stones + captures + komi) and ask how the
  disputed groups were scored. A count that matches the SGF means the engine's score includes
  unfinished play: proceed. Otherwise note the gap and distrust only endgame figures.
- The student disagrees with a verdict: test their line with `analyze_line`, add it to the ledger, report
  the numbers; both lines go on the page (review-dashboard, "Follow-up questions").
- Time is up before verification finished: no lesson rather than an unverified one; the best moves on
  the page still stand. `verification_results(job_id, action: "cancel")` stops the queued work.
- The episode ends REFUTED or WEAK: say so and what the verification showed. If time remains, offer the
  first reserve from survey.md (`start_verification`, its interview, the same protocol); otherwise
  deliver the page without a lesson.
- `sealed`: ask the episode's interview question, `record_interview`, then call again.
- The student cannot see the page's boards: review-dashboard, "Fallback".
