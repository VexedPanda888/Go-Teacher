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
      "args": ["serve", "--config", "/ABS/PATH/katago-mcp/config/m5pro.toml"]
    }
  }
}
```

Or let the repo write it, with absolute paths and without disturbing other servers:

```bash
python3 install/register_claude_desktop.py --config config/m5pro.toml
```

The config lives in your home Library (`~/Library/Application Support/Claude/`, hidden in Finder:
use ⌘⇧G, or Settings → Developer → Edit Config inside the app), not in the app bundle, so it works
wherever Claude Desktop is installed — `~/Applications` is fine when `/Applications` needs admin rights.
Keep the repo itself out of `~/Desktop`, `~/Documents` and `~/Downloads`: macOS guards those folders, and a
server launched from Claude fails there with `/bin/sh: …: Operation not permitted` unless Claude has been
granted access to the folder (on managed Macs that grant may not be possible). `~/GitHub/…` is a good home.
On Windows use `C:\\...\\.venv\\Scripts\\katago-mcp.exe` and `config\\r5700xt.toml`.
Relative paths inside the TOML (`analysis_config`, `model`, `reviews_dir`) resolve against the repo
root (the parent of `config/`), and KataGo runs with the repo root as its working directory, so the
server works no matter where Claude Desktop launches it from. KataGo starts in the background right
after the handshake; a tool called before it is ready answers `engine_unavailable: katago is still
starting` — wait a few seconds and call again.

Logs: Claude Desktop writes the server's stderr to `~/Library/Logs/Claude/mcp-server-katago.log`
(macOS) or `%APPDATA%\\Claude\\logs\\` (Windows). KataGo's own logs go to `analysis_logs/` in the repo.

**Claude Code**

```bash
claude mcp add katago -- /ABS/PATH/.venv/bin/katago-mcp serve --config /ABS/PATH/config/m5pro.toml
```

KataGo starts on the first tool call that needs it, not when the server boots: Claude Desktop launches
two instances of every server (one for chat, one for its Cowork/Code pool), and only the one in use
should load a model. `engine_info` kicks the start off, so Phase 0's first call gets the model loading;
a tool called while KataGo is still loading waits up to 45 s and then answers
`engine_unavailable: katago is still starting` — call it again. Set `[katago].start_on_boot = true`
for eager starting. The server needs `mcp` 1.x (`mcp>=1.2,<2` in `pyproject.toml`); on `mcp` 2.x the
import fails with a message that says to run `pip install 'mcp<2'`.

## 4b. First live test on the Pro (what to look at)

1. `katago-mcp benchmark --config config/m5pro.toml` prints visits/second and writes
   `config/m5pro.throughput.json`. Expect a few hundred visits/s for b18 on an M5 Pro; the exact number
   only changes the plan, not the correctness.
2. `katago-mcp selfcheck --config config/m5pro.toml --sgf ~/Downloads/some-ogs-game.sgf --visits 200`
   - `engine_info` JSON: `engine_status: ready`, `human_model.loaded: true`, a `katago_version`.
   - `corner ownership (should be clearly positive): +0.9x` — if it prints a *negative* number the
     perspective convention is inverted: set `[katago].perspective = "SIDETOMOVE"` and re-run.
   - `human model ok: [...]` lists three plausible moves; a `human_model_unavailable` error means
     `-human-model` did not load (path, or KataGo < 1.15).
   - the survey progress line, then the digest excerpt: `reconciliation.status` should be `ok` for a
     game decided by counting (`n/a` for resignations); `mismatch` means komi/rules/handicap were
     read wrongly — send me the SGF header.
   - episodes: `(id, [from, to], points_lost, tags)` — sanity, not truth, at 200 visits.
3. Register the server (§4), restart Claude Desktop, open a plain chat (no project yet) and ask, in
   turn: "call engine_info", "here is an SGF … run sgf_summary", "start_game_analysis with 300 visits
   per move, then poll job_status", "job_results", "analyze_position for the position after move 60 and
   render_board it", "analyze_line the best move for 4 plies". Each answer should quote numbers that
   appear in the tool results.

If anything fails, the useful things to send back are: the selfcheck output, the last 40 lines of
`~/Library/Logs/Claude/mcp-server-katago.log`, and `katago version`.

## 5. Using it (what Claude does)

The review flow is the project's; the server only makes it cheap and honest:

1. `sgf_summary` (no engine) → confirm colour, rules, komi, handicap; `plan_budget(total_minutes)`
   → survey visits, episode count, per-episode search sizes. Give the game as an **OGS link or id**
   (the server downloads the SGF from online-go.com and caches it in `games/`) or as the **name of a
   file in `games/`**; raw SGF text is accepted but pasting it through the chat mangles long records.
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

## 5a. Memory over long sessions

KataGo's NN cache is capped at 2^19 entries in `analysis.cfg` (about 1–1.5 GB); the old 2^21 setting grew to
~6 GB over a seeding run and made a 24 GB machine swap, each game slower than the last. The server keeps at
most 4,000 analyses in RAM (a few hundred MB) and restarts KataGo before a survey once 6,000 queries have run
since its start (`[katago].restart_after_queries`, ~30 s on Metal). `job_status(job_id, "release")` frees a
finished job's memory; its results stay in `reviews/<game_id>/` and are reused. The seeding command restarts
KataGo every four games (`--restart-every`) and releases each game as it finishes.

## 5b. Seeding memory from past games (WS8)

```bash
katago-mcp-seed --config config/m5pro.toml --sgf-dir seed/ --visits 500
```

Surveys every `.sgf` in the folder (finished surveys are reused on re-runs), then writes
`seed/seed_summary.json` and `seed/seed_summary.md`. Paste the `.md` into a conversation in the Go-teacher
project and ask for the calibration pass: it checks reconciliation per game, reviews the tag frequencies
and the top episodes, proposes threshold changes for `config/*.toml` (`[thresholds]`), and writes the
seeded episodes into memory with `verdict: "SURVEY"` (unverified; half weight in recurrence).
Twenty games at 500 visits/move take roughly `20 × 200 × 500 / vps` seconds — about 50 minutes at
650 visits/s. Run it on the Pro and leave it.

## 6. Deviations from tool contract v0.1 (folded into contract v0.2)

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
