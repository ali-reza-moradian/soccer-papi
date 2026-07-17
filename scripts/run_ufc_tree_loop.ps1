<#
.SYNOPSIS
  UFC tree builder runner (mirrors scripts\run_tennis_tree_loop.ps1, but --sport ufc).

.DESCRIPTION
  Re-invokes `python -m src.genz.cli build-tree --sport ufc` every 2h. Discovers UFC fights in the next
  genz.ufc.lookahead_hours (168h - cards are weekly), pairs Kalshi<->Polymarket FIGHT WINNER only, and
  writes just the UFC tree (data\genz\ufc_tree.json + ufc_tree_meta.json). A FRESH interpreter per build.
  Separate from the soccer/MLB/tennis tree loops; nothing shared is overwritten. Rebuilding refreshes
  each fight start_utc (and drops fights that cancel/swap) so maker_rt re-anchors + disarms on reload.
  ASCII-only (the PS5.1 em-dash trap).
#>

$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location C:\bots\soccer-papi

$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) { . $secrets } else {
    Write-Warning 'scripts\secrets.local.ps1 not found - relying on pre-set environment variables.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

while ($true) { & $py -m src.genz.cli build-tree --sport ufc; Start-Sleep -Seconds 7200 }
