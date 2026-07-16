<#
.SYNOPSIS
  maker_rt runner - the realtime Kalshi/Polymarket maker/hedger (SHADOW by default).

.DESCRIPTION
  Runs `python -m src.genz.maker_rt` (a single long-lived asyncio process: real websockets, paper
  quotes, ZERO orders). The process self-exits 0 on a git HEAD change so a deploy adopts fresh
  bytecode; this wrapper restarts it after a short pause. The live-order path is built but hard-locked
  (config maker_rt.live.enabled false + an arm file + a startup self-check). ASCII-only (PS5.1 trap).
#>

$py = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
Set-Location C:\bots\soccer-papi

# Load local secrets if present (else rely on pre-set / .env environment variables).
$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) { . $secrets } else {
    Write-Warning 'scripts\secrets.local.ps1 not found - relying on .env / pre-set environment variables.'
}
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

while ($true) { & $py -m src.genz.maker_rt; Start-Sleep -Seconds 5 }
