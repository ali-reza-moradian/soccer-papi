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

# RESTART ECONOMICS. Exit 0 is a DELIBERATE restart (a git HEAD change so the deploy adopts fresh
# bytecode, or STOP_ALL) and the process has already cancelled every resting order on the way out - so
# the only thing the pause buys is dead time with no maker on the book. Deploys run 11-21x/day, so 5s
# each is a minute or two of daily absence for nothing. A NON-zero exit is different: it is a crash or
# the singleton refusal (exit 3), where backing off is the point - keep the full 5s there so a
# crash-loop cannot spin.
while ($true) {
    & $py -m src.genz.maker_rt
    if ($LASTEXITCODE -eq 0) { Start-Sleep -Seconds 1 } else { Start-Sleep -Seconds 5 }
}
