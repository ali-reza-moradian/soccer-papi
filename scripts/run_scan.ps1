<#
.SYNOPSIS
  VM entrypoint for the soccer-papi arbitrage scanner (the 24/7 Windows VM is the sole scanner).

.DESCRIPTION
  Runs ONE scan cycle (python -m src.run) and then handles git for data/.

  Two modes, matching the LOCAL_RUN env contract the scanner documents:
    * Default (LOCAL_RUN): Telegram alerts ON, and ALL git add/commit/push is SKIPPED. This is the
      VM's normal mode while there is no push credential configured — data/ stays uncommitted locally.
      It is DISTINCT from DRY_RUN (which would instead suppress Telegram + CSV).
    * -Push: opt in to committing data/ and pushing to origin/main (needs a git remote credential /
      token to be configured first — see README "Local development / VM").

  Secrets (ODDS_PAPI_KEY, ODDS_API_KEY, TELEGRAM_BOT_KEY, TELEGRAM_GROUP_ID) are loaded from the
  gitignored scripts\secrets.local.ps1 if present (copy scripts\secrets.local.ps1.example to create
  it). They may also be supplied as pre-existing environment variables.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run_scan.ps1
      One free-profile scan; Telegram on; no git.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run_scan.ps1 -Push
      Same, then commit + push data/ (requires a configured git credential).
#>
[CmdletBinding()]
param(
    [switch]$Push   # opt IN to git commit/push; default skips all git (LOCAL_RUN)
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# 1) Secrets — source the gitignored local file if it exists (it sets $env:ODDS_PAPI_KEY, etc.).
$secrets = Join-Path $PSScriptRoot 'secrets.local.ps1'
if (Test-Path $secrets) {
    . $secrets
} else {
    Write-Warning "scripts\secrets.local.ps1 not found — relying on pre-set environment variables. " +
        "Copy scripts\secrets.local.ps1.example to scripts\secrets.local.ps1 and fill in the keys."
}

# 2) Mode — LOCAL_RUN keeps Telegram ON but skips ALL git. Pushing is opt-in (-Push).
if ($Push) { $env:LOCAL_RUN = '' } else { $env:LOCAL_RUN = '1' }

# 3) Prefer the repo venv python; fall back to PATH python.
$py = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

# 4) Run one scan cycle. (Native exit code is preserved; stderr is not redirected.)
& $py -m src.run
$scanExit = $LASTEXITCODE

# 5) Git — skipped entirely in LOCAL_RUN; only runs with -Push.
if ($env:LOCAL_RUN) {
    Write-Output "LOCAL_RUN: skipping git add/commit/push (data/ left uncommitted on the VM)."
    exit $scanExit
}

git add data/
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Output "No data changes to commit."
    exit $scanExit
}

$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
git commit -m "arb scan $stamp [skip ci]"
for ($i = 1; $i -le 3; $i++) {
    git pull --rebase --autostash origin main
    git push origin HEAD:main
    if ($LASTEXITCODE -eq 0) { Write-Output "Pushed on attempt $i."; break }
    Write-Output "Push failed (attempt $i), retrying..."
    Start-Sleep -Seconds 5
}
exit $scanExit
