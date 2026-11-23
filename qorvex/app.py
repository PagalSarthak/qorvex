"""Application wiring: settings, engine, clipboard watcher and lifecycle.

The server, the CLI and the web UI all talk to one :class:`App`. Keeping the
wiring here means ``server.py`` stays a thin HTTP adapter.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
import time
from collections import deque

from . import autostart
from .clipboard import ClipboardWatcher, NullWatcher
from .config import Settings, ensure_app_dir, log_path
from .engine import Manager
from .torrent import is_torrent_source

log = logging.getLogger("qorvex")

MAX_PENDING = 20


def setup_logging(level: int = logging.INFO) -> None:
    ensure_app_dir()
    root = logging.getLogger("qorvex")
    if root.handlers:
        return
    root.setLevel(level)
    handler = logging.handlers.RotatingFileHandler(
        log_path(), maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s %(message)s"))
    root.addHandler(handler)


def tail_log(lines: int = 200) -> str:
    try:
        with open(log_path(), encoding="utf-8", errors="replace") as handle:
            return "".join(deque(handle, maxlen=max(1, min(lines, 2000))))
    except OSError:
        return ""


class App:
    """Owns every long-lived object and applies settings changes live."""

    def __init__(self, settings: Settings | None = None, directory: str | None = None,
                 networks=None, max_active: int | None = None):
        setup_logging()
        self.settings = settings or Settings()
        self._dir_override = directory
        self._max_active_override = max_active
        self.mgr = Manager(
            directory or self.settings.get("download_dir"),
            networks,
            max_active if max_active is not None else self.settings.get("max_active"),
            settings=self.settings,
        )
        self.started = time.time()
        self._ext_last_seen: float | None = None
        self.show_requested = threading.Event()
        self._lock = threading.Lock()
        self._pending: deque[dict] = deque(maxlen=MAX_PENDING)
        self.watcher = NullWatcher()
        self._quit = threading.Event()
        self.apply_settings([])
        self._start_watcher()
        try:
            autostart.sync()
        except Exception:  # noqa: BLE001 - autostart is best-effort
            log.exception("autostart sync failed")

    # -- clipboard ---------------------------------------------------------
    def _start_watcher(self) -> None:
        try:
            self.watcher.stop()
        except Exception:  # noqa: BLE001
            pass
        if self.settings.get("clipboard_watch", True):
            self.watcher = ClipboardWatcher(self._on_clipboard)
            self.watcher.start()
        else:
            self.watcher = NullWatcher()

    def _on_clipboard(self, url: str) -> None:
        if self.settings.get("clipboard_auto_add", False):
            self.add_url(url)
            log.info("clipboard auto-added %s", url)
            return
        self.stage_pending(url)
        log.info("clipboard link pending: %s", url)

    def stage_pending(self, url: str, name: str = "", source: str = "clipboard") -> None:
        """Hold a link for the UI banner instead of starting a download."""
        with self._lock:
            if any(item["url"] == url for item in self._pending):
                return
            self._pending.appendleft({"url": url, "name": name, "source": source, "at": time.time()})
        log.info("%s pending: %s", source, url)

    def pending(self) -> list[dict]:
        with self._lock:
            return list(self._pending)

    def take_pending(self) -> list[dict]:
        with self._lock:
            items = list(self._pending)
            self._pending.clear()
            return items

    def dismiss_pending(self, url: str | None = None) -> None:
        with self._lock:
            if url is None:
                self._pending.clear()
            else:
                self._pending = deque(
                    (item for item in self._pending if item["url"] != url), maxlen=MAX_PENDING
                )

    def confirm_pending(self, url: str, media: bool = False) -> dict:
        """Start one held download (banner “Download” click) and drop it."""
        name = ""
        with self._lock:
            for item in self._pending:
                if item["url"] == url:
                    name = item.get("name") or ""
                    break
            self._pending = deque(
                (item for item in self._pending if item["url"] != url), maxlen=MAX_PENDING
            )
        download = self.add_url(url, filename=name, media=media)
        return {"id": download.id, "name": name or url}

    # -- adding ------------------------------------------------------------
    def add_url(self, url: str, *, media: bool = False, quality: str = "best",
                filename: str = "", directory: str | None = None,
                headers: dict | None = None, torrent: bool = False):
        source = str(url).strip()
        if torrent or is_torrent_source(source):
            job = self.mgr.add_torrent(source, directory)
            log.info("queued torrent %s", source[:120])
            return job
        if not source.lower().startswith(("http://", "https://")):
            raise ValueError("only http(s) links are supported")
        if media:
            job = self.mgr.add_media(url, quality, directory)
            log.info("queued media %s (%s)", url, quality)
            return job
        download = self.mgr.add(url, headers or {}, filename or "", directory)
        log.info("queued %s", url)
        return download

    # -- settings ----------------------------------------------------------
    def apply_settings(self, changed: list[str]) -> None:
        settings, mgr = self.settings, self.mgr
        if ("max_active" in changed) or (not changed and self._max_active_override is None):
            mgr.max_active = int(settings.get("max_active", 3))
        if not changed or "global_limit_mb" in changed:
            mgr.set_global_limit(float(settings.get("global_limit_mb", 0) or 0))
        if "download_dir" in changed:
            mgr.dir = settings.get("download_dir")
            self._dir_override = None
        elif not changed and self._dir_override is None:
            mgr.dir = settings.get("download_dir")
        if "autostart" in changed:
            try:
                autostart.set_enabled(bool(settings.get("autostart")))
            except Exception as exc:  # noqa: BLE001
                log.exception("could not change autostart: %s", exc)
        if "clipboard_watch" in changed or "clipboard_auto_add" in changed:
            self._start_watcher()
        if not changed or "video_cookies_browser" in changed:
            browser = str(settings.get("video_cookies_browser", "") or "")
            mgr.ytdlp["cookies_from_browser"] = browser or None
            log.info("video cookies from browser: %s", browser or "none")

    def update_settings(self, patch: dict) -> dict:
        result = self.settings.update(patch)
        self.apply_settings(result["changed"])
        log.info("settings changed: %s", ", ".join(result["changed"]) or "none")
        return result

    def reset_settings(self) -> dict:
        values = self.settings.reset()
        self.apply_settings(list(values))
        return values

    # -- lifecycle ---------------------------------------------------------
    def quit(self) -> None:
        self._quit.set()

    @property
    def quitting(self) -> bool:
        return self._quit.is_set()

    def close(self) -> None:
        self.quit()
        # Give running jobs a chance to save their sidecars before the process exits.
        for download in list(self.mgr.downloads.values()):
            thread = getattr(download, "thread", None)
            if thread is not None and thread.is_alive():
                try:
                    self.mgr.action(download.id, "pause")
                except Exception:  # noqa: BLE001
                    pass
        deadline = time.time() + 6
        while time.time() < deadline:
            alive = [
                d for d in self.mgr.downloads.values()
                if getattr(d, "thread", None) is not None and d.thread.is_alive()
            ]
            if not alive:
                break
            time.sleep(0.05)
        try:
            self.watcher.stop()
        except Exception:  # noqa: BLE001
            pass
        self.mgr.close()

    def note_extension(self) -> None:
        """The browser extension announces itself with a marker header."""
        self._ext_last_seen = time.time()

    def state(self) -> dict:
        data = self.mgr.state()
        values = self.settings.values()
        values.pop("api_token", None)  # only the settings endpoint exposes it
        data.update(
            {
                "version": __import__("qorvex").__version__,
                "settings": values,
                "pending": self.pending(),
                "autostart": autostart.status(),
                "app_dir": ensure_app_dir(),
                "started": self.started,
                "extension_last_seen": self._ext_last_seen,
            }
        )
        return data
