# katago-mcp

An MCP server that exposes KataGo as a *teaching* tool surface for the Go-teacher Claude project.
It implements **tool contract v0.5.0** (25 tools): whole-game surveys, budgeted verification
(`plan_budget`), forced-line playouts, pass probes, swing values, local life-and-death solves,
human-model move distributions, background verification during the student interviews
(`start_verification`, `record_interview`, `verification_results`), checksummed rows for the live review
page (`dashboard_row`), and the checksummed dashboard export.

Design rules baked in:

- Coordinates are GTP (`A1`…`T19`, no `I`). Scores are **points**, from the stated perspective
  (default: the student's colour when the position belongs to a job). Ownership is Black-positive.
- The server computes; Claude narrates. Every derived metric of contract §3 is computed here.
- One server per machine, one KataGo process, one job at a time. Three KataGo priorities: the survey
  at 0, the background verification queue at 5, Claude's own calls at 10, so whatever Claude is waiting
  for returns first.
- A tool call blocks Claude's turn, so slow work runs in the background while Claude talks to the
  student: the survey during the blind self-review, the verification probes during the episode
  interviews. Their results are stored whole, so Claude's later identical calls return at once, and are
  sealed per episode until its interview answer is recorded.
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
  verify.py     background verification queue, stored probe results, per-episode sealing
  tools.py      the 25 tools as plain Python
  server.py     FastMCP wiring (stdio)
  cli.py        serve | benchmark | selfcheck | sgf-summary | survey
  seed.py       katago-mcp-seed: survey a folder of SGFs for the WS8 calibration pass
config/         analysis.cfg (shared) + m5pro.toml, r5700xt.toml, m2air.toml (per machine)
install/        macos.sh, windows.ps1, register_claude_desktop.py
scripts/        verify_export.py (dashboard checksum check)
tests/          unittest suite (runs offline with the mock engine)
games/          created at runtime: SGFs given by name, and the OGS download cache (not in git)
reviews/        created at runtime: <game_id>/game.sgf, analysis.json, queries.jsonl, export-N.json (not in git)
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

**Search threads.** Each machine's thread count is `[katago].search_threads` in its TOML (m5pro 24,
m2air 8, r5700xt 16). The server passes it to KataGo with `-override-config`, so the shared
`analysis.cfg` never changes per machine. `./install/macos.sh m5pro 16` writes a different value
into the TOML.

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

The suite covers coordinates, board rules (captures, ko, suicide, superko), SGF parsing (handicap,
variations, ranks, results), regions (49/35/25 tiling), rendering, `plan_budget` against the two
worked examples of contract §1.2.3 and the interview overlap, all 25 tools end to end on synthetic games
including the dashboard export and its checksum, the background verification (sealing, run order,
KataGo priorities, stored results, line checks), the live review page (the page's checksum against the
server's in node, and the page itself in headless Chrome with a stand-in database: boards arriving,
an answer clicked and saved, the final dashboard keeping its boards; both skip when node or Chrome is
missing), job reuse across a server restart, and a handicap game. It also
checks that the three machine TOMLs share the same `[thresholds]`, `[budget]` and `[student]`, and
that the header of the tool contract (`skills/go-teacher-flow/references/tool-contract.md`) names the
code's versions.

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
server works no matter where Claude Desktop launches it from.

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
for eager starting. The server needs `mcp` 1.x (`mcp>=1.2,<2` in `pyproject.toml`). If `mcp` 2.x is
installed anyway, the server falls back to its renamed `MCPServer` class and warns on stderr; if tools
misbehave, run `pip install 'mcp<2'`.

## 4b. First live test on a machine (what to look at)

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
     game decided by counting (`n/a` for resignations); `mismatch` usually means komi/rules/handicap were
     read wrongly — compare the SGF header (`KM`, `RU`, `HA`, `AB`) with `sgf_summary`.
   - episodes: `(id, [from, to], points_lost, tags)` — sanity, not truth, at 200 visits.
3. Register the server (§4), restart Claude Desktop, open a plain chat (no project yet) and ask, in
   turn: "call engine_info", "here is an SGF … run sgf_summary", "start_game_analysis with 300 visits
   per move, then poll job_status", "job_results", "analyze_position for the position after move 60 and
   render_board it", "analyze_line the best move for 4 plies". Each answer should quote numbers that
   appear in the tool results.

If anything fails, look first at the selfcheck output, the last 40 lines of
`~/Library/Logs/Claude/mcp-server-katago.log`, and `katago version`.

## 5. Using it (what Claude does)

The review flow lives in the skills, not here: `skills/go-teacher-flow` (phase order, handoff files) and
`skills/katago-analysis` (budget steps, the belief protocol, tool recipes). The tool contract is
`skills/go-teacher-flow/references/tool-contract.md`. Give the game as an **OGS link or id** (the server
downloads the SGF from online-go.com and caches it in `games/`) or as the **name of a file in `games/`**;
raw SGF text is accepted but pasting it through the chat mangles long records.

Every call is logged to `reviews/<game_id>/queries.jsonl` with a `query_id` that the ledger cites.

**Background verification.** When a survey that was started under a plan finishes, the server
precomputes the probes of its top episodes (`[verification].speculative_episodes`, default 4; 0 turns it
off). After triage, `start_verification` queues the selected episodes' probes that need no interview
answer, `record_interview` queues the ones that do, and `verification_results` hands them over (contract
§1.22–§1.24). A precomputed result answers an identical probe call with `precomputed: {query_id}`. The
queue and the stored results live in memory: a server restart loses them, and the probes simply run
again when called.

## 5a. Memory over long sessions

Back-to-back games used to get slower one after another, for two reasons. Both are fixed, so the server
does not need periodic restarts:

- **KataGo's NN cache** is a fixed-size table, capped at 2^19 entries in `analysis.cfg` (about 1–1.5 GB).
  Once full it stops growing. The old 2^21 setting reached ~6 GB and made a 24 GB machine swap.
- **The server's own cache** keeps at most 4,000 analyses in RAM (a few hundred MB), evicting the least
  recently used, and looks them up by position. It used to be unbounded and scanned in full on every lookup.

`job_status(job_id, "release")` frees a finished job's memory; its results stay in `reviews/<game_id>/` and
are reused. The seeding command releases each game as it finishes.

As a guard, `start_game_analysis` restarts KataGo if its resident memory is above
`[katago].restart_above_mb` (default 4000; 0 disables). A restart reloads the model (~30 s on Metal). In
normal use memory levels off near 2 GB, so the guard does not fire; if you see its warning in the server log,
check `nnCacheSizePowerOfTwo`. The check uses `ps`, so it is skipped on Windows. `katago-mcp-seed
--restart-every N` still forces a restart every N games if you want one (default 0, off).

## 5b. Seeding memory from past games (WS8)

```bash
katago-mcp-seed --config config/m5pro.toml --sgf-dir seed/ --visits 500
```

Surveys every `.sgf` in the folder (finished surveys are reused on re-runs), then writes
`seed/seed_summary.json` and `seed/seed_summary.md`. Paste the `.md` into a plain Claude Desktop chat
(project chats cannot reach the local server) and ask for the calibration pass: it checks reconciliation per game, reviews the tag frequencies
and the top episodes, proposes threshold changes for `config/*.toml` (`[thresholds]`), and writes the
seeded episodes into memory with `verdict: "SURVEY"` (unverified; half weight in recurrence).
Twenty games at 500 visits/move take roughly `20 × 200 × 500 / vps` seconds — about 50 minutes at
650 visits/s. Run it on the Pro and leave it.

Add `--probes` to also run `intent_probe` on the five biggest episodes of each game (`--probe-episodes`,
`--probe-visits`, default 200). Each probed episode gets an inferred belief (`probe.belief`), and the
`.md` gains a belief table and, when one belief repeats among the biggest losses, the sentence that
says so ("3 of your 5 biggest probed losses share the belief 'needs_defending': …"). An intent probe is
about 8 searches, so this adds a few seconds per episode (estimated at roughly 10 minutes for 20 games
on the Pro). These beliefs are survey grade: inferred at low visits, never stated by the student.

## 6. Known limits

- 19×19 only (`unsupported_board_size` otherwise).
- One job at a time; a second `start_game_analysis` while a survey runs answers `engine_busy`.
- One background worker: queued probes run one after another. A probe already running cannot be
  cancelled; `verification_results(action: "cancel")` stops the ones still queued.
- `human_policy` costs one extra 1-visit query per profile and position (cached per ref).
- The mock engine is *not* a Go engine: it only produces well-formed, self-consistent data for tests.
- The KataGo wrapper uses the analysis-engine JSON protocol (`reportDuringSearchEvery`, `terminate`,
  `overrideSettings.humanSLProfile`, `humanPolicy`). It runs on the M5 Pro (the 20-game seed survey);
  `selfcheck` is the first thing to run on each new machine.
