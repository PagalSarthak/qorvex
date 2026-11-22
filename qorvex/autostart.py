"""Start Qorvex when the user signs in.

Per-user only: HKCU Run on Windows, an XDG autostart entry on Linux, a
LaunchAgent on macOS. No administrator rights, no service, nothing machine-wide.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys

APP_NAME = "Qorvex"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MAC_LABEL = "com.qorvex.agent"
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def launch_command() -> list[str]:
    """The command a login item should run.

    Frozen builds re-run the executable; source checkouts run run.py with the
    windowless interpreter so no console flashes at login.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "serve", "--no-open"]
    exe = sys.executable
    if os.name == "nt":
        windowless = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(windowless):
            exe = windowless
    return [exe, os.path.join(PACKAGE_ROOT, "run.py"), "serve", "--no-open"]


def command_line() -> str:
    return subprocess.list2cmdline(launch_command())


# ---------------------------------------------------------------- Windows
def _win_value() -> str | None:
    try:
        import winreg
    except ImportError:  # pragma: no cover - not Windows
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return str(value)
    except OSError:
        return None


def _win_set(enabled: bool) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command_line())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------ Linux
def _linux_path(home: str | None = None) -> str:
    base = home or os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", "qorvex.desktop")


def desktop_entry(command: list[str] | None = None) -> str:
    exec_line = shlex.join(command or launch_command())
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Multi-network download manager\n"
        f"Exec={exec_line}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        "Categories=Network;FileTransfer;\n"
    )


def _linux_enabled(home: str | None = None) -> bool:
    return os.path.exists(_linux_path(home))


def _linux_set(enabled: bool, home: str | None = None) -> None:
    path = _linux_path(home)
    if not enabled:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(desktop_entry())


# ------------------------------------------------------------------ macOS
def _mac_path(home: str | None = None) -> str:
    base = home or os.path.expanduser("~")
    return os.path.join(base, "Library", "LaunchAgents", f"{MAC_LABEL}.plist")


def _mac_enabled(home: str | None = None) -> bool:
    return os.path.exists(_mac_path(home))


def _mac_set(enabled: bool, home: str | None = None) -> None:
    import plistlib

    path = _mac_path(home)
    if not enabled:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "Label": MAC_LABEL,
        "ProgramArguments": launch_command(),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
    }
    with open(path, "wb") as handle:
        plistlib.dump(payload, handle)


# ------------------------------------------------------------------- API
def supported() -> bool:
    return sys.platform.startswith("win") or sys.platform == "darwin" or sys.platform.startswith("linux")


def enabled(home: str | None = None) -> bool:
    if sys.platform.startswith("win"):
        return _win_value() is not None
    if sys.platform == "darwin":
        return _mac_enabled(home)
    if sys.platform.startswith("linux"):
        return _linux_enabled(home)
    return False


def set_enabled(value: bool, home: str | None = None) -> None:
    if not supported():
        raise RuntimeError(f"autostart is not supported on {sys.platform}")
    if sys.platform.startswith("win"):
        _win_set(bool(value))
    elif sys.platform == "darwin":
        _mac_set(bool(value), home)
    else:
        _linux_set(bool(value), home)


def sync(home: str | None = None) -> None:
    """Refresh the registered command if the install moved."""
    if not enabled(home):
        return
    if sys.platform.startswith("win"):
        if _win_value() != command_line():
            _win_set(True)
    elif sys.platform == "darwin":
        _mac_set(True, home)
    else:
        _linux_set(True, home)


def status(home: str | None = None) -> dict:
    if not supported():
        return {"supported": False, "enabled": False, "method": "none", "command": command_line()}
    method = {"win32": "registry", "darwin": "launch-agent"}.get(sys.platform, "xdg-autostart")
    try:
        is_enabled = enabled(home)
    except OSError:
        is_enabled = False
    return {
        "supported": True,
        "enabled": is_enabled,
        "method": method,
        "command": command_line(),
    }
