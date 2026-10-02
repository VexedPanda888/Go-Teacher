# Tool contract — decisions and version history

Moved out of `skills/go-teacher-flow/references/tool-contract.md` (which describes only the current
contract) so the skill package stays small. Newest changes last.

## Decisions taken (review of v0.1) and changes in v0.2

| # | Question | Decision |
|---|----------|----------|
| 1 | Ladder order | Kept: root_3000 → line_600 → stability_16x → plies_8 (was plies_12 before v0.3) → solve_4000_all_ld → root_6000 → line_1000 |
| 2 | Survey cap | Raised to **1000** visits/move |
| 3 | Tag rules | Kept as hints; WS8 seeding calibrates them |
| 4 | `pass_probe.rank_regions` | Off by default; the urgent-vs-big and slow recipes turn it on |
| 5 | Acceptable margin | 1.0; WS8 calibrates |

Changes from v0.1 to v0.2 (all reflected in the sections above):

1. Survey cap 1000 (§1.2, §6).
2. `pass_probe.urgency` reports the player's best move per region and its value over passing (`best_move_there`, `value`) instead of the opponent's threat (§1.10).
3. `analyze_line.summary.vs_best` is a root estimate with a note; the played-out contrast is a second call (§1.9).
4. Interactive queries run at higher KataGo priority than the survey (§1.4).
5. Phases may be `null` when the game ends early (§3.4).
6. Chains are capped at 24 plies / 6 student moves; the same-region rule needs ≤ 6 plies (§3.3).
7. Quiz candidates carry `labels`; the actual move's loss uses the survey definition (§1.17).
8. `Budget.seconds` requires a measured throughput, otherwise `budget_infeasible` (§0.4).
9. Legality inside the server checks positional superko for all superko rulesets; KataGo remains the arbiter inside searches (§0.1).
10. The survey persists `analysis.json` before the job reports `done`; a restarted server reuses a finished survey from disk (`reuse_existing`).

Changes from v0.2 to v0.3 (causal evidence; the archived plan is `docs/archive/plan-causal-lessons.md`):

1. Liberty counts leave the default outputs: `Group.liberties` is optional (capture races, explicit requests), `sgf_summary.tension_events` and `local_solve.target` drop the count, `render_board.label_low_liberties` defaults to false; new `capture_races` (§3.16) on `analyze_position`, `analyze_line.end` and `group_status`.
2. Tag 2 fires on an unsettled group in the best move's region instead of a group with ≤ 3 liberties (§3.7).
3. Digest episodes carry `best_reply` (§3.15).
4. New `terminal_features` (§1.18) and `forced_line` (§1.19); thresholds `forced_margin`, `human_margin`, `territory_diff_min`, `group_change_min`.
5. New `intent_probe` (§1.20) and `expectation_probe` (§1.21) with their thresholds.
6. `plan_budget`: the per-episode unit counts the probes instead of three lines (≈ 2.3× the old base unit); the plies step of the ladder is 6 → 8; the blind self-review (5) and the episode interviews (5) are reserved separately, and the survey is sized by `survey_minutes_target` (10) so the shorter blind review does not cut its visits.
7. `validate_variations`: branches off branches (`from_branch`, `at_ply`), branch `kind`, a server-computed end comparison of two branches, `rule_check` and `belief` (§1.17); the dashboard shows the comparison table and the check.

Changes from v0.3.0 to v0.3.1 (every line visible):

1. `render_board.line`: a numbered diagram of a sequence from the position, with `notes` and `end_ref`, no engine call (§1.16, §4.1).
2. `validate_variations`: episode `kind` (`"lesson"` default, `"question"` for a follow-up asked after the lessons; error `bad_kind`) and branch kind `"question"`; the dashboard lists questions apart from the lessons (§1.17).

Changes from v0.3.1 to v0.4.0 (verification during the interviews):

1. New `start_verification` (§1.22), `record_interview` (§1.23) and `verification_results` (§1.24): the answer-free probes of the selected episodes run in the background during the interviews; each episode is sealed until its interview is recorded, then its answer work runs ahead of the rest. Errors `sealed`, `verification_not_started`, `episode_not_found`, `episode_not_selected`.
2. Stored results (§0.8): the probe tools keep their whole result; an identical call returns it with `precomputed`, or waits for it while it is being computed. Sealing (§0.8) is enforced by the server.
3. A finished survey with a plan precomputes the answer-free probes of its top episodes (§1.4, `[verification].speculative_episodes`).
4. `plan_budget` (§1.2): the interviews overlap the engine (`overlap`, `overlapped_with_interviews_minutes`, `engine_minutes`); a student-line allowance `L` (`student_lines_per_episode`, `per_episode.student_lines`); a re-plan keeps the earlier sizes when background results exist (`keep_sizes`). The M5 worked example climbs one ladder step further (root 6000).
5. Human-policy queries carry the caller's KataGo priority (they were sent at 0, behind the survey).

Changes from v0.4.0 to v0.5.0 (the review page grows with the review):

1. New `dashboard_row` (§1.25): checksummed rows for the live review page (the game record; boards with a line, marked points, a question and an answer clicked on the board). The page is published in Phase 0 with the `db` capability and rebuilt in place in Phase 6; it shows no engine data before the lessons.
2. `render_board` is the chat fallback for positions and lines when the page cannot be used (§1.16).

Changes from v0.5.0 to v0.5.1 (fixes):

1. A `PositionRef` names its game (§0): the same position in two games has two refs, so each resolves to its own game's student colour, plan and log. A position given as `{sgf, move_number}` now belongs to that SGF's game.
2. SGF input: setup stones in nodes before the first move join the setup; stones added or removed after a move are refused (`invalid_sgf`), not dropped; compressed point lists (`aa:cc`) and results in any case (`b+r`) are read.
3. A KataGo warning about a query field is logged and the query waits for the real result (it was taken as the result: 0 visits, no candidates); a query terminated before it was searched is an error.

Changes from v0.5.1 to v0.5.2 (the rest of the WS8 calibration, measured on the saved seed surveys):

1. Episodes are ranked by the root's `points_lost`, not the chain sum (§3.4); `game_type` takes the largest chain.
2. Positives exclude passes and positions without a real choice, `positive_min_choice` (§3.10b): 13 of 24 games had a pass among them, mostly first.
3. Tag 14's thresholds are config (`tag_passive_own_up`, `tag_passive_opp_down`, 0.5 each; the old 1 / 3 never fired), and the survey requests `ownership_stdev`, without which tag 10 could not fire (§3.7).
4. `last_chance` is documented as the code computes it: at or before `decisive` (§3.6).
