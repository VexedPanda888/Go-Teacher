---
name: go-teaching
description: The teaching method for the Go-teacher project — diagnosis by belief (a mistake is a move that only makes sense if some belief about the position is true), the six beliefs with their proofs and board checks, the 15-category taxonomy kept as labels, triage (costly × learnable × recurring, blind spots first), the fixed lesson template and the concept-word rule, the short blind self-review and the per-episode interview, memory use (recurrence, not progress), and rules of conduct for teaching a 6–7 kyu player with engine evidence. Use in every review from Phase 0 onward, alongside katago-analysis.
---

# Teaching Go with an engine behind you

The student is 6–7 kyu (OGS), plays Japanese rules, often with handicap. They want two or three lessons
per game that they can actually apply, backed by evidence they can inspect on the dashboard, and they
want recurring weaknesses named across games. They track their own progress elsewhere; memory is for
recurrence only.

**The frame.** A mistake is a move that only makes sense if some belief about the position is true.
*Diagnosis* = recover the belief. *Proof* = refute it with a verified sequence, then show what is
concretely different at the end of the better line. A score delta says where the points went, never
why; it is never the explanation.

## 1. Beliefs (the diagnosis) and categories (the labels)

Every move is a threat, a defense, or a value claim. The probes in katago-analysis §3 tell which, and
whether it worked: `intent_probe` returns the belief id below with its evidence, `expectation_probe`
finds the misread, `forced_line` + `terminal_features` supply "what to show". Each belief below has its own proof and its own rule, stated as a check the student
can run at the board.

| Belief (`belief` id) | Probe signature | What to show | Rule — a check at the board | Categories |
|---|---|---|---|---|
| "This group needs defending" (`needs_defending`) | You pass, the opponent plays their strongest local attack, and the group is still owned > 0.7 | The living sequence against that attack, then what the tempo was worth elsewhere | Name the attack you fear and read your answer; if you have one, defending is worth zero | 14, 2 |
| "This move is sente" (`is_sente`) | The best reply is tenuki; your follow-up is worth less than what they took | Your small follow-up vs what they got elsewhere | Forcing means your follow-up is bigger than the biggest move on the board | 11, 15 |
| "This cut / attack / save works" (`sequence_works`) | The reply is local and sharp; the line a player of your rank reads diverges from the engine at move k | The refutation at k — the move you never considered | The specific tactic: shortage of liberties, counter-atari, ladder, net, the vital point | 5, 4, 3, 9 |
| "That group is safe" (`group_is_safe`, you tenuki'd) | The best reply is a local attack; the group's ownership drops below 0.3 | The sealing or killing sequence and why there was no second eye | Count eyes and base before leaving | 3, 2 |
| "This is the biggest move" (`biggest_move`) | Your move and the better one are both gote, no group changes status, the better one is simply larger | Side-by-side territory after each | Value a move by the swing, not by your side alone | 1, 15, 11 |
| "I'm behind, must invade" / "ahead, can coast" (`behind_must_invade` / `ahead_can_coast`) | The score at the position contradicts the move's risk; stronger players' choices disagree | The calm line still wins / the invasion dies | Count before choosing risk | 9, 8 |

The **15 categories** stay as the labels memory counts; every episode carries one category *and*, when
the probes find one, a belief.

| # | Category | Typical signature (hints, not proof) | What to teach |
|---|----------|-------------------|---------------|
| 1 | Direction of play | best move far from played; `local_loss_share` low (`global`) | Ask "where is the biggest open area / which group needs help" before playing locally |
| 2 | Urgent vs big | best move in another region where an unsettled group sits; `pass_probe` value there large | "Urgent before big": settle or attack weak groups first |
| 3 | Own life and death | own group's ownership fell; `local_solve` says dead/unsettled | Eye space and base before tenuki; the vital point |
| 4 | Attack / killing | opponent's group survived when it shouldn't; best move attacks | Attack from the outside, take the base, don't touch weak stones |
| 5 | Tactical reading | a move the student's rank plays often (peer ≥ 20 %) refuted by search | Read the opponent's best reply before playing; ladders, nets, snapbacks |
| 6 | Shape | best move adjacent (≤ 2) to played; target rank plays it | The shape the rank above plays, shown by what goes wrong without it |
| 7 | Joseki | opening corner deviation with a ≥ 2-point contrast | The one joseki line the student keeps deviating from |
| 8 | Invasion / reduction | invasion died or reduction insufficient | Where a framework can still be invaded; the shoulder hit as reduction |
| 9 | Choice of fight | played line high `score_stdev` for little gain; overplay axis | Simplify when ahead, complicate when behind |
| 10 | Thickness / aji | best move removes or exploits aji (`ownership_stdev` high there) | The cutting point and the sequence that uses it |
| 11 | Endgame value | `swing_value` ranks the played point below others | Count the swing; sente before gote |
| 12 | Ko | ko present and mishandled | Count ko threats before starting |
| 13 | Failure to punish | opponent lost ≥ 5 the move before and the student gave most of it back (server rule: `tag_punish_min_loss`, `got_away_ratio`) | Look for the punishment when the opponent plays away |
| 14 | Passive | defensive move protected less than the attack gained | Defend by attacking; check whether the group actually needed help |
| 15 | Slow | best move far away and bigger; played move locally fine but small | Ask "what is my biggest move" before "what is my safest move" |

Orthogonal fields on every episode: **style axis** (overplay / slack / neutral from the digest),
**awareness** (`blind_spot` / `seen` / `diagnosed`, from the self-review comparison in §4.1), **game state**
(ahead / close / behind when the mistake happened), **belief source** (stated by the student in the
interview, or inferred from the probes).

## 2. Triage: which 2–3 episodes become lessons

Two stages. **Selection** (Phase 3, before verification) ranks the digest's candidate episodes and
picks 3–5 to verify:

    selection = cost × learnability × recurrence × awareness

**Lesson choice** (Phase 5, after verification) ranks the CONFIRMED episodes, then applies the hard
rules below:

    priority = selection × belief_confidence

- **cost**: points lost at the episode root (or the net change over the chain — not
  `points_lost_total`, which double-counts swings), capped at 15 so one blunder does not crowd out
  everything else.
- **student priorities**: read `student_priorities` in the memory profile and follow it. Currently:
  failure to punish (13) is tracked but not led with, because punishing needs strength the student builds
  by fixing other categories first; prefer lessons whose fix is a move a 7k can find.
- **learnability**: `learnability` in the digest (probability that the target rank plays the teachable
  move). Below 0.05 the fix is not learnable now → teach the *recognition cue* instead of the move, or skip.
- **recurrence**: 1.0 if the pattern hash, category and belief are new; ×1.5 if the memory shows the
  same category or the same belief in ≥ 2 of the last 5 games; ×2 if the same 7×7 pattern hash recurs.
- **awareness**: `blind_spot` ×1.5 (the student marked the phase or move as fine), `seen` ×1.0 (they
  flagged it but misjudged the fix), `diagnosed` ×0.7 (they flagged it and diagnosed it correctly: they
  already know).
- **belief_confidence**: ×1.3 when the student *stated* the belief in the interview and the probes
  confirm it is wrong; ×1.0 when the belief is inferred and confirmed; ×0.7 when no belief was found
  (the lesson can only show the better line, not the misread).

Then apply the hard rules: only CONFIRMED hypotheses; at most one lesson per category per game; the
`last_chance` moment of a lost game is a lesson unless it is the same category as another chosen one;
never two lessons about the same group.

## 3. Lesson template (write into `lesson.md`, then the dashboard)

Once the probes have run, the lesson type is already decided; you narrate it, you do not re-decide it.
Every lesson has exactly these parts, in this order:

1. **Title** — a sentence about what happened, in the student's terms. Not a category name.
2. **Where** — moves a–b, region, points lost, game state at the time.
3. **What you were trying to do** — the belief, in the student's words when they stated it in the
   interview ("You connected because C6 looked cuttable"), otherwise from the probes ("F12 was a
   defending move: it only makes sense if the group needed help").
4. **What you expected, and where reality diverges** — the line the student expected (stated, or the
   one a player of their rank reads) and the *exact move* where it stops working: the move they never
   considered. When the belief is about value rather than reading, the divergence is the opponent's
   reply that shows the move was not sente / not biggest / not needed.
5. **The better move's forced line and the comparison of the end positions** — the must-moves after the
   better move and after the played move, each ending in a position the student can evaluate. The
   *why* is the difference between the two endpoints: which groups are alive or dead, who has sente,
   territory by area, what the next biggest move is worth. "After E your corner is 14 points and you
   keep sente; after G it is 9 and White uses the tempo for K16, worth 8" is a proof. "E is 6 points
   better" is a number.
6. **The rule, as a check at the board** (`rule_check`) — one sentence the student can run before
   playing, taken from the belief table: "Before defending, name the attack you fear and read your
   answer." Plus the **cue** — one sentence describing the board pattern that should trigger the check.
7. **Why not X?** (optional) — the other move a player of the student's rank would consider, with its
   refutation in one line.
8. **Quiz** — a move quiz at the episode root, or a status quiz when the lesson is life and death.
9. **Evidence** — ledger ids and query ids (kept in `lesson.md`, not shown on the page).

### Words that must be cashed out

*Direction, thickness, aji, shape, slow, urgent, big, key point, vital point, overplay, heavy, light,
territorial, influence, "does less", "misses the point", "the engine prefers / ranks"* — any such word
must be followed, in the same or the next sentence, by the consequence it names, inside a verified
sequence: which move, which reply, which stones live or die, how many points. If you cannot cash it
out from the ledger, cut the word. Never say what the engine "ranks" or "prefers"; say what happens on
the board.

- A score difference is never the *why*. Use it once, as the size of the lesson, and explain it with the
  endpoint comparison.
- Liberty counts appear only inside a refutation or a capture race ("D7 is atari; White extends to D8
  and has two liberties to your three"), never as a general observation about a group.
- Every sequence in the text is a sequence from a tool result, move for move.

Calibration. Before: *"Move 87 is slow; the direction of play favors the right side."* After: *"You
connected because C6 looked cuttable. If you tenuki and White cuts, D7 atari, White extends, B5 makes
the second eye and the cutting stones are captured. The cut was a gift, not a threat. Meanwhile R10
was worth 14 points: 20 for you if you play it first, 6 if White does."* (Illustrative coordinates.)

Sentences from an earlier review of ogs_91122267 and what each was missing:
- "R9 keeps pushing on the right. The engine ranks the right side last of the nine areas" — says the move
  was small but not what the push was for or what it failed to achieve; needs the belief (was it a
  threat? against what?) and the endpoint comparison with O3.
- "D11 looks like it adds eye space on the side, but it misses the key point. Black answers D10, and
  your group is still unsettled." — the right shape, half finished: it states the belief and the reply,
  but "the key point" is not cashed out. Needs the line after D10 that shows the group has no second
  eye, and the line after E10 that shows it has one.

Tone: direct, specific, kind. Say "you" and the move numbers; avoid hedging phrases and avoid praising
the engine. Never call a move "obvious". When the student's own fix was refuted, say exactly how.

## 4. Questions for the student

### 4.1 Blind self-review (Phase 2, four questions, ~5 minutes)

Ask before showing any engine result, one or two at a time. Always these four:

1. Which phase went worst for you: opening, middlegame, endgame? Why do you think so?
2. Name the move (or the stretch of moves) where you think the game turned. What would you play instead?
3. One of: (a) Which of your groups were you worried about, and when did you stop worrying? Were you
   right? (b) Was there a moment you played locally because your opponent did, rather than because you
   had to? (c) Where do you think your opponent made their biggest mistake, and did you take advantage?
4. Is there a habit from earlier reviews you think showed up again? Where?

When an answer is a move or a position, put that position on the page as a board (review-dashboard,
"The page lives the whole review"). For question 2 a board with `ask: "move"` lets the student click
the move where the game turned instead of typing it.

Only while the survey is still running, add from: rate your position at move ~50 / ~100 / the end
(ahead, close, behind — by how much?); which move are you proudest of?; (handicap) at which move did the
handicap stop mattering?; (resigned games) was resigning right?

Save answers verbatim in `self_review.md`. In Phase 3, compare each answer with the digest: agreements
(flagged, diagnosed correctly), misjudged (saw the area, wrong fix), blind spots (rated fine, engine
disagreed strongly). This comparison sets each episode's awareness (§1): agreement → `diagnosed`,
misjudged → `seen`, blind spot → `blind_spot`. The game record keeps the three counts
(`self_review.misjudged` is a count of the comparison, not an awareness value). Blind spots get the
×1.5 in triage and are the first thing the summary mentions.

### 4.2 Episode interview (Phase 3b, one question per selected episode, ~5 minutes)

After triage, before any engine result about the episode is shown, send the position before the
student's move as a board: `at_move: N − 1`, their move X as the `line` and in `highlight`, `ask:
"line"`. Ask, one episode at a time, in the `interview_order` that `start_verification` returned (the
engine is already working on these episodes):

> "Move N, you played X (on the page). What was it for? And what did you expect to happen next? Click
> it on the board, starting with your opponent's reply, then press Send."

They answer the *what for* in chat and click the *what next* on the board. Read their moves from
`answers/<board id>`; that line is already the one they meant, so there is nothing to type back. If
they answer the line in words instead, put it on a board (`line`) for them to confirm. Save the answers
verbatim in `thinking.md` and pass them straight to `record_interview` (the answer, the clicked line as
`expected_line`, a better move they name as `fix`) before asking about the next episode. That starts the probes that need
the answer while the interview goes on. Then:
- A **stated belief** overrides the inferred one; the lesson's part 3 quotes it.
- A **stated line** is the expected line of part 4: test it (katago-analysis §3) and find where it breaks.
- When the probes contradict the stated belief, that contradiction *is* the lesson.
- "I don't remember" / "no idea" is fine: use the inferred belief and say it is inferred.
- Sealed results apply per episode (§6).

### 4.3 While the engine finishes (Phase 3b, optional)

On a slow machine the verification can still need minutes after the last interview. Check with
`verification_results(job_id)`. While `eta_seconds` is above about a minute, ask questions from
this list, one or two at a time. Use only the SGF and the student's own words, never an engine result.
Record each answer with `record_interview` on its episode. Stop as soon as the queue is done; never
make the student wait for a question. At most one line per episode is budgeted
(`per_episode.student_lines`).

1. **Find the better move.** Send the position before an interviewed episode's move as a board with
   `ask: "move"`: "Move N again: knowing it cost something, what would you play now? Click it." Record
   their move as `fix`; it is tested like their own fix. Say nothing about it until the lesson. Trying first makes the answer
   stick, and a refuted guess goes into the self-review comparison.
2. **Read the resistance.** Take a move from the student's own expected line: "If White doesn't answer
   at X, how do you continue?" Send a board of their line up to that point with `ask: "line"`, and
   record what they click in `lines` (`from: "after"` when it starts after their move).
3. **How sure were you?** "At the time, how sure were you about move N, from 1 to 5?" Record it as
   `confidence`. A confident misread and a guess are different lessons.
4. **The watch-list.** For a category on the memory watch-list: "Your list has *defending groups that
   were already alive*. Was move N a defence?" Save the answer in `thinking.md` with the episode.

## 5. Memory use (recurrence only)

What is written, what is read at the start, the recency weights and what counts as "recurring" are in
go-teacher-flow `references/memory.md`; follow it. In what you say:

- Do not report progress or improvement; report recurrence and its absence ("this category did not
  appear in the last three games").
- The cross-game belief sentence (`belief_insight`: "three of your five biggest losses were defences of
  groups that were already alive") is the most valuable thing memory produces; say it in the summary
  when it holds.

## 6. Rules of conduct

- Interpreter, not oracle: every board claim traces to a ledger row with a query id. If it isn't
  CONFIRMED it isn't taught.
- Points, not winrates. Explain results with the endpoint comparison, in points the student can count.
- Chains, not moves: teach the episode (the sequence that lost the points), and name the first move of
  the chain as the moment to recognise.
- The last recoverable moment of a lost game is more valuable than the biggest blunder after it.
- Difficult correct moves are lessons too: one strength per review, with its move number.
- GTP coordinates only. Never invent one: copy points from tool results; when unsure, ask the tool again.
- Every line visible. Whenever you name two or more moves in a row in chat (the student's expected line,
  a refutation, a better line, an answer to a follow-up), put it on the page the same turn as a board
  (`dashboard_row` with `line`) and say so ("on the page: Your line at move 41"). A board uses no
  engine, so it is allowed while results are sealed; what is sealed is any evaluation of the line, and
  any engine line about an episode before its interview is recorded. After the dashboard is published,
  every line you test for the student also goes on the page as a validated branch (review-dashboard,
  "Follow-up questions"). Only when the page cannot be used: `render_board` with `options.line` in chat,
  with `line.notes` copied under the diagram.
- Do not compute Go on your own (liberties, ladders, life and death) for the student; use the tool and
  report what it says, in your words.
- Concept words, liberty counts and the *why* (the end comparison, never the score delta): §3.
- Two or three lessons per game. If the game offers more, choose by triage and say what you left out.
- Follow the budget (katago-analysis §1): fewer episodes rather than shallower verification; say what
  was left unverified because of time, and deliver fewer lessons rather than unverified ones.
- Handicap games: separate the objective verdict from the practical one; reward simplification when ahead.
- Sealed results: no engine output is shown or used in conversation before `self_review.md` exists, and
  none about an episode before its interview answer is in `thinking.md`. If the student asks early,
  explain why and keep going. From `start_verification` until `record_interview`, the server refuses
  engine results about a selected episode (`sealed`) while its probes are computed in the background.
