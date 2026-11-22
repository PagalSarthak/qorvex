"""Pages whose video is not a plain link (YouTube and many other sites).

yt-dlp resolves the page into direct media URLs. Those URLs are often tied to the
IP that requested them, so we resolve once per network and let each network fetch
its own URL. Video + audio are downloaded as two hidden parts and merged with
ffmpeg into one file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import uuid

from .engine import Download, _safe


class Unsupported(Exception):
    pass


def _selector(quality: str) -> str:
    if quality == "audio":
        return "ba/b"
    if str(quality).isdigit():
        return f"bv*[height<={quality}]+ba/b[height<={quality}]/b"
    return "bv*+ba/b"


def _extract(page_url, net, quality, extra):
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError('yt-dlp is not installed. Run: pip install -U "yt-dlp[default]"') from None
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": _selector(quality),
    }
    if net and net.ip:
        opts["source_address"] = net.ip
    if extra.get("cookies_from_browser"):
        opts["cookiesfrombrowser"] = (extra["cookies_from_browser"],)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(page_url, download=False)
    parts = []
    for fmt in info.get("requested_formats") or [info]:
        if fmt.get("protocol") not in ("https", "http") or not fmt.get("url"):
            raise Unsupported(
                "this video uses a streaming format (HLS/DASH); Qorvex needs a direct "
                "http(s) stream for it"
            )
        parts.append(
            {
                "id": str(fmt.get("format_id")),
                "url": fmt["url"],
                "ext": fmt.get("ext") or "bin",
                "headers": fmt.get("http_headers") or {},
            }
        )
    return {
        "title": info.get("title") or info.get("id") or "video",
        "parts": parts,
    }


def _unique(path: str) -> str:
    base, ext = os.path.splitext(path)
    index = 1
    while os.path.exists(path):
        path = f"{base} ({index}){ext}"
        index += 1
    return path


class MediaJob:
    """One row in the UI that owns one or two hidden :class:`Download` parts."""

    kind = "media"

    def __init__(self, mgr, url, quality, directory=None, category=None):
        self.id = uuid.uuid4().hex[:8]
        self.mgr = mgr
        self.url = url
        self.quality = quality
        self.directory = directory
        self.category = category or "video"
        if directory:
            self.dir = directory
        elif mgr.setting("categorize", True):
            self.dir = os.path.join(mgr.dir, self.category)
        else:
            self.dir = mgr.dir
        self.name = url
        self.error = ""
        self.stage = "resolving"
        self.parts: list[Download] = []
        self.thread = threading.Thread(target=self._run, name=f"qorvex-media-{self.id}", daemon=True)
        self.cancelled = False
        self.added = time.time()
        self.finished = None
        self._lock = threading.Lock()
        self.thread.start()

    # -- state -------------------------------------------------------------
    @property
    def status(self) -> str:
        if self.stage != "downloading":
            return self.stage
        with self._lock:
            parts = list(self.parts)
        if not parts:
            return "downloading"
        statuses = [p.status for p in parts]
        if all(s in ("paused", "pausing") for s in statuses):
            return "paused" if "paused" in statuses else "pausing"
        # "done"/"error" are only reported once _wait has merged or cleaned up,
        # so the UI never shows a finished job with half-written files.
        return "downloading"

    def _run(self):
        try:
            self._resolve()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.stage, self.error = "error", str(exc)
            self.finished = time.time()

    # -- resolution --------------------------------------------------------
    def _resolve(self):
        nets = self.mgr.usable()
        if not nets:
            raise RuntimeError("no network enabled")
        results: dict[str, dict] = {}
        errors: list[Exception] = []
        lock = threading.Lock()

        def work(net):
            try:
                info = _extract(self.url, net, self.quality, self.mgr.ytdlp)
                with lock:
                    results[net.name] = info
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in nets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if self.cancelled:
            self.stage = "cancelled"
            self.finished = time.time()
            return
        if not results:
            raise (errors[0] if errors else RuntimeError("could not resolve this page"))

        first = results[next(n.name for n in nets if n.name in results)]
        self.name = _safe(first["title"])[:150] or "video"
        many = len(first["parts"]) > 1
        for index, part in enumerate(first["parts"]):
            urls = {
                name: item["url"]
                for name, info in results.items()
                for item in info["parts"]
                if item["id"] == part["id"]
            }
            filename = f"{self.name}.part{index}.{part['ext']}" if many else f"{self.name}.{part['ext']}"
            download = Download(
                self.mgr,
                part["url"],
                {"User-Agent": self.mgr.setting("user_agent", ""), "Accept": "*/*", **part["headers"],
                 "Accept-Encoding": "identity"},
                filename,
                self.dir,
                self.category,
            )
            download.net_urls = urls
            download.hidden = True
            with self._lock:
                self.parts.append(download)
            self.mgr.downloads[download.id] = download  # the scheduler starts them

        self.stage = "downloading"
        self._wait()

    def _wait(self):
        """Wait for every part, but never spin: any terminal state ends the wait."""
        while True:
            with self._lock:
                parts = list(self.parts)
            if self.cancelled or any(p.id not in self.mgr.downloads for p in parts):
                self.stage = "cancelled"
                self.finished = time.time()
                return
            statuses = [p.status for p in parts]
            if all(s == "done" for s in statuses):
                return self._merge()
            if any(s in ("error", "cancelled") for s in statuses):
                failed = next((p for p in parts if p.status == "error"), None)
                self.error = (failed.error if failed and failed.error else "") or "download stopped"
                self.stage = "error" if failed else "cancelled"
                self.finished = time.time()
                self._discard_parts()
                return
            if all(s in ("paused", "pausing") for s in statuses):
                self.stage = "paused"
                return
            time.sleep(0.5)

    # -- merge -------------------------------------------------------------
    def _merge(self):
        with self._lock:
            parts = list(self.parts)
        if len(parts) > 1:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                # Keep both files and finish: the user still gets the media.
                self.stage = "done"
                self.error = "ffmpeg not found: video and audio were saved as separate files"
                self.finished = time.time()
                return
            self.stage = "merging"
            first, second = parts[0].path, parts[1].path
            if not first or not second:
                self.stage, self.error = "error", "missing part file"
                self.finished = time.time()
                return
            container = "mp4" if first.endswith(".mp4") and second.endswith((".m4a", ".mp4")) else "mkv"
            output = _unique(os.path.join(self.dir, f"{self.name}.{container}"))
            process = subprocess.run(
                [ffmpeg, "-y", "-loglevel", "error", "-i", first, "-i", second, "-c", "copy", output],
                capture_output=True,
                text=True,
            )
            if process.returncode:
                self.stage = "error"
                self.error = "merge failed: " + process.stderr.strip()[-200:]
                self.finished = time.time()
                return
            for part in parts:
                try:
                    os.remove(part.path)
                except OSError:
                    pass
            self.parts = []
            self.name = os.path.basename(output)
        elif parts:
            self.name = parts[0].name
        self.stage = "done"
        self.finished = time.time()
        self._release_parts()

    def _release_parts(self):
        """Drop hidden parts from the manager but keep their files."""
        for part in self.parts:
            self.mgr.downloads.pop(part.id, None)

    def _discard_parts(self):
        for part in self.parts:
            if part.path:
                try:
                    part._discard()
                except Exception:  # noqa: BLE001
                    pass
            self.mgr.downloads.pop(part.id, None)

    # -- actions -----------------------------------------------------------
    def act(self, action: str) -> bool:
        if action == "cancel":
            if self.stage == "resolving":
                self.cancelled = True
                return True
            if self.stage in ("downloading", "paused", "pausing"):
                for part in self.parts:
                    self.mgr.action(part.id, "cancel")
                return True
            return True
        if action == "remove":
            if self.stage in ("done", "error", "cancelled") or self.status in ("cancelled", "error"):
                self._release_parts()
                self.mgr.downloads.pop(self.id, None)
            return True
        if action == "delete":
            if self.stage in ("done", "error", "cancelled") or self.status in ("cancelled", "error"):
                for part in self.parts:
                    if part.path:
                        part._discard()
                self._release_parts()
                self.mgr.downloads.pop(self.id, None)
            return True
        if action in ("pause", "resume", "retry"):
            for part in self.parts:
                self.mgr.action(part.id, action)
            if action in ("resume", "retry") and self.stage == "paused":
                self.stage = "downloading"
                self.thread = threading.Thread(
                    target=self._wait, name=f"qorvex-media-{self.id}", daemon=True
                )
                self.thread.start()
            return True
        return False

    # -- UI ----------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            parts = list(self.parts)
        size = sum(p.size for p in parts) if parts and all(p.size for p in parts) else None
        done = sum(min(p.done, p.size or p.done) for p in parts)
        status = self.status
        speed = sum(p.speed for p in parts) if status in ("downloading", "pausing") else 0
        nets: dict[str, int] = {}
        for part in parts:
            for name, state in list(part.ns.items()):
                nets[name] = nets.get(name, 0) + state["bytes"]
        if status == "done" and size:
            done = size
        if self.stage == "done":
            path = parts[0].path if parts else os.path.join(self.dir, self.name)
        else:
            path = None
        return {
            "id": self.id,
            "kind": "media",
            "name": self.name,
            "url": self.url,
            "status": status,
            "error": self.error or next((p.error for p in parts if p.error), ""),
            "size": size,
            "done": done,
            "speed": speed,
            "eta": (size - done) / speed if size and speed > 1 else None,
            "nets": nets,
            "down": sorted({name for p in parts for name, state in p.ns.items() if state["down"]}),
            "path": path,
            "dir": self.dir,
            "category": self.category or "video",
            "added": self.added,
            "finished": self.finished,
            "connections": sum(len(p.active) for p in parts),
            "hidden": False,
        }
