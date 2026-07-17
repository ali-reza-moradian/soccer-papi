<#
.SYNOPSIS
  UFC price loop runner (mirrors scripts\run_tennis_loop.ps1, but --sport ufc).

.DESCRIPTION
  Re-invokes `python -m src.genz.cli run --sport ufc --once` every 90s (genz.ufc.interval_seconds).
  A FRESH interpreter per cycle -> newly committed code is live within one cycle, no restart. This is a
  SEPARATE process from the soccer/MLB/tennis loops and writes only the UFC-scoped runtime files
  (ufc_tree.json, genz_snapshot_ufc.json, genz_heartbeat_ufc.json, genz_arbs_ufc_*.csv,
  papermaker_ufc_*). The other three pipelines are untouched. ASCII-only (the PS5.1 em-dash trap).
#>

$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location C:\bots\soccer-papi

$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) { . $secrets } else {
    Write-Warning 'scripts\secrets.local.ps1 not found - relying on pre-set environment variables.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

while ($true) { & $py -m src.genz.cli run --sport ufc --once; Start-Sleep -Seconds 90 }
