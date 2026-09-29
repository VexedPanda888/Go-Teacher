# Causal lessons — plan (v0.1)

**Status:** planned, 29 Sep 2026. Nothing built yet. Decisions in §0.3.
**Goal:** make reviews explain *mechanism* instead of grading moves. Today's lessons say where the
points went ("the engine ranks the right side last", "misses the key point", "does much less for
you" — `reviews/ogs_91122267/export-1.json`) because the tools only return evaluative evidence
(score deltas, ownership diffs) plus liberty counts, which Claude anchors on. The fix is causal
evidence produced by the server, and skills that narrate it in a fixed template.

## 0. Frame

### 0.1 The reframe
A mistake is a move that only makes sense if some belief about the position is true.
**Diagnosis** = recover the belief. **Proof** = refute it with a verified sequence, then show what is
concretely different at the end of the better line.

### 0.2 Beliefs (the primary diagnosis; the 15 categories stay as labels)

| Belief | Probe signature | What to show | Rule (a check at the board) | Categories |
|---|---|---|---|---|
| "This group needs defending" | Player passes, opponent's strongest local attack, group ownership still > 0.7 | The living sequence, then what the tempo was worth | Name the attack you fear and read your answer; if you have one, defending is worth zero | 14, 2 |
| "This move is sente" | Best reply is tenuki; v_F < value of the tenuki | Your small follow-up vs what they got elsewhere | Forcing means your follow-up is bigger than the biggest move on the board | 11, 15 |
| "This cut/attack works" | Human-rank line diverges from engine at node k | The refutation at k | The specific tactic (shortage of liberties, counter-atari, ladder) | 5, 4, 9 |
| "That group is safe" (tenuki) | Best reply is a local attack; group ownership drops below 0.3 | The sealing/killing sequence and why there was no second eye | Count eyes and base before leaving | 3, 2 |
| "This is the biggest move" | G and E both gote, no status change, E larger | Side-by-side territory after each | Value a move by the swing, not your side alone | 1, 15, 11 |
| "Behind, must invade" / "ahead, can coast" | Score lead at P contradicts the move's risk; stronger profiles disagree | The calm line still wins / the invasion dies | Count before choosing risk | 9, 8 |

Thresholds (0.7 / 0.3, 3 points) go in `[thresholds]` of `config/*.toml` and are calibrated (§5).

### 0.3 Decisions
1. **Budget:** rigor per episode is fixed; at short review times fewer episodes are verified (2 instead
   of 3). Visits per node are not lowered to keep the count.
2. **Self-review:** the blind self-review shrinks to the four mandatory questions (Q1, Q2, one of Q3–Q5,
   Q8); the freed time goes to per-episode interviews after triage.
3. **Taxonomy:** the 15 categories stay as secondary labels so memory stays continuous; `belief` is
   added alongside them.

## 1. Server (katago-mcp 0.2.0, contract v0.3.0)

**A1. Liberty audit, digest reply.** Drop `liberties` from `analyze_position.groups[]`, `group_status`
and `sgf_summary.tension_events`; `render_board.label_low_liberties` defaults to false. Liberties
appear only when a capturing race is detected (two adjacent opposing groups, both unsettled, each ≤ 4
liberties) and inside refutations (A5). Tag 2's "≤ 3 liberties" becomes "an unsettled group in the
region" (§3.7). Add to each digest episode `best_reply: { move, local, score_stdev }`, taken from the
survey's search at `P_n` (free).

**A2. `terminal_features(position, compare_to?)`.** Per position: status of every group ≥ 2 stones
(label, ownership, status); territory by standard region per side; weak (unsettled) groups per side;
sente holder (side to move, and whether its best move is non-local); **tempo price** = score if the
side to move plays best − score if it passes. `compare_to` returns the diff: groups whose status differs,
regions differing by ≥ 2 points, sente holder change, tempo difference. This replaces `ownership_diff`
as the source of the lesson's "why".

**A3. `forced_line(position, move, options: { max_plies = 8, forced_margin = 3, human_margin = 1 })`.**
From the position after `move`, extend while the reply is forced: re-search the second-best with
`avoidMoves` on the top move so it has real visits; forced if it loses > `forced_margin`. Stop at the
first non-forced node or `max_plies`. Prefer the `target`-profile move when it is within `human_margin`
of the engine's best (legible lines, no probes). At each opponent node include the `opponent`-profile
top move if it differs ("natural resistance") and its short refutation. Returns the tree and
`terminal_features` at each endpoint. Run from E and from G; compare the endpoints.
*As built:* on real games a strictly forced line often stops after one move (in fights the second-best
reply is usually within 1–3 points), before any group's fate shows. `extend: "local"` (the default) also
follows non-forced best moves while they stay local, marked `forced: false`, until the best move is
elsewhere. On ogs_91122267 move 114 this turns "D11 misses the key point" into "after D11 D10 your F12
group is unsettled (0.52); after E10 it is alive (0.83) and Black's F10 stones fall to 0.37".

**A4. `intent_probe(position, move)`** (~5 searches). Threat value: after G the opponent passes, the
player's best follow-up F and v_F. Defensive value: the player passes, the opponent is restricted with
`allowMoves` to G's neighbourhood; best local D and v_D. Reply character: best reply to G is tenuki /
local-calm / local-sharp (score or ownership stdev). Returns `belief: { id, evidence }` or null by the
§0.2 signatures, computed deterministically.

*As built (A4):* the threat is measured as a local swing — X's best follow-up confined to G's
neighbourhood after the opponent passes, against the opponent's best local answer — because an
unrestricted follow-up is just "a second move in a row" (move 96 of ogs_91122267 reported O3, far from
E3). `is_sente` needs `tenuki_value ≥ 0.5`. "This cut/attack works" became `sequence_works` ("this cut /
attack / save works"): on move 114 (D11) the belief is that D11 saves the group, not an attack. On
ogs_91122267 the probes give: move 44 Q7 `needs_defending` (both groups stay at 0.96 after P9) with the
imagined line S11 R10 S10 R9 breaking at R9 and O3 never considered; move 96 E3 `is_sente` (threatened
F2, worth 10.9; Black gains 1.3 more with S4); move 114 D11 `sequence_works`; move 32 S14 no belief
(its lesson is the misread: Black answers P9, not Q7).

**A5. `expectation_probe(position, move, options: { expected_line?, plies = 6, misread_margin = 3 })`.**
Play `peer`-profile moves for both sides (the student's `expected_line` from the interview first, when
given), verify every node with the strong net. The first node losing > `misread_margin` for either side
is the misread; report it, the engine's move there, and a short refutation line (with liberties when a
capture race is involved).

**A6. `plan_budget`.** *As built:* the unit is `root·(1 + Σstability) + (6p + 39)·line_node`, 23,750
visits at base (2.3× the old 10,250). The Pro still verifies five episodes in 40 minutes; the Air needs
about 64 minutes for three (was 36) and gets two in 60. Per-episode unit = intent probe + expectation probe + two forced lines with
second-best re-searches + resistance branches (estimate ~2× today's unit; measure). Rigor floor fixed;
`n` drops when the time does not fit (decision 1). Self-review split into `blind_minutes` (default 5)
and `interview_minutes` (default 5). The survey is sized by a separate `survey_minutes_target` (default
10) so its visits do not halve with the shorter blind review; if the survey is still running when the
four blind questions are answered, Claude asks optional bank questions (Q6, Q7, Q9, Q10) until it
finishes.

**A7. Tests and docs.** Mock tests per new tool (`tests/test_tools_mock.py`); update both copies of the
contract together (`tests/test_docs.py`); bump versions in the header.

## 2. Skills

**go-teaching.**
- §1: the belief table (§0.2) leads diagnosis; the category table stays as labels.
- §2 triage: add a belief-confidence factor (stated and confirmed > inferred and confirmed).
- §3 lesson template, fixed: (1) what you were trying to do; (2) what you expected and the exact move
  where reality diverges; (3) the better move's forced line and the terminal comparison; (4) the rule as
  a check you can run at the board; (5) optional "why not X?" for the other human-rank candidate.
- Concept-word rule: *direction, thickness, aji, shape, slow, key point, urgent, big, does less* must be
  cashed out immediately as a consequence in a verified sequence, or cut. A score delta is never the
  "why". Liberties only inside a refutation.
- Calibration example (before/after): "Move 87 is slow; the direction of play favors the right side"
  → "You connected because C6 looked cuttable. If you tenuki and White cuts, D7 atari, White extends,
  B5 makes the second eye and the cutting stones are captured. The cut was a gift, not a threat.
  Meanwhile R10 was worth 14 points: 20 for you if you play it first, 6 if White does." Add one rewritten
  example from a past review.
- §4: blind self-review = four mandatory questions; new per-episode interview (below).

**Per-episode interview (new Phase 3b → `thinking.md`).** After triage, for each selected episode show
the position with `render_board` (no overlay, no engine) and ask: "What was move N for, and what did you
expect next?" Save verbatim. A stated belief overrides the inferred one; a stated line goes into
`expectation_probe`; a probe that contradicts the stated belief is the lesson. Results for an episode
stay sealed until its answer is saved.

**katago-analysis.** §3 recipes become belief → probes: `intent_probe` → `expectation_probe` →
`forced_line` from E and G → `terminal_features` compare; `local_solve` and `swing_value` as support.
§4 three-line contrast becomes the proof tree (2-point threshold kept; the diff is what is taught).
§5 stability also covers the misread node and forced-line roots. §6 ledger adds columns: belief,
belief source, misread node, terminal diff.

**go-teacher-flow.** Phase 2 shortened; Phase 3b added; `verified.md` records belief, misread node,
forced lines, terminal diff; concept-word and numbers rules added to "Rules that do not bend".

**review-dashboard (+ validator, template).** Branch labels: "As played", "What you expected", "Where it
breaks (move k)", "Better: forced line", "If White resists at X". Resistance branches start inside
another branch → `validate_variations` and the template need branch-from-branch support. A comparison
panel shows the server-computed terminal diff. `principle` + `cue` become `rule_check`.

## 3. Memory and cross-game belief clustering

- `episodes` gain `belief`, `belief_source` (stated / inferred), `divergence_move`, `rule_check`;
  `category` stays.
- `profile` gains `beliefs` (counts over the last 10 games, same recency weighting) and one insight
  sentence ("three of your five biggest losses were defences of groups that were already alive").
- `katago-mcp-seed --probes`: `intent_probe` at quick budget on the top 5 episodes per game, stored as
  SURVEY grade (half weight, as today), so the belief insight exists after one batch upload.

## 4. Order of work

1. A1 — liberty audit, `best_reply` in the digest.
2. Skills, text only — lesson template, concept-word rule, shortened blind review, interview (works with
   today's tools).
3. A2 + A3 — terminal features, forced lines.
4. A4 + A5 — intent and expectation probes; A6 budget.
5. Skills, remainder — recipes, ledger, flow.
6. Dashboard — branch-from-branch, comparison panel.
7. Memory beliefs, seed `--probes`.

## 5. Calibration and acceptance

Fixed test set of ~8 episodes from reviews on disk (ogs_91122267 moves 44–50 and 100–103, ogs_90613406
moves 166–176, others); today's lesson text is the "before". Pass when: every "why" sentence cites a
verified sequence or terminal diff; no uncashed concept words; inferred beliefs match the student's on
≥ 6 of 8; the student prefers the new lessons blind. Tune the §0.2 thresholds on the seed games as the
tags were. Re-run after steps 3, 4 and 5.
