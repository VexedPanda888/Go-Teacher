---
name: go-teaching
description: The teaching method for the Go-teacher project — the 15-category weakness taxonomy with detection signatures, triage (costly × learnable × recurring, blind spots first), the lesson format, the blind self-review question bank, memory use (recurrence, not progress), and rules of conduct for teaching a 6–7 kyu player with engine evidence. Use in every review from Phase 0 onward, alongside katago-analysis.
---

# Teaching Go with an engine behind you

The student is 6–7 kyu (OGS), plays Japanese rules, often with handicap. They want two or three lessons
per game that they can actually apply, backed by evidence they can inspect on the dashboard, and they
want recurring weaknesses named across games. They track their own progress elsewhere; memory is for
recurrence only.

## 1. Taxonomy (categories 1–15)

Each category has the *signature* the survey digest tends to show (hints, not proof), the *test* that
confirms it (see katago-analysis §3), and *what to teach*.

| # | Category | Typical signature | What to teach |
|---|----------|-------------------|---------------|
| 1 | Direction of play | best move far from played; `local_loss_share` low (`global`) | Ask "where is the biggest open area / which group needs help" before playing locally |
| 2 | Urgent vs big | best move in another region where a weak group sits; `pass_probe` value there large | "Urgent before big": settle or attack weak groups first |
| 3 | Own life and death | own group's ownership fell; `local_solve` says dead/unsettled | Count liberties and eye space before tenuki; the vital point |
| 4 | Attack / killing | opponent's group survived when it shouldn't; best move attacks | Attack from the outside, take the base, don't touch weak stones |
| 5 | Tactical reading | a move the student's rank plays often (peer ≥ 20 %) refuted by search | Read the opponent's best reply before playing; ladders, nets, snapbacks |
| 6 | Shape | best move adjacent (≤ 2) to played; target rank plays it | Empty triangle / hane at the head / the tiger's mouth: the shape the rank above plays |
| 7 | Joseki | opening corner deviation with a ≥ 2-point contrast | The one joseki line the student keeps deviating from |
| 8 | Invasion / reduction | invasion died or reduction insufficient | Where a framework can still be invaded; the shoulder hit as reduction |
| 9 | Choice of fight | played line high `score_stdev` for little gain; overplay axis | Simplify when ahead, complicate when behind |
| 10 | Thickness / aji | best move removes or exploits aji (`ownership_stdev` high there) | Don't leave cutting points behind; use the opponent's |
| 11 | Endgame value | `swing_value` ranks the played point below others | Count the swing; sente before gote; the 2-point difference is the game |
| 12 | Ko | ko present and mishandled | Count ko threats before starting |
| 13 | Failure to punish | opponent lost ≥ 5 the move before and the student gave most of it back (server rule: `tag_punish_min_loss`, `got_away_ratio`) | Look for the punishment when the opponent plays away |
| 14 | Passive | defensive move protected less than the attack gained | Defend by attacking; check whether the group actually needed help |
| 15 | Slow | best move far away and bigger; played move locally fine but small | Ask "what is my biggest move" before "what is my safest move" |

Orthogonal fields on every episode: **style axis** (overplay / slack / neutral from the digest),
**awareness** (from the self-review: did the student see the problem? blind spot / seen / misjudged),
**game state** (ahead / close / behind when the mistake happened — a mistake when far ahead is a
different lesson: simplify).

## 2. Triage: which 2–3 episodes become lessons

Score every candidate episode after verification:

    priority = cost × learnability × recurrence × awareness_boost

- **cost**: points lost at the episode root (or the net change over the chain — not
  `points_lost_total`, which double-counts swings), capped at 15 so one blunder does not crowd out
  everything else.
- **student priorities**: read `student_priorities` in the memory profile and follow it. Currently:
  failure to punish (13) is tracked but not led with, because punishing needs strength the student builds
  by fixing other categories first; prefer lessons whose fix is a move a 7k can find.
- **learnability**: `learnability` in the digest (probability that the target rank plays the teachable
  move). Below 0.05 the fix is not learnable now → teach the *recognition cue* instead of the move, or skip.
- **recurrence**: 1.0 if the pattern hash or category is new; ×1.5 if the memory shows the same category
  in ≥ 2 of the last 5 games; ×2 if the same 7×7 pattern hash recurs.
- **awareness_boost**: ×1.5 for a blind spot (student marked the phase or move as fine in the
  self-review), ×1.0 if they flagged it, ×0.7 if they diagnosed it correctly (they already know).

Then apply the hard rules: only CONFIRMED hypotheses; at most one lesson per category per game; the
`last_chance` moment of a lost game is a lesson unless it is the same category as another chosen one;
never two lessons about the same group.

## 3. Lesson format (write into `lesson.md`, then the dashboard)

For each lesson:

1. **Title** — a sentence about what happened, in the student's terms. Not a category name.
2. **Where** — moves a–b, region, points lost, game state at the time.
3. **What you were probably thinking** — reconstructed from the self-review answers and the peer-rank
   probability of the played move ("a 7k plays this 40 % of the time; it looks natural because …").
4. **What the board needed** — the teachable move with the engine's line, in words a 7k follows: which
   group, which liberties, which points. Name the concrete refutation of the played move.
5. **The contrast** — the three lines (as played / teachable / your fix), each with its end score from
   the ledger; the reader can play them on the dashboard.
6. **Principle** — one transferable sentence. **Cue** — one sentence describing the board pattern that
   should trigger the principle next time.
7. **Quiz** — a move quiz at the episode root, or a status quiz when the lesson is life and death.
8. **Evidence** — ledger ids and query ids (kept in `lesson.md`, not shown on the page).

Tone: direct, specific, kind. Say "you" and the move numbers; avoid hedging phrases and avoid
praising the engine. Never call a move "obvious". When the student's own fix was refuted, say exactly how.

## 4. The blind self-review (Phase 2, ~10 minutes, 5–8 questions)

Ask before showing any engine result. Pick from the bank; adapt to the game (opening, fights, handicap,
resignation). Always include Q1, Q2, one of Q3–Q5, and Q8.

1. Which phase went worst for you: opening, middlegame, endgame? Why do you think so?
2. Name the move (or the stretch of moves) where you think the game turned. What would you play instead?
3. Which of your groups were you worried about, and when did you stop worrying? Were you right?
4. Was there a moment you played locally because your opponent did, rather than because you had to?
5. Where do you think your opponent made their biggest mistake, and did you take advantage of it?
6. Rate your position at move ~50 / ~100 / at the end: ahead, close, behind — by how much?
7. Which move are you proudest of?
8. Is there a habit from earlier reviews you think showed up again? Where?
9. (Handicap) At which move did the handicap stop mattering? Did you simplify enough?
10. (Resigned games) Was resigning right? What would you have needed to see to continue?

Save answers verbatim in `self_review.md`. In Phase 3, when the survey digest is opened, compare each answer with the engine: agreements,
misjudgements (saw the area, wrong fix), blind spots (rated fine, engine disagreed strongly). Blind spots
get the awareness boost in triage and are the first thing the summary mentions.

## 5. Memory use (recurrence only)

- After each review write: game record (id, date, colour, handicap, result), the 2–3 lessons
  (category, principle, cue, pattern hash), every CONFIRMED episode (category, tags, points, hash), and
  refresh the compact profile (≤ 1,500 tokens: recurring categories with counts over the last 10 games,
  style axis tendency, awareness tendency, open lessons with the game they were set in).
- At the start of a review read only the profile; pull individual episodes only when a pattern hash or
  category matches.
- Recency weight 0.85 per game when counting recurrence. A category counts as "recurring" at ≥ 2 of
  the last 5 games. Do not report progress or improvement; report recurrence and its absence
  ("this category did not appear in the last three games").

## 6. Rules of conduct

- Interpreter, not oracle: every board claim traces to a ledger row with a query id. If it isn't
  CONFIRMED it isn't taught.
- Points, not winrates. Explain results in points the student can count.
- Chains, not moves: teach the episode (the sequence that lost the points), and name the first move of
  the chain as the moment to recognise.
- The last recoverable moment of a lost game is more valuable than the biggest blunder after it.
- Difficult correct moves are lessons too: one strength per review, with its move number.
- Never invent a coordinate. Copy points from tool results; when unsure, ask the tool again.
- Do not compute Go on your own (liberties, ladders, life and death) for the student; use the tool and
  report what it says, in your words.
- Two or three lessons per game. If the game offers more, choose by triage and say what you left out.
- Handicap games: separate the objective verdict from the practical one; reward simplification when ahead.
- Sealed results: no engine output is shown or used in conversation before `self_review.md` exists.
