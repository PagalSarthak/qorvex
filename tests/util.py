"""Shared helpers for the test suite: a ranged HTTP server and app factories."""
from __future__ import annotations

import hashlib
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qorvex.config import Settings
from qorvex.engine import Manager, Network

SIZE = 12 * 1024 * 1024
HLS_SEG = 512 * 1024
DATA = os.urandom(SIZE)
SHA = hashlib.sha256(DATA).hexdigest()


class State:
    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.fail_until = 0.0
        self.delay = 0.01
        self.lie = False
        self.hits = 0
        self.limited = 0

    def reset(self) -> None:
        self.seen.clear()
        self.fail_until = 0.0
        self.delay = 0.01
        self.lie = False
        self.hits = 0
        self.limited = 0


STATE = State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _status(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        now = time.time()
        rng = self.headers.get("Range")
        if path == "/locked":
            wanted = dict(part.split("=", 1) for part in query.split("&") if "=" in part).get("ip")
            if wanted != self.client_address[0]:
                return self._status(403)
        if path == "/hls":
            # a small HLS media playlist: segments covering exactly DATA
            count = len(DATA) // HLS_SEG
            body = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
            body += "".join(f"#EXTINF:10.0,\n/seg/{i}\n" for i in range(count))
            body += "#EXT-X-ENDLIST\n"
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode())
            return
        if path.startswith("/seg/"):
            index = int(path.rsplit("/", 1)[1])
            seg = DATA[index * HLS_SEG:(index + 1) * HLS_SEG]
            self.send_response(200)
            self.send_header("Content-Length", str(len(seg)))
            self.end_headers()
            self.wfile.write(seg)
            return
        if path == "/fail" and now < STATE.fail_until:
            return self._status(500)
        if path == "/failranges" and now < STATE.fail_until and rng != "bytes=0-0":
            return self._status(500)
        if path == "/404":
            return self._status(404)
        if path == "/limited" and rng != "bytes=0-0" and STATE.limited > 0:
            STATE.limited -= 1
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        STATE.seen.add(self.client_address[0])
        STATE.hits += 1
        start, end = 0, len(DATA) - 1
        ranged = bool(rng) and path != "/norange"
        if ranged:
            first, _, last = rng.replace("bytes=", "").partition("-")
            start = int(first)
            end = min(int(last) if last else end, len(DATA) - 1)
            self.send_response(206)
            announced = start + 1000 if STATE.lie else start
            self.send_header("Content-Range", f"bytes {announced}-{end}/{len(DATA)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Disposition", 'attachment; filename="test.bin"')
        self.send_header("ETag", '"v1"')
        self.end_headers()
        position = start
        while position <= end:
            size = min(64 * 1024, end - position + 1)
            try:
                self.wfile.write(DATA[position:position + size])
            except (BrokenPipeError, ConnectionResetError):
                return
            position += size
            if STATE.delay:
                time.sleep(STATE.delay)


def start_server() -> tuple[int, list[ThreadingHTTPServer]]:
    """One port served on both 127.0.0.1 and 127.0.0.2 (two fake NICs)."""
    first = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    first.daemon_threads = True
    port = first.server_address[1]
    servers = [first]
    try:
        second = ThreadingHTTPServer(("127.0.0.2", port), Handler)
        second.daemon_threads = True
        servers.append(second)
    except OSError:
        first.server_close()
        fallback = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        fallback.daemon_threads = True
        servers = [fallback]
        port = fallback.server_address[1]
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return port, servers


def wait_for(download, statuses, timeout: float = 90.0) -> str:
    deadline = time.time() + timeout
    while download.status not in statuses and time.time() < deadline:
        time.sleep(0.05)
    return download.status


def digest(path: str | None) -> str:
    assert path, "download has no path"
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "download_dir": str(tmp_path / "downloads"),
        "categorize": False,
        "clipboard_watch": False,
        "open_ui_on_start": False,
        "streams_per_network": 4,
        "retry_limit": 8,
        "network_fail_limit": 4,
        **overrides,
    }
    settings = Settings(str(tmp_path / "config.json"))
    settings.update(values)
    return settings


def make_manager(tmp_path, settings=None, names=("netA", "netB")) -> Manager:
    settings = settings or make_settings(tmp_path)
    networks = [Network(name, "127.0.0.1" if index == 0 else "127.0.0.2")
                for index, name in enumerate(names)]
    manager = Manager(str(tmp_path / "downloads"), networks, max_active=8, settings=settings)
    manager.max_active = 8
    return manager
