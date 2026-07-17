<#
.SYNOPSIS
  Tennis price loop runner (mirrors scripts\run_mlb_loop.ps1, but --sport tennis).

.DESCRIPTION
  Re-invokes `python -m src.genz.cli run --sport tennis --once` every 60s (genz.tennis.interval_seconds).
  A FRESH interpreter per cycle -> newly committed code is live within one cycle, no restart. This is a
  SEPARATE process from the soccer/MLB loops and writes only the tennis-scoped runtime files
  (tennis_tree.json, genz_snapshot_tennis.json, genz_heartbeat_tennis.json, genz_arbs_tennis_*.csv,
  papermaker_tennis_*). The soccer + MLB pipelines are untouched. ASCII-only (the PS5.1 em-dash trap).
#>

$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location C:\bots\soccer-papi

$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) { . $secrets } else {
    Write-Warning 'scripts\secrets.local.ps1 not found - relying on pre-set environment variables.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

while ($true) { & $py -m src.genz.cli run --sport tennis --once; Start-Sleep -Seconds 60 }
