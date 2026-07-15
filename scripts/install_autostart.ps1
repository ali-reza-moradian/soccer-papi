<#
.SYNOPSIS
  Install the reboot-proof autostart for the supervisor. Run ONCE, in an elevated (Administrator) shell.

.DESCRIPTION
  Registers a scheduled task 'SoccerPapi' that launches scripts\run_all.ps1 at every boot as SYSTEM.

  BUG HISTORY (do NOT "fix" these):
    * A SINGLE ONSTART trigger - never a repeating schedule. The cadence lives INSIDE the loops; a
      repeating trigger spawned a SECOND supervisor that double-ran and killed the tree twice in July.
    * run_all.ps1 uses the FULL python path, never a bare `python` (SYSTEM's PATH differs and fails).
#>

# --% stops PowerShell parsing so schtasks receives the /TR command verbatim (spaces and all).
schtasks --% /Create /F /TN SoccerPapi /SC ONSTART /RL HIGHEST /RU SYSTEM /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\bots\soccer-papi\scripts\run_all.ps1"

if ($LASTEXITCODE -eq 0) {
    Write-Output "Installed scheduled task 'SoccerPapi' - the supervisor now starts at every boot (SYSTEM, highest run level)."
    Write-Output ""
    Write-Output "Start it NOW without rebooting:   schtasks /Run /TN SoccerPapi"
    Write-Output "Stop the stack (leave task):      powershell -File C:\bots\soccer-papi\scripts\stop_all.ps1"
    Write-Output "Disable autostart entirely:       schtasks /Change /TN SoccerPapi /Disable"
} else {
    Write-Warning "schtasks /Create failed (exit $LASTEXITCODE) - run this in an ELEVATED (Administrator) PowerShell."
}
