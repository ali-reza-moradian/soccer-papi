<#
.SYNOPSIS
  Tennis tree builder runner (mirrors scripts\run_mlb_tree_loop.ps1, but --sport tennis).

.DESCRIPTION
  Re-invokes `python -m src.genz.cli build-tree --sport tennis` hourly. Discovers ATP/WTA matches in the
  next genz.tennis.lookahead_hours (72h), pairs Kalshi<->Polymarket MATCH WINNER only, and writes just
  the tennis tree (data\genz\tennis_tree.json + tennis_tree_meta.json). A FRESH interpreter per build.
  Separate from the soccer/MLB tree loops; nothing shared is overwritten. Rebuilding hourly also
  refreshes each match start_utc so maker_rt re-anchors quotes to slid schedules. ASCII-only (PS5.1 trap).
#>

$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location C:\bots\soccer-papi

$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) { . $secrets } else {
    Write-Warning 'scripts\secrets.local.ps1 not found - relying on pre-set environment variables.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

while ($true) { & $py -m src.genz.cli build-tree --sport tennis; Start-Sleep -Seconds 3600 }
