<#
.SYNOPSIS
  THE SUPERVISOR - keeps the whole soccer-papi stack alive, forever, self-healing.

.DESCRIPTION
  Every 30s it detects each component by matching its command line (Get-CimInstance Win32_Process) and
  (re)starts any that are missing, HIDDEN, with stdout+stderr appended to data\ops\<name>.log:
    1. http : python -m http.server 8080   (cwd data\ - serves the panel)
    2. tree : scripts\run_tree_loop.ps1
    3. genz : scripts\run_genz_loop.ps1     (the fresh-process wrapper - NEVER `run --loop`)
    4. og   : scripts\run_og_loop.ps1
  The "which components are missing?" decision is delegated to scripts\ops.py (`missing`) so it lives in
  ONE tested place. Writes data\ops\supervisor_heartbeat.json {ts, components:{name: pid|null}} each
  sweep. If data\ops\STOP_ALL exists it kills all four, logs, and exits. Refuses to start if another
  run_all.ps1 is already running (no double-supervision).

  BUG HISTORY (why it's built this way): use the FULL python path, never a bare `python` (PATH under
  SYSTEM differs and silently fails); the CADENCE lives INSIDE each loop, the supervisor only restarts
  DEAD components - a supervisor that re-launched on a timer double-ran and killed the tree twice in
  July. The autostart is a SINGLE ONSTART trigger (scripts\install_autostart.ps1), not a repeating one.
#>

$ErrorActionPreference = 'Continue'
$py   = 'C:\Users\Administrator\AppData\Local\Programs\Python\Python314\python.exe'
$repo = 'C:\bots\soccer-papi'
Set-Location $repo
$data = Join-Path $repo 'data'
$ops  = Join-Path $data 'ops'
New-Item -ItemType Directory -Force -Path $ops | Out-Null
$hb    = Join-Path $ops 'supervisor_heartbeat.json'
$slog  = Join-Path $ops 'supervisor.log'
$stop  = Join-Path $ops 'STOP_ALL'

function Log($msg) { "$(Get-Date -Format o) [supervisor] $msg" | Out-File -Append -Encoding utf8 $slog }
function Get-RunningCmdlines { @(Get-CimInstance Win32_Process | ForEach-Object { $_.CommandLine } | Where-Object { $_ }) }

# Double-supervision guard: refuse if ANOTHER run_all.ps1 is already running.
$others = @(Get-CimInstance Win32_Process |
            Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -like '*run_all.ps1*' })
if ($others.Count -gt 0) {
    Log "another run_all.ps1 already running (PID $($others[0].ProcessId)) - refusing to start a second."
    Write-Warning 'Another run_all.ps1 supervisor is already running - refusing to start a second.'
    exit 1
}

# The component list (name + match substring + launch command) is OWNED by scripts\ops.py so ADDING a
# component (e.g. the MLB loops) needs NO edit here. Fetch the specs once and fill the path placeholders.
$components = [ordered]@{}
try {
    $specs = (& $py -m scripts.ops specs) | ConvertFrom-Json
    foreach ($s in $specs) {
        $cmd = $s.cmd.Replace('{py}', $py).Replace('{repo}', $repo).Replace('{data}', $data)
        $components[$s.name] = @{ match = $s.match; cmd = $cmd }
    }
} catch {
    Log "FATAL: could not load component specs from ops.py ($($_.Exception.Message)) - exiting."
    exit 1
}
if ($components.Count -eq 0) { Log 'FATAL: ops.py returned no components - exiting.'; exit 1 }

function Start-Component($name) {
    $c = $components[$name]
    $log = Join-Path $ops "$name.log"
    $inner = "$($c.cmd) *>> '$log'"      # append BOTH stdout+stderr (all streams) to the component log
    $p = Start-Process powershell -WindowStyle Hidden -PassThru -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', $inner)
    Log "started $name (pid $($p.Id))"
}

function Stop-AllComponents {
    # GRACEFUL-STOP FIX: maker_rt holds LIVE Polymarket orders. It polls data\ops\STOP_ALL every loop and
    # self-cancels all orders + exits when it appears. Give the python maker up to 10s to do that BEFORE
    # any Stop-Process -Force, so a resting order is never stranded by a hard kill.
    $graceDeadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $graceDeadline) {
        $maker = @(Get-CimInstance Win32_Process |
                   Where-Object { $_.CommandLine -and $_.CommandLine -like '*-m src.genz.maker_rt*' })
        if ($maker.Count -eq 0) { break }
        Log "waiting for maker_rt to self-cancel + exit gracefully ($($maker.Count) alive)..."
        Start-Sleep -Milliseconds 500
    }
    foreach ($k in $components.Keys) {
        $m = $components[$k].match
        Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like "*$m*" } |
            ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }
    }
    # Backstop: force-kill the python maker directly (its cmdline doesn't match the wrapper match).
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like '*-m src.genz.maker_rt*' } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }
}

function Current-Pids {
    $procs = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine })
    $out = [ordered]@{}
    foreach ($k in $components.Keys) {
        $m = $components[$k].match
        $hit = $procs | Where-Object { $_.CommandLine -like "*$m*" } | Select-Object -First 1
        $out[$k] = if ($hit) { $hit.ProcessId } else { $null }
    }
    $out
}

Log "supervisor up (PID $PID)."
while ($true) {
    if (Test-Path $stop) {
        Log 'STOP_ALL present - killing components and exiting.'
        Stop-AllComponents
        @{ ts = (Get-Date -Format o); components = (Current-Pids) } | ConvertTo-Json -Compress | Out-File -Encoding utf8 $hb
        exit 0
    }
    try {
        $running = Get-RunningCmdlines
        # Pipe each running command line (one per stdin line) to the tested decision logic in ops.py.
        $missing = @($running | & $py -m scripts.ops missing | Where-Object { $_ })
        foreach ($name in $missing) { if ($components.Contains($name)) { Start-Component $name } }
    } catch {
        Log "sweep error: $($_.Exception.Message)"
    }
    @{ ts = (Get-Date -Format o); components = (Current-Pids) } | ConvertTo-Json -Compress | Out-File -Encoding utf8 $hb
    Start-Sleep -Seconds 30
}
