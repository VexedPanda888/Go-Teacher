# Go teacher memory — schema and recipe

Memory lives in the artifact **Go teacher memory**, `https://claude.ai/artifact/XsADdyJrw9nLYJZPa99Net`
(private), in its `db`. Claude reads and writes it with the `ArtifactData` tool, `url` as above. Below,
`read_db` means ArtifactData `get` / `list` / `query` and `write_db` means `set` / `update` / `batch`
(≤ 50 writes per batch; pin `if_version` on documents you have read). The page itself is a read-only view for the student. Memory records **recurrence**,
never progress or improvement.

## Collections

`profile/main` — one document, the only thing read at the start of a review (keep it under ~1,500 tokens):

```json
{
  "schema_version": 1,
  "student": {"username": "<ogs-name>", "rank": "7k", "server": "OGS", "rules": "japanese"},
  "games_reviewed": 12, "updated_at": "2026-10-04",
  "summary": "Three or four sentences: what keeps recurring, in the student's terms.",
  "watch": "One sentence for the next review: the pattern most likely to show up.",
  "recurring": [
    {"category": "2", "label": "Urgent vs big", "count_last10": 4, "weighted": 2.9, "recurring": true,
     "last_game_id": "ogs_12345678", "typical_cue": "answering a contact move against a settled group"}
  ],
  "beliefs": [
    {"belief": "needs_defending", "count_last10": 3, "weighted": 2.2, "recurring": true, "stated": 1,
     "among_top5_losses": 3, "last_game_id": "ogs_12345678"}
  ],
  "belief_insight": "Three of your five biggest losses in the last ten games were defences of groups that were already alive.",
  "tendencies": {
    "style": {"overplay": 3, "slack": 7, "neutral": 5},
    "awareness": {"blind_spot": 6, "seen": 5, "diagnosed": 4},
    "game_state": {"ahead": 4, "close": 8, "behind": 3},
    "phase": {"opening": 21.5, "middlegame": 63.0, "endgame": 12.0}
  },
  "open_lesson_ids": ["ogs_12345678-L1", "ogs_12345001-L2"],
  "student_priorities": "The student's own steer on what to teach first (e.g. 13 tracked but not led with). Follow it in triage.",
  "games_seeded": 19, "seed_note": "Where seeded data came from and how far to trust it."
}
```

`games/<game_id>` — one per reviewed game:

```json
{"game_id": "ogs_12345678", "date": "2026-10-04", "color": "B", "handicap": 3, "komi": 0.5,
 "opponent": "name", "opponent_rank": "4k", "result": "W+12.5", "points_lost_total": 41.2,
 "reviewed_at": "2026-10-04", "total_minutes": 40, "episode_ids": ["ogs_12345678-E1", "ogs_12345678-E3"],
 "lesson_ids": ["ogs_12345678-L1", "ogs_12345678-L2"], "lesson_titles": ["…", "…"],
 "self_review": {"agreements": 2, "misjudged": 1, "blind_spots": 1}, "dashboard_url": "https://claude.ai/artifact/…"}
```
`self_review` counts the Phase 3 comparison (go-teaching §4.1); `misjudged` there is a count, not an
awareness value.

`episodes/<game_id>-<Eid>` — one per CONFIRMED episode (taught or not):

```json
{"game_id": "ogs_12345678", "episode_id": "E1", "date": "2026-10-04", "category": "2", "tags": ["2", "15"],
 "belief": "needs_defending", "belief_source": "stated", "belief_statement": "C6 looked cuttable",
 "misread": {"move": 88, "expected": "S8", "never_considered": "R8"}, "rule_check": "Name the attack you fear and read your answer.",
 "points_lost": 14.2, "moves": [87, 103], "region": "LL", "phase": "middlegame", "game_state": "close",
 "style": "slack", "awareness": "blind_spot", "pattern_hash": "ph_3f9a…", "pattern_hash_5": "ph5_…",
 "teachable_move": "R8", "played": "Q7", "verdict": "CONFIRMED", "taught": true, "query_ids": ["q_ogs_12345678_0042"]}
```
`awareness` is `blind_spot`, `seen` or `diagnosed` (go-teaching §1, §4.1).

`lessons/<game_id>-L<n>` — one per lesson delivered:

```json
{"game_id": "ogs_12345678", "date": "2026-10-04", "title": "…", "category": "2", "belief": "needs_defending",
 "rule_check": "…", "cue": "…",
 "episode_ids": ["ogs_12345678-E1"], "pattern_hash": "ph_3f9a…", "status": "open",
 "recall": [{"date": "2026-10-11", "game_id": "ogs_12399999", "answer": "R8", "result": "found"}]}
```
`recall` lists the recall quizzes on this lesson (go-teaching §4.4), oldest first; `result` is `found`
(the lesson's move), `acceptable` (another move within the acceptable set) or `missed`. Older lessons
have none.
`status` becomes `"retired"` when the category has not appeared in the last five reviewed games.
Lessons written before v0.3 have `principle` instead of `rule_check`, and no `belief`; read either.

`belief` is one of the seven `intent_probe` belief ids (go-teaching §1), or `null` when the probes found none.
`belief_source` is `stated` (the student said it in the interview, `belief_statement` quotes them),
`inferred` (from `intent_probe` in a verified review) or `probe_survey` (from `katago-mcp-seed --probes`:
low visits, never verified). `misread` comes from `expectation_probe`; omit it when there was none.

**Seeded games** (WS8, survey only, no verification) use the same collections with `episodes.verdict:
"SURVEY"`, `taught: false`, `games.seeded: true`, and no lessons. In the profile's recurrence math a
SURVEY episode counts half (weight 0.5 × 0.85^age) and never on its own makes a category "recurring".
Seeded episodes also carry `chain_points` (the digest's chain sum; `points_lost` is the root loss),
`server_tags`, `tags_source`, `persistent_best`, `learnability`, `peer_played` and `stability`, and, when
the seed ran with `--probes`, `belief` with `belief_source: "probe_survey"` (half weight, like the rest). When a
seeded game is later reviewed properly, replace its SURVEY episodes with the CONFIRMED ones.

`patterns/<pattern_hash>` — one per 7×7 canonical pattern seen as a mistake:

```json
{"hash": "ph_3f9a…", "category": "2", "count": 2, "note": "contact move against a settled group, answered locally",
 "occurrences": [{"game_id": "ogs_12345001", "move": 44}, {"game_id": "ogs_12345678", "move": 87}]}
```

## Phase 0: read

One call: ArtifactData `get` with `collection: "profile"`, `doc_id: "main"`. Note the `version`.
Copy `watch`, `belief_insight` and the `recurring` and `beliefs` entries with `recurring: true` into
`intake.md`; a recurring belief is a hypothesis to test first in Phase 4. Pull individual
episodes or patterns only when the survey digest shows a matching category, belief or pattern hash
(`read_db` `query` on `episodes` with `where: [["category","eq","2"]]`, or `get` on `patterns/<hash>`).

The recall list (go-teaching §4.4): `get` up to five of the profile's `open_lesson_ids`, and for each
the first of its `episode_ids` (`episodes/<id>`). Order them: last `recall` result `missed` first, then
never quizzed, then the longest since the last quiz; a recurring category breaks ties. Keep the first
three in `intake.md`: lesson id, game id, date, the episode's first move N, `teachable_move`,
`played`, `rule_check`. Skip a lesson whose episode has no `teachable_move`.

## Phase 6: write

After the dashboard is published, in this order (each write pins `if_version` when the document was
read earlier; new documents need no version):

1. `write_db` `batch` (≤ 50 writes) with `set` for: `games/<game_id>`, each CONFIRMED
   `episodes/<game_id>-<Eid>`, each `lessons/<game_id>-L<n>`; and `update` for each lesson quizzed in
   this review ("Recall" in `thinking.md`), with its `recall` list plus the new entry.
2. For each episode's `pattern_hash`: `read_db` `get` `patterns/<hash>`; if it exists, `update` with the
   appended `occurrences` and the new `count` (pin `if_version`); else `set` it with `count: 1`.
3. Recompute the profile from the last 10 games (query `games` ordered by `date` desc, limit 10, then
   their episodes): for every category, `count_last10` = number of those games with a CONFIRMED episode in
   the category; `weighted` = Σ 0.85^(age in games) over those episodes; `recurring` = present in ≥ 2 of the
   last 5 games. Update `tendencies` counters from the episodes' `style`, `awareness`, `game_state`, and
   points lost by `phase`. Recompute `beliefs` the same way from the episodes' `belief` (skip `null`):
   `count_last10`, `weighted` (a `probe_survey` belief counts half), `recurring` (≥ 2 of the last 5 games),
   `stated` (how many the student said themselves), and `among_top5_losses` (the five episodes with the
   largest `points_lost` over those ten games). When one belief holds ≥ 2 of those five, write
   `belief_insight` as one sentence in the student's terms ("Three of your five biggest losses were
   defences of groups that were already alive"); otherwise set it to `null`. Write `summary` (3–4
   sentences) and `watch` (1 sentence). Retire lessons whose
   category is absent from the last five games. `write_db` `set` `profile/main` with `if_version` from Phase 0.
4. Say in chat, in one line, what was recorded ("memory: game, 1 episode, 1 lesson; urgent-vs-big is
   now recurring, 3 of the last 5 games"), and the `belief_insight` when it is new or changed.

If a pinned write fails with a version conflict, re-read that document and redo only that write.
Never write anything the ledger does not mark CONFIRMED. Never store engine arrays or SGF text in memory;
the dashboard and `reviews/<game_id>/` on the server hold those.
