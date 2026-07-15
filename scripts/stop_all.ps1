<#
.SYNOPSIS
  Stop the whole stack: drop the STOP_ALL flag, wait for the supervisor to wind down, clear the flag.

.DESCRIPTION
  The supervisor (run_all.ps1) sees data\ops\STOP_ALL, kills all four components and exits. This script
  creates that flag, waits until the supervisor process is gone, then removes the flag so a later start
  isn't immediately stopped. It does NOT disable the boot task - the stack will return on the next
  reboot (or `schtasks /Run /TN SoccerPapi`). To stop that too:  schtasks /Change /TN SoccerPapi /Disable
#>

$ops  = 'C:\bots\soccer-papi\data\ops'
New-Item -ItemType Directory -Force -Path $ops | Out-Null
$stop = Join-Path $ops 'STOP_ALL'

if (-not (Test-Path $stop)) { New-Item -ItemType File -Path $stop | Out-Null }   # marker file (no content)
Write-Output 'Created STOP_ALL flag - waiting for the supervisor to kill the components and exit...'

$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {
    $alive = @(Get-CimInstance Win32_Process |
               Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -like '*run_all.ps1*' })
    if ($alive.Count -eq 0) { break }
    Start-Sleep -Seconds 3
}
Remove-Item -Force -Path $stop -ErrorAction SilentlyContinue

Write-Output 'Supervisor stopped and STOP_ALL cleared.'
Write-Output 'It will restart on the next boot. To prevent that too:  schtasks /Change /TN SoccerPapi /Disable'
