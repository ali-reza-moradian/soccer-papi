<#
.SYNOPSIS
  OG multi-sport scanner loop runner (mirrors scripts\run_mlb_loop.ps1, but the 4-book og_multi scan).

.DESCRIPTION
  Re-invokes `python -m src.og_multi` every 300s (the MIN of og_multi.interval_s). A FRESH interpreter
  per cycle -> newly committed code is live within one cycle, no restart. ONE process serves ALL
  sports; per-sport cadence (mlb/tennis 300s, ufc 600s) is enforced INSIDE the python via last-run
  stamps, so a 300s wrapper sleep lets each sport fire on its own schedule. ALERT-ONLY: no executor,
  no orders. Writes only data/og_current_<sport>.json + the data/og_multi/ caches; the soccer OG and
  every GenZ sport loop are untouched. ASCII-only (the PS5.1 em-dash trap).
#>

$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location C:\bots\soccer-papi

# Load local secrets if present (else rely on pre-set environment variables), like run_mlb_loop.ps1.
$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) { . $secrets } else {
    Write-Warning 'scripts\secrets.local.ps1 not found - relying on pre-set environment variables.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

while ($true) { & $py -m src.og_multi; Start-Sleep -Seconds 300 }
