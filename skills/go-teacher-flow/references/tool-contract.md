# katago-mcp — Tool Contract (v0.5.3, as implemented in katago-mcp 0.4.3)

**Status:** current; describes the implemented server, 25 tools. This is the only copy (skills must be self-contained); `katago-mcp/tests/test_docs.py` checks that the header names the code's versions. Decisions and the version history are in `docs/contract-changes.md`. Values marked *config* live in the per-machine config file (§6) and are tuned in WS8. Numbers in examples are illustrative — real values come from the WS1 benchmarks.

Design points worth knowing up front:
1. The self-review minutes are part of the total time you give, so the budget formula reserves them (§1.2).
2. Numeric dashboard data cannot pass through Claude by retyping without error, so `validate_variations` also assembles the complete dashboard data blob and returns it with a checksum (§1.17, §5).
3. A tool call blocks Claude's turn, so Claude cannot talk to the student while a probe runs. The verification probes therefore run in the background (`start_verification`, §1.22) while Claude interviews the student, and the server keeps their results sealed per episode until the interview answer is recorded (§1.23).
4. The student follows the review on one page, published live in Phase 0. Positions and lines reach it as rows Claude writes to the page's database, made by `dashboard_row` (§1.25) with a checksum the page verifies, not as ASCII diagrams in chat.

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
- A `PositionRef` hashes rules, komi, board, side to move, ko state, the position history (for superko) and the game it belongs to: a position of a job or an SGF belongs to that game, one given as `moves` to none. The same opening in two games has two refs, each with its own game's student colour, plan and log. Refs are persisted under `reviews/<game_id>/positions/` and survive server restarts.
- Every response that involves a position includes its `position_ref`, so Claude chains calls without resending moves.
- `then` moves are legality-checked (occupied point, suicide, ko, superko per the rules). An illegal move returns `illegal_move` with the ply and reason.

### 0.3 Perspective and units
- `score_lead`, `winrate` and `points` fields are reported from `perspective`, which defaults to the **student's color when the position comes from a job**, else Black. Every response echoes `perspective`. Positive `score_lead` is good for that color.
- `points_lost` is always from the mover's perspective and never negative.
- **Ownership is a board fact, not an evaluation:** always Black-positive (+1 Black owns the point, −1 White owns it), independent of `perspective`. A group's `mean_ownership` is reported from the **group owner's** perspective (positive = the group is doing well).
- Score units are KataGo `scoreLead` points, approximately final-score points under the game's rules.

### 0.4 Budgets
```ts
type Budget =
  | { visits: number }
  | { seconds: number }                                        // converted with sustained visits/s
  | { profile: "survey" | "root" | "line_node" | "stability" | "local_solve" | "quick" }
```
- A bare number is read as `{ visits }`, a profile name string as `{ profile }`.
- Profiles resolve to visit counts from the **active plan** for the job (§1.2). Without an active plan they resolve to the base unit (§1.2.3). `quick` is always 200 visits, for ownership snapshots and group status.
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
The **standard partition** tiles the board: columns `A–G` | `H–M` | `N–T` (7 | 5 | 7) × rows `13–19` | `8–12` | `1–7` (7 | 5 | 7). Corners are 7×7 (49 points), sides 5×7 (35), center 5×5 (25). Human labels: "upper left corner", "upper side", "center", and so on. Regional attribution (§1.14) uses this partition unless regions are supplied.

### 0.7 Stability-based early stop
For `root`, `stability` and `local_solve` profiles the server issues one KataGo query at the target visits with periodic progress reports (about one per second) and terminates it early when, across the last three reports, the top move is unchanged and `score_lead` has moved less than 0.5 points. Responses include `stopped_early: boolean`. `survey`, `line_node` and explicit `{visits}` budgets run to completion.

### 0.8 Caching, persistence, logging
- Results are cached by (`position_ref`, effective visits, options). `cached: true` marks a hit. A request for more visits than cached re-searches.
- Every tool call appends one line to `reviews/<game_id>/queries.jsonl` (or `reviews/_adhoc/queries.jsonl` when no game is in context). Every response carries `query_id` (`"q_<game_id>_<seq>"`). Ledger entries cite these ids.
- Whole-game analysis persists incrementally to `reviews/<game_id>/analysis.json`, so a crash or timeout leaves partial results available.
- **Stored results (v0.4).** `analyze_position`, `analyze_line`, `swing_value`, `local_solve`, `terminal_features`, `forced_line`, `intent_probe` and `expectation_probe` keep their whole result, keyed by (tool, `position_ref`, effective visits, the move, the plan's plies the tool defaults to, the other arguments and `options`). An identical later call returns the stored result at once, with a fresh `query_id` and `precomputed: { query_id, seconds_saved }` naming the call that computed it (`cached: true` where the output has that field). A call identical to one being computed waits for it rather than searching again. This is how the background verification (§1.22) hands its work to Claude's own calls. In memory only; `job_status(action: "release")` drops a game's stored results.
- **Sealing (v0.4).** Between `start_verification` and `record_interview` for an episode, the server refuses (`sealed`) every stored-result tool on the position before or after its played move, any stored result computed for it, and `render_board` overlays there; `get_position_ref` omits `cached_analysis` there. Plain `render_board` (and `line` diagrams) stay available. The blind self-review is not enforced by the server (no episode is selected yet); it stays a rule of conduct.

### 0.9 Errors
```ts
type Error = { error: { code: ErrorCode; message: string; details?: object; recoverable: boolean; suggestion?: string } }
type ErrorCode =
  | "engine_unavailable" | "engine_busy" | "human_model_unavailable" | "timeout" | "internal"
  | "invalid_sgf" | "unsupported_board_size" | "illegal_move" | "unknown_ref" | "bad_region" | "no_group_at_point"
  | "job_not_found" | "job_not_finished" | "budget_infeasible" | "validation_failed"
  | "bad_request" | "ogs_fetch_failed" | "wrong_color"
  | "sealed" | "verification_not_started" | "episode_not_found" | "episode_not_selected"
```
`bad_request` covers malformed arguments (wrong colour to move, a bad point or budget, a missing field). `ogs_fetch_failed`: the OGS game could not be downloaded (§1.3). `sealed`: an engine result about an episode whose interview is not recorded (§0.8); `details.episode` names it. `validate_variations` reports validation problems in its `errors` list with `valid: false` rather than raising.
`suggestion` is written for Claude to act on ("start the engine with `katago-mcp serve`", "the job is 62% done; call job_status again in ~90 s").

---

## 1. Tools

Each tool is specified as **Purpose · Inputs · Output · Behavior · Cost · Errors**. Types use the conventions above; `?` marks optional fields; defaults follow `=`.

### 1.1 `engine_info`
**Purpose.** Report what this machine's engine is and how fast it is.

**Inputs.** `{ refresh_benchmark?: boolean = false }` — when true, runs a 20-second timed query and updates the sustained figure (only when idle).

**Output.**
```ts
{
  machine: string;                       // "m5pro" | "r5700xt" | "m2air" (config)
  server_version: string; katago_version: string; backend: "metal" | "opencl" | "cuda" | "eigen";
  network: { name: string; blocks: number; channels: number };
  human_model: { name: string; loaded: boolean };
  throughput: { visits_per_second_cold: number; visits_per_second_sustained: number; measured_at: string; method: "benchmark" | "timed_query" };
  engine_status: "ready" | "starting" | "error";
  active_job: null | { job_id: string; progress: number };
  active_plan: null | { job_id: string; total_minutes: number | "unlimited"; episodes: number };
  student: { username: string; rank: string; peer: string; target: string; horizon: string };
  thresholds: object;                    // the [thresholds] table from config (§6)
}
```
**Cost.** None (benchmark: ~20 s). **Errors.** `engine_unavailable`.

---

### 1.2 `plan_budget`
**Purpose.** Turn "how much time may this review take" into a concrete allocation for this machine, and report the minimum time a full-rigor review needs. Called in Phase 0 before `start_game_analysis`, and again after Phase 3 with the selected episodes to re-plan the remaining time.

**Inputs.**
```ts
{
  total_minutes: number | "unlimited";
  move_count?: number;                   // required unless job_id is given
  job_id?: string;                       // re-plan: move count, elapsed time and job state come from the job
  self_review_minutes?: number;          // blind self-review; = config self_review_minutes_default (5); 0 for "review just this sequence"
                                         // (when given, the survey is sized to it, as before v0.3)
  interview_minutes?: number;            // per-episode interviews after triage; = config interview_minutes_default (5)
  episodes_requested?: number;           // 1–5; caps the episode count (e.g. 1 for a single-sequence review)
  expected_ld_episodes?: number;         // = config ld_reserve_episodes (1); how many episodes will need local solves
  selected?: { id: string; needs_local_solve: boolean }[];   // Phase 3 re-plan with the real selection
  keep_sizes?: boolean;                  // re-plan: keep the earlier per-episode sizes (default: when the background has already
                                         // computed results for a selected episode, §1.22); false climbs the ladder again
}
```

**Output.**
```ts
{
  feasible: boolean;                     // three or more episodes at base rigor fit
  total_minutes: number | "unlimited"; elapsed_minutes: number; throughput_vps: number;
  reserved: { overhead_minutes: number; self_review_minutes: number; interview_minutes: number };   // the interviews overlap the engine
  survey: { visits_per_move: number; expected_minutes: number; runs_past_self_review: boolean; charged_minutes: number };
  verification: {
    wall_minutes_available: number;
    episodes: number;                    // 0–5
    per_episode: { root_visits: number; line_node_visits: number; follow_pv_plies: number; forced_line_plies: number; expectation_plies: number;
                   stability_multipliers: number[]; local_solve_visits: number; student_lines: number; probes: string[] };
    ld_episodes_budgeted: number;
    ladder_steps_applied: string[];      // e.g. ["root_3000", "line_600", "stability_16x"]
    expected_minutes: number;            // wall-clock after the interviews: engine work they did not cover + Claude time
    slack_minutes: number | null;
    engine_minutes: number;              // all engine work of the episodes, interviews or not
    overlapped_with_interviews_minutes: number;   // the part of it that runs during the interviews (background, §1.22)
  };
  minimum_minutes_for_three_episodes: number;   // on this machine, for this game
  expected_total_minutes: number;                 // what "unlimited" or the plan will actually take
  profiles: { survey: number; root: number; line_node: number; stability: number[]; local_solve: number; quick: 200 };
  notes: string[];                       // plain-language remarks, e.g. why the survey runs past the self-review
}
```

#### 1.2.1 Constants (config, §6)
| Symbol | Meaning | Default |
|---|---|---|
| `O` | fixed overhead: Claude's reasoning, composition, build, memory writes | 4 min |
| `C_ep` | Claude's think time per verified episode | 1.0 min |
| `S` | blind self-review minutes (input; default) | 5 min |
| `I` | episode interview minutes (input; default); the background verification runs during them | 5 min |
| `L` | lines the student proposes per episode (`student_lines_per_episode`), each budgeted as an `analyze_line` of 4 moves | 1 |
| `S_survey` | minutes the survey is sized to (`survey_minutes_target`; `S` when `self_review_minutes` is given) | 10 min |
| `floor`, `cap` | survey visits per move | 100, 1000 |
| `vps` | sustained visits/s of this machine | measured |
| base unit | `root=1000`, `line_node=250`, `plies=6`, `stability=[4]`, `solve=1500` | |
| cap unit | `root=6000`, `line_node=1000`, `plies=8`, `stability=[4,16]`, `solve=4000` | |

#### 1.2.2 Algorithm
Let `M` be the number of analyzable positions (the game's move count), `T` the total minutes, `E_req` the episode cap (default 5), `LD` the expected life-and-death episodes.

```
minutes(v) = v / vps / 60                                   # engine minutes for v visits

unit_visits(u) = u.root                                     # root analysis with wide root noise
               + Σ(u.stability) · u.root                    # stability reruns at multiples of the root visits
               + (6·u.plies + 39) · u.line_node             # the probes, p = u.plies:
                                                            #   intent_probe 8
                                                            #   expectation_probe 2p + 5 (a search per imagined move, one more
                                                            #     when it is off the candidates, a 4-ply refutation)
                                                            #   forced_line ×2 (better and played move) 2 · (2p + 13): two searches
                                                            #     per node, end features, two resistances with 3-ply refutations
solve_visits(u) = 2 · u.solve                               # attacker-first and defender-first
line_visits(u)  = L · (1 + 4 + u.plies) · u.line_node       # the student's own lines (a fix, a resistance line)
answer_visits(u) = (2·u.plies + 5) · u.line_node + line_visits(u)   # the work that needs the interview answer:
                                                            #   expectation_probe with the stated line, the student's lines
engine(u, n, ld) = minutes(n · (unit_visits(u) + line_visits(u)) + min(ld, n) · solve_visits(u))
overlap(u, n, ld) = min(I, max(0, engine(u, n, ld) − minutes(answer_visits(u))))
                                                            # everything but the last episode's answer work can run
                                                            # in the background during the interviews (§1.22)
verif(u, n, ld) = engine(u, n, ld) − overlap(u, n, ld) + n · C_ep    # wall-clock after the interviews

1. if T == "unlimited":
       v_s = cap; n = min(5, E_req); u = cap unit; all ladder steps applied
       expected_total = O + max(S, minutes(M·v_s)) + I + verif(u, n, LD)
       return (feasible = true)

2. survey:
       v_s = clamp( floor(vps · S_survey · 60 / M), floor, cap )   # sized by its own target, not the blind review
       t_s = minutes(M · v_s)
       c_s = max(0, t_s − S)                                 # the part that runs past the blind self-review
                                                             # (Claude asks the optional questions meanwhile)

3. wall-clock available for verification:
       W = T − O − S − I − c_s
       if W ≤ 0: n = 0 (infeasible; go to 5)

4. episode count at base rigor:
       n = largest n ≤ min(5, E_req) with  verif(base, n, LD) ≤ W

5. if n < 3:  feasible = false
       v_min = floor; c_min = max(0, minutes(M·v_min) − S)
       minimum_minutes_for_three_episodes = O + S + I + c_min + verif(base, 3, LD)
       still return the allocation for n (0, 1 or 2) so "accept fewer" is possible

6. surplus X = W − verif(base, n, LD)
       apply the ladder in order; a step is applied only if its full cost for n episodes fits in X:
         L1  root 1000 → 3000        (also raises the 4× stability rerun to 12,000)
         L2  line_node 250 → 600
         L3  add the 16× stability rerun
         L4  plies 6 → 8             (forced lines and imagined lines)
         L5  solve 1500 → 4000, and budget a solve for every expected L&D episode
         L6  root 3000 → 6000
         L7  line_node 600 → 1000
       no partial steps; surplus after the last applicable step is reported as slack

7. re-plan (job_id + selected given): replace LD with the count of selected episodes needing solves,
       set W = T − O − elapsed_minutes − I (the interviews follow the re-plan), skip steps 2–3,
       recompute 4–6 for exactly len(selected) episodes.
       keep_sizes (default: the background already computed results for a selected episode): if the earlier
       plan's unit u_prev fits, verif(u_prev, n, LD) ≤ W, use it as is and skip the ladder, so those results
       stay valid; the rest is slack. Otherwise climb the ladder as in 6.
```
The resulting plan becomes the **active plan** for the job; `Budget.profile` values resolve against it. The plan is advisory: Claude may pass explicit budgets, and you may extend `total_minutes` mid-review (call again).

#### 1.2.3 Worked example (illustrative numbers)
Pro at 650 vps, 187 moves, 40 minutes, S = 5, I = 5, LD = 1:
- `v_s = clamp(650·600/187 = 2085, 100, 1000) = 1000`; `t_s = 4.8 min`; `c_s = 0`.
- `W = 40 − 4 − 5 − 5 = 26`.
- Base unit = 1000 + 4·1000 + 75·250 = 23,750 visits ≈ 0.61 min, plus one student line 11·250 ≈ 0.07 min; solve ≈ 0.08 min. `n = 5`.
- The interviews overlap 5 of the engine minutes, which buys one more ladder step than in v0.3: L1–L6 fit, L7 does not. Result: five episodes at root 6000 / line 600 / 8 plies / 4× and 16× stability, ≈ 24 engine minutes, of which 5 run during the interviews.

Air at 30 vps, same game, 20 minutes: `v_s = 100` (floor), `t_s = 10.4`, `c_s = 5.4`, `W = 0.6`; one episode's engine work is 13.2 + 1.5 (student line) min → `n = 0`; minimum for three episodes ≈ 4 + 5 + 5 + 5.4 + (3·14.7 + 1.7 − 5) + 3·1.0 ≈ 63 min (60 minutes buy two). On this machine the overlap and the student-line allowance about cancel. Claude reports that and asks. Rigor per episode is fixed; slower machines verify fewer episodes.

**Cost.** None. **Errors.** `job_not_found`, `budget_infeasible` is *not* an error — infeasibility is a normal result with `feasible: false`.

---

### 1.3 `sgf_summary`

> **Input forms (v0.2.1):** `sgf` may be raw SGF text, the path or file name of an `.sgf` on the server machine (looked up in `games/`), or an OGS game id / link (`12345678`, `ogs_12345678` as memory writes it, or the URL), which the server fetches from `https://online-go.com/api/v1/games/<id>/sgf` and caches as `games/ogs_<id>.sgf`. The same applies to `start_game_analysis.sgf`, `get_position_ref.sgf` and `Position.sgf`. Errors: `ogs_fetch_failed`. Prefer link or path: SGF text retyped into a tool call by the model is unreliable for long games.

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

### 1.4 `start_game_analysis`
**Purpose.** Start the asynchronous survey: analyze every position of the game at the survey budget and compute the derived metrics of §3.

**Inputs.**
```ts
{ sgf: string; budget?: { visits_per_move: number } | Budget = { profile: "survey" };   // normalized like every other budget (§0.4): bare number, { visits }, { seconds }
  student_username?: string; game_id?: string;      // reuse an existing id (re-analysis)
  options?: { human_profiles?: string[] = ["peer","target","horizon","opponent"];
              ownership: "all" = "all";              // ownership at every position (needed for phases and tags)
              analyze_moves?: [number, number];      // restrict to a range (single-sequence reviews)
              reuse_existing?: boolean = true } }    // reuse saved analysis of the same SGF at ≥ these visits
```
**Output.**
```ts
{ job_id: string; game_id: string; positions_total: number; visits_per_move: number;
  expected_seconds: number; started_at: string; reused: boolean }
```
**Behavior.** One job per machine at a time; a second call returns `engine_busy` with the running job's id. Each position is searched at `visits_per_move` with ownership; the played move's policy prior and the human-profile probabilities of the played move and the top candidates are recorded (one NN evaluation per profile). Results are written incrementally (every 20 positions, and before the job reports `done`). Interactive tools may run during a job and are sent at a *higher* KataGo priority (10) than the survey (0), so they return promptly; the survey resumes afterwards. When a survey started under a plan finishes (or is reused), the server precomputes the answer-free probes of its top `speculative_episodes` episodes by root loss in the background (§1.22) while the blind self-review goes on; nothing is shown, and the probes are refused only once triage selects the episode and until its interview is recorded. Batch seeding and the CLI run without a plan and never do this.
**Cost.** `M · visits_per_move` visits. **Errors.** `invalid_sgf`, `engine_unavailable`, `engine_busy`.

---

### 1.5 `job_status`
**Inputs.** `{ job_id: string; action?: "status" | "cancel" | "release" = "status" }`
**Output.**
```ts
{ job_id: string; state: "queued" | "running" | "done" | "failed" | "cancelled";
  positions_done: number; positions_total: number; progress: number; current_move: number;
  elapsed_seconds: number; eta_seconds: number | null; partial_results_available: boolean; error?: Error["error"] }
```
`cancel` stops the engine work; partial results stay available. `release` frees a finished job's memory; its results stay in `reviews/<game_id>/` and are reused. **Errors.** `job_not_found`.

---

### 1.6 `job_results`
**Purpose.** The survey digest. **Claude must not call this before `self_review.md` is written** (enforced by the project instructions, not the server).

**Inputs.** `{ job_id: string; detail?: "digest" | "full" | "moves" = "digest"; range?: [number, number]; max_episodes?: number = 10; include_positives?: boolean = true }`

**Output (`digest`)** — target ≤ 4k tokens.
```ts
{
  job_id: string; game_id: string; complete: boolean; positions_analyzed: number; visits_per_move: number;
  game: { student_color: Color; handicap: number; komi: number; rules: Rules; result: string;
          reconciliation: { status: "ok" | "mismatch" | "n/a"; engine_final_score: number | null; sgf_margin: number | null; diff: number | null } };
  phases: { opening: [number, number]; middlegame: [number, number]; endgame: [number, number]; method: "ownership_settledness" };
  game_type: { type: "single_blunder" | "accumulation" | "mixed"; top_episode_share: number };
  decisive: null | { move: number; by: "student" | "opponent"; basis: "winrate" | "score"; before: number; after: number };
  last_chance: null | { move: number; played: Point; best: Point; eval_if_best: number; eval_actual: number; basis: "winrate" | "score" };
  points_lost: { student: PhaseLoss; opponent: PhaseLoss };      // PhaseLoss = { opening, middlegame, endgame, total, per_move }
  episodes: Episode[];                                            // ranked by root.points_lost, ≤ max_episodes
  positives: { move: number; played: Point; points_lost: number; peer_probability: number; target_probability: number }[];
  reliability: { low_visit_positions: number[]; unstable_episodes: string[] };
}

type Episode = {
  id: string;                                   // "E1", "E2", …
  moves: [number, number];                      // first and last move of the chain (both colors)
  student_moves: number[];
  root: { move: number; played: Point; best: Point; points_lost: number; position_ref_before: PositionRef; position_ref_after: PositionRef };
  points_lost_total: number;
  region: { standard: string; label: string; bbox: [Point, Point] };
  phase: "opening" | "middlegame" | "endgame";
  game_state_before: { score_lead: number; label: "ahead" | "close" | "behind" };
  acceptable_set: { margin: number; moves: Point[]; played_in_set: boolean; violation: number };
  candidate_tags: string[];                     // taxonomy ids, ≤ 3, from §3.7
  signature: { prior_played: number; prior_best: number; policy_top: Point; search_best: Point;
               local_loss_share: number | null; score_stdev_played: number; score_stdev_best: number; ko_present: boolean };
  style_axis: { label: "overplay" | "slack" | "neutral"; own_share: number; opponent_share: number };
  got_away_with_it: null | { opponent_move: number; restored_points: number };
  persistent_best: string[];                    // other episode ids whose root best or teachable point is the same point (one big point left open)
  human: { played: Record<string, number>; best: Record<string, number> };    // keyed by resolved profile
  learnability: number;                         // target-rank probability of the preliminary teachable move
  teachable_move_preliminary: Point;            // argmax target probability within the acceptable set
  stability: "stable" | "unstable" | "unknown";
  pattern_hash: string; pattern_hash_5: string;
  group_status_change: { group: string; before: string; after: string }[];   // e.g. "B right side (7)", "alive 0.78", "unsettled 0.31"
  best_reply: null | { move: Point; local: boolean; character: "tenuki" | "local_calm" | "local_sharp"; gap: number | null; score_stdev: number };   // §3.15
};
```
**Output (`full`)** adds `moves: MoveRow[]` for every position: `{ n, color, move, score_before, score_after, points_lost, best, prior_played, prior_best, in_acceptable_set, visits, position_ref }`. **Output (`moves`)** returns `MoveRow[]` for `range` only.
**Errors.** `job_not_found`, `job_not_finished` (unless `partial_results_available`, in which case the digest is returned with `complete: false`).

---

### 1.7 `get_position_ref`
**Inputs.** `{ job_id: string; move_number: number } | { sgf: string; move_number: number }`
**Output.** `{ position_ref: PositionRef; game_id: string; move_number: number; to_move: Color; last_move: Move | null; captures: { B: number; W: number }; cached_analysis: null | { visits: number; score_lead: number; top_move: Point } }`; `cached_analysis` is null at a sealed episode's positions (§0.8).
**Errors.** `job_not_found`, `invalid_sgf`, `unknown_ref`.

---

### 1.8 `analyze_position`
**Purpose.** Candidates, evaluation, ownership and group statuses at one position. The hypothesis tool.

**Inputs.**
```ts
{ position: Position; budget: Budget;
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
type Group = { id: string; color: Color; stones: Point[]; size: number; liberties?: number; liberty_points?: Point[];   // liberties only for groups in a capture race (§3.16) or when asked for
               mean_ownership: number; status: "alive" | "unsettled" | "dead"; region: string; label: string };   // label e.g. "W lower right corner (5)"
type CaptureRace = { groups: { label: string; anchor: Point; color: Color; liberties: number; liberty_points: Point[] }[] };   // §3.16
```
**KataGo mapping.** `wideRootNoise` via `overrideSettings`; `includeOwnership`, `includeOwnershipStdev`, `includePolicy`, `includePVVisits`; human probabilities via one query per profile with the human model's profile set in `overrideSettings` (exact key per the installed version's Analysis_Engine.md).
**Cost.** `budget`. **Errors.** `illegal_move`, `unknown_ref`, `engine_unavailable`.

---

### 1.9 `analyze_line`
**Purpose.** Play out a sequence — forced moves, engine replies, or a mix — and report what happens. The general line tool: plan tests (`restrict`), ad-hoc continuations and punishability (`refutation_probability`). Proof lines for a lesson use `forced_line` (§1.19); the belief protocol uses `intent_probe` and `expectation_probe` (§1.20–1.21).

**Inputs.**
```ts
{ position: Position; budget: Budget;                    // per searched node; typically { profile: "line_node" }
  line: LineStep[];                                      // at least one step
  follow_pv_plies?: number;                              // = active plan's plies (6 base); engine-vs-engine continuation after `line`
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

### 1.10 `pass_probe`
**Purpose.** Measure urgency: how much a move was worth locally, and what the opponent would take if the mover tenukis.

**Inputs.**
```ts
{ position: Position;                                    // the position BEFORE the move in question
  player: Color; move?: Point; budget: Budget;           // budget per search; typically { profile: "root" }
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

### 1.11 `swing_value`
**Purpose.** Endgame counting and sente/gote classification.

**Inputs.** `{ position: Position; points: Point[]; budget: Budget; options?: { local_radius?: number = 4; perspective?: Color } }` (1–6 points, ranked together)

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

### 1.12 `local_solve`
**Purpose.** Classify a group as alive, dead or unsettled by confining both sides to a region and letting each side move first.

**Inputs.**
```ts
{ position: Position; group_point: Point;               // any stone of the target group
  region?: Region;                                       // = bounding box of the group + 2, auto-expanded to include all its liberties
  budget: Budget;                                        // per search; typically { profile: "local_solve" }
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

### 1.13 `group_status`
**Inputs.** `{ position: Position; options?: { ownership: "cached" | "compute" = "cached"; budget?: Budget = { profile: "quick" }; min_size?: number = 1; include_liberty_points?: boolean = false } }`
**Output.**
```ts
{ position_ref: PositionRef; groups: Group[]; unsettled: string[]; capture_races?: CaptureRace[];       // ids of unsettled groups
  summary: { B: { alive: number; unsettled: number; dead: number }; W: { alive: number; unsettled: number; dead: number } };
  ownership_visits: number; query_id: string }
```
**Behavior.** Uses cached ownership for the position if any search has produced it; otherwise runs the `quick` search. Groups are maximal connected same-color stone sets. **Errors.** `unknown_ref`.

---

### 1.14 `ownership_diff`
**Purpose.** Attribute a score change to regions and groups, and classify the loss as local or global.

**Inputs.** `{ a: Position; b: Position; regions?: Region[]; budget?: Budget = { profile: "quick" }; options?: { perspective?: Color } }`
**Output.**
```ts
{
  a: { position_ref: PositionRef; score_lead: number }; b: { position_ref: PositionRef; score_lead: number };
  score_delta: number;                                     // b − a, perspective
  regional: { region: string; ownership_delta_points: number; share: number }[];   // Σ Δownership over the region (perspective-signed); share of |score_delta|
  explained_share: number;                                 // Σ of same-sign regional deltas / |score_delta|, capped at 1
  groups_changed: { group: string; before: number; after: number; status_before: string; status_after: string; points: number }[];
  local_vs_global: { primary_region: string; local_share: number; classification: "local" | "mixed" | "global" };
  query_id: string;
}
```
**Behavior.** Uses cached ownership when available, else `budget`. Δownership summed over a region approximates the points that moved there. `classification`: `local` if the primary region's share ≥ `local_share_local` (0.7), `global` if ≤ `local_share_global` (0.4), else `mixed`. When `explained_share` is low the loss is diffuse or not territorial (tempo, thinness), and a local explanation is not supported.
**Cost.** 0–2 quick searches. **Errors.** `unknown_ref`, `bad_region`.

---

### 1.15 `human_move_distribution`
**Inputs.** `{ position: Position; profiles?: string[] = ["peer","target","horizon","opponent"]; moves_of_interest?: Point[]; top_n?: number = 8 }`
**Output.**
```ts
{ position_ref: PositionRef; to_move: Color;
  profiles: Record<string, { top: { move: Point; probability: number }[]; moves_of_interest: Record<Point, number>; entropy: number }>;
  resolved_profiles: Record<string, string>; query_id: string }
```
**Behavior.** One NN evaluation per profile, no search. Probabilities are what a player of that rank would play here — they inform learnability and punishability, never correctness. **Errors.** `human_model_unavailable`, `unknown_ref`.

---

### 1.16 `render_board`
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
**Behavior.** Since v0.5 the review shows positions and lines on the live page (`dashboard_row`, §1.25); `render_board` is the chat fallback when the page cannot be used, and remains the tool for overlays. Format in §4. Overlays need ownership/policy: cached if present, else a `quick` search. With `line`, `ascii` is a numbered diagram of the sequence (§4.1) instead of the position with its last move: no engine call, so it may be shown while results are sealed. `position_ref`, `to_move`, `last_move`, `captures` and any overlay still describe `position`; `line.end_ref` is the position after the line (render or analyze it next, e.g. for a line longer than 35 moves). `notes` carries what the grid cannot: `"4 at 1"` (a move on a point already labelled), `"2 captures 1 stone (1 among them)"`, `"5: W passes"`. **Errors.** `unknown_ref`, `illegal_move`, `wrong_color` (a `line` move of the side not to move), `bad_request` (a `line` longer than 35 moves), `sealed` (an overlay at a sealed episode's position, §0.8).

---

### 1.17 `validate_variations`
**Purpose.** The gate to the dashboard: check every branch for legality, fill in evaluations, and assemble the complete dashboard data blob with a checksum. Nothing reaches the dashboard that the server has not played and evaluated.

**Inputs.**
```ts
{
  job_id: string;
  episodes: DashboardEpisodeSpec[];
  summary: DashboardSummary;                              // copied verbatim into the blob; rendered by the template
  options?: { evaluate_missing?: boolean = true; budget?: Budget = { profile: "line_node" }; ownership_at?: "roots_and_branch_ends" = "roots_and_branch_ends" }
}
type DashboardEpisodeSpec = {
  id: string; kind?: "lesson" | "question" = "lesson";     // "question": a follow-up the student asked after the lessons (v0.3.1)
  moves: [number, number]; title: string; category: string; tags: string[]; points_lost: number;
  commentary: { at_move: number; text: string }[];
  branches: { id: string; label: string; from_move?: number; moves: Move[]; ledger_ref?: string;
              kind?: "as_played" | "expected" | "misread" | "better" | "resistance" | "fix" | "question";
              from_branch?: string; at_ply?: number }[];   // from_branch: start after at_ply moves of an earlier branch (then from_move is the parent's)
  comparison?: { a: string; b: string };                    // two branch ids; the server compares their end positions (§1.18)
  rule_check?: string; belief?: { id: string; source: "stated" | "inferred"; statement?: string };
  quiz?: { at_move: number; type: "move" | "status"; candidates?: Point[];            // the server always adds the actual move and the peer move
           status?: { group_point: Point; solve_query_id: string } };
  principle?: string; cue: string;                         // principle: kept for pre-v0.3 reviews; rule_check replaces it
};
type DashboardSummary = {                              // the shape the review-dashboard skill writes and the template reads
  headline?: string;
  lessons?: { episodeId: string; title: string; ruleCheck?: string; principle?: string; cue: string }[];
  strengths?: string[];
  selfReview?: { agreements?: string[]; blindSpots?: string[] };
  nextGame?: string;
  reliability?: string;
};
```
**Output.**
```ts
{ valid: boolean;
  errors: { episode_id: string; branch_id?: string; ply?: number; move?: string; code: "illegal_move" | "wrong_color" | "bad_from_move" | "unknown_query" | "bad_quiz" | "bad_branch_parent" | "bad_comparison"; message: string }[];
  warnings: string[];                                            // e.g. "branch B2 of E3 never diverges from the game"
  dashboard_data?: string;                                       // compact JSON per §5, only when valid
  sha256?: string; size_bytes?: number; query_id: string }
```
**Behavior.** A branch with `from_branch` is expanded to the parent's first `at_ply` moves followed by its own, from the parent's `from_move`, and exported with `parentBranch` and `branchPly` (plies in errors count from the parent's start). With `comparison`, both branch ends get `terminal_features` from the student's perspective and the episode exports `comparison: { a, b, aLabel, bLabel, scoreDiff, groups, territory, territoryTotal, sente, nextMove, weakGroups }`; `rule_check`, `belief` and each branch's `kind` are exported as `ruleCheck`, `belief`, `kind`. For each branch: the position after `from_move` is taken from the job; every move is legality-checked with alternating colors from that position's side to move; evaluations per node come from cache or new searches at `budget`. Quiz candidates get `pointsLost` relative to the best move (the actual move's from the survey's after-position score, the same definition as §3.1) and `labels ⊆ {actual, peer, best}`; `status` quizzes pull the verdict from the cited `local_solve` query. Ownership is encoded at episode roots and branch ends. Game metadata, moves, setup, score series (student perspective) and the episodes' numeric fields come from the server's records; all text fields are copied verbatim from the inputs. On any error, nothing is exported. Claude writes `dashboard_data` verbatim to `data.json`; `build_dashboard.py` recomputes the checksum and refuses to build on mismatch.
**Cost.** Searches for uncached branch nodes only. **Errors.** `job_not_found`; validation problems are returned as `valid: false` with the details in `errors` (§0.9).

---

### 1.18 `terminal_features`
**Purpose.** Say what an end position *is*, and what is concretely different between two end positions — the "why" of a lesson, instead of a score delta.

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

### 1.19 `forced_line`
**Purpose.** A human proof: from a move, the narrow line of must-moves ending in a position the student can evaluate, with the opponent's natural resistance and its refutation.

**Inputs.**
```ts
{ position: Position;                                       // before the move
  move: Point | string;                                     // "Q7" or "BQ7"; must be the side to move
  budget?: Budget = { profile: "line_node" };
  options?: { max_plies?: number = plan's forced_line_plies (8);
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
  end: Features;                                            // §1.18
  visits_used: number; seconds_used: number; query_id: string }
```
**Behavior.** At each node the position is searched, then searched again with the top move avoided (`avoidMoves`, `untilDepth` 1) so the second-best has real visits; `gap_to_second` is how much the best beats it for the side to move, and the reply is `forced` when the gap exceeds `forced_margin`. The move played is the engine's best, replaced by the `legible_profile` human model's favourite among candidates within `human_margin` (lines stay human-legible, not full of probes). At the first `resistance_nodes` opponent nodes, the `resistance_profile` model's most likely move, when different, is played and refuted by `refutation_plies` engine moves. `extend: "local"` exists because in fights the best reply is often only 1–3 points better than the second: a strictly forced line stops after one move, before any group's fate is visible; the local extension follows the fight until the best move is elsewhere (`quiet`), with `forced: false` on the nodes that were a choice. Run it from the better move E and from the played move G, then `terminal_features(end of G, compare_to: end of E)`.
**Cost.** About 2 searches per node, plus 2 + `refutation_plies` per resistance, plus 2 for the end features (1 more when cut off by `max_plies`). **Errors.** `bad_request` (wrong colour, bad point), `illegal_move`, `unknown_ref`.

---

### 1.20 `intent_probe`
**Purpose.** Recover the belief behind a move. Every move is a threat, a defense or a value claim; three cheap probes say which, and whether it worked.

**Inputs.** `{ position: Position /* before the move */; move: Point | string; budget?: Budget = { profile: "line_node" }; options?: { neighborhood_radius?: number = 3 } }`

**Output.**
```ts
{ position_ref: PositionRef; player: Color; perspective: Color;          // perspective = the player
  move: Point; best_move: Point;
  score: { before: number; after_move: number; after_best: number; loss: number };
  reply: ReplyCharacter;                                  // §3.15, the opponent's best answer to the move
  threat: { follow_up: Point; value: number; opponent_local_answer: Point };   // what the move threatened (local swing)
  tenuki_value: number;                                   // how much the opponent gains by not answering locally
  defense: { opponent_local_move: Point | null; value: number | null;           // what the move prevented
             groups: { label: string; anchor: Point; ownership_before: number; ownership_after_move: number; ownership_if_attacked: number }[] };
  left_behind: { label: string; anchor: Point; ownership_before: number; ownership_after_reply: number }[];   // when the reply is elsewhere
  better_move: { gote: boolean | null; groups_that_differ: string[] | null };
  risk: { score_lead_before: number; stdev_move: number; stdev_best: number | null;
          human: Record<"peer" | "horizon", { move: number; best: number | null }> };
  belief: null | { id: BeliefId; evidence: string; categories: string[] };        // the first match
  matches: BeliefId[];
  visits_used: number; seconds_used: number; query_id: string }
type BeliefId = "needs_defending" | "group_is_safe" | "is_sente" | "behind_must_invade" | "ahead_can_coast" | "sequence_works" | "biggest_move";
```
**Behavior.** With X the player, G the move, E the engine's best, all scores from X's side, `N(p)` the empty points within `neighborhood_radius` of p (plus pass):
- *Threat.* `threat.value` = [after G, the opponent passes, X's best move in N(G)] − [after G, the opponent's best move in N(G)]: the local swing, the size of what G threatened. `tenuki_value` = [opponent's best answer in N(G)] − [opponent's best move anywhere]: positive when ignoring G was better for the opponent.
- *Defense.* X passes instead; the opponent's best move D in N(G); `defense.value` = [after G] − [after pass and D]. `groups` are X's groups (≥ 2 stones) within `defend_radius` of G, with their ownership before, after G, and after the pass and D.
- *Reply character* (§3.15) of the opponent's best answer R to G. When R is elsewhere, `left_behind` holds X's groups near R and their ownership after R.
- *Better move.* After E: is the best reply elsewhere (`gote`), and which groups of ≥ 3 stones differ by ≥ `group_change_min` between after G and after E.

Beliefs, in this order (the first is `belief`):
1. `needs_defending` — `defense.groups` is non-empty and every one stays above `needs_defending_alive` (0.7) after the pass and D.
2. `group_is_safe` — R is elsewhere and a `left_behind` group falls from ≥ `safe_group_dead` (0.3) to below it.
3. `is_sente` — R is elsewhere, `threat.value` ≥ `sente_threat_min` (0.5) and `tenuki_value` ≥ `tenuki_min` (0.5).
4. `behind_must_invade` — X led by ≥ `game_state_close` (5) and G's score stdev ≥ `risk_stdev_ratio` (1.5) × E's; `ahead_can_coast` — X trailed by ≥ 5 and G's stdev × 1.5 ≤ E's.
5. `sequence_works` — R is local and sharp and (1) did not match: the reading behind G fails; `expectation_probe` finds where.
6. `biggest_move` — R is elsewhere, E is gote too, and no group differs between the two.
**Cost.** 7–8 searches at `budget`. **Errors.** `bad_request` (pass, wrong colour), `illegal_move`, `unknown_ref`.

---

### 1.21 `expectation_probe`
**Purpose.** Find the misread: play out the line the student expected (or the one a player of their rank reads), check it move by move, and return the first move where it stops working and the move they never considered.

**Inputs.**
```ts
{ position: Position /* before the move */; move: Point | string; budget?: Budget = { profile: "line_node" };
  options?: { expected_line?: (string | LineStep)[];     // the student's line after the move, opponent first ("WD10", "BC10", …)
              plies?: number = active plan's per_episode.expectation_plies (6 base, 8 after ladder step L4);   // imagined moves after the move
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
**Behavior.** After the move, each imagined move comes from `expected_line` while it lasts, then from the `profile` human model's favourite legal move, for both sides. Each is scored against the engine's best at that node (the candidate's score when it was searched with a real share of visits, else one more search): `loss` for the side that plays it. The first move losing more than `misread_margin` is the misread: `whose: "opponent"` means the student expected a reply the opponent would not play — `never_considered` is the reply they missed; `whose: "you"` means the student's own follow-up fails. The refutation is `never_considered` followed by `refutation_plies` engine moves; its end carries `capture_races`, where liberty counts belong. No misread within `plies` means the reading holds; look for the belief in `intent_probe` (value, not reading).
**Cost.** 1–2 searches per imagined move, plus `refutation_plies` + 1. **Errors.** `bad_request` (an `expected_line` step for the wrong colour), `illegal_move`, `unknown_ref`.

---

### 1.22 `start_verification`
**Purpose.** After triage and the re-plan, start the background verification of the selected episodes: the probes of the belief protocol that do not need the interview answer run while Claude interviews the student.

**Inputs.**
```ts
{ job_id: string;
  episodes: (string | { id: string;                       // a digest episode id ("E3"), or any id with move_number
                        move_number?: number;               // the student's move, for a moment outside the digest
                        teachable?: Point;                  // = teachable_move_preliminary (else the survey's best)
                        local_solve?: true | { group_point: Point; at?: "before" | "after" = "before" } })[] }
```
**Output.**
```ts
{ job_id: string; interview_order: string[];             // episodes with a local_solve first, then as given
  episodes: EpisodeView[]; already_computed: Record<string, string[]>;   // task names already done (speculative work, §1.4)
  eta_seconds: number | null; sealed: true; note: string; query_id: string }
type EpisodeView = { episode: string; move_number: number; played: Point; teachable: Point;
  position_ref_before: PositionRef; position_ref_after: PositionRef; selected: boolean; sealed: boolean; interviewed: boolean;
  state: "queued" | "running" | "done";
  tasks: { task: string; needs_answer: boolean; state: "pending" | "running" | "done" | "failed" | "skipped";
           query_id?: string; error?: Error["error"]; note?: string }[] }    // query_id, error, note withheld while sealed
```
**Behavior.** Queues, per episode, from `position_ref_before` with the played move G and the teachable move E: `intent_probe(G)`, `forced_line(E)`, `forced_line(G)`, `terminal_features(end of G's line, compare_to: end of E's line)`, `analyze_position` at `root` and at each of the plan's `stability` multipliers, a supporting `swing_value` when `intent_probe`'s belief is `is_sente` (the threat's follow-up and the opponent's reply) or `biggest_move` (G and E), and `local_solve` when asked: at `group_point`, or (`true`) on the first defended group for `needs_defending` (before G) or the weakest group left behind for `group_is_safe` (after G); other beliefs skip it with a note. Each runs through the same tool Claude would call, at the active plan's sizes, so it lands in the stored results (§0.8). One worker runs everything at KataGo priority `background_priority` (5): above the survey (0), below Claude's own calls (10), which run in between. Order: answer work of interviewed episodes first (§1.23), then the selected episodes' work in `interview_order`, then speculative work breadth-first. From now until `record_interview`, each listed episode is **sealed** (§0.8). A speculative episode with the same move is adopted, keeping its done work; done work at sizes the current plan no longer uses runs again; speculative episodes not listed are cancelled. Calling again adds or updates episodes.
**Cost.** The answer-free part of the plan's per-episode unit (§1.2.2), in the background. **Errors.** `job_not_found`, `job_not_finished`, `episode_not_found` (an id not in the digest without `move_number`), `bad_request`, `illegal_move` (a `teachable` that cannot be played).

---

### 1.23 `record_interview`
**Purpose.** Save the student's interview answer for an episode: unseal it and queue the probes that need the answer. Call again to add later answers (a guess at the better move, a resistance line).

**Inputs.**
```ts
{ job_id: string; episode: string;
  answer?: string;                         // verbatim, kept with the episode
  expected_line?: (string | LineStep)[];   // what they expected after their move, opponent first; checked for legality now
  fix?: Point | Point[];                   // their own better move at the episode's position (from the interview or a quiz)
  lines?: { label?: string; moves: (string | LineStep)[]; from?: "before" | "after" = "before" }[];   // e.g. a resistance line
  confidence?: number }                    // 1–5, how sure they were at the time
```
**Output.** `{ job_id; episode; sealed: false; first_answer: boolean; expected_line: string[] | null; fixes: Point[]; lines: { label; from; moves: string[] }[]; queued: string[]; eta_seconds: number | null; query_id }`
**Behavior.** Every line and move is legality-checked with alternating colours before anything changes (an error leaves the episode sealed, so the question can be asked again). The first call queues `expectation_probe(G, expected_line)` (the peer-read line when none was given) and, after it, `analyze_position` at the first `stability` multiplier at the node where it found the misread (`skipped` when there is none). A later `expected_line` replaces it (a done peer-read version is kept as `expectation_probe_peer`). Each `fix` queues `forced_line(fix)`; each line queues `analyze_line` from the position before (or after) the played move, under the name `line:<label>`. This work runs ahead of other episodes' answer-free work.
**Cost.** As the probes queued. **Errors.** `job_not_found`, `episode_not_selected`, `illegal_move`, `wrong_color`, `bad_request`.

---

### 1.24 `verification_results`
**Purpose.** Progress of the background verification, or one episode's results.

**Inputs.** `{ job_id: string; episode?: string; wait_seconds?: number = 0 /* ≤ max_wait_seconds (240) */; parts?: string[]; action?: "results" | "cancel" = "results" }`
**Output.** Without `episode`: `{ job_id; episodes: EpisodeView[] /* the selected ones, else the speculative */; speculative: string[]; running: string | null; eta_seconds; state: "running" | "done"; query_id }`. With `episode`: its `EpisodeView` plus, once its interview is recorded,
```ts
{ results: Record<string, object>;        // task name -> that probe's full output, with its query_id: intent_probe, forced_line_best,
                                          // forced_line_played, terminal_features, root, stability_x4, stability_x16, supporting_test
                                          // ({ belief, points, swing_value }), local_solve ({ group_point, at, … }), expectation_probe,
                                          // stability_misread, fix:<point>, line:<label>
  stability_check: null | { root: { top_move; score_lead; visits; query_id };
                            runs: { multiplier; visits; top_move; score_moved; stable; query_id }[]; stable: boolean | null;
                            misread_node?: { never_considered; stability_top_move; stable; query_id } };
  interview: { answers: { answer; confidence; at }[]; expected_line; fixes; lines };
  eta_seconds: number | null }
```
A sealed episode returns its `EpisodeView` (states only) and a `note`. `stability_check` applies katago-analysis §5: stable when the top move is unchanged and the score moved less than `stability_margin` (0.5). `wait_seconds` blocks until the episode's queued work (or, without `episode`, all of it) is done. `parts` limits `results` to the named tasks or kinds. `action: "cancel"` cancels the queued work (a running probe finishes).
**Errors.** `job_not_found`, `verification_not_started`, `episode_not_found`, `bad_request`.

---

### 1.25 `dashboard_row`
**Purpose.** Make a row for the live review page: the game record, or a board (a position of the game with an optional line, marked points, a question, and an answer the student gives by clicking). Claude writes the row unchanged to the page's `db` with ArtifactData; the page checks its SHA-256 and shows it. No engine, so it is allowed while results are sealed.

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
            episode?: string;
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
**Behavior.** `line` is legality-checked from the position after `at_move` (errors as `record_interview`); `ask_color` is the side to move after `line`. `sha256` is the SHA-256 of the canonical JSON (§5) of the row without `title`, `text` and `sha256`: wording may be changed when writing, nothing else. The game row of a job and of its SGF are identical. Every row is appended to `reviews/<game_id>/dashboard_rows.jsonl`, which also numbers the boards (`seq`). With `from_game` the row still belongs to this review (`game_id`, `seq`, the log) and carries the past game's record up to `at_move`, which the page replays instead of the review's game (a recall quiz on an old lesson, go-teaching §4.4).

**The page side** (review-dashboard skill). The live page (`build_dashboard.py --live`) is published with `capabilities: { db: { rules: [{ path: "", read: "view", write: "owner" }] }, user: {} }` and reads `review/game`, `boards/*` and `answers/*`. It shows no engine data: no score, graph or ownership. A row whose checksum fails is listed as "did not arrive intact". The student's answer to a board with `ask` is written by the page to `answers/<board id>` as `{ board, moves: string[] /* "WQ7" */, sent_at }` (one move for `ask: "move"`); the page refuses an occupied point, suicide and an immediate ko retake, and `record_interview` checks the moves again. The Phase 6 dashboard is republished to the same URL; the `db` survives the republish, and the page lists its boards under "During the review".
**Cost.** None. **Errors.** `bad_request` (unknown `kind`, no `title`, `at_move` out of range, a bad `ask` or `highlight` point), `illegal_move`, `wrong_color`, `invalid_sgf`, `job_not_found`.

---

## 2. Tool × mechanic map

| Mechanic (from the plan) | Tools |
|---|---|
| Plan tests; ad-hoc continuations; punishability | `analyze_line` |
| Proof tree: must-move lines (played and better move, natural resistance) and what differs at their ends | `forced_line`, `terminal_features` |
| Belief protocol: the belief behind a move; the misread and the move never considered | `intent_probe`, `expectation_probe` |
| Pass probe for urgency | `pass_probe` |
| Swing counting; sente/gote | `swing_value` |
| Local solve (alive / dead / unsettled) | `local_solve` |
| Acceptable set | `analyze_position`, `job_results` |
| Risk via score stdev | `analyze_position` (`score_stdev`) |
| Human-model layers; learnability; punishability | `human_move_distribution`, `analyze_line` (`refutation_probability`), `job_results` |
| Group status; ownership attribution; aji map | `group_status`, `ownership_diff`, `analyze_position` (`ownership_stdev`) |
| Overplay vs slack; search-signature tags; chains; decisive / last chance; got-away-with-it; positives; pattern hash | `job_results` (§3) |
| Stability | `analyze_position` at 4× / 16× (`profile: "stability"`) |
| Coordinate discipline | every tool; `render_board` |
| Engine-validated dashboard | `validate_variations` |
| Result reconciliation | `job_results.game.reconciliation` |
| Time budget | `plan_budget`, `engine_info` |
| Verification during the interviews; sealed results | `start_verification`, `record_interview`, `verification_results` |
| The live review page: boards instead of chat diagrams, answers clicked on the board | `dashboard_row` |

---

## 3. Derived metrics (exact definitions)

Notation: `P_k` is the position after move `k` (`P_0` after setup). `s_k` is the root `score_lead` of `P_k` from Black's perspective at survey visits. `w_k` likewise for winrate. Mover of move `n` has sign `σ_n` (+1 Black, −1 White). Thresholds are config (§6).

### 3.1 Points lost
`points_lost_n = max(0, σ_n · (s_{n−1} − s_n))`. Best-reply basis is implicit: `s_n` assumes best play from `P_n`.

### 3.2 Acceptable set at `P_{n−1}`
Candidates with `visits ≥ 0.05 × root visits` and `σ_n · score_lead ≥ σ_n · best − acceptable_margin` (1.0). `violation = σ_n · (best − played)` when the played move is outside the set.

### 3.3 Episodes (chains)
Seeds: the student's moves with `points_lost ≥ episode_min_loss` (2.0). Two seeds join a chain when they are within `cluster_plies` (12) of each other and their played points or best moves lie within Chebyshev distance `cluster_distance` (4), or when they are within 6 plies and fall in the same standard region; a chain spans at most `cluster_max_span` (24) plies and `cluster_max_moves` (6) student moves. The chain's root is its first seed; chains are ranked by the root's `points_lost` (the chain sum counts a group again each time both sides swing it: in the seed games 58 of 118 chains summed to more than 5× their root); `points_lost_total` sums the chain's seeds; `moves` spans from the root to the last seed. Region = standard region of the root's played point; `bbox` covers all seed points.

### 3.4 Phases from ownership settledness
`settled_k` = fraction of points with `|ownership| ≥ settled_abs` (0.8) at `P_k`. `opening_end` = the first `k` with `settled_k ≥ opening_settledness` (0.35) or `k ≥ 50`, whichever comes first. `endgame_start` = the first `k` with `settled_k ≥ endgame_settledness` (0.75) such that `settled_j ≥ 0.70` for all `j > k`. Between them is the middlegame. `middlegame` or `endgame` is `null` when the game ends before that phase begins (short or resigned games).

### 3.5 Game type
`top_episode_share = max episode points_lost_total / student total points lost`. `single_blunder` if ≥ 0.40; `accumulation` if ≤ 0.20; else `mixed`.

### 3.6 Decisive moment and last chance
Student perspective series `w'_k = w_k` (Black) or `1 − w_k` (White). If the student lost: `decisive` = the first student move `n` with `w'_n ≤ decided_winrate` (0.15) and `w'_j ≤ recovery_winrate` (0.35) for all `j > n`. If the student won: the first opponent move `n` with `w'_n ≥ 1 − decided_winrate` and `w'_j ≥ 1 − recovery_winrate` thereafter, reported as `by: "opponent"`. In handicap games (or whenever `w'` is below 0.05 or above 0.95 for the first 30 moves) the same rule is applied to score with `decided_score_handicap` (−15) and `recovery_score_handicap` (−8), and `basis: "score"`. `last_chance` = the last student move `n` at or before `decisive` (it is often the decisive move itself, when a move there still kept the game) such that the best move at `P_{n−1}` evaluates to `w' ≥ recovery_winrate` (or score ≥ recovery score), with that counterfactual evaluation reported.

### 3.7 Search-signature candidate tags
Computed at each episode root (taxonomy ids from `go-teaching` §1). Up to three tags, in this order of precedence:
Thresholds calibrated in WS8 (20 seed games; notes in `seed/calibration.md`, which stays local because `seed/` is not in git). "Plausible" uses the human model's peer-rank probabilities (`human.played.peer`, `human.best.peer`), falling back to KataGo's policy priors when the human model is unavailable: KataGo's policy almost always prefers the best move, so the policy-only rule never fired.
- **13 Failure to punish**: the opponent's previous move lost ≥ `tag_punish_min_loss` (5) points and the student's move gives back ≥ `got_away_ratio` (0.6) of it.
- **3 / 4 / 5**: peer(played) ≥ `tag_plausible_min_peer` (0.20), peer(played) ≥ `tag_plausible_ratio` (1.5) × peer(best) and `points_lost ≥ tag_plausible_min_loss` (2) (search refutes a move the student's rank plays) → 3 if the student's own group status falls after the played move, 4 if the opponent's group status rises after the best move (missed attack), else 5.
- **6 / 15 / 1 / 2**: intuition failed — `prior_best ≥ 0.20` and `prior_played ≤ 0.10`, or (when 3/4/5 did not fire) target(best) ≥ `tag_intuition_best_min` (0.20) and peer(played) ≤ `tag_intuition_played_max` (0.10) → 6 if `dist(best, played) ≤ 2`; else 15 if the best move is in the same standard region; else, when `dist ≥ tag_direction_min_distance` (5), 1, plus 2 when the best move's region contains an unsettled group (§3.9, ownership at `P_{n−1}`, ≥ 2 stones) of either color.
- **1**: `local_loss_share ≤ local_share_global` (0.4) and `dist(best, played) ≥ tag_direction_min_distance` (5), unless 6 or 15 already applies.
- **9**: `style_axis = overplay` and `score_stdev_played ≥ 1.5 × score_stdev_best`.
- **10**: the best move's region has mean `ownership_stdev ≥ 0.35` and the played move is elsewhere (the survey requests `ownership_stdev` from katago-mcp 0.4.1; older surveys lack it and never fire 10).
- **11**: phase is endgame and none of 3/4/5 applies.
- **12**: a ko capture is legal for either side at `P_{n−1}`.
- **7**: opening phase, best move in a corner region, `n ≤ 40`.
- **14**: the played move raises the student's own groups' ownership by ≥ `tag_passive_own_up` (0.5) points while the best move lowers the opponent's by ≥ `tag_passive_opp_down` (0.5). (The earlier 1 and 3 never fired: on 210 seed episodes the opponent's drop is ≤ 0.7 at the 90th percentile; 0.5/0.5 fires on about 3 %. Not yet rated by the student.)
`local_loss_share` is the primary region's share from `ownership_diff(P_{n−1}, P_n)`.

### 3.8 Style axis
Requires ownership after the best move (one `quick` search per episode root, done in the survey for the top `max_episodes` episodes). Let `Δown = Σ over the student's groups of (ownership after best − ownership after played)` and `Δopp = Σ over the opponent's groups of (ownership after played − ownership after best)`, both in points, both ≥ 0 by construction of the comparison. `own_share = Δown / (Δown + Δopp)`. `overplay` if `own_share ≥ 0.6` (the best move mainly protects you: you neglected defense), `slack` if `≤ 0.4` (the best move mainly hurts the opponent: you missed the attack), else `neutral`.

### 3.9 Group status labels
Owner-perspective mean ownership: `alive` if ≥ `alive` (0.6); `dead` if ≤ `dead` (−0.6); else `unsettled`. Status *changes* in `Episode.group_status_change` list the three largest ownership changes among groups of ≥ 3 stones.

### 3.10 Got away with it
Student move `n` with `points_lost ≥ 3` where the opponent's move `n+1` or `n+3` has `points_lost ≥ got_away_ratio × points_lost_n`; `restored_points` is that opponent loss.

### 3.10b Positives
Student moves with `points_lost ≤ 0.5` that the peer rank rarely plays (peer probability ≤ 0.10), excluding passes and positions without a real choice: the best two candidates must differ by ≥ `positive_min_choice` (0.5) points (otherwise every move was as good, as in dame). Sorted by peer probability, at most 5.

### 3.11 Learnability and preliminary teachable move
Within the acceptable set at the root, the preliminary teachable move is the one with the highest `target` human probability; `learnability` is that probability. Verification may change the teachable move; the final value is whatever Claude records in the ledger from the verified `analyze_position`.

### 3.12 Pattern hash
The 7×7 window centered on the root's played point at `P_{n−1}`: each cell ∈ {own stone, opponent stone, empty, off-board} with colors normalized so the mover is Black; canonicalized over the 8 board symmetries (lexicographically smallest serialization); SHA-1, first 12 hex, prefixed `ph_`. `pattern_hash_5` is the same over 5×5, prefixed `ph5_`.

### 3.13 Stability flag
An episode is `stable` when, at survey visits, the best move's visit share is ≥ 0.35 and its score-lead margin over the second candidate is ≥ 0.5; `unstable` when the margin is < 0.3 or the played move's `lcb` exceeds the best move's; else `unknown`. Verification replaces this with the 4× / 16× reruns.

### 3.14 Reconciliation
For games decided by counting: `engine_final_score = s_M` (Black perspective) compared with the SGF margin signed for Black. `ok` if `|diff| ≤ 2.5`; else `mismatch` (komi, rules or handicap bonus is probably wrong; Claude reports and stops). `n/a` for resignations, timeouts and forfeits.

### 3.15 Reply character
From the survey search at `P_n` (the position after the episode's root move, the opponent to move): the best reply is `local` when it lies within `local_radius` (4, Chebyshev) of the root move. `gap` = how much the best reply beats the best candidate of the other kind (local vs non-local), for the replier. `tenuki` — the move did not need an answer; `local_sharp` — answering here is worth ≥ `sharp_margin` (3) more than the best move elsewhere, or no non-local candidate was searched (the move started a fight, or overplayed); `local_calm` — answered, but little rides on it. A cheap first signal for the belief probes (§1.18–1.21); not a verdict.

### 3.16 Capture races
Two adjacent groups of opposite colour (each ≥ 2 stones), both `unsettled` (§3.9) and both with at most `race_max_liberties` (4) liberties. Liberty counts are reported only for groups in a race (and when `include_liberty_points` / `label_low_liberties` ask for them): outside a race the count is not what decides the position, and a number in the output invites commentary about it.

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

The exported JSON is what `validate_variations` assembles (§1.17) and what the `review-dashboard` template reads, with these encodings, chosen so Claude can copy the blob verbatim:
- `moves`: array of `Move` values as `"BQ7"` strings (color letter + point, `"Bpass"` for a pass).
- `scoreSeries`: numbers rounded to one decimal, student perspective, index = move number (index 0 = after setup).
- Ownership snapshots: 361-character strings; each character encodes ownership in 0.1 steps, `a` = −1.0 … `k` = 0.0 … `u` = +1.0 (`index = round((o + 1) × 10)`), Black-positive. Keys: `"m87"` for the position after move 87; `"E1:B1:end"` for a branch end.
- Branch `evals`: one number per node (score lead, student perspective, one decimal).
- Quiz candidates: `[{ "move": "Q8", "pointsLost": 0.0, "note": "" }]`, including the actual and peer moves, labeled.
- `meta`: `{ "game_id", "job_id", "visits_per_move", "server_version", "contract_version": "0.5.0", "exported_at" }`.
- The blob is minified; `sha256` is over the exact bytes of `dashboard_data`. Typical size: 10–20 KB.

---

## 6. Per-machine configuration (`config/<machine>.toml`)

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

[budget]
overhead_minutes = 4.0
claude_minutes_per_episode = 1.0
self_review_minutes_default = 5           # blind self-review
interview_minutes_default = 5             # per-episode interviews
survey_minutes_target = 10                # the survey is sized to finish in about this long
survey_floor = 100
survey_cap = 1000
ld_reserve_episodes = 1
student_lines_per_episode = 1             # §1.2.1 L
[budget.unit_base]
root = 1000
line_node = 250
plies = 6
stability = [4]
solve = 1500
[budget.unit_cap]
root = 6000
line_node = 1000
plies = 8
stability = [4, 16]
solve = 4000
ladder = ["root_3000", "line_600", "stability_16x", "plies_8", "solve_4000_all_ld", "root_6000", "line_1000"]

[thresholds]
acceptable_margin = 1.0
episode_min_loss = 2.0
cluster_plies = 12
cluster_distance = 4
alive = 0.6
dead = -0.6
settled_abs = 0.8
opening_settledness = 0.35
endgame_settledness = 0.75
decided_winrate = 0.15
recovery_winrate = 0.35
decided_score_handicap = -15
recovery_score_handicap = -8
got_away_ratio = 0.6
local_share_local = 0.7
local_share_global = 0.4
stability_visit_share = 0.35
stability_margin = 0.5
race_max_liberties = 4                    # §3.16
local_radius = 4                          # §3.15: a reply this close to the move is local
sharp_margin = 3.0                        # §3.15
forced_margin = 3.0                       # §1.19
human_margin = 1.0                        # §1.19
territory_diff_min = 2.0                  # §1.18
group_change_min = 0.2                    # §1.18
defend_radius = 2                         # §1.20
neighborhood_radius = 3                   # §1.20
needs_defending_alive = 0.7               # §1.20
safe_group_dead = 0.3                     # §1.20
sente_threat_min = 0.5                    # §1.20
tenuki_min = 0.5                          # §1.20
risk_stdev_ratio = 1.5                    # §1.20
misread_margin = 3.0                      # §1.21

[verification]
speculative_episodes = 4                  # §1.4: precompute this many top episodes after a planned survey (0: off)
background_priority = 5                   # §1.22: KataGo priority of queued probes
max_wait_seconds = 240                    # §1.24: cap of wait_seconds

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
    analysis.json         # survey results and derived metrics, written incrementally
    positions/            # PositionRef → position record
    queries.jsonl         # one line per tool call: query_id, timestamp, tool, arguments, KataGo query ids, visits, seconds, cached, result summary
    plan.json             # the active plan and its re-plans
    export-<n>.json       # each dashboard_data blob returned by validate_variations, with its sha256
    dashboard_rows.jsonl  # every row made by dashboard_row (the game record, the boards), in order
  _adhoc/
    queries.jsonl
```

---
