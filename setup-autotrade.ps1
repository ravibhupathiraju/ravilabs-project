# =============================================================================
#  setup-autotrade.ps1  --  one-time setup for unattended 0DTE/swing auto-trading
#
#  Turns this laptop into a hands-off trader that stays SECURE off-hours:
#    - hibernate overnight  = powered off, no network, minimal attack surface
#    - wakes on a timer at 9:00 ET (weekdays) and launches the tracker
#    - the app holds it awake through the session, then hibernates ~16:20 ET
#
#  REVIEW THIS FIRST, then run it once in an ELEVATED PowerShell:
#     Right-click Start > "Terminal (Admin)"  ->  cd to this folder  ->
#     powershell -ExecutionPolicy Bypass -File .\setup-autotrade.ps1
#
#  Everything here is reversible; the "UNDO" section at the bottom lists how.
# =============================================================================

$Project  = "C:\Ravi\fable\swing\ravilabs-project"
$Py       = "C:\Users\ravib\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
$TaskName = "SwingTracker-AutoTrade"

# Wake ~30 min before the 9:30 ET open = 9:00 AM EASTERN, expressed in THIS
# machine's LOCAL time (Task Scheduler triggers fire in local time). This is
# correct on any timezone: on Pacific it resolves to 06:00, on Eastern 09:00.
$etZone    = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$et9       = [DateTime]::SpecifyKind((Get-Date).Date.AddHours(9), 'Unspecified')
$WakeLocal = [System.TimeZoneInfo]::ConvertTime($et9, $etZone, [System.TimeZoneInfo]::Local)
$WakeAt    = $WakeLocal.ToString("HH:mm")   # local HH:mm for 9:00 AM ET

function Step($m) { Write-Host "`n=> $m" -ForegroundColor Cyan }

# --- must be admin ----------------------------------------------------------
$admin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
          ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "Run this in an ADMIN PowerShell (Terminal (Admin))." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $Py))      { Write-Host "Python not found at $Py -- edit `$Py." -ForegroundColor Red; exit 1 }
if (-not (Test-Path $Project)) { Write-Host "Project not found at $Project -- edit `$Project." -ForegroundColor Red; exit 1 }

# --- 1. enable hibernate ----------------------------------------------------
Step "Enabling hibernate (powercfg /hibernate on)"
powercfg /hibernate on

# --- 2. allow wake timers on AC (so the scheduled wake can fire) -------------
Step "Allowing wake timers while plugged in"
# SUB_SLEEP > 'Allow wake timers' = Enable(1)
powercfg /SETACVALUEINDEX SCHEME_CURRENT `
    238C9FA8-0AAD-41ED-83F4-97BE242C8F20 BD3B718A-0680-4D9D-8AB2-E1D2B4AC806D 1
powercfg /SETACTIVE SCHEME_CURRENT

# --- 3. lid close (plugged in) = do nothing ---------------------------------
Step "Setting lid-close (plugged in) to 'Do nothing'"
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /SETACTIVE SCHEME_CURRENT

# --- 4. disable Fast Startup (it breaks scheduled wake) ----------------------
Step "Disabling Fast Startup"
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" `
    -Name HiberbootEnabled -Value 0 -Type DWord

# --- 5. disable Wake-on-LAN (only the RTC timer should wake us) --------------
Step "Disabling network-wake (Wake-on-LAN) on physical adapters"
foreach ($a in (Get-NetAdapter -Physical -ErrorAction SilentlyContinue)) {
    try { & powercfg /devicedisablewake "$($a.InterfaceDescription)" 2>$null } catch {}
    try { Set-NetAdapterPowerManagement -Name $a.Name -WakeOnMagicPacket Disabled `
              -WakeOnPattern Disabled -ErrorAction Stop } catch {}
}

# --- 6. the wake-and-launch scheduled task ----------------------------------
Step "Creating scheduled task '$TaskName' (weekdays $WakeAt, wake-to-run)"
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action  = New-ScheduledTaskAction -Execute $Py -Argument "serve.py" -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -Weekly -At $WakeAt `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday
$settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # no run-time limit
# Interactive = runs in your logged-on (even locked) session, so trades fire
# and toasts queue for when you unlock. Keep yourself logged in; locking is OK.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "`nDONE." -ForegroundColor Green
Write-Host @"

The app auto-hibernates after the close by default (hibernate_after_close in
app_settings.json). It only fires when you have been idle 5+ min, so it never
interrupts active use; set it to false there to turn it off.

VERIFY / TEST
  powercfg /waketimers            # should list the SwingTracker task
  powercfg /lastwake              # after a wake, shows what woke the PC
  Start-ScheduledTask $TaskName   # launch it now to confirm the app comes up,
                                  # then open http://127.0.0.1:5000
  Quick wake test (do this once): hibernate the laptop, and the task at $WakeAt
  should wake + launch it. Some laptops also need BIOS "Wake on RTC / timer" on.

UNDO
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false
  powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_BUTTONS LIDACTION 1; powercfg /SETACTIVE SCHEME_CURRENT
  Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' HiberbootEnabled 1
  (re-enable Wake-on-LAN from Device Manager > adapter > Power Management if you want it)
"@ -ForegroundColor Gray
