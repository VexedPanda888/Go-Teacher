# Go-Teacher
A Claude project that reviews your OGS games the way a strong, patient teacher would: KataGo is the source of truth, every claim is verified before it is taught, each game yields two or three prioritized lessons delivered through an interactive dashboard, and a memory of recurring weaknesses makes the lessons increasingly personal.

## What is here

| Path | What it is |
|---|---|
| [katago-mcp/](katago-mcp/README.md) | The local MCP server that runs KataGo and computes every derived metric. Install, benchmark, register and test instructions are in its README. |
| [skills/go-teacher-flow/](skills/go-teacher-flow/SKILL.md) | The full review procedure, phase by phase, for a plain Claude Desktop chat. Also holds the memory schema and the canonical tool contract. |
| [skills/katago-analysis/](skills/katago-analysis/SKILL.md) | How to use the tools: budget, survey digest, the belief protocol and proof tree, the ledger. |
| [skills/go-teaching/](skills/go-teaching/SKILL.md) | What to teach: beliefs and taxonomy, triage, lesson format, self-review and interview questions, rules of conduct. |
| [skills/review-dashboard/](skills/review-dashboard/SKILL.md) | The dashboard template and the build script that only accepts engine-validated data. |
| [docs/plan.md](docs/plan.md) | The build plan, decisions and current status. |
| [skills/go-teacher-flow/references/tool-contract.md](skills/go-teacher-flow/references/tool-contract.md) | The tool contract (the only copy; skills must be self-contained). |

`seed/` holds the local seed games and calibration notes and is not in git.

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
