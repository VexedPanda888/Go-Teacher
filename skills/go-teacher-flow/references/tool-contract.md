# katago-mcp — Tool Contract (v0.6.0, as implemented in katago-mcp 0.5.0)

**Status:** current; describes the implemented server, 19 tools. This is the only copy (skills must be self-contained); `katago-mcp/tests/test_docs.py` checks that the header names the code's versions. Decisions and the version history are in `docs/contract-changes.md`. Values marked *config* live in the per-machine config file (§6). Numbers in examples are illustrative.

Design points worth knowing up front:
1. No time budget. Search sizes are fixed per machine (`[search]`, §6): a slower machine searches less per position, never fewer checks, and a review takes as long as it needs. `engine_info` says how long the main call (`explain_moment`) takes on this machine.
2. The tools follow the way a teacher reviews: a quick pass over the whole game told as a story (`job_results`, §1.5), then a deep look at one key moment at a time (`explain_moment`, §1.6). The other tools answer the student's follow-up questions.
3. A tool call blocks Claude's turn, so Claude cannot talk to the student while a search runs. When a survey finishes, the server prepares `explain_moment` for the story's top key moments in the background, and Claude may queue more; Claude's identical call later returns the stored result (§0.8). Nothing is shown to the student before Claude asks: the review asks the student what they were thinking first.
4. Numeric dashboard data cannot pass through Claude by retyping without error, so `validate_variations` assembles the complete dashboard data blob and returns it with a checksum (§1.18, §5).
5. The student follows the review on one page, published live at the start. Positions and lines reach it as rows Claude writes to the page's database, made by `dashboard_row` (§1.19) with a checksum the page verifies.

---

## 0. Conventions

### 0.1 Coordinates, colors, moves
- Points use GTP notation: columns `A`–`T` skipping `I`, rows `1`–`19` (`"D4"`, `"Q16"`). `"pass"` is a legal move value.
- The server converts SGF coordinates at its boundary. **No tool ever emits SGF letters**, and Claude never sends them.
- `Color = "B" | "W"`. `Move = [Color, Point | "pass"]`.
- Board size is 19 in v1. Every position carries `board_size`; anything else returns `unsupported_board_size`.

### 0.2 Positions and references
```ts
type Position =
  | { ref: PositionRef; then?: Move[] }                      // a stored position, optionally extended
  | { job_id: string; move_number: number; then?: Move[] }   // after move N of a job's game (0 = after setup stones)
  | { sgf: string; move_number: number; then?: Move[] }
  | { setup?: { B?: Point[]; W?: Point[] }; moves: Move[]; rules?: Rules; komi?: number; to_move?: Color }

type PositionRef = string   // "pos_" + 16 hex chars
type Rules = "japanese" | "chinese" | "korean" | "aga" | "tromp-taylor"   // v1 default: "japanese"
```
- A `PositionRef` hashes rules, komi, board, side to move, ko state, the position history (for superko) and the game it belongs to: a position of a job or an SGF belongs to that game, one given as `moves` to none. The same opening in two games has two refs, each with its own game's student colour and log. Refs are persisted under `reviews/<game_id>/positions/` and survive server restarts.
- Every response that involves a position includes its `position_ref`, so Claude chains calls without resending moves.
- `then` moves are legality-checked (occupied point, suicide, ko, superko per the rules). An illegal move returns `illegal_move` with the ply and reason.

### 0.3 Perspective and units
- `score_lead`, `winrate` and `points` fields are reported from `perspective`, which defaults to the **student's color when the position comes from a job**, else Black. Every response echoes `perspective`. Positive `score_lead` is good for that color.
- `points_lost` is always from the mover's perspective and never negative.
- **Ownership is a board fact, not an evaluation:** always Black-positive (+1 Black owns the point, −1 White owns it), independent of `perspective`. A group's `mean_ownership` is reported from the **group owner's** perspective (positive = the group is doing well).
- Score units are KataGo `scoreLead` points, approximately final-score points under the game's rules.

### 0.4 Budgets and search sizes
```ts
type Budget =
  | { visits: number }
  | { seconds: number }                                        // converted with sustained visits/s
  | { profile: "survey" | "root" | "line_node" | "stability" | "local_solve" | "quick"; multiplier?: number }
```
- A bare number is read as `{ visits }`, a profile name string as `{ profile }`. Tools default to the profile their section names; Claude normally passes no budget at all.
- Profiles resolve to the machine's fixed sizes in `[search]` (§6): `root`, `line_node`, `local_solve` and `quick` as written; `stability` = `root × stability` (or `× multiplier`); `survey` = visits per move sized from the measured visits/s so the survey takes about `survey_minutes_target` (10) minutes, clamped to `survey_floor`–`survey_cap` (100–1000). `plies` (8 on the Pro) is the default length of forced lines, imagined lines and PV continuations.
- `root`, `stability` and `local_solve` searches stop early when stable (§0.7). Every response reports `visits_used` and `seconds_used`.

### 0.5 Human profiles
- KataGo human-model profile strings: `rank_20k` … `rank_1k`, `rank_1d` … `rank_9d`. OGS ranks outside that range are clamped (`25k` → `rank_20k`, `10d` → `rank_9d`).
- Aliases, resolved from the game context (or config when there is none): `peer` = the student's rank; `target` = the student's rank + `target_offset_stones` (config, 3) stronger; `horizon` = `horizon_rank` (config, `rank_1d`); `opponent` = the opponent's rank from the SGF. Responses list `resolved_profiles`.
- One profile costs one neural-network evaluation (no search); requesting four profiles is cheap.

### 0.6 Regions
```ts
type Region =
  | { standard: "UL"|"U"|"UR"|"L"|"C"|"R"|"LL"|"D"|"LR" }
  | { rect: [Point, Point] }              // inclusive opposite corners
  | { near: Point; radius: number }       // Chebyshev distance
  | { points: Point[] }
```
The **standard partition** tiles the board: columns `A–G` | `H–M` | `N–T` (7 | 5 | 7) × rows `13–19` | `8–12` | `1–7` (7 | 5 | 7). Corners are 7×7 (49 points), sides 5×7 (35), center 5×5 (25). Human labels: "upper left corner", "upper side", "center", and so on. Territory by region (§1.10) uses this partition.

### 0.7 Stability-based early stop
For `root`, `stability` and `local_solve` profiles the server issues one KataGo query at the target visits with periodic progress reports (about one per second) and terminates it early when, across the last three reports, the top move is unchanged and `score_lead` has moved less than 0.5 points. Responses include `stopped_early: boolean`. `survey`, `line_node` and explicit `{visits}` budgets run to completion.

### 0.8 Caching, stored results, persistence, logging
- Searches are cached by (`position_ref`, effective visits, options). `cached: true` marks a hit. A request for more visits than cached re-searches.
- **Stored results.** `explain_moment`, `analyze_position`, `analyze_line`, `swing_value`, `local_solve`, `terminal_features`, `forced_line`, `intent_probe` and `expectation_probe` keep their whole result, keyed by (tool, `position_ref`, effective visits, the move, the other arguments and `options`). An identical later call returns the stored result at once, with a fresh `query_id` and `precomputed: { query_id, seconds_saved }` naming the call that computed it. A call identical to one being computed waits for it rather than searching again. This is how the background preparation of key moments (§1.6) hands its work to Claude's own calls. In memory only (at most 2,000 results); `job_status(action: "release")` drops a game's stored results.
- **Background work.** One worker thread runs the prepared `explain_moment` calls at KataGo priority `[prefetch].priority` (5): above the survey (0), below Claude's own calls (10), which run in between.
- Every tool call appends one line to `reviews/<game_id>/queries.jsonl` (or `reviews/_adhoc/queries.jsonl` when no game is in context). Every response carries `query_id` (`"q_<game_id>_<seq>"`); Claude notes the ids behind each claim.
- Whole-game analysis persists incrementally to `reviews/<game_id>/analysis.json`, so a crash or timeout leaves partial results available.

### 0.9 Errors
```ts
type Error = { error: { code: ErrorCode; message: string; details?: object; recoverable: boolean; suggestion?: string } }
type ErrorCode =
  | "engine_unavailable" | "engine_busy" | "human_model_unavailable" | "timeout" | "internal"
  | "invalid_sgf" | "unsupported_board_size" | "illegal_move" | "unknown_ref" | "bad_region" | "no_group_at_point"
  | "job_not_found" | "job_not_finished" | "budget_infeasible" | "validation_failed"
  | "bad_request" | "ogs_fetch_failed" | "wrong_color" | "no_candidates"
```
`bad_request` covers malformed arguments (wrong colour to move, a bad point or budget, a missing field). `ogs_fetch_failed`: the OGS game could not be downloaded (§1.2). `budget_infeasible`: a `{seconds}` budget or the survey sizing without a measured throughput. `validate_variations` reports validation problems in its `errors` list with `valid: false` rather than raising.
`suggestion` is written for Claude to act on ("start the engine with `katago-mcp serve`", "the job is 62% done; call job_status again in ~90 s").

---

## 1. Tools

Each tool is specified as **Purpose · Inputs · Output · Behavior · Cost · Errors**. Types use the conventions above; `?` marks optional fields; defaults follow `=`.

### 1.1 `engine_info`
**Purpose.** Report what this machine's engine is, how fast it is, and how long the main calls take here.

**Inputs.** `{ refresh_benchmark?: boolean = false }` — when true, runs a 20-second timed query and updates the sustained figure (only when idle).

**Output.**
```ts
{
  machine: string;                       // "m5pro" | "r5700xt" | "m2air" (config)
  server_version: string; contract_version: string; katago_version: string; backend: "metal" | "opencl" | "cuda" | "eigen";
  network: { name: string };
  human_model: { name: string; loaded: boolean };
  throughput: { visits_per_second_cold: number; visits_per_second_sustained: number; measured_at: string; method: "timed_query" | "unmeasured" };
  engine_status: string;                 // "ready", "starting (…)", "not started" or "failed: …"
  active_job: null | { job_id: string; progress: number };
  search: { root: number; line_node: number; plies: number; stability: number; local_solve: number; quick: number };   // §0.4, visits
  estimates: { explain_moment_seconds: number | null;                  // one explain_moment with nothing cached
               explain_moment_with_reading_seconds: number | null;     // ... with the reading check
               survey_minutes: number | null };
  student: { username: string; rank: string; peer: string; target: string; horizon: string; opponent: string };
  thresholds: object;                    // the [thresholds] table from config (§6)
}
```
**Behavior.** Also starts KataGo in the background when it is not running, so the model loads while Claude asks for the game. `estimates` count the searches of §1.6 at the machine's sizes and divide by the sustained visits/s; `null` without a measured throughput.
**Cost.** None (benchmark: ~20 s). **Errors.** `engine_unavailable`.

---

### 1.2 `sgf_summary`

> **Input forms (v0.2.1):** `sgf` may be raw SGF text, the path or file name of an `.sgf` on the server machine (looked up in `games/`), or an OGS game id / link (`12345678`, `ogs_12345678` as memory writes it, or the URL), which the server fetches from `https://online-go.com/api/v1/games/<id>/sgf` and caches as `games/ogs_<id>.sgf`. The same applies to `start_game_analysis.sgf` and `Position.sgf`. Errors: `ogs_fetch_failed`. Prefer link or path: SGF text retyped into a tool call by the model is unreliable for long games.

**Purpose.** Everything Claude needs for intake and blind self-review, from the SGF alone. No engine.

**Inputs.**
```ts
{ sgf: string; student_username?: string;   // = config student.username
  boards_at?: (number | "end")[] = [50, 100, 150, "end"]; ascii_options?: RenderOptions }
```

**Output.**
```ts
{
  game_id: string;                        // "ogs_<id>" when an OGS id is found in PC/GC/GN, else "sgf_<12 hex>"
  source: { ogs_game_id?: string; url?: string; date?: string; event?: string };
  board_size: 19;
  players: { B: { name: string; rank?: string }; W: { name: string; rank?: string } };
  student: null | { color: Color; rank?: string; matched_by: "PB" | "PW" | "config_default" };
  opponent: null | { color: Color; rank?: string; human_profile?: string };
  rules: { sgf_ru?: string; katago_rules: Rules; komi: number; handicap: number; setup: { B: Point[]; W: Point[] }; warnings: string[] };
  result: { raw: string; winner: Color | null; margin: number | null; method: "resign" | "score" | "time" | "forfeit" | "unknown" };
  moves: { count: number; passes: number[]; last_move: { number: number; color: Color; point: Point | "pass" } };
  time_settings?: { main: number; overtime: string; per_move_times_present: boolean };
  phases_rough: { opening_end: number; middlegame_end: number };   // move-count heuristic; refined by ownership in job_results
  capture_events: { move: number; by: Color; stones: number; points: Point[] }[];     // captures of ≥ 2 stones
  tension_events: { move: number; color: Color; group_point: Point; group_size: number }[];  // a group of ≥ 3 stones left with ≤ 2 liberties (the count itself is not reported)
  boards: { after_move: number; ascii: string }[];    // §4 format
  position_refs: { after_move: number; ref: PositionRef }[];   // for the boards listed
  warnings: string[];                     // e.g. "student username not found in PB/PW"
}
```
**Behavior.** Parses `HA`/`AB`/`AW`, `KM`, `RU`, `RE`, `PB`/`PW`, `BR`/`WR`, `DT`, `PC`, `GC`, `TM`, `OT`. Komi is read from `KM` (never assumed); a missing `KM` produces a warning and Japanese defaults (6.5 even, 0.5 handicap) — Claude must confirm with you. Handicap stones become `setup`. The student is matched by username, case-insensitive; if absent the tool still succeeds with `student: null` and a warning.
**Cost.** None. **Errors.** `invalid_sgf`, `unsupported_board_size`.

---

### 1.3 `start_game_analysis`
**Purpose.** Start the asynchronous survey: analyze every position of the game and compute the derived metrics of §3.

**Inputs.**
```ts
{ sgf: string; budget?: { visits_per_move: number } | Budget = { profile: "survey" };   // normalized like every other budget (§0.4)
  student_username?: string; game_id?: string;      // reuse an existing id (re-analysis)
  options?: { analyze_moves?: [number, number];     // restrict to a range
              reuse_existing?: boolean = true;       // reuse saved analysis of the same SGF at ≥ these visits
              prefetch?: boolean = true } }          // prepare the top key moments when the survey finishes
```
**Output.**
```ts
{ job_id: string; game_id: string; positions_total: number; visits_per_move: number;
  expected_seconds: number | null; started_at: string; reused: boolean; student_color: Color | null;
  profiles: Record<string, string>; state: string; query_id: string }
```
**Behavior.** One job per machine at a time; a second call returns `engine_busy` with the running job's id. Each position is searched at `visits_per_move` with ownership and ownership stdev; the played move's policy prior and the human-profile probabilities (peer, target, horizon, opponent) are recorded. Results are written incrementally (every 20 positions, and before the job reports `done`). Interactive tools may run during a job and are sent at a *higher* KataGo priority (10) than the survey (0), so they return promptly. When the survey finishes (or is reused) and `prefetch` is on, the server queues `explain_moment` for the story's top `[prefetch].moments` (3) key moments (§1.6): each moment's position before the move, the move played, no options. Nothing is shown; Claude reads it with the identical call. The CLI runs with `prefetch: false`.
**Cost.** `M · visits_per_move` visits. **Errors.** `invalid_sgf`, `engine_unavailable`, `engine_busy`.

---

### 1.4 `job_status`
**Inputs.** `{ job_id: string; action?: "status" | "cancel" | "release" = "status" }`
**Output.**
```ts
{ job_id: string; state: "queued" | "running" | "done" | "failed" | "cancelled";
  positions_done: number; positions_total: number; progress: number; current_move: number;
  elapsed_seconds: number; eta_seconds: number | null; partial_results_available: boolean; error?: Error["error"] }
```
`cancel` stops the engine work; partial results stay available. `release` frees a finished job's memory; its results stay in `reviews/<game_id>/` and are reused. **Errors.** `job_not_found`.

---

### 1.5 `job_results`
**Purpose.** The survey read as a teacher's first pass over the game: the story. Claude tells it to the student after their guided self-review, as the check of their reasoning (go-teaching §2–§3).

**Inputs.** `{ job_id: string; detail?: "story" | "moves" | "full" = "story"; range?: [number, number]; max_moments?: number = 6 }`

**Output (`story`)** — target ≤ 4k tokens.
```ts
{
  job_id: string; game_id: string; complete: boolean; positions_analyzed: number; visits_per_move: number; state: string;
  game: { student_color: Color; handicap: number; komi: number; rules: Rules; result: string;
          reconciliation: { status: "ok" | "mismatch" | "n/a"; engine_final_score: number | null; sgf_margin: number | null; diff: number | null } };
  phases: { opening: [number, number]; middlegame: [number, number] | null; endgame: [number, number] | null; method: string };
  lead: { move: number; lead: number }[];                         // student perspective, every 20 moves and at the end (§3.7)
  group_events: GroupEvent[];                                     // §3.6, in move order
  swings: Swing[];                                                // §3.7, in move order
  decisive: null | { move: number; by: "student" | "opponent"; basis: "winrate" | "score"; before: number; after: number };
  last_chance: null | { move: number; played: Point; best: Point; eval_if_best: number; eval_actual: number; basis: "winrate" | "score" };
  points_lost: { you: PhaseLoss; opponent: PhaseLoss };           // PhaseLoss = { opening, middlegame, endgame, total, per_move, moves }
  moments: Moment[];                                              // §3.3, ranked by the first move's loss, ≤ max_moments
  positives: { move: number; played: Point; points_lost: number; peer_probability: number; target_probability: number }[];   // §3.9
  reliability: { low_visit_positions: number[]; unstable_moments: string[] };
  profiles: Record<string, string>;
}
type GroupEvent = { move: number; by: "you" | "opponent"; group: string /* "B lower left corner (7)" */; whose: "yours" | "opponent's";
                    anchor: Point; size: number; from: "alive" | "unsettled" | "dead"; to: "alive" | "unsettled" | "dead";
                    ownership_before: number; ownership_after: number };
type Swing = { move: number; by: "you" | "opponent"; played: Point; best: Point; points_lost: number; lead_after: number;
               region: string | null; phase: string; gave_back?: number };   // gave_back: the student's next move's loss, after an opponent swing
type Moment = {
  id: string;                                   // "M1", "M2", …
  moves: [number, number]; student_moves: number[];   // the sequence: first and last move (both colors), the student's costly moves in it
  move: number; played: Point; best: Point; points_lost: number;     // the first move
  net_loss: number;                             // the student's lead before the first move minus after the sequence
  position_ref_before: PositionRef;
  region: string; phase: "opening" | "middlegame" | "endgame";
  lead_before: { score_lead: number; label: "ahead" | "close" | "behind" };
  acceptable_moves: Point[]; played_in_acceptable_set: boolean;      // §3.2
  human: { played: { peer?: number; target?: number }; best: { peer?: number; target?: number } };
  findable_move: Point; findable_probability: number;                // §3.10
  opponent_best_reply: null | { move: Point; character: "tenuki" | "local_calm" | "local_sharp" };   // §3.13
  group_events: GroupEvent[];                   // those from the first move to group_event_hold moves after the last
  stability: "stable" | "unstable" | "unknown"; // §3.11
  prepared: "done" | "running" | "queued" | "failed" | "no";        // explain_moment for this moment (§1.6, the move played)
};
```
**Output (`full`)** adds `moves: MoveRow[]` for every position: `{ n, color, move, score_before, score_after, points_lost, best, prior_played, prior_best, in_acceptable_set, visits, position_ref }`. **Output (`moves`)** returns `MoveRow[]` for `range` only.
**Behavior.** Survey numbers come from a short search; `explain_moment` searches deeper, and its numbers stand when the two disagree. A partial survey returns the story of the analyzed prefix with `complete: false`.
**Errors.** `job_not_found`, `job_not_finished` (fewer than two positions analyzed), `bad_request` (unknown `detail`).

---

### 1.6 `explain_moment`
**Purpose.** Everything a teacher needs to explain one move against the best one, in one call: the evidence for a key moment, or for a follow-up question ("why is the blue move best?", "what about X here?").

**Inputs.**
```ts
{ position: Position;                // before the move: { job_id, move_number: N − 1 } for move N of a surveyed game
  move?: Point | string;             // = the move played next in the game; required for any other position
  options?: { expected_line?: (string | LineStep)[];   // the moves the student expected after `move`, opponent first; checked for legality now
              reading?: boolean;                       // run the reading check; default: when expected_line is given or the opponent's best reply is local_sharp
              deep?: boolean = true;                   // the stability re-run
              perspective?: Color };
  background?: boolean = false }     // queue it and return at once (a position of a surveyed game only)
```
**Output.**
```ts
{
  position_ref: PositionRef; move_number: number | null; to_move: Color; perspective: Color;
  best: { move: Point; score_lead: number; human: { peer: number; target: number } };
  move: { move: Point; score_lead: number; points_lost: number;          // mover's perspective
          verdict: "best" | "as_good" | "mistake";                        // as_good = within the acceptable set (§3.2)
          human: { peer: number; target: number } };
  candidates: { move: Point; score_lead: number; human: Record<string, number>; in_acceptable_set: boolean }[];   // top 5
  findable: null | { move: Point; target: number };                       // the acceptable move the target rank plays most
  stability: null | { stable: boolean; first_search_best: Point; deeper_search_best: Point; score_moved: number; visits: number };
  lines: { best: LineSummary; move: LineSummary | null };                 // move: null when it is the best move
  comparison: null | Comparison;                                          // §1.10: a = the end of the move's line, b = the best line's
  purpose: { best: Purpose | null; move: Purpose | null };                // null for a pass
  reading: null | { source: "stated" | "mixed" | string; line: string[]; misread: Misread | null; note: string | null };   // §1.12
  notes: string[];                                                        // what not to claim (below)
  query_ids: Record<"search" | "stability" | "human" | "line_best" | "line_move" | "comparison" | "purpose_best" | "purpose_move" | "reading", string>;
  seconds_used: number; query_id: string; precomputed?: { query_id: string; seconds_saved: number }
}
type LineSummary = { line: string[];                                      // ["BQ7","WR8",…], ready for a dashboard branch
  forced: boolean[];                                                      // per reply after the first move
  stop_reason: string; free_at_end: object | null;
  resistance: { at_ply: number; move: Point; probability: number; loss_for_resister: number; refutation: string[] }[];   // at_ply counts from line[0]
  end: { position_ref: PositionRef; score_lead: number; weak_groups: Side; sente: "you" | "opponent"; next_move: Tempo; capture_races?: CaptureRace[] };
  query_id: string };
type Purpose = { threat; tenuki_value; defense: { opponent_local_move; value; groups: { label; anchor; ownership_if_attacked }[] };
                 reply: ReplyCharacter; left_behind; query_id };          // from intent_probe (§1.11)
```
With `background: true` the output is `{ queued: true, state, position_ref, move, estimate_seconds }`.
**Behavior.** In order, each through the tool Claude would call, so every part is a stored result of its own (§0.8): `analyze_position` at `root` (top 6 candidates, peer and target probabilities); with `deep`, `analyze_position` at `stability`, whose candidates and best move are used from then on (`stability.stable` = same best move and the score moved < `stability_margin`, 0.5; a changed best move adds a note); `human_move_distribution` for both moves; `forced_line` from the best move and from `move` (§1.9); `terminal_features` of the two ends (§1.10); `intent_probe` on both moves (§1.11); and the reading check, `expectation_probe(move, expected_line)` (§1.12). `move.points_lost` uses the move's candidate score when it was searched, else the score after it in its forced line. `notes`: the ends differ by less than 2 points (no "why one is better"); a resistance that costs the opponent nothing (`loss_for_resister ≤ 0`: the line depends on the opponent cooperating); the deeper search changed the best move. Background requests run on the prefetch worker (§0.8); `job_results` reports each key moment's state as `prepared`. An identical later call returns the stored result.
**Cost.** About `root × (1 + stability)` plus 78 searches at `line_node` with 8 plies (≈ 95 s at 650 visits/s on the Pro's sizes), plus `2·plies + 5` for the reading check; `engine_info.estimates` gives the figure for this machine. **Errors.** `bad_request` (no move and no game move to default to, `background` outside a surveyed game), `wrong_color` / `illegal_move` (`move` or `expected_line`), `no_candidates`, `unknown_ref`.

---

### 1.7 `analyze_position`
**Purpose.** Candidates, evaluation, ownership and group statuses at one position.

**Inputs.**
```ts
{ position: Position; budget?: Budget = { profile: "root" };
  options?: { wide_root_noise?: number = 0.04;          // diversity of candidates; 0 = KataGo default
              max_candidates?: number = 8; pv_len?: number = 8;
              human_profiles?: string[] = ["peer","target","horizon"];
              include_ownership?: boolean = false; include_ownership_stdev?: boolean = false;
              include_groups?: boolean = true;           // computed from ownership even when the array is not returned
              perspective?: Color } }
```
**Output.**
```ts
{
  position_ref: PositionRef; to_move: Color; perspective: Color; visits_used: number; seconds_used: number; cached: boolean; stopped_early: boolean;
  root: { score_lead: number; winrate: number; score_stdev: number; visits: number };
  candidates: Candidate[];
  acceptable_set: { margin: number; moves: Point[] };
  policy_top: { move: Point; prior: number }[];        // raw network policy, top 8
  ownership?: number[]; ownership_stdev?: number[];    // 361 floats, Black-positive, §4.3 order
  groups?: Group[]; capture_races?: CaptureRace[];       // capture_races only when one is detected
  resolved_profiles: Record<string, string>; query_id: string;
}
type Candidate = { move: Point | "pass"; order: number; visits: number; prior: number; winrate: number; score_lead: number; score_stdev: number; lcb: number;
                   pv: Point[]; human: Record<string, number>; in_acceptable_set: boolean };
type Group = { id: string; color: Color; stones: Point[]; size: number; liberties?: number; liberty_points?: Point[];   // liberties only for groups in a capture race (§3.14) or when asked for
               mean_ownership: number; status: "alive" | "unsettled" | "dead"; region: string; label: string };   // label e.g. "W lower right corner (5)"
type CaptureRace = { groups: { label: string; anchor: Point; color: Color; liberties: number; liberty_points: Point[] }[] };   // §3.14
```
**KataGo mapping.** `wideRootNoise` via `overrideSettings`; `includeOwnership`, `includeOwnershipStdev`, `includePolicy`, `includePVVisits`; human probabilities via one query per profile with the human model's profile set in `overrideSettings` (exact key per the installed version's Analysis_Engine.md).
**Cost.** `budget`. **Errors.** `illegal_move`, `unknown_ref`, `engine_unavailable`.

---

### 1.8 `analyze_line`
**Purpose.** Play out a sequence — forced moves, engine replies, or a mix — and report what happens. The general line tool: the student's own lines, plan tests (`restrict`), ad-hoc continuations and punishability (`refutation_probability`). The lines of an explanation come from `forced_line` (§1.9) through `explain_moment` (§1.6).

**Inputs.**
```ts
{ position: Position; budget?: Budget = { profile: "line_node" };   // per searched node
  line: LineStep[];                                      // at least one step
  follow_pv_plies?: number;                              // = [search].plies (8); engine-vs-engine continuation after `line`
  options?: { restrict?: { color: Color; region: Region; until_ply: number };   // confine one side's searched moves (plan tests)
              opponent_profile?: string = "opponent";   // human probability of each engine-chosen reply by the opponent
              punish_plies?: number = 3;                 // how many opponent plies enter refutation_probability
              ownership_at_end?: boolean = true; groups_at_end?: boolean = true; compare_to_best?: boolean = true; perspective?: Color } }
type LineStep = { color: Color; move: Point | "pass" } | { color: Color; engine: true }    // forced move, or "engine chooses"
```
**Output.**
```ts
{
  start: { position_ref: PositionRef; score_lead: number; to_move: Color };
  nodes: LineNode[];
  end: { position_ref: PositionRef; score_lead: number; winrate: number; ownership?: number[]; groups?: Group[]; capture_races?: CaptureRace[]; captures: { B: number; W: number } };
  summary: { score_start: number; score_end: number; total_change: number;
             largest_drop: { ply: number; color: Color; move: Point; delta: number };
             vs_best?: { best_first_move: Point; score_after_best_root_estimate: number; gap_vs_first_step: number; note: string } };   // root estimate only; play the best move out with a second analyze_line for the contrast
  refutation_probability?: { by_profile: string; per_ply: { ply: number; move: Point; probability: number }[]; product: number };
  legality: "ok"; visits_used: number; seconds_used: number; query_id: string;
}
type LineNode = { ply: number; color: Color; move: Point | "pass"; forced: boolean; position_ref: PositionRef;
                  eval_after: { score_lead: number; winrate: number; score_stdev: number; visits: number };
                  delta: number;                          // change from the previous node, perspective
                  alternatives?: { move: Point; score_lead: number; visits: number }[];   // top 3 at engine-chosen nodes
                  human_probability?: number };           // opponent_profile's probability of this move, when the mover is the opponent
```
**Behavior.** A forced move is appended without search; the position after it is searched to evaluate it and, when the next step is `engine`, to choose the reply. After the last step, `follow_pv_plies` engine-vs-engine plies continue, then the final position is evaluated. Searches per call = engine-chosen plies + follow-through plies + 1. `restrict` uses KataGo `allowMoves` for that color inside the region (plus `pass`) until `until_ply`. `refutation_probability.product` multiplies the opponent's human probabilities over the first `punish_plies` opponent plies — the chance a player of that rank finds the whole punishment.
**Cost.** `(searched nodes) × budget`. **Errors.** `illegal_move` (with ply), `unknown_ref`, `bad_region`.

---

### 1.9 `forced_line`
**Purpose.** A human proof: from a move, the narrow line of must-moves ending in a position the student can evaluate, with the opponent's natural resistance and its refutation.

**Inputs.**
```ts
{ position: Position;                                       // before the move
  move: Point | string;                                     // "Q7" or "BQ7"; must be the side to move
  budget?: Budget = { profile: "line_node" };
  options?: { max_plies?: number = [search].plies (8);
              extend?: "local" | "forced" = "local";        // "forced": stop at the first reply that is not forced;
                                                            // "local": also follow non-forced best moves while they stay local (≤ local_radius of the last three moves)
              forced_margin?: number = 3;                   // a reply is forced when the second-best loses more than this
              human_margin?: number = 1;                    // a legible-profile move within this of the best replaces it
              legible_profile?: string = "target"; resistance_profile?: string = "opponent";
              resistance_nodes?: number = 2; refutation_plies?: number = 3; perspective?: Color } }
```
**Output.**
```ts
{ start: { position_ref: PositionRef; to_move: Color; score_lead: number; best_move: Point };
  move: { color: Color; move: Point; score_after: number; loss_vs_best: number };
  perspective: Color; line: string[];                       // ["BQ7","WR8",…] — ready for a dashboard branch
  nodes: { ply: number; color: Color; move: Point; forced: boolean; chosen_by: "engine" | "human"; engine_best: Point;
           gap_to_second: number | null; second_best: Point | null; score_after: number; legible_probability: number;
           resistance?: { move: Point; probability: number; loss_for_resister?: number; refutation?: string[]; score_end?: number; note?: string } }[];
  stop_reason: "not_forced" | "quiet" | "max_plies" | "pass";
  free_at_end: null | { side: Color; who: "you" | "opponent"; best_move: Point; second_best: Point; gap: number; best_is_local: boolean };
  end: Features;                                            // §1.10
  visits_used: number; seconds_used: number; query_id: string }
```
**Behavior.** At each node the position is searched, then searched again with the top move avoided (`avoidMoves`, `untilDepth` 1) so the second-best has real visits; `gap_to_second` is how much the best beats it for the side to move, and the reply is `forced` when the gap exceeds `forced_margin`. The move played is the engine's best, replaced by the `legible_profile` human model's favourite among candidates within `human_margin` (lines stay human-legible, not full of probes). At the first `resistance_nodes` opponent nodes, the `resistance_profile` model's most likely move, when different, is played and refuted by `refutation_plies` engine moves. `extend: "local"` exists because in fights the best reply is often only 1–3 points better than the second: a strictly forced line stops after one move, before any group's fate is visible; the local extension follows the fight until the best move is elsewhere (`quiet`), with `forced: false` on the nodes that were a choice. Run it from the better move E and from the played move G, then `terminal_features(end of G, compare_to: end of E)`.
**Cost.** About 2 searches per node, plus 2 + `refutation_plies` per resistance, plus 2 for the end features (1 more when cut off by `max_plies`). **Errors.** `bad_request` (wrong colour, bad point), `illegal_move`, `unknown_ref`.

---

### 1.10 `terminal_features`
**Purpose.** Say what an end position *is*, and what is concretely different between two end positions — the "why" of an explanation, instead of a score delta.

**Inputs.** `{ position: Position; compare_to?: Position; budget?: Budget = { profile: "line_node" }; options?: { perspective?: Color } }`

**Output.**
```ts
{ a: Features; b?: Features;                                // b and comparison only with compare_to
  comparison?: {
    score_diff: number;                                     // b − a, perspective (the size, not the reason)
    groups_changed: { group: string; anchor: Point; in_a: string; in_b: string; ownership_a: number | null; ownership_b: number | null }[];
    territory_changed: { region: string; label: string; a: Side; b: Side; you_diff: number; opponent_diff: number }[];
    territory_total: { a: Side; b: Side };
    sente: { a: "you" | "opponent"; b: "you" | "opponent"; changed: boolean };
    tempo: { a: Tempo; b: Tempo }; weak_groups: { a: Side; b: Side } };
  visits_used: number; seconds_used: number; query_id: string }
type Side = { you: number; opponent: number };
type Tempo = { side_to_move: Color; best_move: Point; value: number; region: string };   // value = best move − pass, for the side to move
type Features = { position_ref: PositionRef; to_move: Color; perspective: Color; score_lead: number;
  groups: { label: string; anchor: Point; color: Color; size: number; status: string; mean_ownership: number; liberties?: number }[];   // ≥ 2 stones
  weak_groups: Side;                                        // unsettled groups per side
  territory: Record<string, { label: string; you: number; opponent: number }>;   // nine standard regions
  territory_total: Side;
  sente: { holder: "you" | "opponent"; holder_color: Color; side_to_move_must_answer: boolean };
  tempo: Tempo; capture_races?: CaptureRace[] };
```
**Behavior.** One search with ownership plus one after a pass by the side to move, at `budget`. `territory` sums ownership over the points that are not the owner's own stones (empty points, and the other side's stones counted as dead). `tempo.value` is the price of the move: what the side to move gains by playing its best move instead of passing. `sente.holder` is the side to move — it chooses freely — unless the caller knows it still has to answer (`forced_line` passes that when a line is cut off by `max_plies` while forced); a big local best move is not treated as an answer, because it may be an attack. In the comparison a group is matched by its stones; a status change is reported only when its ownership moved by ≥ `group_change_min` (0.2) or the group was captured; regions are reported when either side's territory differs by ≥ `territory_diff_min` (2). Convention: `a` = the end of the played line, `b` = the end of the better line.
**Cost.** 2 searches per position. **Errors.** `unknown_ref`, `illegal_move`.

---

### 1.11 `intent_probe`
**Purpose.** What a move was for, played out. Every move is a threat, a defense or a value claim; three cheap probes say which, and whether it worked. `explain_moment` runs it on both moves (§1.6).

**Inputs.** `{ position: Position /* before the move */; move: Point | string; budget?: Budget = { profile: "line_node" }; options?: { neighborhood_radius?: number = 3 } }`

**Output.**
```ts
{ position_ref: PositionRef; player: Color; perspective: Color;          // perspective = the player
  move: Point; best_move: Point;
  score: { before: number; after_move: number; after_best: number; loss: number };
  reply: ReplyCharacter;                                  // §3.13, the opponent's best answer to the move
  threat: { follow_up: Point; value: number; opponent_local_answer: Point };   // what the move threatened (local swing)
  tenuki_value: number;                                   // how much the opponent gains by not answering locally
  defense: { opponent_local_move: Point | null; value: number | null;           // what the move prevented
             groups: { label: string; anchor: Point; ownership_before: number; ownership_after_move: number; ownership_if_attacked: number }[] };
  left_behind: { label: string; anchor: Point; ownership_before: number; ownership_after_reply: number }[];   // when the reply is elsewhere
  better_move: { gote: boolean | null; groups_that_differ: string[] | null };
  risk: { score_lead_before: number; stdev_move: number; stdev_best: number | null;
          human: Record<"peer" | "horizon", { move: number; best: number | null }> };
  visits_used: number; seconds_used: number; query_id: string }
```
**Behavior.** With X the player, G the move, E the engine's best, all scores from X's side, `N(p)` the empty points within `neighborhood_radius` of p (plus pass):
- *Threat.* `threat.value` = [after G, the opponent passes, X's best move in N(G)] − [after G, the opponent's best move in N(G)]: the local swing, the size of what G threatened. `tenuki_value` = [opponent's best answer in N(G)] − [opponent's best move anywhere]: positive when ignoring G was better for the opponent.
- *Defense.* X passes instead; the opponent's best move D in N(G); `defense.value` = [after G] − [after pass and D]. `groups` are X's groups (≥ 2 stones) within `defend_radius` of G, with their ownership before, after G, and after the pass and D.
- *Reply character* (§3.13) of the opponent's best answer R to G. When R is elsewhere, `left_behind` holds X's groups near R and their ownership after R.
- *Better move.* After E: is the best reply elsewhere (`gote`), and which groups of ≥ 3 stones differ by ≥ `group_change_min` between after G and after E.

**Cost.** 7–8 searches at `budget`. **Errors.** `bad_request` (pass, wrong colour), `illegal_move`, `unknown_ref`.

---

### 1.12 `expectation_probe`
**Purpose.** Find the misread: play out the line the student expected (or the one a player of their rank reads), check it move by move, and return the first move where it stops working and the move they never considered.

**Inputs.**
```ts
{ position: Position /* before the move */; move: Point | string; budget?: Budget = { profile: "line_node" };
  options?: { expected_line?: (string | LineStep)[];     // the student's line after the move, opponent first ("WD10", "BC10", …)
              plies?: number = [search].plies (8);           // imagined moves after the move
              profile?: string = "peer";                  // who fills in the moves after expected_line runs out (both sides)
              misread_margin?: number = 3; refutation_plies?: number = 4; perspective?: Color } }
```
**Output.**
```ts
{ position_ref: PositionRef; perspective: Color; profile: string;
  move: { color: Color; move: Point; loss_vs_best: number };
  expected_source: "stated" | "mixed" | string;           // "peer" when no line was given
  nodes: { ply: number; color: Color; move: Point; source: "stated" | string; engine_best: Point; loss: number;
           human_probability: number | null; score_after: number }[];
  line: string[];                                         // the imagined line up to the misread
  misread: null | { ply: number; color: Color; whose: "you" | "opponent"; expected: Point; never_considered: Point; loss: number;
                    refutation: string[];                  // starts with the move never considered
                    refutation_end: { position_ref: PositionRef; score_lead: number; capture_races: CaptureRace[] };
                    line_to_here: string[] };
  note: string | null; visits_used: number; seconds_used: number; query_id: string }
```
**Behavior.** After the move, each imagined move comes from `expected_line` while it lasts, then from the `profile` human model's favourite legal move, for both sides. Each is scored against the engine's best at that node (the candidate's score when it was searched with a real share of visits, else one more search): `loss` for the side that plays it. The first move losing more than `misread_margin` is the misread: `whose: "opponent"` means the student expected a reply the opponent would not play — `never_considered` is the reply they missed; `whose: "you"` means the student's own follow-up fails. The refutation is `never_considered` followed by `refutation_plies` engine moves; its end carries `capture_races`, where liberty counts belong. No misread within `plies` means the reading holds: the mistake is about value (size, sente, safety), not reading.
**Cost.** 1–2 searches per imagined move, plus `refutation_plies` + 1. **Errors.** `bad_request` (an `expected_line` step for the wrong colour), `illegal_move`, `unknown_ref`.

---

### 1.13 `pass_probe`
**Purpose.** Measure urgency: how much a move was worth locally, and what the opponent would take if the mover tenukis.

**Inputs.**
```ts
{ position: Position;                                    // the position BEFORE the move in question
  player: Color; move?: Point; budget?: Budget = { profile: "root" };   // per search
  options?: { rank_regions?: boolean = false;            // per-region opponent threats via restricted searches (adds up to 9 searches)
              perspective?: Color } }
```
**Output.**
```ts
{
  position_ref: PositionRef; player: Color; perspective: Color;
  score_if_pass: number;                                  // after `player` passes and the opponent replies best
  opponent_reply_to_pass: { move: Point; region: string };
  score_after_move: null | { move: Point; score: number };
  score_after_best: { move: Point; score: number };
  local_value: { played: number | null; best: number };  // score_after_X − score_if_pass
  urgency?: { region: string; label: string; best_move_there: Point; value: number }[];   // the player's best move confined to the region, and its value over passing; sorted by value
  visits_used: number; seconds_used: number; query_id: string;
}
```
**Behavior.** `score_if_pass` is the root evaluation of the position after a forced pass. `urgency` (when `rank_regions` is true) runs nine searches at one third of the root budget, each confining the *player's* move to one standard region with `allowMoves`; `value = score_after_best_in_region − score_if_pass` from the player's perspective, so a region where a group is in danger scores high because passing would lose it. This is the direct urgent-vs-big ranking. Interpretation is Claude's: a large `local_value.best` in the region you left is the "urgent before big" signature; a top-ranked region far from the played move is the "slow" signature.
**Cost.** 3 searches (+9 with `rank_regions`). **Errors.** `illegal_move`, `unknown_ref`.

---

### 1.14 `swing_value`
**Purpose.** Endgame counting and sente/gote classification.

**Inputs.** `{ position: Position; points: Point[]; budget?: Budget = { profile: "root" }; options?: { local_radius?: number = 4; perspective?: Color } }` (1–6 points, ranked together)

**Output.**
```ts
{ position_ref: PositionRef; to_move: Color; perspective: Color;
  results: { point: Point;
             black_first: { score_after: number; best_reply: Point | "pass"; reply_is_local: boolean };
             white_first: { score_after: number; best_reply: Point | "pass"; reply_is_local: boolean };
             swing: number;                                          // black_first.score_after − white_first.score_after (Black-positive), then perspective-adjusted
             sente_gote: { for_black: "sente" | "gote" | "unclear"; for_white: "sente" | "gote" | "unclear" } }[];
  ranked: Point[];                                                   // by swing, descending
  visits_used: number; seconds_used: number; query_id: string }
```
**Behavior.** Two searches per point (Black plays there / White plays there). `reply_is_local` = the best reply lies within `local_radius` (Chebyshev) of the point; local reply → the move was sente for the mover. `unclear` when the reply's evaluation gap to the best non-local move is under 0.5 points.
**Cost.** `2 × points × budget`. **Errors.** `illegal_move` (a point occupied or illegal for either color is skipped with a note), `unknown_ref`.

---

### 1.15 `local_solve`
**Purpose.** Classify a group as alive, dead or unsettled by confining both sides to a region and letting each side move first.

**Inputs.**
```ts
{ position: Position; group_point: Point;               // any stone of the target group
  region?: Region;                                       // = bounding box of the group + 2, auto-expanded to include all its liberties
  budget?: Budget = { profile: "local_solve" };          // per search
  options?: { max_plies?: number = 20; allow_tenuki?: boolean = true } }   // tenuki = pass allowed inside the restriction
```
**Output.**
```ts
{
  target: { color: Color; stones: Point[]; label: string }; region_used: Region;
  attacker_first: SolveRun; defender_first: SolveRun;
  status: "alive" | "dead" | "unsettled" | "unclear";   // alive/alive → alive; dead/dead → dead; alive/dead → unsettled; either run unclear → unclear
  confidence: "high" | "medium" | "low";               // from ownership margins beyond the thresholds and early-stop stability
  caveats: string[];                                    // "group touches the region boundary at …: outside liberties may matter", "ko present", "seki-like ownership near 0"
  visits_used: number; seconds_used: number; query_id: string;
}
type SolveRun = { sequence: Move[]; final_position_ref: PositionRef; final_group_ownership: number; final_status: "alive" | "unsettled" | "dead"; plies: number; stopped_reason: "stable" | "max_plies" | "group_captured" | "both_passed" };
```
**Behavior.** Both sides restricted to `region` (`allowMoves` with `untilDepth` = `max_plies`), `pass` allowed when `allow_tenuki`. Each run follows the engine's play until the target group's mean ownership has held within 0.1 for three plies, or another stop reason. Status thresholds from config (§6). A local verdict can be wrong when outside liberties, ko or a second weak group interact; the caveats say when, and Claude must repeat them when teaching.
**Cost.** Up to `2 × max_plies × budget`, usually far less. **Errors.** `no_group_at_point`, `bad_region`, `unknown_ref`.

---

### 1.16 `human_move_distribution`
**Inputs.** `{ position: Position; profiles?: string[] = ["peer","target","horizon","opponent"]; moves_of_interest?: Point[]; top_n?: number = 8 }`
**Output.**
```ts
{ position_ref: PositionRef; to_move: Color;
  profiles: Record<string, { top: { move: Point; probability: number }[]; moves_of_interest: Record<Point, number>; entropy: number }>;
  resolved_profiles: Record<string, string>; query_id: string }
```
**Behavior.** One NN evaluation per profile, no search. Probabilities are what a player of that rank would play here — they inform learnability and punishability, never correctness. **Errors.** `human_model_unavailable`, `unknown_ref`.

---

### 1.17 `render_board`
**Inputs.**
```ts
{ position: Position;
  options?: RenderOptions }
type RenderOptions = { mark_last?: boolean = true; overlay?: null | "ownership" | "ownership_stdev" | "policy" = null;
                       highlight?: Point[]; region_box?: Region; label_low_liberties?: boolean = false;  // list groups with ≤ 3 liberties (off by default since v0.3)
                       coordinates?: boolean = true;
                       line?: Move[] };                  // ≤ 35 moves from `position`, colors alternating from its side to move (v0.3.1)
```
**Output.** `{ position_ref: PositionRef; to_move: Color; last_move: Move | null; captures: { B: number; W: number }; ascii: string; overlay_ascii?: string; legend: string; low_liberty_groups?: { label: string; liberties: number; liberty_points: Point[] }[];
  line?: { moves: Move[]; first: Color; notes: string[]; end_ref: PositionRef } }`
**Behavior.** Since v0.5 the review shows positions and lines on the live page (`dashboard_row`, §1.19); `render_board` is the chat fallback when the page cannot be used, and remains the tool for overlays. Format in §4. Overlays need ownership/policy: cached if present, else a `quick` search. With `line`, `ascii` is a numbered diagram of the sequence (§4.1) instead of the position with its last move: no engine call. `position_ref`, `to_move`, `last_move`, `captures` and any overlay still describe `position`; `line.end_ref` is the position after the line (render or analyze it next, e.g. for a line longer than 35 moves). `notes` carries what the grid cannot: `"4 at 1"` (a move on a point already labelled), `"2 captures 1 stone (1 among them)"`, `"5: W passes"`. **Errors.** `unknown_ref`, `illegal_move`, `wrong_color` (a `line` move of the side not to move), `bad_request` (a `line` longer than 35 moves).

---

### 1.18 `validate_variations`
**Purpose.** The gate to the dashboard: check every branch for legality, fill in evaluations, and assemble the complete dashboard data blob with a checksum. Nothing reaches the dashboard that the server has not played and evaluated.

**Inputs.**
```ts
{
  job_id: string;
  episodes: DashboardEpisodeSpec[];                       // the key moments, then the follow-up questions
  summary: DashboardSummary;                              // copied verbatim into the blob; rendered by the template
  options?: { evaluate_missing?: boolean = true; budget?: Budget = { profile: "line_node" } }
}
type DashboardEpisodeSpec = {
  id: string; kind?: "moment" | "question" = "moment";     // "question": a follow-up the student asked
  moves: [number, number]; title: string; points_lost?: number;
  commentary: { at_move: number; text: string }[];
  branches: { id: string; label: string; from_move?: number; moves: Move[];
              kind?: "as_played" | "expected" | "misread" | "better" | "resistance" | "fix" | "question";
              from_branch?: string; at_ply?: number }[];   // from_branch: start after at_ply moves of an earlier branch (then from_move is the parent's)
  comparison?: { a: string; b: string };                    // two branch ids; the server compares their end positions (§1.10)
  quiz?: { at_move: number; type: "move" | "status"; candidates?: Point[];            // the server always adds the actual move and the peer move
           status?: { group_point: Point; solve_query_id: string } };
  takeaway?: string;                                        // the student's own sentence: what they will do differently
};
type DashboardSummary = {                              // the shape the review-dashboard skill writes and the template reads
  story?: string;
  takeaways?: { momentId: string; title?: string; takeaway: string }[];   // default: the moments' own takeaway fields
  selfReview?: { surprises?: string[]; shifts?: string[]; successes?: string[] };   // the student's self-review, each line with what the check found
  strengths?: string[];
  nextGame?: string;
  reliability?: string;
};
```
**Output.**
```ts
{ valid: boolean;
  errors: { episode_id: string; branch_id?: string; ply?: number; move?: string; code: "illegal_move" | "wrong_color" | "bad_from_move" | "bad_kind" | "unknown_query" | "bad_quiz" | "bad_branch_parent" | "bad_comparison"; message: string }[];
  warnings: string[];                                            // e.g. "branch B2 of M3 never diverges from the game"
  dashboard_data?: string;                                       // compact JSON per §5, only when valid
  sha256?: string; size_bytes?: number; query_id: string }
```
**Behavior.** A branch with `from_branch` is expanded to the parent's first `at_ply` moves followed by its own, from the parent's `from_move`, and exported with `parentBranch` and `branchPly` (plies in errors count from the parent's start). With `comparison`, both branch ends get `terminal_features` from the student's perspective and the episode exports `comparison: { a, b, aLabel, bLabel, scoreDiff, groups, territory, territoryTotal, sente, nextMove, weakGroups }`; each branch's `kind` and the `takeaway` are exported as written. For each branch: the position after `from_move` is taken from the job; every move is legality-checked with alternating colors from that position's side to move; evaluations per node come from cache or new searches at `budget`. Quiz candidates get `pointsLost` relative to the best move (the actual move's from the survey's after-position score, the same definition as §3.1) and `labels ⊆ {actual, peer, best}`; `status` quizzes pull the verdict from the cited `local_solve` query. Ownership is encoded at episode roots and branch ends. Game metadata, moves, setup, score series (student perspective), the best move at every move and the episodes' numeric fields come from the server's records; all text fields are copied verbatim from the inputs. On any error, nothing is exported. Claude writes `dashboard_data` verbatim to a file; `build_dashboard.py` recomputes the checksum and refuses to build on mismatch.
**Cost.** Searches for uncached branch nodes only. **Errors.** `job_not_found`, `job_not_finished`; validation problems are returned as `valid: false` with the details in `errors` (§0.9).

---

### 1.19 `dashboard_row`
**Purpose.** Make a row for the live review page: the game record, or a board (a position of the game with an optional line, marked points, a question, and an answer the student gives by clicking). Claude writes the row unchanged to the page's `db` with ArtifactData; the page checks its SHA-256 and shows it. No engine.

**Inputs.**
```ts
{ kind: "game" | "board";
  game: string;                          // a job_id, or the OGS link/id or .sgf name (before the survey exists)
  board?: { id?: string;                 // [A-Za-z0-9_-]; default "b01", "b02", … in the order made
            title: string;               // the list label
            text?: string;               // the question or note, one or two sentences
            at_move: number;             // the position after this move (0 = setup only)
            line?: (string | LineStep)[];// moves from there, numbered on the board; colours alternate from the side to move
            highlight?: Point[];         // marked with a square
            ask?: "move" | "line";       // the student answers by clicking one move, or a sequence, starting with ask_color
            episode?: string;          // the key moment it belongs to ("M1")
            from_game?: string } }          // a past game (OGS link/id or .sgf name): the position comes from it; at_move counts in it
```
**Output.**
```ts
{ collection: "review" | "boards"; doc_id: "game" | string;
  row: GameRow | BoardRow;              // write as the document's data exactly as returned
  write: string; query_id: string }
type GameRow = { kind: "game"; game_id: string; you: Color | null; players: { B: { name; rank }; W: { name; rank } };
                 handicap: number; komi: string /* "6.5": no float formatting to disagree on */; rules: Rules; result: string;
                 date: string | null; first_to_move: Color; setup: { AB: Point[]; AW: Point[] }; moves: string[] /* "BQ16" */;
                 sha256: string };
type BoardRow = { kind: "board"; id: string; seq: number; game_id: string; title: string; text: string; at_move: number;
                  line: string[]; highlight: Point[]; ask: "move" | "line" | null; ask_color: Color | null; episode: string | null;
                  from_game?: { game_id: string; date: string | null; you: Color | null; opponent: string | null;
                                setup: { AB: Point[]; AW: Point[] }; moves: string[] /* that game's first at_move moves */ };
                  sha256: string };
```
**Behavior.** `line` is legality-checked from the position after `at_move` (`illegal_move`, `wrong_color` with the ply); `ask_color` is the side to move after `line`. `sha256` is the SHA-256 of the canonical JSON (§5) of the row without `title`, `text` and `sha256`: wording may be changed when writing, nothing else. The game row of a job and of its SGF are identical. Every row is appended to `reviews/<game_id>/dashboard_rows.jsonl`, which also numbers the boards (`seq`). With `from_game` the row still belongs to this review (`game_id`, `seq`, the log) and carries the past game's record up to `at_move`, which the page replays instead of the review's game (a recall quiz on an old takeaway, go-teaching §6).

**The page side** (review-dashboard skill). The live page (`build_dashboard.py --live`) is published with `capabilities: { db: { rules: [{ path: "", read: "view", write: "owner" }] }, user: {} }` and reads `review/game`, `boards/*` and `answers/*`. It shows no engine data: no score, graph or ownership. A row whose checksum fails is listed as "did not arrive intact". The student's answer to a board with `ask` is written by the page to `answers/<board id>` as `{ board, moves: string[] /* "WQ7" */, sent_at }` (one move for `ask: "move"`); the page refuses an occupied point, suicide and an immediate ko retake, and `explain_moment` checks the moves again when they are passed as `expected_line`. The final dashboard is republished to the same URL; the `db` survives the republish, and the page lists its boards under "During the review".
**Cost.** None. **Errors.** `bad_request` (unknown `kind`, no `title`, `at_move` out of range, a bad `ask` or `highlight` point), `illegal_move`, `wrong_color`, `invalid_sgf`, `job_not_found`.

---

## 2. Tool × teaching step

| Step of the review (go-teacher-flow) | Tools |
|---|---|
| Intake; what the machine can do | `engine_info`, `sgf_summary` |
| The quick pass and its story: lead, group fates, swings, decisive move, last chance, key moments | `start_game_analysis`, `job_status`, `job_results` |
| The evidence for a key moment or a follow-up question | `explain_moment` (it runs `analyze_position`, `human_move_distribution`, `forced_line`, `terminal_features`, `intent_probe`, `expectation_probe`) |
| The student's own line; ad-hoc continuations; punishability | `analyze_line` |
| Life and death; a status quiz | `local_solve` |
| Endgame size, sente or gote | `swing_value` |
| Urgent vs big: the value of each area now | `pass_probe` (`rank_regions`) |
| Would a player of this rank find it | `human_move_distribution` |
| Positions and lines on the live page; answers clicked on the board | `dashboard_row` |
| Positions and lines in chat (fallback), overlays | `render_board` |
| The engine-validated dashboard | `validate_variations` |
| Result reconciliation | `job_results.game.reconciliation` |
| Stability | `explain_moment.stability`; `analyze_position` with `profile: "stability"` |

---

## 3. Derived metrics (exact definitions)

Notation: `P_k` is the position after move `k` (`P_0` after setup). `s_k` is the root `score_lead` of `P_k` from Black's perspective at survey visits. `w_k` likewise for winrate. Mover of move `n` has sign `σ_n` (+1 Black, −1 White). Thresholds are config (§6).

### 3.1 Points lost
`points_lost_n = max(0, σ_n · (s_{n−1} − s_n))`. Best-reply basis is implicit: `s_n` assumes best play from `P_n`.

### 3.2 Acceptable set at `P_{n−1}`
Candidates with `visits ≥ 0.05 × root visits` and `σ_n · score_lead ≥ σ_n · best − acceptable_margin` (1.0).

### 3.3 Key moments (chains)
Seeds: the student's moves with `points_lost ≥ episode_min_loss` (2.0). Two seeds join a chain when they are within `cluster_plies` (12) of each other and their played points or best moves lie within Chebyshev distance `cluster_distance` (4), or when they are within 6 plies and fall in the same standard region; a chain spans at most `cluster_max_span` (24) plies and `cluster_max_moves` (6) student moves. A key moment is a chain; its first seed is the move it is named by. Moments are ranked by the first move's `points_lost` (a chain sum counts a group again each time both sides swing it: in 24 seed games 58 of 118 chains summed to more than 5× their first move); `net_loss` is the student's lead before the first move minus the lead after the chain's last move (+1), floored at 0. Region = standard region of the first move's played point.

### 3.4 Phases from ownership settledness
`settled_k` = fraction of points with `|ownership| ≥ settled_abs` (0.8) at `P_k`. `opening_end` = the first `k` with `settled_k ≥ opening_settledness` (0.35) or `k ≥ 50`, whichever comes first. `endgame_start` = the first `k` with `settled_k ≥ endgame_settledness` (0.75) such that `settled_j ≥ 0.70` for all `j > k`. Between them is the middlegame. `middlegame` or `endgame` is `null` when the game ends before that phase begins (short or resigned games).

### 3.5 Decisive moment and last chance
Student perspective series `w'_k = w_k` (Black) or `1 − w_k` (White). If the student lost: `decisive` = the first student move `n` with `w'_n ≤ decided_winrate` (0.15) and `w'_j ≤ recovery_winrate` (0.35) for all `j > n`. If the student won: the first opponent move `n` with `w'_n ≥ 1 − decided_winrate` and `w'_j ≥ 1 − recovery_winrate` thereafter, reported as `by: "opponent"`. In handicap games (or whenever `w'` is below 0.05 or above 0.95 for the first 30 moves) the same rule is applied to score with `decided_score_handicap` (−15) and `recovery_score_handicap` (−8), and `basis: "score"`. `last_chance` = the last student move `n` at or before `decisive` (it is often the decisive move itself, when a move there still kept the game) such that the best move at `P_{n−1}` evaluates to `w' ≥ recovery_winrate` (or score ≥ recovery score), with that counterfactual evaluation reported.

### 3.6 Group events
For every move `k` and every group `g` of at least `group_event_min_size` (4) stones on the board at `P_{k−1}`: its owner-perspective mean ownership at `P_{k−1}` and at `P_k` (stones no longer on the board count −1), labelled by §3.8. An event is reported when the label changes, the old label also held at `P_{k−2}` (a one-position wobble is not an event), and the new label still holds at `P_{min(M, k + group_event_hold)}` (6). A change already reported for an overlapping group with the same new label is not repeated. `by` is who played move `k`; `whose` is whose group it is. At most 15 events, the largest groups kept. Events need ownership at every position (the survey has it).

### 3.7 Lead and swings
`lead` is `σ_student · s_k` at `k = 0, 20, 40, …` and at `M`. `swings` are the ten moves of either player with the largest `points_lost` (≥ `episode_min_loss`), listed in move order with `lead_after` (student perspective, after the move). After an opponent's swing at `n`, `gave_back` is the `points_lost` of move `n + 1`: a large value means the mistake was not punished.

### 3.8 Group status labels
Owner-perspective mean ownership: `alive` if ≥ `alive` (0.6); `dead` if ≤ `dead` (−0.6); else `unsettled`. Group events (§3.6) use these labels.

### 3.9 Positives
Student moves with `points_lost ≤ 0.5` that the peer rank rarely plays (peer probability ≤ 0.10), excluding passes and positions without a real choice: the best two candidates must differ by ≥ `positive_min_choice` (0.5) points (otherwise every move was as good, as in dame). Sorted by peer probability, at most 3.

### 3.10 Findable move
Within the acceptable set at the moment's position, the move with the highest `target` human probability (the best move when none is higher); `findable_probability` is that probability. Below about 0.05 the better move is not findable at the target rank: teach the recognition cue, or the simpler acceptable move.

### 3.11 Stability flag
A moment is `stable` when, at survey visits, the best move's visit share is ≥ 0.35 and its score-lead margin over the second candidate is ≥ 0.5; `unstable` when the margin is < 0.3 or the played move's `lcb` exceeds the best move's; else `unknown`. `explain_moment` replaces this with its stability re-run.

### 3.12 Reconciliation
For games decided by counting: `engine_final_score = s_M` (Black perspective) compared with the SGF margin signed for Black. `ok` if `|diff| ≤ 2.5`; else `mismatch` (komi, rules or handicap bonus is probably wrong; Claude reports and stops). `n/a` for resignations, timeouts and forfeits.

### 3.13 Reply character
From the search at the position after a move (the opponent to move): the best reply is `local` when it lies within `local_radius` (4, Chebyshev) of the move. `gap` = how much the best reply beats the best candidate of the other kind (local vs non-local), for the replier. `tenuki` — the move did not need an answer; `local_sharp` — answering here is worth ≥ `sharp_margin` (3) more than the best move elsewhere, or no non-local candidate was searched (the move started a fight, or overplayed); `local_calm` — answered, but little rides on it. A cheap first signal of what the move was for; `explain_moment` uses it to decide on the reading check.

### 3.14 Capture races
Two adjacent groups of opposite colour (each ≥ 2 stones), both `unsettled` (§3.8) and both with at most `race_max_liberties` (4) liberties. Liberty counts are reported only for groups in a race (and when `include_liberty_points` / `label_low_liberties` ask for them): outside a race the count is not what decides the position, and a number in the output invites commentary about it.

---

---

## 4. Board rendering format

### 4.1 Stone grid
```
Black to move · last move: W Q7 (@) · captures B 3, W 6
    A B C D E F G H J K L M N O P Q R S T
 19 . . . . . . . . . . . . . . . . . . . 19
 18 . . . . . . . . . . . . . . . . . . . 18
 …
  4 . . . , . . . . . , . . . . . , . . .  4
 …
  1 . . . . . . . . . . . . . . . . . . .  1
    A B C D E F G H J K L M N O P Q R S T
Low liberties: B P8 group (4 stones) 2 libs at O9 P9 · W R6 group (2) 3 libs
```
`X` Black, `O` White, `.` empty, `,` star point, `@` the last move (its color is in the header), `*` highlighted points. Rows are printed from 19 down to 1; the row number appears on both sides.

With `line` the grid shows the position after the line, each line move labelled at its point by its number, `1`–`9` then `a`–`z` (moves 10–35), like a book diagram; the header names the first mover:
```
Line of 4 from here: 1 = White, colors alternate · then White to move · captures B 1, W 0
    A B C D E F G H J K L M N O P Q R S T
 …
  5 . . . X . . . . . . . . . . . . . . .  5
  4 . . X 1 X . . . . , . . . . . O . . .  4
  3 . . . 2 . . . . . . . . . . . . . . .  3
 …
```
A label stays on its point after its stone is captured; a later move on the same point is not drawn but listed in `line.notes` (`"4 at 1"`), as are captures and passes.

### 4.2 Overlay grid
`overlay_ascii` prints every point (stones included) by value: for `ownership`, `B` (≥ +0.6), `b` (+0.2 to +0.6), `.` (−0.2 to +0.2), `w` (−0.6 to −0.2), `W` (≤ −0.6), Black-positive; for `ownership_stdev`, digits `0`–`9` for tenths; for `policy`, `9`–`1` for the top nine moves by prior, `.` elsewhere.

### 4.3 Array order
All 361-element arrays are row-major from `A19` to `T19`, then `A18` … down to `T1`. The dashboard template and the build script use the same order.

---

## 5. Dashboard data export (from `validate_variations`)

The exported JSON is what `validate_variations` assembles (§1.18) and what the `review-dashboard` template reads, with these encodings, chosen so Claude can copy the blob verbatim:
- `moves`: array of `Move` values as `"BQ7"` strings (color letter + point, `"Bpass"` for a pass).
- `scoreSeries`: numbers rounded to one decimal, student perspective, index = move number (index 0 = after setup).
- `bestMoves`: one entry per move, index = move number − 1: `{ "best": "Q7" | "pass" | null, "pointsLost": 2.3 }`, from the survey: the best move in the position before that move (null when the survey has no candidate there) and the points the played move lost, from the mover's perspective (the same definition as the story's `points_lost`). Both players' moves are included.
- Ownership snapshots: 361-character strings; each character encodes ownership in 0.1 steps, `a` = −1.0 … `k` = 0.0 … `u` = +1.0 (`index = round((o + 1) × 10)`), Black-positive. Keys: `"m87"` for the position after move 87; `"M1:B1:end"` for a branch end.
- Branch `evals`: one number per node (score lead, student perspective, one decimal).
- Quiz candidates: `[{ "move": "Q8", "pointsLost": 0.0, "note": "" }]`, including the actual and peer moves, labeled.
- `meta`: `{ "game_id", "job_id", "visits_per_move", "server_version", "contract_version": "0.6.0", "exported_at" }`.
- The blob is minified; `sha256` is over the exact bytes of `dashboard_data`. Typical size: 10–20 KB.

---

## 6. Per-machine configuration (`config/<machine>.toml`)

`[student]`, `[thresholds]`, `[prefetch]` and `[paths]` are the same on every machine (a test checks it); `[search]` differs.

```toml
[machine]
name = "m5pro"                            # "r5700xt" | "m2air"

[katago]
binary = "/usr/local/bin/katago"
analysis_config = "config/analysis.cfg"   # threads, batch size, cache, analysisPVLen ≥ 12
model = "models/kata1-b18c384nbt-latest.bin.gz"
human_model = "models/b18c384nbt-humanv0.bin.gz"
search_threads = 24                       # numSearchThreadsPerAnalysisThread, passed with -override-config

[throughput]
visits_per_second_cold = 0                # filled by `katago-mcp benchmark`
visits_per_second_sustained = 0
measured_at = ""

[student]
username = "<ogs-name>"
rank = "7k"
target_offset_stones = 3
horizon_rank = "rank_1d"

[search]                                  # §0.4; m5pro / r5700xt / m2air
survey_minutes_target = 10
survey_floor = 100
survey_cap = 1000
root = 3000                               # 3000 / 2000 / 1000
line_node = 600                           # 600 / 400 / 250
plies = 8                                 # 8 / 8 / 6
stability = 4                             # the stability search runs at root × this
local_solve = 4000                        # 4000 / 2500 / 1500
quick = 200

[thresholds]                              # the file sets the first eight; the rest are code defaults
acceptable_margin = 1.0                   # §3.2
episode_min_loss = 2.0                    # §3.3, §3.7
alive = 0.6                               # §3.8
dead = -0.6
reconciliation_tolerance = 2.5            # §3.12
positive_min_choice = 0.5                 # §3.9
group_event_min_size = 4                  # §3.6
group_event_hold = 6                      # §3.6
cluster_plies = 12                        # §3.3
cluster_distance = 4
settled_abs = 0.8                         # §3.4
opening_settledness = 0.35
endgame_settledness = 0.75
decided_winrate = 0.15                    # §3.5
recovery_winrate = 0.35
decided_score_handicap = -15
recovery_score_handicap = -8
stability_visit_share = 0.35              # §3.11
stability_margin = 0.5                    # §1.6, §3.11
race_max_liberties = 4                    # §3.14
local_radius = 4                          # §3.13: a reply this close to the move is local
sharp_margin = 3.0                        # §3.13
forced_margin = 3.0                       # §1.9
human_margin = 1.0                        # §1.9
territory_diff_min = 2.0                  # §1.10
group_change_min = 0.2                    # §1.10
defend_radius = 2                         # §1.11
neighborhood_radius = 3                   # §1.11
misread_margin = 3.0                      # §1.12

[prefetch]
moments = 3                               # §1.3: key moments prepared when a survey finishes (0: off)
priority = 5                              # §0.8: KataGo priority of background work

[paths]
reviews_dir = "reviews"

[logging]
level = "info"
```

---

## 7. Files on disk

```
reviews/
  <game_id>/
    game.sgf
    analysis.json         # survey results, written incrementally
    positions/            # PositionRef → position record
    queries.jsonl         # one line per tool call: query_id, timestamp, tool, arguments, visits, seconds, cached, result summary
    export-<n>.json       # each dashboard_data blob returned by validate_variations, with its sha256
    dashboard_rows.jsonl  # every row made by dashboard_row (the game record, the boards), in order
  _adhoc/
    queries.jsonl
```

---
