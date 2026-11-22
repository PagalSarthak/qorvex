"""Clipboard link catcher.

IDM's signature trick: copy a URL anywhere and get a one-click download. This
module only reads text; it never writes to the clipboard.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
TRAILING = ".,;:!?)]}'\"»…"
MAX_TEXT = 100_000


def find_url(text: str | None) -> str | None:
    """Return the first plausible http(s) URL in `text`, or None."""
    if not text or len(text) > MAX_TEXT:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    url = match.group(0)
    while url and url[-1] in TRAILING:
        url = url[:-1]
    if len(url) < 10:
        return None
    return url


def is_local(url: str) -> bool:
    """True for our own UI/API and other loopback addresses."""
    lowered = url.lower()
    return any(
        token in lowered
        for token in ("://127.0.0.1", "://localhost", "://[::1]", "://0.0.0.0")
    )


# ------------------------------------------------------------- platform reads
def _windows_read() -> str | None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _command_read(commands: list[list[str]]) -> str | None:
    for command in commands:
        try:
            done = subprocess.run(
                command, capture_output=True, text=True, timeout=3, check=False
            )
            if done.returncode == 0:
                return done.stdout
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _linux_read() -> str | None:
    commands = []
    if os.environ.get("WAYLAND_DISPLAY"):
        commands.append(["wl-paste", "--no-newline"])
    commands += [
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ]
    return _command_read(commands)


def _macos_read() -> str | None:
    return _command_read([["pbpaste"]])


def read_text() -> str | None:
    try:
        if sys.platform.startswith("win"):
            return _windows_read()
        if sys.platform == "darwin":
            return _macos_read()
        if sys.platform.startswith("linux"):
            return _linux_read()
    except Exception:  # clipboard access is best-effort
        return None
    return None


class ClipboardWatcher(threading.Thread):
    """Poll the clipboard and call `on_url` once per newly copied link."""

    def __init__(self, on_url, interval: float = 1.0, reader=read_text):
        super().__init__(name="qorvex-clipboard", daemon=True)
        self._on_url = on_url
        self._interval = max(0.2, float(interval))
        self._reader = reader
        self._stop = threading.Event()
        self._seen: str | None = None
        self.last_url: str | None = None

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                text = self._reader()
            except Exception:
                continue
            if not text:
                continue
            digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()
            if digest == self._seen:
                continue
            self._seen = digest
            url = find_url(text)
            if not url or is_local(url):
                continue
            self.last_url = url
            try:
                self._on_url(url)
            except Exception:
                pass


class NullWatcher(threading.Thread):
    """Stand-in used when clipboard watching is disabled."""

    def __init__(self):
        super().__init__(name="qorvex-clipboard-off", daemon=True)
        self.last_url = None

    def stop(self) -> None:
        return None

    def run(self) -> None:
        return None
