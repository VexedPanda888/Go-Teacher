# Go teacher memory — schema and recipe

Memory lives in the artifact **Go teacher memory**, `https://claude.ai/artifact/XsADdyJrw9nLYJZPa99Net`
(private), in its `db`. Claude reads and writes it with the `ArtifactData` tool, `url` as above
(`get` / `list` / `query` to read; `set` / `update` / `batch` to write, ≤ 50 writes per batch; pin
`if_version` on documents you have read). The page itself is a read-only view for the student. Memory
records what the student took from each game and what keeps coming back, never progress or improvement.

## Collections

`lessons/<game_id>-L<n>` — **one per takeaway**, the heart of memory. The collection keeps its old
name so the lessons of earlier reviews stay where they are and count as takeaways. A takeaway written
from this version on:

```json
{"game_id": "ogs_12345678", "date": "2026-10-04", "moment": "M1", "move": 87, "played": "Q7", "better": "R8",
 "title": "Saving a group that was already alive",
 "takeaway": "When my group already has a base and White plays a contact move nearby, I'll read whether I can ignore it before answering.",
 "student_words": "Don't answer every contact move near a living group.",
 "cue": "A contact move next to a group with a base and eye shape.",
 "theme": "answering moves that did not need an answer",
 "status": "open", "recall": []}
```

- `takeaway` is the sharpened sentence you agreed on; `student_words` what the student first said.
- `cue` (optional): the situation on the board that should trigger it, one sentence.
- `theme`: a short phrase in plain words naming what the takeaway is about. Reuse the wording of an
  earlier takeaway's theme when it is the same thing again: that is how recurrence is found. No
  category numbers.
- `move`, `played`, `better`: the key moment's move number, the move played, and the better move the
  student learned (for recall quizzes).
- `recall`: the recall quizzes on it (go-teaching §6), oldest first: `{date, game_id, answer, result}`
  with `result` `found` (the better move), `acceptable` (another move within a point) or `missed`.
- `status`: `open`, or `retired` after the student found it in two recall quizzes in a row.

**Lessons from earlier reviews** (before October 2026) have the same document shape with older field
names: `rule_check` (or `principle`) in place of `takeaway`, `cue`, `title`, `status`, `recall`, and no
`move` / `played` / `better`: those are in `episodes/<the first of episode_ids>` (`moves[0]`, `played`,
`teachable_move`). They also carry `category`, `belief` and `pattern_hash` from the old taxonomy:
ignore those. Treat each one exactly like a takeaway; never rewrite them except to append `recall` or
change `status`.

`games/<game_id>` — one per reviewed game:

```json
{"game_id": "ogs_12345678", "date": "2026-10-04", "color": "B", "handicap": 3, "komi": 0.5,
 "opponent": "name", "opponent_rank": "4k", "result": "W+12.5", "reviewed_at": "2026-10-04",
 "story": "One or two sentences: how the game went and where it turned.",
 "moments": [{"id": "M1", "move": 87, "played": "Q7", "better": "R8", "points_lost": 14.2}],
 "lesson_ids": ["ogs_12345678-L1"], "dashboard_url": "https://claude.ai/artifact/…"}
```

`profile/main` — one document, the only thing read in full at the start (keep it under ~1,500 tokens):

```json
{
  "schema_version": 2,
  "student": {"username": "<ogs-name>", "rank": "7k", "server": "OGS", "rules": "japanese"},
  "games_reviewed": 13, "updated_at": "2026-10-04",
  "summary": "Three or four sentences, in the student's terms: what they keep taking away, what keeps coming back.",
  "watch": "One sentence for the next review: the situation most likely to show up again.",
  "recurring_themes": [{"theme": "answering moves that did not need an answer", "count_last10": 3,
                        "lesson_ids": ["ogs_12345678-L1", "ogs_91232433-L1"]}],
  "open_lesson_ids": ["ogs_12345678-L1", "ogs_12345001-L2"],
  "student_priorities": "The student's own steer on what to focus on. Follow it when choosing key moments."
}
```

Older collections (`episodes`, `patterns`) and older profile fields (`recurring`, `beliefs`,
`belief_insight`, `tendencies`) come from the taxonomy reviews. Read an `episodes` document only to find
an old lesson's move; write neither.

## At the start

1. `get` `profile/main`. Note its `version`. Copy `watch`, `student_priorities` and the
   `recurring_themes` into `notes.md`: a recurring theme is worth watching for when you choose key
   moments (go-teaching §3). A profile with `schema_version` 1 is the old shape: use `summary`, `watch`,
   `student_priorities` and `open_lesson_ids`, and rewrite it in the new shape at the end.
2. The recall list (go-teaching §6): `get` up to five of the profile's `open_lesson_ids` (and, for an
   old lesson, the first of its `episode_ids` to find its move). Order them: last `recall` result
   `missed` first, then never quizzed, then the longest since the last quiz; a recurring theme breaks
   ties. Keep the first three in `notes.md`: lesson id, game id, date, move N, the better move, the
   move played, the takeaway (or `rule_check` / `principle`). Skip one with no better move.

## At the end

After the dashboard is published, in this order (each write pins `if_version` when the document was
read earlier; new documents need no version):

1. `batch` (≤ 50 writes): `set` `games/<game_id>`; `set` each takeaway as `lessons/<game_id>-L<n>`
   (n = 1, 2, … in the order of the moments, continuing after any lessons an earlier review of the same
   game left; a moment the student did not take a takeaway from is not written); `update` each lesson quizzed in this review ("Recall" in `notes.md`) with its `recall` list
   plus the new entry, and `status: "retired"` when this was its second `found` in a row.
2. Recompute the profile from the last 10 games (query `games` ordered by `date` desc, limit 10, then
   their `lesson_ids`): group the takeaways by `theme` (same wording, or the same situation in other
   words: then reuse one wording); a theme with takeaways from two or more games is recurring. Write
   `summary` (3–4 sentences), `watch` (1 sentence), `recurring_themes`, `open_lesson_ids` (every
   lesson with `status: "open"`, newest first), `games_reviewed`, `updated_at`, `schema_version: 2`;
   keep `student` and `student_priorities`. `set` `profile/main` with `if_version` from the start.
3. Say in chat, in one line, what was recorded ("memory: the game and 2 takeaways; 'answering moves that
   did not need an answer' has now come up in 3 of the last 10 games").

If a pinned write fails with a version conflict, re-read that document and redo only that write.
Write only what the student agreed to as a takeaway. Never store engine arrays or SGF text in memory;
the dashboard and `reviews/<game_id>/` on the server hold those.
