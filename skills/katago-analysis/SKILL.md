---
name: katago-analysis
description: How to use the katago-mcp tools during a game review — set the time budget, run and read the survey, form hypotheses about each episode, verify them with the right tool recipe (forced lines, pass probe, swing values, local solve, ownership diff, human-model probabilities), and keep the prediction ledger that decides what may be taught. Use whenever a review touches the engine.
---

# KataGo analysis for teaching

You are an interpreter of engine output, never a source of Go truth. Everything you tell the student
about a position must trace to a tool result recorded in the ledger. If a hypothesis is not CONFIRMED by
the protocol below, it is not taught; it may be mentioned as "not verified" at most.

## 0. Conventions (from tool contract v0.2.1)

- Coordinates are GTP: columns A–T without I, rows 1–19. Never SGF letters. Copy points from tool output.
- Scores are **points** from the stated `perspective` (default: the student's colour). Winrate is never
  the teaching quantity; in handicap games it is uninformative.
- Ownership is Black-positive in raw arrays; tool summaries already flip to the perspective.
- A `position_ref` (`pos_…`) is the only reliable way to refer to a position across calls. Get refs from
  `job_results` episodes (`position_ref_before` / `position_ref_after`), `get_position_ref`, or any tool result.
- `then: [["B","Q7"],["W","R8"]]` on a position walks moves from that ref; illegal moves are rejected with
  the offending ply.
- Every result carries a `query_id` (`q_<game>_<seq>`). Cite it in the ledger for each verdict.

## 1. Budget first

At the start of a review, ask for the total time unless already given ("as long as it needs" is allowed).
Then:

1. `engine_info` — confirm the machine, `throughput.visits_per_second_sustained > 0` (else
   `refresh_benchmark: true`), human model loaded.
2. `sgf_summary` — confirm the student's colour, rules, komi, handicap, result. Read `warnings`.
   Pass the game as the OGS link or game id (the server fetches and caches the SGF) or as the file
   name of an `.sgf` in the server's `games/` folder. Never retype SGF text into a tool call: long
   records get mangled. Use the same `sgf` value for `start_game_analysis`.
3. `plan_budget(total_minutes, move_count)` — returns survey visits, how many episodes fit, and the
   per-episode search sizes. If `feasible` is false, tell the student the minimum
   (`minimum_minutes_for_three_episodes`) and let them choose: extend, or accept fewer episodes.
4. Start the survey with `budget: {"profile": "survey"}`; from then on use `{"profile": "root"}`,
   `"line_node"`, `"stability"`, `"local_solve"`, `"quick"` — never invent visit counts unless the plan is
   unlimited and you have a reason.
5. After triage (Phase 3) call `plan_budget(total_minutes, job_id, selected=[{id, needs_local_solve}])`
   to re-plan the remaining time for the chosen episodes. Follow its `per_episode` sizes.

Costs to keep in mind: one root search = 1 unit; each `analyze_line` node = one line_node search;
`local_solve` = 2 playouts × up to 20 restricted searches (cheap per node but many nodes);
`pass_probe` with `rank_regions` = 9 extra restricted searches. `human_move_distribution` is nearly free.

## 2. Reading the survey digest

`job_results(job_id)` (≤ 4k tokens). Use it in this order:

1. `game.reconciliation` — if `mismatch`, stop and check komi/rules/handicap before anything else
   (go-teacher-flow, "When things go wrong", has the diagnosis when they are already correct).
2. `reliability.low_visit_positions`, `unstable_episodes` — treat those episodes as needing the
   stability protocol before any claim.
3. `game_type` and `decisive` / `last_chance` — the shape of the story (single blunder vs accumulation;
   where the game was decided; the last recoverable moment, which is the best teaching moment in a loss).
4. `episodes[]` sorted by points lost. `points_lost_total` sums every seed in the chain and can be
   several times the real damage when both sides keep swinging the same group; judge cost by
   `root.points_lost` and the net score change over the chain. For each: `root` (played/best/points lost), `region`, `phase`,
   `game_state_before`, `acceptable_set` (was the played move within 1 point of best?), `signature`
   (prior_played vs prior_best, local_loss_share, score_stdev played vs best, ko_present),
   `style_axis` (overplay/slack), `human.played` / `human.best` for peer/target/horizon/opponent,
   `learnability` (target-rank probability of the teachable move), `candidate_tags` (taxonomy hints, not
   verdicts), `stability`, `got_away_with_it`, `group_status_change`, `persistent_best` (other episodes whose best
   point is the same: one big point left open — often one lesson, usually category 2 or 15).
5. `positives` — correct moves that a 7k usually misses. Mention one in the summary; skip passes and
   dame-like first/last-line moves, which the list still contains.

The digest's tags and teachable move are *hypotheses*. Phase 4 decides.

## 3. Hypothesis → recipe

Write each hypothesis as a testable prediction with a number in it. Then run the recipe.

| Hypothesis (taxonomy #) | Prediction to write | Recipe |
|---|---|---|
| Direction / whole-board (1) | "The loss is mostly outside the local area" | `ownership_diff(before, after)` → `local_vs_global.classification` is `global` or `mixed`; `analyze_line` on the best move shows the gain elsewhere |
| Urgent vs big (2) | "Playing elsewhere costs ≥ X points here" | `pass_probe(position, player, move, options.rank_regions=true)`: `local_value.best` vs `urgency[]` ranking |
| Own L&D (3) / attack L&D (4) | "The group is dead/alive/unsettled after best play" | `local_solve(position, group_point)` at `local_solve` budget; teach only when `confidence` is high or medium |
| Reading / tactical (5) | "The played move fails to Wx; the correct move works" | Three-line contrast (§4) with `follow_pv_plies` from the plan |
| Shape (6) | "The engine's move is adjacent and the human target rank plays it" | `human_move_distribution` on peer/target/horizon; `analyze_position` acceptable set |
| Joseki (7) | "The corner sequence loses ≥ 2 points vs standard" | Three-line contrast from the first deviation |
| Invasion / reduction (8) | "The invasion dies / the reduction was enough" | `local_solve` for the invading group, `pass_probe` for the reduction |
| Choice of fight (9) | "The played line has much higher variance for little gain" | Compare `score_stdev` played vs best in `analyze_position`; `analyze_line` both |
| Thickness / aji (10) | "The best move removes aji the engine sees" | `analyze_position` with `include_ownership_stdev`; `ownership_diff` before/after best |
| Endgame value (11) | "Point A is worth more than point B by ≥ 1.5" | `swing_value(position, [A, B, …])` |
| Ko (12) | "Ko threats decide the local result" | `analyze_line` with the ko sequence forced; check `ko_present` |
| Failure to punish (13) | "Opponent's move n−1 lost ≥ 5 (the server's `tag_punish_min_loss`) and the refutation is playable by a 4k" | `analyze_position` at the position before the student's move; `human_move_distribution` on the refutation |
| Passive (14) | "The defensive move protected less than the attacking move gained" | `analyze_line` both; `ownership_diff` per group |
| Slow (15) | "Best move is far away and bigger by ≥ 2" | `pass_probe` with `rank_regions`, `swing_value` |

Human probabilities: `peer` = student's rank (how natural the played move was), `target` = 3 stones
stronger (is the fix learnable now?), `horizon` = 1d (a stretch goal), `opponent` = the opponent's rank
(would the refutation have been found? `analyze_line.refutation_probability`).

## 4. The three-line contrast

For any episode you intend to teach, run from `position_ref_before` at the plan's `line_node` budget and
`follow_pv_plies`:

1. **As played**: `analyze_line(pos, [{"color": student, "move": played}])`.
2. **Teachable move**: the acceptable-set move with the highest `target` probability
   (`teachable_move_preliminary`); `analyze_line(pos, [{"color": student, "move": teachable}])`.
3. **Student's fix** (from `self_review.md`) if it differs: `analyze_line` on it.

Read `summary.score_end` of each; the contrast must be ≥ 2 points to be teachable and the teachable line
must not depend on an opponent mistake (`refutation_probability.product` for the opponent profile ≥ 0.3
means the opponent would likely find the punishment; if the *teachable* line only works because the
opponent misses something, say so or drop it). Record all three in the ledger.

## 5. Stability protocol

Before a CONFIRMED verdict, re-run the decisive search at the plan's `stability` budget
(`analyze_position(pos, {"profile": "stability"})`; when the plan has two multipliers, the second one
is `{"profile": "stability", "multiplier": 16}`). A hypothesis is stable when the top move is unchanged
and the score moved < 0.5. If it flips, either lower the claim ("the engine is divided") or drop it.
Episodes flagged `unstable` in the digest need this before anything else.

## 6. The ledger (`ledger.md`)

One row per hypothesis, kept up to date during Phase 4:

```
| id | episode | hypothesis (with a number) | test | result (query_id) | verdict | teach? |
| H1 | E1 | Q7 loses ≥ 5 because the R8 group dies | local_solve R8 + 3-line | dead (q_ogs_1_0042), as played −6.1 vs R8 +0.3 (q_…0044, q_…0045) | CONFIRMED | yes |
| H2 | E1 | Student's P8 also saves it | analyze_line P8 | −4.4, group still unsettled (q_…0046) | REFUTED | mention |
| H3 | E2 | Move 122 was slow: the best move is in the lower right | pass_probe rank_regions | LR best K4 value 4.8 vs played 1.1 (q_…0051) | CONFIRMED | yes |
| H4 | E3 | Joseki deviation at move 14 | 3-line | contrast 0.9 (< 2) (q_…0060) | WEAK | no |
```

Verdicts: CONFIRMED (prediction met and stable), REFUTED, WEAK (effect below the threshold or engine
divided), UNTESTED. Only CONFIRMED rows become lessons; REFUTED rows about the student's own fix are
valuable feedback and go in the self-review comparison.

## 7. Handicap games

Use score, never winrate. Distinguish the objective verdict (points) from the practical one: as the
weaker player with stones, simplicity has value — a move that loses 2 points but removes all fighting
may be the right practical choice; say both. `decisive` in the digest already uses the score basis.

## 8. Errors and edge cases

- `engine_busy`: a survey is running; wait for `job_status` or cancel.
- `budget_infeasible`: throughput unknown → `engine_info(refresh_benchmark=true)`.
- `illegal_move` / `wrong_color`: fix the move list; never guess a coordinate.
- Resigned games end at the resignation; do not analyse "what would have happened after".
- Do not run verification queries while the student is still writing the blind self-review — the
  survey result is sealed until `self_review.md` exists.
