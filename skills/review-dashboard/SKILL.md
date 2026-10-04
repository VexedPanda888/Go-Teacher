---
name: review-dashboard
description: The review page from start to finish — published live at the start of a review, it shows every board Claude sends (positions, lines, questions) and collects moves the student clicks; at the end it is rebuilt in place as the interactive dashboard (the key moments with their lines and takeaways, the best move at every move) from engine-validated data. Use in every step that shows the student a position or a line.
---

# Review dashboard

The page is a fixed template. You never edit it for a review and never hand-write its data. During
the review it is a **live page**: it reads rows from its own `db`, each made by katago-mcp's
`dashboard_row` with a SHA-256 the page checks. At the end it becomes the **dashboard**: the same URL,
rebuilt with the blob from `validate_variations`, also checksummed. Nothing unchecked reaches the student
either way, and the live page shows no engine data (the student is asked before they are told).

## The page lives the whole review

Every position or line you want the student to look at goes on the page as a **board**, not as a
`render_board` diagram in chat. `render_board` is the fallback for when the page cannot be used
(below).

**Open the page** (intake, right after `sgf_summary`):
1. `python3 <skill>/scripts/build_dashboard.py --live --title "Go review: vs <opponent>" --out review.html`
2. Publish `review.html` with the Artifact tool: icon `go`, title "Review: vs <opponent>, <date>", and
   `capabilities: {"db": {"rules": [{"path": "", "read": "view", "write": "owner"}]}, "user": {}}`, so
   only the student (the owner, which is also who your ArtifactData writes act as) writes to it. Keep the
   URL for the whole review.
3. `dashboard_row(kind: "game", game: <the OGS link or file>)`, then ArtifactData `set` with the
   returned `collection` (`review`), `doc_id` (`game`) and `data` = `row` exactly as returned.
4. Give the student the link: "Keep this page open next to the chat. Boards appear there as we go."
   Ask them to say whether they see the game. If they don't, use the fallback.

**A board** (any time): `dashboard_row(kind: "board", game: <job_id, or the link before the survey>,
board: {title, text?, at_move, line?, highlight?, ask?, episode?})`, then ArtifactData `set` with the
returned `collection` (`boards`), `doc_id` and `row`. In the same message, tell the student what is on
the page ("On the page: Move 41").
- `at_move` is the position after that move (`N − 1` for the position before move N). `line` is a
  sequence from there, numbered 1, 2, … on the board; colours alternate from the side to move.
  `highlight` marks points with a square. `title` is the list label and `text` the question in one or
  two sentences. You may reword both later; every other field is checked against the SHA-256.
  `episode` names the key moment the board belongs to (`M1`).
- The open page shows the newest board at once and keeps the list. Rewriting a board (same `id`)
  updates it in place.
- `from_game` (an OGS link or game id, e.g. `ogs_12345678`, or an `.sgf` name) shows a position of
  that past game instead, with `at_move` counted in that game: a recall quiz (go-teaching §6). The
  page labels it with the game's opponent and date.
- An engine line on a board is fine once the guided self-review is done and the student has answered
  about that moment (go-teaching §7, "Ask before you tell"). The server checks legality, not timing.

**Answers on the board** (`ask: "move"` or `"line"`): the student clicks the move, or the sequence
starting with `ask_color`, then presses Send, and tells you "done" in chat. Read it with ArtifactData
`get` on collection `answers`, doc id = the board's id: `{board, moves: ["WQ7", …], sent_at}`. Pass
`moves` on as given (`explain_moment` checks them again as `expected_line`). Free-text answers ("what
was it for?") stay in chat. Save answers in `notes.md` / `moments.md` like any other answer.

**Fallback.** The page reports "Live updates are not available in this view" when its `db` cannot run
(opened outside claude.ai, signed out), and the student cannot see a board you sent. Then show positions
and lines with `render_board` in chat, and take moves typed in chat. Write the boards anyway; they show
once the page works.

## The dashboard (at the end)

1. Write the page text from `moments.md`: per key moment a title, commentary per move, the takeaway.
   Read `references/dashboard-input.md` for how to write them and the input shape.
2. Call `validate_variations(job_id, episodes, summary)`. Fix any `errors` it reports (illegal move,
   wrong colour, bad range, unknown solve query) by correcting the input, not by dropping the check. A
   `warnings` entry like "never diverges from the game" is fine for an "As played" branch.
3. Save the returned `dashboard_data` string to a file exactly as returned (do not re-serialize it), and
   note the `sha256`. The server also saved `reviews/<game_id>/export-N.json`.
4. Build: `python3 <skill>/scripts/build_dashboard.py --blob blob.json --sha <sha256> --out review.html`
   (or `--export reviews/<game_id>/export-N.json` when that file is reachable).
5. Republish `review.html` to **the live page's URL** (pass `url`; leave `capabilities` out so the
   `db` and its boards stay). The open page reloads as the dashboard; its boards stay under "During the
   review", next to the key moments. That link is the deliverable; the student can also open it on a
   phone. Without a live page (the fallback from the start), publish a new artifact (icon `go`).
   The page shows, at every move of the game, the survey's best move in blue (a blue ring when the move
   played was the best) and the points the move lost. `validate_variations` exports it (`bestMoves`);
   there is nothing to write for it.
6. If the text needs a change after publishing, change the input, re-run `validate_variations`,
   rebuild, and republish the same artifact URL. Never patch the HTML or the blob by hand.
7. Keep the `validate_variations` input you sent (`dashboard_input.json` next to the notes): follow-up
   questions add to it.

## Follow-up questions (after publishing)

Every line you test for the student after the page is published goes on the page, so they can step
through it at the board instead of reading coordinates in chat.

1. Answer it as at a key moment: `explain_moment` (or the tool katago-analysis §4 names), explained to
   go-teaching §5. While you discuss it, show the lines as boards.
2. Add it to the saved input as a branch, with `"kind": "question"` and a label that names the question
   ("Your question: P8 first?"):
   - about a position inside a key moment's move range: a branch of that moment, next to its lines;
   - otherwise: a question episode, `{"id": "Q1", "kind": "question", "moves": [k, m], "title": "…",
     "commentary": [{"at_move": k, "text": "the answer, 1–4 sentences"}], "branches": […]}`, numbered
     Q1, Q2, … in the order asked. `comparison` works in a question episode too.
3. Re-run `validate_variations` with the whole input, rebuild, and republish the same artifact URL
   (step 6). Batch the questions of one exchange into one republish; tell the student the page is updated.

A line the engine did not play or evaluate never goes on the page; one that fails validation is fixed
from the error or left in chat with the reason.

## Troubleshooting (live page)

- A board is listed as "did not arrive intact": its `row` was changed between `dashboard_row` and the
  write (a move, a number, a key). Write the row again exactly as returned; to change wording, change
  only `title` / `text`.
- "The game record did not arrive intact": the same for the `review/game` row.
- ArtifactData `get` on `answers/<id>` finds nothing: the student has not pressed Send yet, or wrote on
  another board. Ask them; never guess the moves.
