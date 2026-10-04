# Dashboard input (the end of a review, and follow-up questions)

Read before writing the page text and the `validate_variations` input.

## Writing for the page

- Commentary entries appear when the student steps onto that move; write each as a self-contained
  paragraph of 1–4 sentences in plain language for a player of the student's rank. Name points as on
  the board (Q7).
- Commentary follows the explanation standard (go-teaching §5): at the position before the move, what
  the student was trying to do and what was right in it; at the move, where it goes wrong and what the
  better move does; at the end of the sequence, what is different from the better line's end. Concept
  words and "the engine ranks/prefers": go-teaching §5.
- `takeaway` is the student's own sentence about what they will do differently, as you agreed it in the
  review (go-teaching §4.5). Keep their words; one or two sentences.
- Label branches by what they are for the student: "As played", "What you expected", "Where it breaks
  (move k)", "Better: E", "If White resists at X", "Your idea (P8)", "Your question: P8 first?". Never
  "Engine's line".
- Every number on the page comes from the engine; do not restate scores in the text unless they came
  from a tool result.

## validate_variations input (what you write)

```json
{
  "job_id": "job_…",
  "episodes": [{
    "id": "M1", "kind": "moment", "moves": [87, 103], "title": "Saving the wrong group", "points_lost": 14.2,
    "commentary": [
      {"at_move": 86, "text": "Before your move: …"},
      {"at_move": 87, "text": "You played Q7 …"},
      {"at_move": 103, "text": "By here the group is dead …"}
    ],
    "branches": [
      {"id": "B1", "label": "As played",                 "kind": "as_played", "from_move": 86, "moves": ["BQ7", "WR8"]},
      {"id": "B2", "label": "What you expected",         "kind": "expected",  "from_move": 86, "moves": ["BQ7", "WS8", "BR8"]},
      {"id": "B3", "label": "Where it breaks (move 88)", "kind": "misread",   "from_move": 86, "moves": ["BQ7", "WR8", "BS8", "WT7"]},
      {"id": "B4", "label": "Better: R8",                "kind": "better",    "from_move": 86, "moves": ["BR8", "WQ7", "BS8"]},
      {"id": "B5", "label": "If White resists at Q8",    "kind": "resistance", "from_branch": "B4", "at_ply": 1, "moves": ["WQ8", "BP8"]},
      {"id": "B6", "label": "Your idea (P8)",            "kind": "fix",       "from_move": 86, "moves": ["BP8", "WR8"]}
    ],
    "comparison": {"a": "B1", "b": "B4"},
    "quiz": {"at_move": 87, "type": "move", "candidates": ["R8", "P8"]},
    "takeaway": "When my group already has a base and White plays a contact move nearby, I'll read whether I can ignore it before answering."
  }],
  "summary": { … see below … }
}
```

- `moves` is the moment's move range; `from_move` is the position a branch starts from (the position
  *before* move `from_move + 1`); branch moves alternate colours from that position's side to move and are
  written `BQ7`, `WR8`, `Bpass`.
- Branch lines come from `explain_moment`: `lines.best.line` for "Better", `lines.move.line` for "As
  played", `reading.line` plus `reading.misread.refutation` for "What you expected" / "Where it breaks"
  (the misread's `line_to_here` + `refutation`), and for each `lines.best.resistance` entry a branch
  `{"kind": "resistance", "from_branch": <the Better branch>, "at_ply": entry.at_ply, "moves":
  [<opponent colour> + entry.move] + entry.refutation}` (`at_ply` counts from the move itself: line[0]).
  A `local_solve` sequence is pairs (`["B", "Q7"]`): join each into `"BQ7"`; when its first move is not
  by the side to move there, start with that side's pass (`"Wpass"`).
- `from_branch` + `at_ply` starts a branch inside another one (after `at_ply` of its moves; the parent
  must come earlier in the list). The page opens it at the fork. `kind` is one of `as_played`,
  `expected`, `misread`, `better`, `resistance`, `fix`, `question`.
- Episode `kind` is `"moment"` (the default) or `"question"`: a follow-up the student asked (see
  review-dashboard, "Follow-up questions"). Questions are listed apart under "Your questions" and need
  only `id`, `kind`, `moves`, `title`, `commentary` and `branches`.
- `comparison` names two branches (`a` = the played line, `b` = the better line); the server computes
  what differs at their ends (groups, territory, who plays next freely, the next biggest move, weak
  groups) and the page shows it as a table. Write no numbers of your own about it.
- Quiz `type: "move"`: the student guesses move `at_move`; `candidates` are the moves you want graded
  (the actual move and the peer-typical move are added and labelled automatically).
  Quiz `type: "status"`: `{"at_move": n, "type": "status", "status": {"group_point": "F3",
  "solve_query_id": "q_…"}}` where the query id comes from a `local_solve` you ran in this session.

## summary (free text you write, shown at the bottom of the page)

```json
{
  "story": "two to four sentences: how the game went and where it turned",
  "takeaways": [{"momentId": "M1", "title": "…", "takeaway": "the student's sentence"}],
  "strengths": ["a correct move that was hard for your level, with its move number"],
  "nextGame": "one concrete thing to try once per game, from the takeaways",
  "reliability": "how deep the checks went, e.g. 'Each key moment checked at 12,000 visits with 8-move lines.'"
}
```

`takeaways` may be left out: the page then lists the moments' own `takeaway` fields.

## Errors from validate_variations and the builder

- `checksum mismatch` from the builder: the blob was modified after export. Re-run `validate_variations`.
- `bad_branch_parent`: `from_branch` names a branch that is later in the list or failed validation, or
  `at_ply` is longer than it. `bad_comparison`: `comparison` names a branch that is not valid.
- `wrong_color`: the branch starts with the colour that is not to move at `from_move`; either start one
  move earlier or begin the branch with the opponent's actual move.
- `bad_kind`: an episode `kind` other than `moment` or `question`.
- `unknown_query` for a status quiz: the `solve_query_id` is from another server session; run
  `local_solve` again and use the new id.
- The page shows "No estimate stored for this position" on the territory toggle: ownership is exported
  only for moment roots (before/after) and branch ends; that is expected elsewhere.
