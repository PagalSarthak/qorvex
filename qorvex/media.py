"""Pages whose video is not a plain link (YouTube and many other sites).

yt-dlp resolves the page into direct media URLs. Those URLs are often tied to the
IP that requested them, so we resolve once per network and let each network fetch
its own URL. Video + audio are downloaded as two hidden parts and merged with
ffmpeg into one file.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

from .engine import READ, Download, _safe, open_url


def _ffmpeg_path() -> str | None:
    """Bundled ffmpeg first (frozen builds / build_cache), then PATH, then env override."""
    env = os.environ.get("QORVEX_FFMPEG")
    if env and os.path.isfile(env):
        return env
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    roots = []
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        roots.append(bundled)                     # PyInstaller onefile/onedir bundle
    if getattr(sys, "frozen", False) and sys.executable:
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    roots.append(project)                         # source checkouts: repo root
    roots.append(os.path.join(project, "build_cache"))  # downloaded by build scripts
    for root in roots:
        candidate = os.path.join(root, exe)
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("ffmpeg")


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
    if os.environ.get("QORVEX_DEBUG"):
        opts["verbose"] = True
        _debug_logger = logging.getLogger("qorvex.ytdlp")
        _debug_logger.setLevel(logging.DEBUG)
        opts["logger"] = _debug_logger
    if net and net.ip:
        opts["source_address"] = net.ip
    if extra.get("cookies_from_browser"):
        opts["cookiesfrombrowser"] = (extra["cookies_from_browser"],)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(page_url, download=False)
    parts = []
    all_formats = info.get("formats") or []
    selected = info.get("requested_formats") or [info]

    # Prefer HLS (combined audio+video) whenever it matches the requested
    # quality: its segment URLs are the one thing YouTube's SABR gating does
    # not 403-flip, so downloads stay reliable. Fall back to direct https
    # parts when no HLS format covers the selected quality.
    video = next((f for f in selected if f.get("vcodec") not in (None, "none")), None)
    audio = next((f for f in selected if f.get("vcodec") in (None, "none") and f.get("acodec") != "none"), None)
    hls_best = None
    if video:
        candidates = [
            f for f in all_formats
            if str(f.get("protocol") or "").startswith("m3u8")
            and f.get("vcodec") not in (None, "none")
            and f.get("acodec") not in (None, "none")  # combined: includes audio
            and int(f.get("height") or 0) >= int(video.get("height") or 0)
        ]
        if candidates:
            hls_best = max(candidates, key=lambda f: int(f.get("height") or 0))

    def _part(fmt, hls=False):
        return {
            "id": str(fmt.get("format_id")),
            "url": fmt["url"],
            "ext": fmt.get("ext") or ("mp4" if hls else "bin"),
            "headers": fmt.get("http_headers") or {},
            "hls": hls,
        }

    if hls_best:
        parts.append(_part(hls_best, hls=True))
    elif video and audio:
        parts.append(_part(video))
        parts.append(_part(audio))
    elif video or audio:
        parts.append(_part(video or audio))
    else:
        raise Unsupported(
            "this video uses a streaming format (HLS/DASH) whose playlist could "
            "not be resolved"
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


class HlsDownload(Download):
    """Fallback for YouTube's SABR-only responses: the resolved "url" is an HLS
    playlist. Fetch the segments sequentially, then concatenate with ffmpeg.
    Bonding does not apply (segments are short-lived URLs), but the download works."""

    def __init__(self, mgr, url, headers, name="", directory=None, category=None):
        super().__init__(mgr, url, headers, name, directory, category)
        self.temp_dir = None

    def _cleanup_temp(self):
        temp, self.temp_dir = self.temp_dir, None
        if temp:
            shutil.rmtree(temp, ignore_errors=True)

    def _read_url(self, net, url):
        conn = None
        try:
            conn, r, final = open_url(
                url, net,
                {**self.headers, "Accept": "*/*", "Accept-Encoding": "identity"},
                verify=self._verify(),
            )
            if r.status != 200:
                raise OSError(f"HTTP {r.status}")
            return r.read().decode("utf-8", "replace"), final
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    def _unique_target(self):
        os.makedirs(self.dir, exist_ok=True)
        self.name = _safe(self.name)
        path = os.path.join(self.dir, self.name)
        base, ext = os.path.splitext(path)
        index = 1
        while os.path.exists(path):
            path = f"{base} ({index}){ext}"
            index += 1
        return path

    def _concat(self, segments, output, ext):
        ffmpeg = _ffmpeg_path()
        if not ffmpeg:
            raise OSError("ffmpeg not found (needed for HLS)")
        listfile = os.path.join(self.temp_dir, "list.txt")
        with open(listfile, "w", encoding="utf-8") as handle:
            for seg in segments:
                handle.write("file '" + seg.replace("'", "'\\''") + "'\n")
        args = [ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listfile]
        if ext == "ts":  # transport streams need a real remux into mp4
            args += ["-c", "copy", "-movflags", "+faststart"]
        else:
            args += ["-c", "copy"]
        args += [output]
        process = subprocess.run(args, capture_output=True, text=True)
        if process.returncode:
            raise OSError("concat failed: " + process.stderr.strip()[-160:])

    def _run_inner(self):
        nets = [n for n in self.mgr.usable() if self.allowed(n)]
        if not nets:
            return self._fail("no network enabled")
        net = nets[0]
        self._cleanup_temp()

        # master playlist -> best variant -> media playlist
        url, base, playlist, last = self.url_for(net), None, None, None
        for _ in range(3):
            if self.stop or self.cancel:
                return
            try:
                playlist, base = self._read_url(net, url)
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(1.2)
                continue
            if "#EXT-X-STREAM-INF" not in playlist:
                break
            best, bandwidth = None, -1
            for match in re.finditer(r"#EXT-X-STREAM-INF[^\r\n]*[\r\n]+([^\r\n#][^\r\n]*)", playlist):
                found = re.search(r"BANDWIDTH=(\d+)", match.group(0))
                rate = int(found.group(1)) if found else 0
                if rate > bandwidth:
                    bandwidth, best = rate, match.group(1).strip()
            if not best:
                return self._fail("empty HLS playlist")
            url = urllib.parse.urljoin(base, best)
        if not playlist or "#EXTINF" not in playlist:
            return self._fail(f"playlist fetch failed: {last}")
        if "#EXT-X-KEY" in playlist:
            return self._fail("encrypted HLS stream; no decoder for it")
        segments = [
            urllib.parse.urljoin(base, line.strip())
            for line in playlist.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not segments:
            return self._fail("empty HLS playlist")

        output = self._unique_target()
        ext = os.path.splitext(output)[1].lstrip(".") or "mp4"
        self.status = "downloading"
        self.size = None
        self.temp_dir = os.path.join(self.dir, f".qorvex-hls-{self.id}")
        os.makedirs(self.temp_dir, exist_ok=True)
        saved = []
        try:
            for index, segment in enumerate(segments):
                data = None
                for attempt in range(6):
                    if self.stop or self.cancel:
                        return
                    conn = None
                    try:
                        conn, r, _ = open_url(
                            segment, None,
                            {**self.headers, "Accept": "*/*", "Accept-Encoding": "identity"},
                            verify=self._verify(),
                        )
                        if r.status != 200:
                            raise OSError(f"HTTP {r.status}")
                        buffer = []
                        while True:
                            if self.stop or self.cancel:
                                return
                            chunk = r.read(READ)
                            if not chunk:
                                break
                            buffer.append(chunk)
                        data = b"".join(buffer)
                        break
                    except Exception as exc:  # noqa: BLE001 - retry the segment
                        last = exc
                        time.sleep(min(8.0, 1.0 * (attempt + 1)))
                    finally:
                        if conn is not None:
                            try:
                                conn.close()
                            except OSError:
                                pass
                if data is None:
                    return self._fail(f"segment {index} failed: {last}")
                out = os.path.join(self.temp_dir, f"{index:06d}.seg")
                with open(out, "wb") as handle:
                    handle.write(data)
                self._account(net, len(data))
                saved.append(out)
            if self.cancel:
                self.status = "cancelled"
                return
            self._concat(saved, output, ext)
            self.path = output
            self.name = os.path.basename(output)
            self.size = os.path.getsize(output)
            self.done = self.size
            self.status = "done"
        finally:
            self._cleanup_temp()


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
            cls = HlsDownload if part.get("hls") else Download
            download = cls(
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
            ffmpeg = _ffmpeg_path()
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
