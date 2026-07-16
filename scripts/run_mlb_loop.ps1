<#
.SYNOPSIS
  MLB price loop runner (mirrors scripts\run_genz_loop.ps1, but --sport mlb).

.DESCRIPTION
  Re-invokes `python -m src.genz.cli run --sport mlb --once` every 45s (genz.mlb.interval_seconds).
  A FRESH interpreter per cycle -> newly committed code is live within one cycle, no restart. This is
  a SEPARATE process from the soccer loop and writes only the MLB-scoped runtime files
  (mlb_tree.json, genz_snapshot_mlb.json, genz_heartbeat_mlb.json, genz_arbs_mlb_*.csv,
  papermaker_mlb_*). The soccer pipeline is untouched. ASCII-only (the PS5.1 em-dash trap).
#>

$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location C:\bots\soccer-papi

# Load local secrets if present (else rely on pre-set environment variables), like run_genz_loop.ps1.
$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) { . $secrets } else {
    Write-Warning 'scripts\secrets.local.ps1 not found - relying on pre-set environment variables.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

while ($true) { & $py -m src.genz.cli run --sport mlb --once; Start-Sleep -Seconds 45 }
