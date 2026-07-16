<#
.SYNOPSIS
  MLB tree builder runner (mirrors scripts\run_tree_loop.ps1, but --sport mlb).

.DESCRIPTION
  Re-invokes `python -m src.genz.cli build-tree --sport mlb` hourly. Discovers MLB games in the next
  genz.mlb.lookahead_hours (24h), pairs Kalshi<->Polymarket moneyline + total-runs, and writes only the
  MLB tree (data\genz\mlb_tree.json + mlb_tree_meta.json). A FRESH interpreter per build. Separate from
  the soccer tree loop; nothing shared is overwritten. ASCII-only (the PS5.1 em-dash trap).
#>

$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location C:\bots\soccer-papi

$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) { . $secrets } else {
    Write-Warning 'scripts\secrets.local.ps1 not found - relying on pre-set environment variables.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

while ($true) { & $py -m src.genz.cli build-tree --sport mlb; Start-Sleep -Seconds 3600 }
