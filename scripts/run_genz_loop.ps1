<#
.SYNOPSIS
  GenZ loop runner — run this in ITS OWN WINDOW (mirrors scripts\run_og_loop.ps1).

.DESCRIPTION
  Re-invokes `python -m src.genz.cli run --once` every 20s. Notes:
    * A FRESH interpreter per cycle -> newly committed code is live within one cycle, with no manual
      restart. Prefer this over a long-lived `run --loop`: a long-lived process keeps running STALE
      bytecode after a pull (the snapshot then lacks new fields and the dashboard flags OLD CODE).
    * Keep `run --loop` for dev only.

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

while ($true) { & $py -m src.genz.cli run --once; Start-Sleep -Seconds 20 }
