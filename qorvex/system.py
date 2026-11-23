"""Small OS integration helpers: open files, reveal folders, app identity."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import webbrowser


def open_url(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def find_browsers() -> dict[str, str]:
    """Installed browsers that can load the extension: name -> exe path."""
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.environ.get("LOCALAPPDATA", "")]
    rels = {
        "chrome": r"Google\Chrome\Application\chrome.exe",
        "edge": r"Microsoft\Edge\Application\msedge.exe",
        "brave": r"BraveSoftware\Brave-Browser\Application\brave.exe",
        "firefox": r"Mozilla Firefox\firefox.exe",
        "vivaldi": r"Vivaldi\Application\vivaldi.exe",
    }
    found: dict[str, str] = {}
    for name, rel in rels.items():
        for root in roots:
            if not root:
                continue
            exe = os.path.join(root, rel)
            if os.path.isfile(exe):
                found[name] = exe
                break
    return found


def copy_clipboard(text: str) -> bool:
    """Put text on the Windows clipboard (used to paste paths into file dialogs)."""
    if sys.platform != "win32":
        return False
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.EmptyClipboard.argtypes = []
    user32.CloseClipboard.argtypes = []
    data = text.encode("utf-16-le") + b"\x00\x00"
    for _ in range(5):  # another app may hold the clipboard open briefly
        if user32.OpenClipboard(None):
            break
        ctypes.windll.kernel32.Sleep(50)
    else:
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


def open_path(path: str) -> bool:
    """Open a file or folder with the user's default application."""
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(target):
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(target)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])
        return True
    except OSError:
        return False


def reveal_path(path: str) -> bool:
    """Show a file in the system file manager, selecting it when possible."""
    target = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(target):
        target = os.path.dirname(target)
        if not os.path.isdir(target):
            return False
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", target])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", target])
        else:
            subprocess.Popen(["xdg-open", target if os.path.isdir(target) else os.path.dirname(target)])
        return True
    except OSError:
        return False


def default_download_dir() -> str:
    home = os.path.expanduser("~")
    for name in ("Downloads", "downloads"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return home


def hide_console() -> None:
    """Hide the console of a double-clicked frozen build (CLI use keeps it)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)  # SW_HIDE
    except Exception:
        pass


def set_app_id() -> None:
    """Give the Windows taskbar a stable identity instead of 'python.exe'."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Qorvex.DownloadManager.1")
    except Exception:
        pass


def create_mutex():
    """Named mutex so installers (Inno Setup) can detect a running Qorvex."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.kernel32.CreateMutexW(None, False, "QorvexRunningMutex")
    except Exception:
        pass


def prepend_path(*dirs: str) -> None:
    """Make bundled helpers (ffmpeg, deno) visible to child processes like yt-dlp."""
    here = [d for d in dirs if d and os.path.isdir(d)]
    if not here:
        return
    current = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(here) + os.pathsep + current
