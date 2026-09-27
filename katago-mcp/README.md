# katago-mcp

An MCP server that exposes KataGo as a *teaching* tool surface for the Go-teacher Claude project.
It implements **tool contract v0.1** (17 tools): whole-game surveys, budgeted verification
(`plan_budget`), forced-line playouts, pass probes, swing values, local life-and-death solves,
human-model move distributions, and the checksummed dashboard export.

Design rules baked in:

- Coordinates are GTP (`A1`…`T19`, no `I`). Scores are **points**, from the stated perspective
  (default: the student's colour when the position belongs to a job). Ownership is Black-positive.
- The server computes; Claude narrates. Every derived metric of contract §3 is computed here.
- One server per machine, one KataGo process, one job at a time. Verification queries run at a
  higher KataGo priority than the survey so they return promptly.
- Nothing is a Go-truth claim unless it came out of the engine: branches are validated for legality
  and evaluated before they reach the dashboard, and the export carries a SHA-256.

## 1. Layout

```
katago_mcp/
  coords.py     GTP / SGF / index conversions
  board.py      board, groups, captures, ko, superko, suicide, Zobrist hashes
  sgf.py        SGF parser (OGS files, main line, handicap, result, ranks, OGS id)
  regions.py    the standard 9-region partition and Region specs
  render.py     ASCII boards and overlays (contract §4)
  config.py     per-machine TOML (contract §6)
  budget.py     plan_budget (contract §1.2), pure function
  engine.py     KataGo analysis-engine wrapper (+ MockEngine for tests)
  store.py      position refs, analysis cache, reviews/<game_id>/ persistence, query log
  metrics.py    derived metrics (contract §3): points lost, episodes, phases, tags, style axis, …
  jobs.py       asynchronous survey jobs
  tools.py      the 17 tools as plain Python
  server.py     FastMCP wiring (stdio)
  cli.py        serve | benchmark | selfcheck | sgf-summary | survey
config/         analysis.cfg + m5pro.toml, r5700xt.toml, m2air.toml
install/        macos.sh, windows.ps1
scripts/        verify_export.py (dashboard checksum check)
tests/          unittest suite (runs offline with the mock engine)
reviews/        created at runtime: <game_id>/game.sgf, analysis.json, queries.jsonl, export-N.json
```

## 2. Install (WS1) — per machine

Requirements: Python ≥ 3.11, KataGo ≥ 1.15 (human model support), the two networks
`kata1-b18c384nbt-…` (latest from <https://katagotraining.org/networks/>) and
`b18c384nbt-humanv0.bin.gz` (KataGo releases page), saved in `models/` under the names used in the TOML.

**M5 MacBook Pro / M2 MacBook Air**

```bash
./install/macos.sh m5pro      # or: ./install/macos.sh m2air
source .venv/bin/activate
katago-mcp benchmark --config config/m5pro.toml        # ~20 s, saves config/m5pro.throughput.json
katago-mcp selfcheck --config config/m5pro.toml --sgf path/to/game.sgf
```

Homebrew's KataGo may be an OpenCL build; a Metal build from source is usually faster on Apple
silicon. Either works — `plan_budget` uses the *measured* visits/s, so the plan adapts to whatever
you have. The Air is fanless: the benchmark is the 20-second figure, so sustained throughput will be
lower; if a survey runs late, `plan_budget` re-plans with the remaining time after triage.

**Windows + RX 5700 XT**

```powershell
.\install\windows.ps1
.\.venv\Scripts\katago-mcp.exe benchmark --config config\r5700xt.toml
.\.venv\Scripts\katago-mcp.exe selfcheck --config config\r5700xt.toml --sgf path\to\game.sgf
```

The first OpenCL start tunes kernels for the GPU (minutes); the install script triggers it once.

**What `selfcheck` proves**

1. the engine starts and the human model answers (`rank_7k` distribution on a test position);
2. the perspective convention: a solid Black corner must show clearly positive ownership — if it
   doesn't, `[katago].perspective` and `reportAnalysisWinratesAs` disagree;
3. with `--sgf`: a real survey runs end to end and the final engine score is reconciled against the
   SGF result (`mismatch` beyond ±2.5 points means komi/rules/handicap were read wrongly).

## 3. Running the tests

```bash
./run_tests.sh              # unittest, no KataGo needed (MockEngine)
python -m pytest            # if pytest is installed
```

36 tests: coordinates, board rules (captures, ko, suicide, superko), SGF parsing (handicap,
variations, ranks, results), regions (49/35/25 tiling), rendering, `plan_budget` against the two
worked examples of contract §1.2.4, and all 17 tools end to end on synthetic games including the
dashboard export and its checksum, job reuse across a server restart, and a handicap game.

## 4. Registering the server

**Claude Desktop** — add to `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "katago": {
      "command": "/ABS/PATH/katago-mcp/.venv/bin/katago-mcp",
      "args": ["serve", "--config", "/ABS/PATH/katago-mcp/config/m5pro.toml"],
      "cwd": "/ABS/PATH/katago-mcp"
    }
  }
}
```

On Windows use `C:\\...\\.venv\\Scripts\\katago-mcp.exe` and `config\\r5700xt.toml`.
Paths inside the TOML (`analysis_config`, `model`, `reviews_dir`) are relative to the working
directory, so keep `cwd` at the repo root or make them absolute.

**Claude Code**

```bash
claude mcp add katago -- /ABS/PATH/.venv/bin/katago-mcp serve --config /ABS/PATH/config/m5pro.toml
```

The engine starts when the server starts (10–60 s; OpenCL first run longer). Until it is ready,
tools answer with `engine_unavailable`.

## 5. Using it (what Claude does)

The review flow is the project's; the server only makes it cheap and honest:

1. `sgf_summary` (no engine) → confirm colour, rules, komi, handicap; `plan_budget(total_minutes)`
   → survey visits, episode count, per-episode search sizes.
2. `start_game_analysis` with `{"profile": "survey"}` → job runs while the student does the blind
   self-review; `job_status` to poll.
3. `job_results` → digest ≤ 4 k tokens: phases, points lost, episodes with signatures, candidate
   tags, human probabilities, decisive move, last chance.
4. `plan_budget(job_id, selected=[…])` → re-plan for the chosen episodes; then per episode:
   `analyze_position` (root), `analyze_line` ×3 (played / teachable / student's fix), `pass_probe`,
   `swing_value`, `local_solve`, `ownership_diff`, `human_move_distribution` as the hypothesis needs.
5. `validate_variations` → the dashboard data blob and its SHA-256, saved as `reviews/<game>/export-N.json`;
   `scripts/verify_export.py` re-checks the file.

Every call is logged to `reviews/<game_id>/queries.jsonl` with a `query_id` that the ledger cites.

## 6. Deviations from tool contract v0.1 (to fold into v0.2)

| # | Contract | Implementation |
|---|----------|----------------|
| 1 | survey cap 500 visits/move | **1000** (approved in review) — `[budget].survey_cap` |
| 2 | `pass_probe.urgency[]` = `opponent_best_there`, `threat` | `best_move_there`, `value`: the **player's** best move confined to each region and its value over passing (the direct "urgent vs big" ranking); nine restricted searches at ⅓ of the root budget |
| 3 | `analyze_line.summary.vs_best` | root estimate of the best first move (`score_after_best_root_estimate`, `gap_vs_first_step`); the played-out contrast is a second `analyze_line` |
| 4 | verification queries at lower KataGo priority during a job | **higher** priority (10 vs 0), so interactive calls return promptly |
| 5 | phases always three ranges | `middlegame` / `endgame` may be `null` (short or resigned games) |
| 6 | — | superko is checked positionally for every superko ruleset; KataGo remains the arbiter of legality inside searches |
| 7 | `validate_variations` quiz candidates | each candidate carries `labels` ⊆ {`actual`,`peer`,`best`} instead of separate fields |
| 8 | `Budget.seconds` | requires a measured throughput; otherwise `budget_infeasible` |
| 9 | — | chain clustering also caps a chain at 24 plies / 6 student moves (`cluster_max_span`, `cluster_max_moves`) |

## 7. Known limits (v0.1)

- 19×19 only (`unsupported_board_size` otherwise).
- One job at a time; a second `start_game_analysis` while a survey runs answers `engine_busy`.
- `human_policy` costs one extra 1-visit query per profile and position (cached per ref).
- The mock engine is *not* a Go engine: it only produces well-formed, self-consistent data for tests.
- The KataGo wrapper is written against the analysis-engine JSON protocol (`reportDuringSearchEvery`,
  `terminate`, `overrideSettings.humanSLProfile`, `humanPolicy`) but has not been run against a live
  engine in this sandbox — `selfcheck` is the first thing to run on each machine.
