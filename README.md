# Go-Teacher
A Claude project that reviews your OGS games the way a strong, patient teacher would: a quick pass over the whole game for its story, then the key moments one at a time — what you were thinking, what actually happens (KataGo is the authority on that), back and forth until you can say what you will do differently next time. Everything ends on an interactive page that also shows the engine's best move at every move, and a memory of your takeaways notices when the same thing keeps coming back.

## What is here

| Path | What it is |
|---|---|
| [katago-mcp/](katago-mcp/README.md) | The local MCP server that runs KataGo and computes every derived metric. Install, benchmark, register and test instructions are in its README. |
| [skills/go-teacher-flow/](skills/go-teacher-flow/SKILL.md) | The review procedure, step by step, for a plain Claude Desktop chat. Also holds the memory schema and the canonical tool contract. |
| [skills/katago-analysis/](skills/katago-analysis/SKILL.md) | How to use the tools: the story of the game, `explain_moment`, follow-up tools, what keeps explanations honest. |
| [skills/go-teaching/](skills/go-teaching/SKILL.md) | How to teach: the teacher's review pattern, the questions, the explanation standard, takeaways, rules of conduct. |
| [skills/review-dashboard/](skills/review-dashboard/SKILL.md) | The dashboard template and the build script that only accepts engine-validated data. |
| [skills/go-teacher-flow/references/seeding.md](skills/go-teacher-flow/references/seeding.md) | Seeding: a brief pass over your recent games (story, your feedback, one confirmed lesson each) that fills or refills the memory. Ask a chat to "seed the teacher". |
| [docs/plan.md](docs/plan.md) | The build plan, decisions and current status. |
| [docs/contract-changes.md](docs/contract-changes.md) | The tool contract's decisions and version history. |
| [skills/go-teacher-flow/references/tool-contract.md](skills/go-teacher-flow/references/tool-contract.md) | The tool contract (the only copy; skills must be self-contained). |

`seed/` (local, not in git) holds the seed games and calibration notes from the earlier taxonomy design.

## Quick start

```bash
cd katago-mcp
./install/macos.sh m5pro                     # or m2air; Windows: .\install\windows.ps1
source .venv/bin/activate
katago-mcp benchmark --config config/m5pro.toml
katago-mcp selfcheck --config config/m5pro.toml
python3 install/register_claude_desktop.py --config config/m5pro.toml
./run_tests.sh                               # offline, no KataGo needed
```
