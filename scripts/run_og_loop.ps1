<#
.SYNOPSIS
  OG scanner loop runner — run this in ITS OWN WINDOW (mirrors the GenZ manual-window pattern).

.DESCRIPTION
  Re-invokes `python -m src.run` every 300s. Notes:
    * Run it in its own dedicated window; leave it up.
    * src/run.py's scan lock already prevents overlap, so a slow scan never double-runs.
    * 300s cadence  ~=  12 scans/hr  ~=  48 the-odds-api credits/hr
      (4 markets x 1 region = 4 credits/poll; see config.yaml theoddsapi.markets/regions).

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

while ($true) { & $py -m src.run; Start-Sleep -Seconds 300 }
