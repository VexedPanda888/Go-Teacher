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
  "student": {"username": "cwhay888", "rank": "7k", "server": "OGS", "rules": "japanese"},
  "games_reviewed": 12, "updated_at": "2026-10-04",
  "summary": "Three or four sentences: what keeps recurring, in the student's terms.",
  "watch": "One sentence for the next review: the pattern most likely to show up.",
  "recurring": [
    {"category": "2", "label": "Urgent vs big", "count_last10": 4, "weighted": 2.9, "recurring": true,
     "last_game_id": "ogs_12345678", "typical_cue": "answering a contact move against a settled group"}
  ],
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

`episodes/<game_id>-<Eid>` — one per CONFIRMED episode (taught or not):

```json
{"game_id": "ogs_12345678", "episode_id": "E1", "date": "2026-10-04", "category": "2", "tags": ["2", "15"],
 "points_lost": 14.2, "moves": [87, 103], "region": "LL", "phase": "middlegame", "game_state": "close",
 "style": "slack", "awareness": "blind_spot", "pattern_hash": "ph_3f9a…", "pattern_hash_5": "ph5_…",
 "teachable_move": "R8", "played": "Q7", "verdict": "CONFIRMED", "taught": true, "query_ids": ["q_ogs_12345678_0042"]}
```

`lessons/<game_id>-L<n>` — one per lesson delivered:

```json
{"game_id": "ogs_12345678", "date": "2026-10-04", "title": "…", "category": "2", "principle": "…", "cue": "…",
 "episode_ids": ["ogs_12345678-E1"], "pattern_hash": "ph_3f9a…", "status": "open"}
```
`status` becomes `"retired"` when the category has not appeared in the last five reviewed games.

**Seeded games** (WS8, survey only, no verification) use the same collections with `episodes.verdict:
"SURVEY"`, `taught: false`, `games.seeded: true`, and no lessons. In the profile's recurrence math a
SURVEY episode counts half (weight 0.5 × 0.85^age) and never on its own makes a category "recurring".
Seeded episodes also carry `chain_points` (the digest's chain sum; `points_lost` is the root loss),
`server_tags`, `tags_source`, `persistent_best`, `learnability`, `peer_played` and `stability`. When a
seeded game is later reviewed properly, replace its SURVEY episodes with the CONFIRMED ones.

`patterns/<pattern_hash>` — one per 7×7 canonical pattern seen as a mistake:

```json
{"hash": "ph_3f9a…", "category": "2", "count": 2, "note": "contact move against a settled group, answered locally",
 "occurrences": [{"game_id": "ogs_12345001", "move": 44}, {"game_id": "ogs_12345678", "move": 87}]}
```

## Phase 0: read

One call: ArtifactData `get` with `collection: "profile"`, `doc_id: "main"`. Note the `version`.
Copy `watch` and the `recurring` entries with `recurring: true` into `intake.md`. Pull individual
episodes or patterns only when the survey digest shows a matching category or pattern hash
(`read_db` `query` on `episodes` with `where: [["category","eq","2"]]`, or `get` on `patterns/<hash>`).

## Phase 6: write

After the dashboard is published, in this order (each write pins `if_version` when the document was
read earlier; new documents need no version):

1. `write_db` `batch` (≤ 50 writes) with `set` for: `games/<game_id>`, each CONFIRMED
   `episodes/<game_id>-<Eid>`, each `lessons/<game_id>-L<n>`.
2. For each episode's `pattern_hash`: `read_db` `get` `patterns/<hash>`; if it exists, `update` with the
   appended `occurrences` and the new `count` (pin `if_version`); else `set` it with `count: 1`.
3. Recompute the profile from the last 10 games (query `games` ordered by `date` desc, limit 10, then
   their episodes): for every category, `count_last10` = number of those games with a CONFIRMED episode in
   the category; `weighted` = Σ 0.85^(age in games) over those episodes; `recurring` = present in ≥ 2 of the
   last 5 games. Update `tendencies` counters from the episodes' `style`, `awareness`, `game_state`, and
   points lost by `phase`. Write `summary` (3–4 sentences) and `watch` (1 sentence). Retire lessons whose
   category is absent from the last five games. `write_db` `set` `profile/main` with `if_version` from Phase 0.
4. Say in chat, in one line, what was recorded ("memory: game, 2 episodes, 2 lessons; urgent-vs-big is
   now recurring, 3 of the last 5 games").

If a pinned write fails with a version conflict, re-read that document and redo only that write.
Never write anything the ledger does not mark CONFIRMED. Never store engine arrays or SGF text in memory;
the dashboard and `reviews/<game_id>/` on the server hold those.
