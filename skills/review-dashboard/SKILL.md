---
name: review-dashboard
description: The review page from start to finish. Published live in Phase 0, it shows the game and every board Claude sends during the review (self-review positions, interview positions, the student's lines) in place of ASCII diagrams in chat, and collects answers the student clicks on the board. In Phase 6 it is rebuilt in place as the interactive dashboard (goban, replay, score graph, synced commentary, engine-verified variations, quiz). Use in every phase that shows the student a position or a line. Only rows from katago-mcp's dashboard_row and data that passed validate_variations go on the page.
---

# Review dashboard

The page is a fixed template. You never edit it for a review and never hand-write its data. During
the review it is a **live page**: it reads rows from its own `db`, each made by katago-mcp's
`dashboard_row` with a SHA-256 the page checks. In Phase 6 it becomes the **dashboard**: the same URL,
rebuilt with the blob from `validate_variations`, also checksummed. Nothing unverified reaches the student
either way, and nothing from the engine reaches the live page.

## The page lives the whole review

Every position or line you want the student to look at goes on the page as a **board**, not as a
`render_board` diagram in chat. `render_board` is the fallback for when the page cannot be used
(below).

**Phase 0: open the page** (right after `sgf_summary`):
1. `python3 <skill>/scripts/build_dashboard.py --live --title "Go review: vs <opponent>" --out review.html`
2. Publish `review.html` with the Artifact tool: icon `go`, title "Review: vs <opponent>, <date>", and
   `capabilities: {"db": {"rules": [{"path": "", "read": "view", "write": "owner"}]}, "user": {}}`, so
   only the student (the owner, which is also who your ArtifactData writes act as) writes to it. Keep the
   URL for the whole review.
3. `dashboard_row(kind: "game", game: <the OGS link or file>)`, then ArtifactData `set` with the
   returned `collection` (`review`), `doc_id` (`game`) and `data` = `row` exactly as returned.
4. Give the student the link: "Keep this page open next to the chat. Boards appear there as we go."
   Ask them to say whether they see the game. If they don't, use the fallback.

**A board** (any phase): `dashboard_row(kind: "board", game: <job_id, or the link before the survey>,
board: {title, text?, at_move, line?, highlight?, ask?, episode?})`, then ArtifactData `set` with the
returned `collection` (`boards`), `doc_id` and `row`. In the same message, tell the student what is on
the page ("On the page: Move 41").
- `at_move` is the position after that move (`N − 1` for the position before move N). `line` is a
  sequence from there, numbered 1, 2, … on the board; colours alternate from the side to move.
  `highlight` marks points with a square. `title` is the list label and `text` the question in one or
  two sentences. You may reword both later; every other field is checked against the SHA-256.
- The open page shows the newest board at once and keeps the list. Rewriting a board (same `id`)
  updates it in place.
- A board shows no engine data. Never put an engine line on it before its episode's interview is
  recorded (go-teaching §6). The server checks legality, not sealing.

**Answers on the board** (`ask: "move"` or `"line"`): the student clicks the move, or the sequence
starting with `ask_color`, then presses Send, and tells you "done" in chat. Read it with ArtifactData
`get` on collection `answers`, doc id = the board's id: `{board, moves: ["WQ7", …], sent_at}`. Pass
`moves` on as given (`record_interview` checks them again). Free-text answers ("what was it for?") stay
in chat. Answers belong in `thinking.md` / `self_review.md` like any other answer.

**Fallback.** The page reports "Live updates are not available in this view" when its `db` cannot run
(opened outside claude.ai, signed out), and the student cannot see a board you sent. Then show positions
and lines with `render_board` in chat as before, and take moves typed in chat. Write the boards anyway;
they show once the page works.

## Phase 6: the dashboard

1. Finish the lesson text first (`lesson.md`): titles, commentary per move, `rule_check`, `cue`, quiz choice, summary.
2. Call `validate_variations(job_id, episodes, summary)` with the input shape below. Fix any `errors` it
   reports (illegal move, wrong colour, bad range, unknown solve query) by correcting the episode input —
   not by dropping the check. A `warnings` entry like "never diverges from the game" is fine for an
   "As played" branch.
3. Save the returned `dashboard_data` string to a file exactly as returned (do not re-serialize it), and
   note the `sha256`. The server also saved `reviews/<game_id>/export-N.json`.
4. Build: `python3 <skill>/scripts/build_dashboard.py --blob blob.json --sha <sha256> --out review.html`
   (or `--export reviews/<game_id>/export-N.json` when that file is reachable).
5. Republish `review.html` to **the live page's URL** (pass `url`; leave `capabilities` out so the
   `db` and its boards stay). The open page reloads as the dashboard. Its boards stay under "During the
   review", next to the lessons. That link is the deliverable; the student can also open it on a phone.
   Without a live page (the fallback from Phase 0 on), publish a new artifact as before (icon `go`).
6. If the text needs a change after publishing, change the episode input, re-run `validate_variations`,
   rebuild, and republish the same artifact URL. Never patch the HTML or the blob by hand.
7. Keep the `validate_variations` input you sent (`dashboard_input.json` next to the handoff files):
   follow-up questions add to it.

## Follow-up questions (after publishing)

Every line you test for the student after the page is published goes on the page, so they can step
through it at the board instead of reading coordinates in chat.

1. Test it as usual (`analyze_line`, `forced_line`, …) and add it to the ledger. While you discuss it,
   show it as a board (`dashboard_row`, above) rather than in chat (go-teaching §6).
2. Add it to the saved input as a branch, with `"kind": "question"` and a label that names the question
   ("Your question: P8 first?"):
   - about a position inside a lesson's move range: a branch of that lesson, next to its lines;
   - otherwise: a question episode, `{"id": "Q1", "kind": "question", "moves": [k, m], "title": "…",
     "commentary": [{"at_move": k, "text": "the answer, 1–3 sentences"}], "branches": […]}`, numbered
     Q1, Q2, … in the order asked. `comparison` works in a question episode too.
3. Re-run `validate_variations` with the whole input, rebuild, and republish the same artifact URL
   (step 6). Batch the questions of one exchange into one republish; tell the student the page is updated.

A line the engine did not play or evaluate never goes on the page; one that fails validation is fixed
from the error or left in chat with the reason.

## validate_variations input (what you write)

```json
{
  "job_id": "job_…",
  "episodes": [{
    "id": "E1", "moves": [87, 103], "title": "Saving the wrong group",
    "category": "3 Own life and death", "tags": ["3", "13"], "points_lost": 14.2,
    "commentary": [
      {"at_move": 86, "text": "Before your move: …"},
      {"at_move": 87, "text": "You played Q7 …"},
      {"at_move": 103, "text": "By here the group is dead …"}
    ],
    "belief": {"id": "needs_defending", "source": "stated"},
    "branches": [
      {"id": "B1", "label": "As played",                 "kind": "as_played", "from_move": 86, "moves": ["BQ7", "WR8"]},
      {"id": "B2", "label": "What you expected",         "kind": "expected",  "from_move": 86, "moves": ["BQ7", "WS8", "BR8"], "ledger_ref": "H2"},
      {"id": "B3", "label": "Where it breaks (move 88)", "kind": "misread",   "from_move": 86, "moves": ["BQ7", "WR8", "BS8", "WT7"], "ledger_ref": "H2"},
      {"id": "B4", "label": "Better: forced line",       "kind": "better",    "from_move": 86, "moves": ["BR8", "WQ7", "BS8"], "ledger_ref": "H1"},
      {"id": "B5", "label": "If White resists at Q8",    "kind": "resistance", "from_branch": "B4", "at_ply": 1, "moves": ["WQ8", "BP8"]},
      {"id": "B6", "label": "Your fix (P8)",             "kind": "fix",       "from_move": 86, "moves": ["BP8", "WR8"], "ledger_ref": "H3"}
    ],
    "comparison": {"a": "B1", "b": "B4"},
    "quiz": {"at_move": 87, "type": "move", "candidates": ["R8", "P8"]},
    "rule_check": "…the check to run at the board, one sentence…", "cue": "…what to notice at the board…"
  }],
  "summary": { … see below … }
}
```

- `moves` is the episode's move range; `from_move` is the position a branch starts from (the position
  *before* move `from_move + 1`); branch moves alternate colours from that position's side to move and are
  written `BQ7`, `WR8`, `Bpass`.
- Quiz `type: "move"`: the student guesses move `at_move`; `candidates` are the moves you want graded
  (the actual move and the peer-typical move are added and labelled automatically).
  Quiz `type: "status"`: `{"at_move": n, "type": "status", "status": {"group_point": "F3",
  "solve_query_id": "q_…"}}` where the query id comes from the `local_solve` you ran for this episode.
- Keep `ledger_ref` pointing at the ledger hypothesis the branch tests; the dashboard does not show it,
  the memory does.
- Branch lines come from tool results: `forced_line.line` for "Better" / "As played", the
  `expectation_probe` line plus its `refutation` for "What you expected" / "Where it breaks", a node's
  `resistance.move` plus its `refutation` for "If White resists".
- `from_branch` + `at_ply` starts a branch inside another one (after `at_ply` of its moves; the parent
  must come earlier in the list). The page opens it at the fork. `kind` is one of `as_played`,
  `expected`, `misread`, `better`, `resistance`, `fix`, `question`.
- Episode `kind` is `"lesson"` (the default) or `"question"`: a follow-up the student asked after the
  lessons (see "Follow-up questions"). Question episodes are listed apart under "Your questions" and
  need only `id`, `kind`, `moves`, `title`, `commentary` and `branches`.
- `comparison` names two branches (`a` = the played line, `b` = the better line); the server computes
  what differs at their ends (groups, territory, who plays next freely, the next biggest move, weak
  groups) with `terminal_features`, and the page shows it as a table. Write no numbers of your own
  about it.
- `rule_check` is the check to run at the board (go-teaching §3 part 6); it is shown first in the
  lesson box. `principle` is still accepted for older reviews.

## summary (free text you write, shown at the bottom of the page)

```json
{
  "headline": "one or two sentences: what decided this game and what to take from it",
  "lessons": [{"episodeId": "E1", "title": "…", "ruleCheck": "…", "cue": "…"}],
  "strengths": ["a correct move that was hard for your level, with its move number"],
  "selfReview": {"agreements": ["…"], "blindSpots": ["…"]},
  "nextGame": "one concrete thing to try once per game",
  "reliability": "how deep the verification went, e.g. 'E1 and E2 checked at 6,000 visits with 8-ply lines; E3 at 1,000.'"
}
```

## Writing for the page

- Commentary entries appear when the student steps onto that move; write each as a self-contained
  paragraph of 1–3 sentences in plain language for a player of the student's rank. Name points as on the board (Q7).
- Commentary follows the lesson template (go-teaching §3): at the root, what the student was trying to
  do; at the divergence, the move they did not consider; at the end of the chain, what is different
  from the better line's end. Concept words and "the engine ranks/prefers": go-teaching §3.
- Put the "what to notice" cue in `cue`, the check in `rule_check`. One sentence each.
- Label branches by what they are for the student: "As played", "What you expected", "Where it breaks
  (move k)", "Better: forced line", "If White resists at X", "Your fix (P8)", "Your question: P8 first?". Never "Engine's line".
- Every number on the page comes from the engine; do not restate scores in the text unless they came
  from a tool result you can cite in the ledger.

## Troubleshooting

- `checksum mismatch` from the builder: the blob was modified after export. Re-run `validate_variations`.
- `bad_branch_parent`: `from_branch` names a branch that is later in the list or failed validation, or
  `at_ply` is longer than it. `bad_comparison`: `comparison` names a branch that is not valid.
- `wrong_color` error: your branch starts with the colour that is not to move at `from_move`; either
  start one move earlier or begin the branch with the opponent's actual move.
- `unknown_query` for a status quiz: the `solve_query_id` is from another server session; run
  `local_solve` again and use the new id.
- The page shows "No estimate stored for this position" on the territory toggle: ownership is exported
  only for episode roots (before/after) and branch ends; that is expected elsewhere.
- A board is listed as "did not arrive intact": its `row` was changed between `dashboard_row` and the
  write (a move, a number, a key). Write the row again exactly as returned; to change wording, change
  only `title` / `text`.
- "The game record did not arrive intact": the same for the `review/game` row.
- ArtifactData `get` on `answers/<id>` finds nothing: the student has not pressed Send yet, or wrote on
  another board. Ask them; never guess the moves.
