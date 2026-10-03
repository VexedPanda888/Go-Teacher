# Explaining why a move is best (follow-up questions)

Read when the student asks why the blue move at move N is best, why their move was not, or "what about
X here?" (then X takes the place of the played move G below). E is the best move, G the move compared
with it, `pos` = `{job_id, move_number: N − 1}`, the position before move N. Every claim in the answer
comes from a result of the steps below; record each in the ledger as `Q<k>-H<n>` with its query id.

## 0. Say how long it takes

Two tiers. Estimate each from `engine_info` → `throughput.visits_per_second_sustained` and the plan's
`profiles` (searches × visits ÷ visits per second), tell the student both, and let them choose; default
to the full tier when it takes under about 5 minutes.

- **Quick** (steps 1 without stability, 2a–2b, 4–5): about 40 `line_node` searches and one `root`.
- **Full** (everything): about 100 `line_node` searches, five `root` searches and the `stability` search.

Several questions in one message: run them one after another, answer each as it finishes.

## 1. Confirm the move really is best

The blue move comes from the survey, a short search. Before explaining it:
`analyze_position(pos, {"profile": "root"})`, then (full tier) `{"profile": "stability"}`
(katago-analysis §5).

- Same top move, score moved < 0.5: proceed with E.
- The top move changed, or the two searches disagree: say so in one sentence ("at a deeper search K10 is
  as good as the blue move"); explain the deeper search's best move, or both when within a point.
- G is in the deeper search's `acceptable_set`: G was as good. Say so and stop, unless the student asks
  what the difference between the two is (then continue, as a comparison of two good moves).

## 2. The evidence

**Always:**

a. `forced_line(pos, E)` and `forced_line(pos, G)`: the must-moves after each, the resistance, the ends.
b. `terminal_features({ref: end of G's line}, compare_to: {ref: end of E's line})`: what differs at the
   ends (groups, territory by region, who plays next freely, the next biggest move).
c. `intent_probe(pos, E)` and `intent_probe(pos, G)`: for each, what it threatens (`threat`), what it
   prevents (`defense`), the opponent's best answer (`reply`), the groups left behind (`left_behind`),
   whether the answer can be ignored (`tenuki_value`). On E, ignore the `belief` field: it diagnoses a
   mistake, and E is not one.
d. `human_move_distribution(pos, moves_of_interest: [E, G])`: whether a player of the student's rank
   (`peer`) or a few stones above (`target`) finds E. Below 0.05 for `target`, teach what in the
   position points to E (the recognition cue), not only the move.

**Then the test the position calls for** (full tier; run every row that applies):

| Signal in the results above | Test | What it adds to the answer |
|---|---|---|
| A group changes status in (b), or a `defense.groups` / `left_behind` group of (c) falls below 0.3 | `local_solve` on that group, in the position where it is at stake | the sequence that kills or saves it, and its `caveats` |
| E is far from G (outside `local_radius`), or (b) differs mostly in another region | `pass_probe(pos, player, move: G, options: {rank_regions: true})` | the value of each area right now: why there and not here (urgent vs big) |
| Endgame (the survey's phases), or both moves gote and no group changes in (b) | `swing_value(pos, [G, E])` | each move counted as a swing, sente or gote |
| G's `reply.character` is `local_sharp`, or the student says how they read it | `expectation_probe(pos, G, options: {expected_line: their line})` | the move they did not consider, and its refutation |
| The difference in (b) is small or spread over the board (no region or group explains it) | `ownership_diff(after G, after E)` | where the points went; a low `explained_share` means tempo or thinness, not territory: say that |

## 3. When the evidence is thin

- `comparison.score_diff` under 2 points: there is no lesson in it. Say the two moves are close and what
  each aims at (step 2c), without a "why one is better".
- A line that depends on an opponent mistake (its `resistance` refutation shows the opponent does better):
  say so, or drop the line.
- A part of the answer template (step 4) with no evidence: write "not verified" for it, never fill it in.

## 4. The answer

In chat, in this order, one or two sentences each, every sequence on the page (step 5):

1. **What E does.** Its threat or its defense from (c), shown as a line ("E threatens F, which cuts off
   the three stones: after F and G, they have no second eye").
2. **What G did instead, and the answer.** G's purpose from (c), the opponent's best reply to it, and
   what G left behind.
3. **The difference at the ends.** From (b) and the extra test: which groups live or die, territory by
   region, who plays next freely and where. The score difference appears once, as the size, never as
   the reason.
4. **If the opponent resists.** The `resistance` move of E's forced line and its refutation; or "the
   reply is forced" when no resistance was found.
5. **The check at the board.** One sentence the student can run before playing, in the form of the
   belief table's rules (go-teaching §1); plus the cue from 2d when E is hard to find at their rank.

Words that must be cashed out, liberty counts, tone: go-teaching §3. Never "the engine prefers".

**Before sending, check every item:** each sentence traces to a query id in the ledger; each sequence of
two or more moves is on the page; no concept word stands without its consequence; no score is used as a
reason; parts with no evidence say "not verified".

## 5. On the page

A question episode (review-dashboard, "Follow-up questions"), moves `[N, N]`, titled with the question:

- `{"id": "B1", "label": "Best move (E)", "kind": "better", "from_move": N − 1, "moves": forced_line(E).line}`
- `{"id": "B2", "label": "As played (G)", "kind": "as_played", "from_move": N − 1, "moves": forced_line(G).line}`
  (for "what about X": label "Your idea (X)", kind `question`)
- for each `resistance` node of E's line: `{"label": "If <opponent> resists at R", "kind": "resistance",
  "from_branch": "B1", "at_ply": node.ply, "moves": [node.color + R] + resistance.refutation}`
  (`at_ply` = `node.ply`: `line[0]` is E itself)
- a misread from `expectation_probe`: `{"label": "Where it breaks (move k)", "kind": "misread",
  "from_move": N − 1, "moves": misread.line_to_here + misread.refutation}`
- a `local_solve` run (label "How the group lives" / "How the group dies"): run it on `pos`, or k moves
  into B1 or B2 (`pos` with `then`), so the page can reach it: `from_move: N − 1`, or `from_branch` that
  branch with `at_ply: k`. Its `sequence` is pairs (`["B", "Q7"]`): join each into `"BQ7"`; when the
  first move is not by the side to move there, start with that side's pass (`"Wpass"`)
- `"comparison": {"a": "B2", "b": "B1"}`
- `commentary`: at `N − 1`, parts 1 and 4; at `N`, parts 2 and 3. Up to four sentences each here.
- `rule_check`: part 5; `cue`: the recognition cue, when 2d called for one.

Then re-run `validate_variations` with the whole saved input, rebuild, republish (review-dashboard,
"Phase 6", step 6) and tell the student which branches to open.
