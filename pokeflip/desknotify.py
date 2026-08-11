"""Native desktop notifications.

The PC counterpart of phone push: alerts land in the corner of the screen
instead of on a lock screen. Each platform is driven through tooling it already
ships with, so this adds no dependencies:

* **Linux** - ``notify-send`` (libnotify, present on every desktop install)
* **macOS** - ``osascript`` with ``display notification``
* **Windows** - PowerShell driving ``System.Windows.Forms.NotifyIcon``

Desktop notifications cannot carry action buttons the way ntfy can, so an
actionable alert includes its link as text - the dashboard is one click away
anyway when you are sitting at the machine.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from typing import Any, Sequence

log = logging.getLogger("pokeflip.desknotify")

# Notification daemons truncate anyway, and a wall of text in a toast is
# unreadable.
MAX_BODY = 220
TIMEOUT = 15

# Urgency as each platform understands it.
_LINUX_URGENCY = {"info": "low", "warn": "normal", "urgent": "critical"}


class NotifierUnavailable(RuntimeError):
    """No way to raise a desktop notification on this machine."""


def available() -> bool:
    """Can this machine show a desktop notification at all?"""
    try:
        _backend()
        return True
    except NotifierUnavailable:
        return False


def backend_name() -> str:
    try:
        return _backend()[0]
    except NotifierUnavailable:
        return "none"


def _backend() -> tuple[str, Any]:
    if sys.platform == "darwin":
        if shutil.which("osascript"):
            return "osascript", _notify_macos
        raise NotifierUnavailable("osascript not found")
    if sys.platform == "win32":
        if shutil.which("powershell") or shutil.which("powershell.exe"):
            return "powershell", _notify_windows
        raise NotifierUnavailable("powershell not found")
    if shutil.which("notify-send"):
        return "notify-send", _notify_linux
    raise NotifierUnavailable(
        "notify-send not found; install libnotify-bin for desktop notifications")


def notify(title: str, body: str = "", severity: str = "info",
           link: str = "") -> None:
    """Raise one desktop notification. Raises on failure so callers can report."""
    name, sender = _backend()
    text = body[:MAX_BODY]
    if link:
        text = f"{text}\n{link}" if text else link
    try:
        sender(title, text, severity)
    except subprocess.SubprocessError as exc:
        raise NotifierUnavailable(f"{name} failed: {exc}") from exc


def _run(command: Sequence[str], stdin: str | None = None) -> None:
    subprocess.run(command, input=stdin, text=True, timeout=TIMEOUT,
                   check=True, capture_output=True)


def _notify_linux(title: str, body: str, severity: str) -> None:
    _run([
        "notify-send",
        "--app-name=pokeflip",
        f"--urgency={_LINUX_URGENCY.get(severity, 'normal')}",
        # Urgent alerts stay until dismissed; the rest time out.
        "--expire-time=0" if severity == "urgent" else "--expire-time=12000",
        title,
        body,
    ])


def _notify_macos(title: str, body: str, severity: str) -> None:
    script = (
        f'display notification {_applescript_string(body)} '
        f'with title {_applescript_string("pokeflip")} '
        f'subtitle {_applescript_string(title)}'
    )
    if severity == "urgent":
        script += ' sound name "Submarine"'
    _run(["osascript", "-e", script])


def _notify_windows(title: str, body: str, severity: str) -> None:
    """A balloon tip through WinForms.

    Deliberately not the modern toast API: that needs a registered AppUserModelID
    or a third-party package, and this works on any Windows with .NET present.
    """
    icon = {"urgent": "Error", "warn": "Warning"}.get(severity, "Info")
    script = f"""
[void][System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms')
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::{icon}
$notify.BalloonTipTitle = {_powershell_string(title)}
$notify.BalloonTipText = {_powershell_string(body)}
$notify.Visible = $true
$notify.ShowBalloonTip({TIMEOUT * 1000})
Start-Sleep -Seconds 6
$notify.Dispose()
"""
    _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
         stdin=script)


def _applescript_string(text: str) -> str:
    """Quote for AppleScript, where a stray quote ends the script."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{escaped}"'


def _powershell_string(text: str) -> str:
    """Quote for PowerShell single-quoted strings, where '' is a literal quote."""
    return "'" + text.replace("'", "''").replace("\n", " ") + "'"
