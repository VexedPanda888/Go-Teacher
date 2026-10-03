# Dashboard input (Phases 5–6)

Read before writing the lesson commentary and the `validate_variations` input.

## Writing for the page

- Commentary entries appear when the student steps onto that move; write each as a self-contained
  paragraph of 1–3 sentences (up to four in a "why is this best" answer: `explaining-a-move.md`) in plain language for a player of the student's rank. Name points as on the board (Q7).
- Commentary follows the lesson template (go-teaching §3): at the root, what the student was trying to
  do; at the divergence, the move they did not consider; at the end of the chain, what is different
  from the better line's end. Concept words and "the engine ranks/prefers": go-teaching §3.
- Put the "what to notice" cue in `cue`, the check in `rule_check`. One sentence each.
- Label branches by what they are for the student: "As played", "What you expected", "Where it breaks
  (move k)", "Better: forced line", "If White resists at X", "Your fix (P8)", "Your question: P8 first?". Never "Engine's line".
- Every number on the page comes from the engine; do not restate scores in the text unless they came
  from a tool result you can cite in the ledger.

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
  "reliability": "how deep the verification went, e.g. 'E1 checked at 6,000 visits with 8-ply lines.'"
}
```

## Errors from validate_variations and the builder

- `checksum mismatch` from the builder: the blob was modified after export. Re-run `validate_variations`.
- `bad_branch_parent`: `from_branch` names a branch that is later in the list or failed validation, or
  `at_ply` is longer than it. `bad_comparison`: `comparison` names a branch that is not valid.
- `wrong_color` error: your branch starts with the colour that is not to move at `from_move`; either
  start one move earlier or begin the branch with the opponent's actual move.
- `unknown_query` for a status quiz: the `solve_query_id` is from another server session; run
  `local_solve` again and use the new id.
- The page shows "No estimate stored for this position" on the territory toggle: ownership is exported
  only for episode roots (before/after) and branch ends; that is expected elsewhere.
