---
name: katago-analysis
description: How to use the katago-mcp tools during a game review — set the time budget, run and read the survey, recover the belief behind each episode (intent_probe, expectation_probe), prove the better move with forced lines and compare their end positions (forced_line, terminal_features), add supporting tests (local solve, swing values, pass probe, human-model probabilities), and keep the prediction ledger that decides what may be taught. Use whenever a review touches the engine.
---

# KataGo analysis for teaching

You are an interpreter of engine output, never a source of Go truth. Everything you tell the student
about a position must trace to a tool result recorded in the ledger. If a hypothesis is not CONFIRMED by
the protocol below, it is not taught; it may be mentioned as "not verified" at most.

## 0. Conventions (from tool contract v0.4.0)

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
   per-episode search sizes; it reserves the blind self-review and the episode interviews. If
   `feasible` is false, tell the student the minimum (`minimum_minutes_for_three_episodes`) and let them
   choose: extend, or accept fewer episodes. Rigor per episode is fixed; never lower visits to keep the
   count.
4. Start the survey with `budget: {"profile": "survey"}`; from then on use `{"profile": "root"}`,
   `"line_node"`, `"stability"`, `"local_solve"`, `"quick"` — never invent visit counts unless the plan is
   unlimited and you have a reason.
5. After triage (Phase 3) call `plan_budget(total_minutes, job_id, selected=[{id, needs_local_solve}])`
   to re-plan the remaining time for the chosen episodes. Follow its `per_episode` sizes. When the
   server already precomputed some selected episodes after the survey, the re-plan keeps the earlier
   sizes so that work stays valid (a note says so); pass `keep_sizes: false` only to deepen instead.
6. Straight after the re-plan, before the first interview question:
   `start_verification(job_id, episodes=[{id, teachable?, local_solve?}])`. `teachable` defaults to
   `teachable_move_preliminary`; pass the engine's best when learnability is very low. `local_solve` is
   `true` for the episodes the re-plan counted as needing one (the server picks the group from the
   belief), or `{group_point, at}` when you know the group. Interview in the returned `interview_order`.
   After each answer, `record_interview(job_id, episode, answer, expected_line, fix)`. Then read each
   episode with `verification_results(job_id, episode, wait_seconds=120)`.

A tool call blocks your turn: you cannot talk to the student while a probe runs. That is why the
probes go through `start_verification`. Calling them one by one after the interviews leaves the engine
idle while the student answers and the student idle while the engine searches. The plan already
assumes this overlap (`overlapped_with_interviews_minutes`).

Costs to keep in mind: one root search = 1 unit; the probes run at `line_node` visits — `intent_probe`
≈ 8 searches, `expectation_probe` ≈ 1–2 per imagined move plus a 4-move refutation, `forced_line` ≈ 2 per
node plus resistances and 2 for its end; `terminal_features` reuses the searches `forced_line` already
cached. `local_solve` = 2 playouts × up to 20 restricted searches; `pass_probe` with `rank_regions` = 9
extra restricted searches. `human_move_distribution` is nearly free. The budget also allows one line
per episode that the student proposes (`per_episode.student_lines`): their fix or a resistance line.

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
   `style_axis` (overplay/slack), `best_reply` (the opponent's best answer to the played move:
   `tenuki` = it did not need an answer, `local_sharp` = it started a fight or overplayed,
   `local_calm` = answered but small — the first hint at the belief),
   `human.played` / `human.best` for peer/target/horizon/opponent,
   `learnability` (target-rank probability of the teachable move), `candidate_tags` (taxonomy hints, not
   verdicts), `stability`, `got_away_with_it`, `group_status_change`, `persistent_best` (other episodes whose best
   point is the same: one big point left open — often one lesson, usually category 2 or 15).
5. `positives` — correct moves that the student's rank (peer) usually misses, only where the position
   offered a real choice (passes and dame are excluded). Mention one in the summary; the list can be empty.

The digest's tags and teachable move are *hypotheses*. Phase 4 decides.

## 3. The belief protocol (every episode you intend to teach)

After `start_verification` and `record_interview`, `verification_results` returns steps 1–4, the
stability searches, and a supporting test when the inferred belief names one (`is_sente`,
`biggest_move`: `swing_value`; `local_solve` when asked). Each result keeps its own `query_id` for the
ledger. Run the rest yourself, such as the test a *stated* belief needs. The same call returns at once
whenever the background already ran it (`precomputed`).

A mistake is a move that only makes sense if some belief about the position is true. For each
selected episode, from `position_ref_before`, with the played move G and the teachable move E
(`teachable_move_preliminary`, or the engine's best when learnability is very low):

1. **Intent.** `intent_probe(pos, G)` → `belief` (and `matches`), with its numbers: what G threatened
   (`threat.value`, a local swing), what it prevented (`defense`), how the opponent answered (`reply`),
   what it left behind (`left_behind`). A belief the student *stated* in `thinking.md` overrides the
   inferred one; record both when they differ — the contradiction is the lesson.
2. **Expectation.** `expectation_probe(pos, G, options.expected_line = the student's line from
   thinking.md, if any)` → `misread`: the first imagined move that loses more than 3, whose it is, the
   move `never_considered`, and its `refutation`. No misread means the reading held and the belief is
   about value (sente, size, safety), not reading.
3. **Proof lines.** `forced_line(pos, E)` and `forced_line(pos, G)` (and the student's fix, if any):
   the must-moves after each, the opponent's natural resistance with its refutation, and the end
   position's features.
4. **What differs at the ends.** `terminal_features({ref: end of G line}, compare_to: {ref: end of E
   line})` → `comparison`: groups whose status differs, territory by region, who holds sente, and the
   tempo price. This comparison is what the lesson teaches (the *why*: go-teaching §3).
5. **Supporting tests** when the belief needs them:

| Belief (go-teaching §1) / category | Supporting test | What it adds |
|---|---|---|
| `needs_defending` | `local_solve` on the defended group | the living sequence against the feared attack |
| `group_is_safe` | `local_solve` on the group left behind | the killing sequence; why there was no second eye |
| `is_sente` | `swing_value([follow-up, the opponent's tenuki point])` | the follow-up vs what they took, counted |
| `biggest_move` | `swing_value([G, E])`, or `pass_probe` with `rank_regions` | both moves counted as swings |
| `behind_must_invade` / `ahead_can_coast` | `human_move_distribution` (horizon), `local_solve` on an invading group | what stronger players choose; the invasion dies / the calm line still wins |
| `sequence_works` | `expectation_probe` is the test; `local_solve` when a group's life is at stake | the refutation and the tactic |
| Joseki (7) | `forced_line` from the first deviation | the standard line's end vs the played one |
| Ko (12) | `analyze_line` with the ko sequence forced | who has the threats |
| Failure to punish (13) | `expectation_probe` from the opponent's mistake | the punishment and whether a 4k finds it (`human_move_distribution`) |
| Thickness / aji (10) | `forced_line` on the cutting point | the sequence that uses the aji |

Write each hypothesis as a testable prediction with a number in it ("Q7 was a defence the groups did
not need: after a pass and P9 they stay above 0.7"). Human probabilities: `peer` = the student's rank
(how natural the played move was), `target` = 3 stones stronger (is the fix learnable now?), `horizon` =
1d, `opponent` = the opponent's rank (would they find the refutation?).

## 4. The proof tree

A human proof is a narrow tree of must-moves ending in a position the student can evaluate:

- `forced_line` extends while replies are forced (the second-best, re-searched with the best avoided,
  loses > 3) and, by default (`extend: "local"`), while the fight stays local; nodes marked
  `forced: false` were choices — say "Black would play", not "Black must play". It prefers the move the target rank
  plays when it is within a point of the engine's, so the line stays legible.
- `resistance` on an opponent node is the move a player of the opponent's rank would most likely try,
  with its refutation — the answer to "what if he doesn't cooperate?". Show it when it differs from the
  forced move.
- The end of each line has `end` features; the lesson teaches the comparison of the two ends
  (go-teaching §3, part 5).
- A contrast smaller than 2 points (`comparison.score_diff`) is not a lesson. A teachable line must not
  depend on an opponent mistake: if its `resistance` refutation shows the opponent does better, say so
  or drop it.
- The student's expected line (`expectation_probe`) and their fix from the self-review are branches of
  the same tree; record them all in the ledger.

`analyze_line` stays available for arbitrary sequences (a ko fight, a line the student proposes in
conversation) and for `refutation_probability`. Show any line you discuss on the review page as a board
(`dashboard_row`, go-teaching §6); after the dashboard is published, add it to the page as a validated
branch (review-dashboard, "Follow-up questions").

## 5. Stability protocol

Before a CONFIRMED verdict, re-run the decisive searches at the plan's `stability` budget
(`analyze_position(pos, {"profile": "stability"})`; when the plan has two multipliers, the second one
is `{"profile": "stability", "multiplier": 16}`): the episode root, and the node where
`expectation_probe` found the misread. A hypothesis is stable when the top move is unchanged and the
score moved < 0.5. If it flips, either lower the claim ("the engine is divided") or drop it. Episodes
flagged `unstable` in the digest need this before anything else. `verification_results` reports the
check as `stability_check` (`runs[].stable`, `misread_node.stable`); cite its query ids.

## 6. The ledger (`ledger.md`)

One row per hypothesis, kept up to date during Phase 4:

```
| id | episode | belief (source) | hypothesis (with a number) | test | result (query_id) | misread / end diff | verdict | teach? |
| H1 | E1 | needs_defending (stated: "C6 looked cuttable") | the connection was unnecessary: after a pass and the cut the group stays > 0.7 | intent_probe + local_solve | 0.91 after W C6 (q_…0042); lives with D7, B5 (q_…0043) | end diff: R10 corner 20 vs 6 (q_…0047) | CONFIRMED | yes |
| H2 | E1 | — | student's fix D6 also saves the tempo | forced_line D6 | group unsettled at the end (q_…0046) | — | REFUTED | mention |
| H3 | E2 | sequence_works (inferred) | the peer line breaks at W's 2nd move | expectation_probe | misread ply 2: expected D12, never considered G17 (q_…0051) | end diff: F12 group 0.52 vs 0.83 (q_…0054) | CONFIRMED | yes |
| H4 | E3 | biggest_move (inferred) | E is bigger by ≥ 2 | swing_value | 1.1 (q_…0060) | — | WEAK | no |
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
- Sealed results (go-teaching §6): no verification queries while the student is still writing the
  blind self-review; nothing about an episode is shown before its interview answer is saved. From
  `start_verification` on, the server enforces the second rule: probes at a selected episode's
  positions return `sealed` until `record_interview`.
