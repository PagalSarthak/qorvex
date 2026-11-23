"""System tray icon: Qorvex keeps running and downloading when its window closes.

Works on Windows (Shell tray), macOS (menu bar) and Linux (AppIndicator/X11 via
pystray). Everything here is best-effort: without a tray environment the app
runs exactly as before.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time

from .system import open_path, open_url

log = logging.getLogger(__name__)

STATUSES_DONE = ("done", "seeding")


def tray_available() -> bool:
    try:
        import pystray  # noqa: F401

        return True
    except Exception:  # ImportError, and on Linux a missing X display
        return False


def _logo_path() -> str:
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = os.path.join(bundled, "qorvex", "logo.png")
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(os.path.dirname(__file__), "logo.png")


class TrayController:
    """Tray icon + completion notifications. Safe to start on any platform."""

    def __init__(self, app) -> None:
        self.app = app
        self.icon = None
        self.show_window = None  # set by __main__ in windowed mode
        self._known: dict[str, str] = {}  # download id -> status

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> bool:
        if not tray_available():
            return False
        try:
            import pystray
            from PIL import Image

            image = Image.open(_logo_path())
            self.icon = pystray.Icon(
                "Qorvex",
                image,
                "Qorvex — download manager",
                pystray.Menu(
                    pystray.MenuItem("Open Qorvex", self._on_open, default=True),
                    pystray.MenuItem("Open downloads folder", self._on_folder),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Pause all", self._on_pause_all),
                    pystray.MenuItem("Resume all", self._on_resume_all),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Quit Qorvex", self._on_quit),
                ),
            )
            threading.Thread(target=self.icon.run, daemon=True, name="qorvex-tray").start()
            threading.Thread(
                target=self._watch_completions, daemon=True, name="qorvex-tray-notify"
            ).start()
            log.info("tray icon running")
            return True
        except Exception as exc:  # no tray environment (some Wayland setups etc.)
            log.warning("tray unavailable: %s", exc)
            self.icon = None
            return False

    def stop(self) -> None:
        icon, self.icon = self.icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    # -- notifications ------------------------------------------------------
    def notify(self, title: str, message: str) -> None:
        try:
            if self.icon is not None:
                self.icon.notify(message, title)
                return
        except Exception:
            pass
        if sys.platform.startswith("linux"):  # pystray cannot notify on all Linux backends
            try:
                subprocess.Popen(
                    ["notify-send", "-a", "Qorvex", title, message],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass

    def _watch_completions(self) -> None:
        time.sleep(4)  # let startup state settle
        try:
            state = self.app.state()
        except Exception:
            state = {"downloads": []}
        for row in state["downloads"]:
            self._known[row["id"]] = row["status"]
        while not self.app.quitting:
            time.sleep(2)
            try:
                rows = self.app.state()["downloads"]
            except Exception:
                continue
            for row in rows:
                did, status = row["id"], row["status"]
                previous = self._known.get(did)
                self._known[did] = status
                if previous in (None, status) or did in STATUSES_DONE and previous in STATUSES_DONE:
                    continue
                if status == "done":
                    self.notify("Download complete", row.get("name") or row.get("url") or did)
                elif status == "error":
                    self.notify("Download failed", row.get("name") or row.get("url") or did)

    # -- menu actions -------------------------------------------------------
    def _on_open(self, icon=None, item=None) -> None:
        if self.show_window is not None:
            try:
                self.show_window()
                return
            except Exception:
                pass
        open_url(f"http://127.0.0.1:{self.app.settings.get('port', 6805)}/")

    def _on_folder(self, icon=None, item=None) -> None:
        open_path(self.app.settings.get("download_dir", os.path.expanduser("~/Downloads")))

    def _on_pause_all(self, icon=None, item=None) -> None:
        for row in self.app.mgr.state()["downloads"]:
            if row["status"] in ("queued", "active"):
                try:
                    self.app.mgr.action(row["id"], "pause")
                except Exception:
                    pass

    def _on_resume_all(self, icon=None, item=None) -> None:
        for row in self.app.mgr.state()["downloads"]:
            if row["status"] == "paused":
                try:
                    self.app.mgr.action(row["id"], "resume")
                except Exception:
                    pass

    def _on_quit(self, icon=None, item=None) -> None:
        self.stop()
        self.app.quit()
