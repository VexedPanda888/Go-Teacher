---
name: katago-analysis
description: How to use the katago-mcp tools in a game review — the survey and its story (lead, group fates, swings, key moments), explain_moment (the evidence for one move against the best one, prepared in the background), the follow-up tools for the student's questions, and what keeps the explanations honest (stability, close moves, resistance). Use whenever a review touches the engine.
---

# KataGo analysis for teaching

You interpret engine output; you are not a source of Go facts. Everything concrete you tell the student
about a position comes from a tool result whose `query_id` you note in `moments.md`.

## 0. Conventions

- Coordinates are GTP: columns A–T without I, rows 1–19. Never SGF letters. Copy points from tool output.
- Scores are **points** from the stated `perspective` (default: the student's colour). Winrate is never
  the teaching quantity; in handicap games it is uninformative.
- Ownership is Black-positive in raw arrays; tool summaries already flip to the perspective.
- A position is `{job_id, move_number}` (the position *after* that move: `N − 1` is the position before
  move N), `{ref: "pos_…"}` from any result, or `{sgf, move_number}`. `then: [["B","Q7"],["W","R8"]]`
  walks moves from it; illegal moves are rejected with the offending ply.
- Lines are written `"BQ7"`, `"WR8"`, `"Bpass"`, colours alternating from the side to move.
- Search sizes are fixed per machine (`engine_info` → `search`); a review takes as long as it needs.
  Budgets are profiles: `{"profile": "root" | "line_node" | "stability" | "local_solve" | "quick"}`;
  leave them out and each tool uses its default. Never invent visit counts.

## 1. Starting a review

1. `engine_info` — the machine, `throughput.visits_per_second_sustained > 0` (else
   `refresh_benchmark: true`), the human model loaded, and `estimates`: how long `explain_moment` takes
   here. Use it to set expectations ("each key moment needs about a minute and a half of engine time;
   the first few are prepared while we talk").
2. `sgf_summary` — the student's colour, rules, komi, handicap, result; read `warnings`. Pass the game as
   the OGS link or id (the server fetches and caches the SGF) or as the file name of an `.sgf` in the
   server's `games/` folder. Never retype SGF text into a tool call. Use the same `sgf` value for
   `start_game_analysis`.
3. `start_game_analysis(sgf)` — the survey, sized to take about 10 minutes on this machine. When it
   finishes, the server prepares `explain_moment` for the story's top key moment in the
   background (nothing is shown; you ask first). Queue nothing else before the story: queue a key
   moment you choose that is not `prepared` (`background: true`) when you choose it.

A tool call blocks your turn: you cannot talk to the student while a search runs. That is why the slow
work runs in the background: the survey while the student does the guided self-review, the key
moments while you talk about the story. Poll with
`job_status`; between the student's answers is a good time.

## 2. Reading the story (`job_results`)

Use it in this order:

1. `game.reconciliation` — if `mismatch`, stop and check komi, rules and handicap before anything else
   (go-teacher-flow, "When things go wrong").
2. `lead` — the student's score lead every 20 moves: the shape of the game.
3. `group_events` — groups of four or more stones whose status changed and stayed changed: the move,
   who played it (`by`), whose group (`whose`), `from` → `to` (alive / unsettled / dead). The plot of
   the game.
4. `swings` — the biggest single-move losses of both players, in move order, with the best move, the
   region and the lead after. An opponent's swing carries `gave_back`: how much the student's next
   move returned. Large `gave_back` = a mistake not punished.
5. `decisive` / `last_chance` — where the game was decided, and (in a loss) the last student move where
   the best move still kept it close.
6. `moments` — candidate key moments: the student's costly sequences ranked by their first move's loss
   (`M1`, `M2`, …). Each has the move, played and best, `points_lost` (of the first move) and
   `net_loss` (over the whole sequence), the lead before, `acceptable_moves` (within a point of the
   best), how often players of the student's rank and a few stones up play each move (`human`), the
   `findable_move` (the acceptable move the stronger players find most), the opponent's best reply and
   its character (`tenuki`: the move did not need an answer; `local_sharp`: it started a fight;
   `local_calm`), the `group_events` it caused, `stability`, and `prepared` (done / running / queued /
   no).
7. `positives` — correct moves the student's rank rarely finds, where the position offered a real choice.
8. `reliability` — low-visit positions and unstable moments: be careful with claims there.

These are survey numbers from a short search. For anything you teach about a moment, use
`explain_moment`, which searches deeper; when the two disagree, its numbers stand.

## 3. `explain_moment` — the evidence for one moment

`explain_moment({job_id, move_number: N − 1})` explains the move played at N against the best move.
Pass `move` to explain another move instead ("what about X here?"). Options: `expected_line` (the moves
the student expected after their move, as they clicked them) adds a reading check; `reading: true`
forces one, `false` skips it (by default it runs when the opponent's answer is a local fight);
`deep: false` skips the stability re-run. `background: true` queues it and returns at once; the
identical call later returns the stored result (`precomputed`). An `expected_line` adds work, but only
the reading check: the rest is already stored.

What it returns, and what each part is for in the explanation (go-teaching §5):

| Field | What it is | Use it for |
|---|---|---|
| `best`, `move` | each move with the score after it and how often peer / target players choose it; `move.points_lost` and `verdict` (`best`, `as_good`: within a point, `mistake`) | the size of the mistake, said once |
| `stability` | a deeper search: same best move and a score that moved < 0.5 → `stable` | whether to trust the best move; when not stable, explain the deeper search's move (it already does) |
| `candidates`, `findable` | the top moves, and the acceptable move stronger players find most | a simpler move to recommend when the best is hard to find |
| `lines.best`, `lines.move` | each move's forced line (`line`, which replies were `forced`, `stop_reason`), the opponent's natural `resistance` with its `refutation`, and the end in brief | the sequences you show, as boards and dashboard branches |
| `comparison` | what differs at the two ends: `groups_changed`, `territory_changed` by region, `sente`, `tempo` (the next move and its value), `weak_groups`, `score_diff` | the *why* |
| `purpose.best`, `purpose.move` | what each move threatened (`threat`: follow-up and value), what it prevented (`defense`), the opponent's best `reply` and its character, the groups `left_behind`, `tenuki_value` | what each move is *for*, against what the student said |
| `reading` | when run: the student's (or a peer's) expected line checked move by move; `misread` = the first move that loses more than 3, the move `never_considered` and its `refutation` | where their reading broke; no misread means the reading held and the mistake is about value |
| `notes` | warnings: the ends are within 2 points; a line depends on the opponent cooperating; the deeper search changed the best move | what not to claim |
| `query_ids` | one per part | `moments.md` |

## 4. The student's follow-up questions

`explain_moment` covers most questions. When one needs a specific test:

| Question | Tool |
|---|---|
| "What about my line?" (a sequence) | `analyze_line(pos, line)`: every node evaluated, then the engine continues |
| "Is this group alive?" | `local_solve(pos, group_point)`: attacker-first and defender-first playouts; read `caveats` |
| "Was this sente?" / endgame size | `swing_value(pos, [points])`: each point as a swing, sente or gote |
| "Why there and not here?" (urgent vs big) | `pass_probe(pos, player, move, {rank_regions: true})`: the value of playing in each area now |
| "Would my opponent have found it?" | `human_move_distribution(pos, ["opponent"], [moves])` |
| A line from any position, or a comparison of two ends | `forced_line`, `terminal_features(a, compare_to: b)` |
| What a move was for, alone | `intent_probe(pos, move)`; where reading breaks: `expectation_probe(pos, move, {expected_line})` |

Every line you discuss goes on the page (go-teaching §6, review-dashboard "Follow-up questions").

## 5. What keeps the explanations honest

- **Stability.** `explain_moment` re-runs the position at four times the root visits. Teach the best
  move only when `stability.stable` is true, or say "at a deeper search K10 is as good" when the top move
  changes. For a claim deep inside a line, re-check that node with `analyze_position(pos with then,
  {"profile": "stability"})`.
- **Close is close.** `comparison.score_diff` under 2 points is not a lesson: say the moves are close
  and what each aims at.
- **Resistance.** A line that holds only if the opponent cooperates (a resistance that costs them
  nothing) is shown with the resistance, or dropped.
- **Forced vs chosen.** Nodes with `forced: false` were choices: say "White would play", not "White
  must play".
- **Human numbers are not correctness.** Peer and target probabilities say what is natural and
  learnable; whether a move is good comes only from search.

## 6. Handicap games

Use score, never winrate. Separate the objective verdict (points) from the practical one: as the weaker
player with stones, simplicity has value. A move that loses 2 points but removes all fighting may be
the right practical choice; say both. `decisive` already uses the score basis in handicap games.

## 7. Errors and edge cases

- `engine_busy`: a survey is running; wait for `job_status` or cancel it.
- `budget_infeasible`: throughput unknown → `engine_info(refresh_benchmark=true)`.
- `illegal_move` / `wrong_color`: fix the move list; never guess a coordinate.
- Resigned games end at the resignation; do not analyse "what would have happened after".
- `explain_moment` without `move` on a position that is not before a game move: pass the move.
