# Seeding the memory from past games

Use when the student asks to seed or reseed the teacher, to "go through my last games", or to continue a
seeding. Seeding fills memory with the student's recent games so later reviews can see what keeps coming
back. Each game gets a brief pass on one page: the story of its survey, the student's feedback, and one
key lesson they confirm. The memory is written once, at the end.

**How it differs from a review.** The student asked for the engine's story first, so there is no guided
self-review and no "ask before you tell" about the story. Aim for three to five minutes a game: one
question on the story, one lesson, one confirmation. No recall quizzes and no follow-up questions beyond
what the lesson needs. The rest of go-teaching still holds. Every concrete claim comes from a tool result
(§7), the story is told in points (§3), and the lesson follows the explanation standard briefly (§5) with
a takeaway in the §4.5 form.

Keep `seed-notes.md` in the sandbox. Note the page URL, whether the student confirmed an overwrite, and
for each game the feedback verbatim, the lesson agreed and its query ids. After a compaction,
`seed_status` and the notes say where you are.

## 1. Check what exists

1. `engine_info`, then `seed_status`.
2. Memory (`memory.md`): `get` `profile/main` and note its `version`. `query` `games` with `seeded == true`.
   `list` `games` to find the reviewed games, the ones without `seeded: true`. Note the themes in
   `recurring_themes` and those of the open lessons, so a lesson about the same thing reuses that
   wording.
3. **A previous seeding** shows as the profile's `seed` (this procedure), or as `games_seeded` /
   `seed_note` (the survey seeding of September 2026), or as any game with `seeded: true`. When there
   is one, ask before anything else, in one message:
   > "You seeded on <date> (<n> games, <m> lessons). Seeding again replaces that: the <n> seeded game
   > records, the <m> seeded lessons and the <k> survey-only episodes from the old seeding. Your
   > reviews and their takeaways stay. Overwrite it?"

   Count the seeded lessons with `query` `lessons` where `source == "seed"`. Count the old episodes with
   `query` `episodes` where `verdict == "SURVEY"`. Go on only after a clear yes, and note it. On a no,
   stop.
4. **An unfinished seeding on this machine** (`seed_status` has a session without `finished_at`): ask
   whether to continue it or start over. A seeding prepared with the CLI shows here too, with its
   surveys done. Ask this in the same message as the overwrite question when both apply.

## 2. Start

`seed_start({exclude: <the reviewed game ids>})`. Pass `count` when the student gives a number, and
`games` when they name games (OGS links or `.sgf` names). Pass `restart: true` when they chose to start
over. Continuing is a plain `seed_start()`.

Tell the student in two or three sentences:
- which games, by number and date range;
- what was left out and why, briefly;
- that each game takes a few minutes, and the engine prepares the next ones while you talk.

When `engine_seconds_ahead` is more than about 15 minutes, say that later games may need a short wait.
Next time they can prepare overnight with `katago-mcp seed --config config/<machine>.toml` while the
desktop app is closed.

**Open the seed page** (review-dashboard, "The seed page"). Give the link: "Keep this page open. Each
game appears there with its score graph; the tabs at the top switch games."

## 3. Each game

1. **Show it.** Call `seed_game()`. When `ready` is false, say how long it needs (`eta_seconds`). When
   that is more than a couple of minutes, offer a game that is ready (`seed_status`, then
   `seed_game(<n>)`), or a pause. Write the two `page_rows` with ArtifactData exactly as given; put your
   story in the `seedgames` row's `text` and nothing else changes.
2. **The story, as the survey's best guess.** First check `story.game.reconciliation`. On `mismatch`,
   say in one sentence that the final count is uncertain, and go on. Tell the story in four to six
   sentences, in order (go-teaching §3): how the lead moved, which groups lived or died and when, the
   swings that mattered, the decisive move, and the last chance in a loss. Use points. Then ask one
   question:
   > "That's the survey's reading. Is it how you remember it? What's wrong or missing?"
3. **Their feedback.** Save it verbatim. Use it:
   - **They name a moment.** It is the lesson candidate when it is one of their moves with a real loss
     (in `story.moments` or `story.swings`). If the survey says it lost little, say so with the number,
     in one sentence.
   - **They dispute a fact**, such as a group's status or the count. Check once with `local_solve` or
     the reconciliation, and answer in a sentence. If it stays unresolved, note it and move on.
   - **Rewrite the story** in one to three sentences with their corrections. That version is recorded,
     not the long one.
4. **The lesson.** Pick one moment: the one their feedback points to, otherwise `top_moment`, preferring
   one that is `prepared`. Call `explain_moment({job_id, move_number: N − 1})`. When its notes say the
   moves are close, take the next moment. Put two boards on the page with `dashboard_row(kind: "board",
   page: "seed", game: job_id, board: {title, at_move: N − 1, line, episode: "M1"})`:
   - "Move N as played", with `lines.move.line`;
   - "Better: X", with `lines.best.line`.

   Explain in three to five sentences: what their move did and how it was answered, what the better move
   does, and what is different at the end of the two lines. Give the points lost once, as the size. Then
   propose the lesson:
   - a **title** of a few words;
   - the **takeaway**: the situation to recognise and what to check there (go-teaching §4.5);
   - a **theme**: a short plain phrase, reusing an earlier theme's wording when it is the same thing.

   Ask: "Does this lesson fit this game? Yes, or tell me what to change."
5. **Confirm and record.**
   - **Yes:** `seed_record(n, "confirmed", story, feedback, lesson: {move: N, better: X, title, takeaway,
     theme, cue?, moment})`.
   - **They reword it:** the agreed sentence is `takeaway`, and their own wording goes in
     `student_words`.
   - **They want another moment:** one more `explain_moment` for it. If nothing fits after that, record
     `no_lesson` with the story.
   - **They want the game left out:** record `skipped`.

   Write the returned `page_row`, then go to the next game. When a theme repeats an earlier game's, say
   so in one line: "That's the same thing as game 2."

Every three or four games, give one line of progress. When the student wants to stop, offer two choices.
They can finish now, leaving the rest out (`seed_finish(..., leave_out_unrecorded: true)`), or pause and
continue later by asking to continue the seeding.

## 4. Save

When `seed_status.next` is null, call `seed_finish({page_url})`. Then:

1. Write each of `batches` with ArtifactData `batch`, the documents exactly as given.
2. **Only after a confirmed overwrite**, delete the earlier seeding's documents that this seeding did
   not just rewrite:
   - `games` with `seeded: true` whose id is not in `game_ids`;
   - `lessons` with `source: "seed"` whose id is not in `lesson_ids`;
   - `episodes` with `verdict: "SURVEY"`.

   Use batches of at most 50 deletes. Never delete anything else.
3. Recompute the profile as `memory.md` "At the end", step 2 describes; the seeded games count like any
   other. Set `seed` to `profile_seed` and leave out `games_seeded` and `seed_note`. Write it with `set`,
   pinning the `version` you read.
4. Tell the student in three to five lines:
   - how many games and lessons were saved;
   - the themes that came up more than once ("own weaknesses before big points: 3 of 10 games");
   - the page link;
   - which lesson to keep in mind first.

## When things go wrong

- **`ogs_fetch_failed`**: ask for the games as OGS links, or as `.sgf` files put in the server's
  `games/` folder.
- **A game's survey `failed`**: `seed_game(<n>)` tries again. If it fails twice, record it `skipped`.
- **`engine_unavailable`**: as in the review flow. The seeding stays on disk and resumes with
  `seed_start()`.
- **The page cannot show the rows**: use the review flow's fallback (review-dashboard, "Fallback").
  Lines go to chat with `render_board`, and the seeding itself goes on.
- **A memory write fails on a version conflict**: re-read that document and redo only that write.
  `seed_finish` returns the same documents when called again.
