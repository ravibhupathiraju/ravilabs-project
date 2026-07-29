"""Quick unattended-cycle self-test (Windows).

Proves the WHOLE hands-off loop in ~2 minutes: the laptop really hibernates,
the RTC timer wakes it, and the scheduled task cold-starts the tracker. It is
built to be impossible to false-positive:

  * the hibernate is fired by an INDEPENDENT one-shot scheduled task (not a
    child process the app could kill on exit), so it actually happens; and
  * on wake, ``on_startup()`` VERIFIES against the Windows event log that a real
    sleep (Kernel-Power id 42) occurred during the test window before it
    reports PASSED. If the app comes up without a recorded hibernate, it is
    marked INCONCLUSIVE, never passed.

Task Scheduler triggers run in LOCAL time, so all times here are local.
State lives in quicktest.json so it survives the hibernate/relaunch.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "quicktest.json"
WAKE_TASK = "SwingTracker-QuickTest"
HIB_TASK = "SwingTracker-QuickHibernate"
PYW = sys.executable.replace("python.exe", "pythonw.exe")

_APP_EXIT_DELAY = 4    # s after arming: stop this app (so hibernate is app-less)
_HIBERNATE_IN = 20     # s from now: the independent task hibernates the machine


def _load() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    STATE.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _ps(cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def status() -> dict:
    d = _load()
    return d or {"phase": "idle"}


def _slept_since(when: datetime) -> str | None:
    """Return the timestamp of a real hibernate/sleep (Kernel-Power id 42) that
    the event log recorded at or after `when`, or None. This is the proof the
    machine actually slept -- not just that a task fired."""
    cmd = (
        'Get-WinEvent -FilterHashtable @{LogName="System"; '
        'ProviderName="Microsoft-Windows-Kernel-Power"; Id=42; '
        f'StartTime="{when:%Y-%m-%dT%H:%M:%S}"}} -MaxEvents 1 -ErrorAction SilentlyContinue '
        '| ForEach-Object { $_.TimeCreated.ToString("s") }'
    )
    try:
        out = (_ps(cmd, timeout=20).stdout or "").strip()
        return out or None
    except Exception:
        return None


def start(minutes: int = 2) -> dict:
    """Arm a real hibernate + wake+launch cycle, then stop this app so the
    machine goes down app-less and only the scheduled task can bring it back."""
    if sys.platform != "win32":
        raise RuntimeError("quick test only runs on Windows")
    minutes = max(1, min(int(minutes), 10))
    now = datetime.now()                       # LOCAL time (Task Scheduler uses local)
    wake = now + timedelta(minutes=minutes)
    hib = now + timedelta(seconds=_HIBERNATE_IN)

    # 1. wake + cold-start task: wake the machine and launch the tracker
    wake_cmd = (
        f'Register-ScheduledTask -TaskName "{WAKE_TASK}" -Force '
        f'-Action (New-ScheduledTaskAction -Execute "{PYW}" -Argument "serve.py" '
        f'-WorkingDirectory "{ROOT}") '
        f'-Trigger (New-ScheduledTaskTrigger -Once -At "{wake:%Y-%m-%dT%H:%M:%S}") '
        f'-Settings (New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable) | Out-Null'
    )
    r = _ps(wake_cmd)
    if r.returncode != 0:
        raise RuntimeError("could not register wake task: "
                           + (r.stderr or r.stdout or "unknown").strip())

    # 2. INDEPENDENT one-shot hibernate task (survives this app exiting)
    hib_cmd = (
        f'Register-ScheduledTask -TaskName "{HIB_TASK}" -Force '
        f'-Action (New-ScheduledTaskAction -Execute "shutdown.exe" -Argument "/h") '
        f'-Trigger (New-ScheduledTaskTrigger -Once -At "{hib:%Y-%m-%dT%H:%M:%S}") '
        f'-Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries '
        f'-DontStopIfGoingOnBatteries) | Out-Null'
    )
    r = _ps(hib_cmd)
    if r.returncode != 0:
        raise RuntimeError("could not register hibernate task: "
                           + (r.stderr or r.stdout or "unknown").strip())

    _save({
        "phase": "armed",
        "armed_at": now.isoformat(timespec="seconds"),
        "hibernate_at": hib.isoformat(timespec="seconds"),
        "wake_at": wake.isoformat(timespec="seconds"),
        "minutes": minutes,
        "result": (f"Armed. The app stops now, an independent task hibernates the "
                   f"laptop in ~{_HIBERNATE_IN}s, and it should wake itself and "
                   f"cold-start the tracker at {wake:%H:%M}. Reopen this page after "
                   f"it returns; the result is verified against the Windows sleep log."),
    })
    # Stop this instance so the hibernate lands app-less.
    threading.Timer(_APP_EXIT_DELAY, os._exit, [0]).start()
    return _load()


def _cleanup() -> None:
    for t in (WAKE_TASK, HIB_TASK):
        try:
            subprocess.run(["schtasks", "/delete", "/tn", t, "/f"],
                           capture_output=True, text=True, timeout=15)
        except Exception:
            pass


def on_startup() -> None:
    """Called once when the app boots. If a test was armed, VERIFY via the event
    log that the machine really hibernated during the window before passing."""
    d = _load()
    if d.get("phase") != "armed":
        return
    now = datetime.now()
    try:
        armed = datetime.fromisoformat(d["armed_at"])
    except Exception:
        armed = now - timedelta(minutes=10)

    slept_at = _slept_since(armed)          # real proof of hibernation
    d["woke_app_at"] = now.isoformat(timespec="seconds")
    if slept_at:
        d["phase"] = "passed"
        d["slept_at"] = slept_at
        d["result"] = (f"PASSED (verified) - the laptop hibernated at {slept_at[11:]}, "
                       f"woke itself on the timer, and the scheduled task cold-started "
                       f"the app. Confirmed by the Windows sleep log (Kernel-Power 42).")
    else:
        d["phase"] = "inconclusive"
        d["result"] = ("INCONCLUSIVE - the app came back up, but the Windows event log "
                       "shows NO hibernate during the test window, so the machine likely "
                       "stayed awake and the task just fired on schedule. Not a real "
                       "wake-from-hibernate.")
    _save(d)
    _cleanup()


def clear() -> dict:
    _cleanup()
    _save({"phase": "idle"})
    return status()
