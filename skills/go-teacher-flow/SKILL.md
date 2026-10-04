---
name: go-teacher-flow
description: The Go-teacher review procedure for a plain Claude Desktop chat, the only kind that reaches the local katago MCP server — a quick pass for the story of the game, then the key moments one at a time (ask what the student was thinking, explain with engine-checked lines, go back and forth until they can say what they will do differently), then the page and memory. Use when the student asks for a game review or "the Go teacher". Works with go-teaching, katago-analysis and review-dashboard.
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
**go-teaching** (how a teacher reviews, the questions, the explanation standard, conduct),
**katago-analysis** (the story, `explain_moment`, follow-up tools), **review-dashboard** (the page).
Memory is the "Go teacher memory" artifact, read and written with `ArtifactData` as `references/memory.md`
describes.

**The goal of a review:** the student understands the mistakes that mattered and leaves each one able
to say, in their own words, what they will do differently next time. There is no time budget: aim for
about 30 minutes, say so at the start, and let the review take longer when the student wants to keep
going. Never cut a key moment short of its takeaway to save time.

The student follows the review on one page that grows as you go: every position or line you show them
is a board there, and they answer moves by clicking (review-dashboard; go-teaching §7, "Every line
visible").

## A review, step by step

Keep two notes files in the sandbox so later steps (and a compacted conversation) can rely on them:
`notes.md` (game facts, page URL, recall list, the upfront answers, the story, the moments chosen) and
`moments.md` (per key moment: the student's thinking verbatim, the evidence with query ids, the
explanation's main line, the takeaway). Update them as you go.

**1 — Intake.** Ask for the OGS game link (or an `.sgf` name in the server's `games/` folder). Run
`sgf_summary`; resolve every warning with the student. Read memory (`references/memory.md`, "At the
start"). Open the live page (review-dashboard, "Open the page") and give the student the link. Tell them
the plan in two sentences: a quick pass over the whole game, then a few key moments together, about 30
minutes in all.

**2 — Survey and upfront questions.** `start_game_analysis(sgf)` (sized to this machine, about 10
minutes). While it runs, ask go-teaching §2 (the upfront questions); save the answers verbatim in
`notes.md`. If the survey still runs after them, a recall quiz on an old takeaway (go-teaching §6).

**3 — The story.** `job_results(job_id)`; check `game.reconciliation` first (katago-analysis §2).
Tell the student the story of the game in a few sentences and set it against their own answers
(go-teaching §3). Choose the key moments as you go (go-teaching §3, "Choosing key moments"): name the
first one or two and why; queue any that is not `prepared` with `explain_moment(..., background: true)`.

**4 — Each key moment, one at a time** (go-teaching §4):
1. *Ask first.* The position before the move as a board, the move marked; "what was it for, and what did
   you expect next?" — they type the purpose and click the line. Optionally "knowing it cost something,
   what would you play now?" Nothing from the engine about this moment before their answer.
2. *Evidence.* `explain_moment({job_id, move_number: N − 1}, options: {expected_line: their clicked line})`
   (it returns at once when prepared; the expected line adds only the reading check). Note query ids in
   `moments.md`.
3. *Explain* to the explanation standard (go-teaching §5): their thinking first, then what actually
   happens, every sequence on the page as a board.
4. *Back and forth* until they can explain it back: their questions ("what about X?" →
   `explain_moment(pos, X)`), your checks (go-teaching §4.4). Use the waits for the next moment's
   question or a recall quiz.
5. *Takeaway.* They say what they will do differently; sharpen it with questions until it is concrete
   and checkable at the board (go-teaching §4.5). Record it in `moments.md`.

Then decide whether there is another moment worth the student's time. Around 30 minutes, say where you
are and ask whether to look at one more or finish.

**5 — Deliver.** Build the dashboard from the moments (review-dashboard, "The dashboard") and republish
it to the live page's URL. In chat, 5–8 lines: the story in one sentence, each takeaway in the student's
words, and two or three other moves from `swings` they might ask about (the page shows the best move
at every move in blue). Follow-ups go on the same page (review-dashboard, "Follow-up questions").

**6 — Remember.** Write memory (`references/memory.md`, "At the end") and say in one line what was
recorded; when a takeaway repeats an earlier one, say that too.

## When things go wrong

- `engine_unavailable`: tell the student which machine to start the server on; with a survey that
  finished earlier, continue from it (`reuse_existing`).
- Reconciliation mismatch: check rules, komi and handicap with `sgf_summary`; if wrong, fix them with
  the student and rerun the survey. If right, count the final board from the ownership overlay
  (`render_board`, `overlay: "ownership"`: territory + dead stones + captures + komi) and ask how the
  disputed groups were scored. A count that matches the SGF means the engine's score includes
  unfinished play: proceed. Otherwise note the gap and distrust only endgame figures.
- The student disagrees with an explanation: test their line (`analyze_line`, or `explain_moment` with
  their move), put both lines on the page, and report what happens at the ends.
- `explain_moment` notes say the two moves are close, or the line depends on the opponent cooperating:
  say so plainly; if nothing else in the moment is worth teaching, move on to another moment.
- The deeper search disagrees with the survey's best move (`stability.stable` false): say so in one
  sentence and explain the deeper search's move.
- The student wants to stop early: deliver the page with the moments finished so far.
- The student cannot see the page's boards: review-dashboard, "Fallback".
