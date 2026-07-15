<#
.SYNOPSIS
  GenZ tree-builder loop runner - run this in ITS OWN WINDOW (mirrors scripts\run_og_loop.ps1).

.DESCRIPTION
  Re-invokes `python -m src.genz.cli build-tree` every 3600s (hourly). The static match tree is Job 1;
  the fast price loop (run_genz_loop.ps1) reloads it each cycle, so an hourly rebuild is picked up
  without restarting anything. A FRESH interpreter per cycle -> committed code is live within one hour.

  Secrets (ODDS_PAPI_KEY, ODDS_API_KEY, TELEGRAM_*) are sourced from the gitignored
  scripts\secrets.local.ps1 if present, exactly like scripts\run_scan.ps1.
#>

$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location C:\bots\soccer-papi

# Load local secrets if present (else rely on pre-set environment variables), like run_scan.ps1.
$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) { . $secrets } else {
    Write-Warning 'scripts\secrets.local.ps1 not found - relying on pre-set environment variables.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

while ($true) { & $py -m src.genz.cli build-tree; Start-Sleep -Seconds 3600 }
