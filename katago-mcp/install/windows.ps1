# katago-mcp install helper for Windows + AMD RX 5700 XT (OpenCL backend).
# Run from the repo root in PowerShell:  .\install\windows.ps1
# Verify afterwards:  .\.venv\Scripts\katago-mcp.exe selfcheck --config config\r5700xt.toml
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$KataDir = "C:\katago"
if (-not (Test-Path "$KataDir\katago.exe")) {
  Write-Host ">> KataGo not found in $KataDir."
  Write-Host "   Download the latest *opencl-windows-x64* zip from https://github.com/lightvector/KataGo/releases"
  Write-Host "   and extract it to $KataDir (so that $KataDir\katago.exe exists), then re-run this script."
  exit 1
}
& "$KataDir\katago.exe" version | Select-Object -First 3

New-Item -ItemType Directory -Force -Path models | Out-Null
if (-not (Test-Path "models\kata1-b18c384nbt-latest.bin.gz")) {
  Write-Host ">> download the latest kata1 b18c384nbt network from https://katagotraining.org/networks/"
  Write-Host "   save as models\kata1-b18c384nbt-latest.bin.gz"
}
if (-not (Test-Path "models\b18c384nbt-humanv0.bin.gz")) {
  Write-Host ">> download b18c384nbt-humanv0.bin.gz from https://github.com/lightvector/KataGo/releases (v1.15.0+)"
  Write-Host "   save as models\b18c384nbt-humanv0.bin.gz"
}

if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\pip.exe install -q -e ".[dev]"

# The 5700 XT's thread count is [katago].search_threads in config\r5700xt.toml; analysis.cfg is shared and left alone.

# The first start of the OpenCL backend tunes kernels for the GPU (several minutes); do it once now.
# `katago tuner`, not `katago benchmark`: benchmark rejects analysis.cfg (no numSearchThreads, and its
# humanSL parameters need a -human-model flag that benchmark does not accept).
Write-Host ">> first-run OpenCL tuning (this can take a few minutes) ..."
& "$KataDir\katago.exe" tuner -model models\kata1-b18c384nbt-latest.bin.gz -config config\analysis.cfg 2>&1 | Select-Object -Last 5

Write-Host ""
Write-Host "Next steps"
Write-Host "  .\.venv\Scripts\katago-mcp.exe benchmark --config config\r5700xt.toml"
Write-Host "  .\.venv\Scripts\katago-mcp.exe selfcheck --config config\r5700xt.toml --sgf path\to\game.sgf"
Write-Host "  then register the server in Claude Desktop (README §4)."
