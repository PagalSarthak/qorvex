"""Qorvex engine: chunked HTTP(S) downloads bonded across several interfaces.

Design notes
------------
* One :class:`Download` owns a file, a chunk queue and per-network workers.
* Every worker writes with ``pwrite`` (or a locked seek+write fallback), so
  chunks can arrive in any order without a merge step.
* Idle connections steal the tail of the largest in-flight chunk.
* Resume state is a sidecar (``file.qorvex``) validated against URL, size and
  ETag/Last-Modified so a changed remote file never silently corrupts output.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from collections import deque

from .config import DEFAULT_UA
from .system import default_download_dir

UA = DEFAULT_UA
READ = 64 * 1024
MiB = 1 << 20
STREAMS = 4
MIN_STEAL = 512 * 1024
SKIP_IFACES = ("docker", "veth", "br-", "virbr", "vmnet", "vboxnet", "utun", "awdl", "llw", "lo")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {
    f"lpt{i}" for i in range(1, 10)
}

CATEGORY_EXTENSIONS: dict[str, set[str]] = {
    "video": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".3gp"},
    "audio": {".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma"},
    "documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".epub", ".mobi", ".csv"},
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".dmg"},
    "programs": {".exe", ".msi", ".pkg", ".deb", ".rpm", ".apk", ".appimage", ".bin"},
}


def category_for(name: str) -> str:
    extension = os.path.splitext(name or "")[1].lower()
    for category, extensions in CATEGORY_EXTENSIONS.items():
        if extension in extensions:
            return category
    return "other"


class RateLimited(Exception):
    """The server asked us to slow down (HTTP 429/503) instead of failing."""

    def __init__(self, delay: float = 2.0) -> None:
        super().__init__(f"server rate limited the download (retry in {delay:.0f}s)")
        self.delay = delay


# ───────────────────────── rate limiting ─────────────────────────
class RateLimiter:
    """Aggregate byte/s limiter shared by every connection and download."""

    def __init__(self, bytes_per_second: float = 0):
        self._limit = 0.0
        self._next = 0.0
        self._lock = threading.Lock()
        self.configure(bytes_per_second)

    def configure(self, bytes_per_second: float) -> None:
        with self._lock:
            self._limit = max(0.0, float(bytes_per_second or 0))
            self._next = 0.0

    @property
    def limit(self) -> float:
        with self._lock:
            return self._limit

    def wait(self, amount: int) -> None:
        if amount <= 0:
            return
        with self._lock:
            if not self._limit:
                return
            now = time.monotonic()
            self._next = max(self._next, now) + amount / self._limit
            delay = self._next - now
        if delay > 0.02:
            time.sleep(min(delay, 2.0))


# ───────────────────────── networks ─────────────────────────
class Network:
    def __init__(self, name, ip, device=None, enabled=True, limit=0):
        self.name, self.ip, self.device = name, ip, device
        self.enabled, self.limit = enabled, limit  # limit: bytes/s, 0 = unlimited
        self.total = 0
        self._win = deque()
        self._lock = threading.Lock()
        self._next = 0.0

    def record(self, n):
        now = time.monotonic()
        with self._lock:
            self.total += n
            self._win.append((now, n))
            while self._win and now - self._win[0][0] > 3:
                self._win.popleft()

    @property
    def speed(self):
        now = time.monotonic()
        with self._lock:
            return sum(n for t, n in self._win if now - t <= 3) / 3

    def throttle(self, n):
        if not self.limit:
            return
        with self._lock:
            now = time.monotonic()
            self._next = max(self._next, now) + n / self.limit
            wait = self._next - now
        if wait > 0.02:
            time.sleep(min(wait, 2))

    def snapshot(self):
        return {
            "name": self.name,
            "ip": self.ip,
            "enabled": self.enabled,
            "speed": self.speed,
            "total": self.total,
            "limit": self.limit,
        }


def _psutil():
    try:
        import psutil

        return psutil
    except ImportError:
        return None


def device_for_ip(ip):
    ps = _psutil()
    if ps:
        for dev, addrs in ps.net_if_addrs().items():
            if any(a.family == socket.AF_INET and a.address == ip for a in addrs):
                return dev
    return None


def detect_networks(include_loopback=False, manual=()):
    """Every IPv4 interface that is up. `manual` entries look like NAME=IP."""
    nets = []
    for spec in manual:
        name, _, ip = str(spec).partition("=")
        name, ip = name.strip(), ip.strip()
        if name and ip:
            nets.append(Network(name, ip, device_for_ip(ip)))
    ps = _psutil()
    if ps and not manual:
        stats = ps.net_if_stats()
        for dev, addrs in ps.net_if_addrs().items():
            if dev in stats and not stats[dev].isup:
                continue
            if not include_loopback and dev.startswith(SKIP_IFACES):
                continue
            for a in addrs:
                if a.family != socket.AF_INET or not a.address or a.address.startswith("169.254."):
                    continue
                if a.address.startswith("127.") and not include_loopback:
                    continue
                nets.append(Network(dev, a.address, dev))
    seen = {}
    for n in nets:  # make names unique
        seen[n.name] = seen.get(n.name, 0) + 1
        if seen[n.name] > 1:
            n.name = f"{n.name}-{seen[n.name]}"
    return nets or [Network("default", None)]


# ───────────────────────── HTTP plumbing ─────────────────────────
def _tls_context(verify: bool):
    if verify:
        return ssl.create_default_context()
    return ssl._create_unverified_context()  # noqa: SLF001 - explicit debug opt-out


def _bound_socket(host, port, net, timeout):
    """TCP socket that leaves through `net`.

    Binding the source IP alone is not enough on Linux/macOS (packets still
    follow the default route), so also pin the device where allowed.
    """
    err = None
    for fam, typ, proto, _, addr in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        s = socket.socket(fam, typ, proto)
        try:
            s.settimeout(timeout)
            if net is not None and net.ip:
                try:
                    if sys.platform.startswith("linux") and net.device:
                        s.setsockopt(socket.SOL_SOCKET, 25, net.device.encode() + b"\0")  # SO_BINDTODEVICE
                    elif sys.platform == "darwin" and net.device:
                        s.setsockopt(socket.IPPROTO_IP, 25, socket.if_nametoindex(net.device))  # IP_BOUND_IF
                except OSError:
                    pass  # needs privileges on some kernels; source-IP bind below still applies
                s.bind((net.ip, 0))
            s.connect(addr)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return s
        except OSError as e:
            err = e
            s.close()
    raise err or OSError("cannot connect")


class _HTTP(http.client.HTTPConnection):
    net = None

    def connect(self):
        self.sock = _bound_socket(self.host, self.port, self.net, self.timeout)


class _HTTPS(http.client.HTTPSConnection):
    net = None
    context = None

    def connect(self):
        raw = _bound_socket(self.host, self.port, self.net, self.timeout)
        self.sock = self.context.wrap_socket(raw, server_hostname=self.host)


def open_url(url, net, headers, redirects=8, timeout=20, verify=True):
    hdrs = dict(headers)
    for _ in range(redirects + 1):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise OSError("unsupported URL")
        if u.scheme == "https":
            conn = _HTTPS(u.hostname, u.port or 443, timeout=timeout)
            conn.context = _tls_context(verify)
        else:
            conn = _HTTP(u.hostname, u.port or 80, timeout=timeout)
        conn.net = net
        conn.request("GET", (u.path or "/") + ("?" + u.query if u.query else ""), headers=hdrs)
        r = conn.getresponse()
        loc = r.getheader("Location")
        if r.status in (301, 302, 303, 307, 308) and loc:
            nxt = urllib.parse.urljoin(url, loc)
            r.read()
            conn.close()
            if urllib.parse.urlsplit(nxt).hostname != u.hostname:  # never leak credentials cross-host
                hdrs = {k: v for k, v in hdrs.items() if k.lower() not in ("cookie", "authorization")}
            url = nxt
            continue
        return conn, r, url
    raise OSError("too many redirects")


def _safe(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name or ""))
    name = name.strip(" .")
    if not name:
        return "download"
    stem = name.split(".")[0].lower()
    if stem in WINDOWS_RESERVED:
        name = "_" + name
    if len(name) > 180:
        root, ext = os.path.splitext(name)
        name = root[: 180 - len(ext)] + ext
    return name


def _filename(resp, url):
    cd = resp.getheader("Content-Disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    if m:
        raw = urllib.parse.unquote(m.group(1))
    else:
        raw = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    # servers sometimes leak a full path into the name; keep the basename only
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    return _safe(raw)


def probe(url, net, headers, verify=True):
    conn, r, final = open_url(url, net, {**headers, "Range": "bytes=0-0"}, verify=verify)
    try:
        size, ranged = None, False
        if r.status == 206:
            m = re.match(r"bytes \d+-\d+/(\d+)", r.getheader("Content-Range") or "")
            if m:
                size, ranged = int(m.group(1)), True
        elif r.status == 200:
            cl = r.getheader("Content-Length")
            size = int(cl) if cl and cl.isdigit() else None
        elif r.status in (429, 503):
            delay = 2.0
            raw = r.getheader("Retry-After") or ""
            try:
                delay = max(1.0, min(60.0, float(raw)))
            except ValueError:
                pass
            raise RateLimited(delay)
        else:
            raise OSError(f"HTTP {r.status}")
        return {
            "size": size,
            "ranged": ranged,
            "url": final,
            "name": _filename(r, final),
            "etag": r.getheader("ETag"),
            "last_modified": r.getheader("Last-Modified"),
        }
    finally:
        conn.close()


# ───────────────────────── a single download ─────────────────────────
class Chunk:
    __slots__ = ("pos", "end", "retries")

    def __init__(self, pos, end):
        self.pos, self.end, self.retries = pos, end, 0


class Download:
    kind = "file"

    def __init__(self, mgr, url, headers, name="", directory=None, category=None):
        self.id = uuid.uuid4().hex[:8]
        self.mgr, self.url, self.headers = mgr, url, headers
        self.requested_name = name
        self.name = name
        self.dir = directory or mgr.dir
        self.directory = directory
        self.category = category
        self.path = None
        self.status, self.error = "queued", ""
        self.size, self.ranged, self.done = None, False, 0
        self.etag = self.last_modified = None
        self.added = time.time()
        self.finished = None
        self.ns = {}                      # per-network stats for this download
        self.lock = threading.Lock()
        self.pending, self.active = deque(), []
        self.stop = self.cancel = False
        self.fatal = None
        self.win = deque()
        self.thread = None
        self.fd = None
        self.net_urls = {}   # per-network URLs (e.g. YouTube links are tied to the IP that resolved them)
        self.hidden = False  # part of a MediaJob
        self.discard_on_close = False
        self.stream_penalty = 0     # drop parallel connections when the server says 429
        self.cooldown_until = 0.0

    # -- helpers
    def _cfg(self, key, default=None):
        return self.mgr.setting(key, default)

    def _verify(self):
        return bool(self._cfg("verify_tls", True))

    def _streams(self):
        return max(1, int(self._cfg("streams_per_network", STREAMS)))

    def _active_streams(self):
        return max(1, self._streams() - self.stream_penalty)

    def _wait_cooldown(self):
        while not self.stop and not self.fatal:
            remaining = self.cooldown_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    def st(self, net):
        return self.ns.setdefault(net.name, {"bytes": 0, "fails": 0, "down": False})

    def url_for(self, net):
        return self.net_urls.get(net.name, self.url)

    def allowed(self, net):
        return not self.net_urls or net.name in self.net_urls

    @property
    def speed(self):
        now = time.monotonic()
        with self.lock:
            return sum(n for t, n in self.win if now - t <= 3) / 3

    def _write(self, off, data):
        with self.lock:
            if self.fd is None:
                raise OSError("download is closing")
            if hasattr(os, "pwrite"):
                os.pwrite(self.fd, data, off)
            else:
                os.lseek(self.fd, off, os.SEEK_SET)
                os.write(self.fd, data)

    def _account(self, net, n):
        now = time.monotonic()
        with self.lock:
            self.done += n
            self.st(net)["bytes"] += n
            self.win.append((now, n))
            while self.win and now - self.win[0][0] > 3:
                self.win.popleft()
        net.record(n)

    def _save_state(self):
        if not self.path:
            return
        with self.lock:
            remaining = [[c.pos, c.end] for c in list(self.pending) + list(self.active) if c.pos <= c.end]
            document = {
                "url": self.url,
                "size": self.size,
                "etag": self.etag,
                "last_modified": self.last_modified,
                "remaining": remaining,
            }
        tmp = self.path + ".qorvex.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(document, f)
            os.replace(tmp, self.path + ".qorvex")
        except OSError:
            pass

    # -- file allocation
    def _open_file(self):
        os.makedirs(self.dir, exist_ok=True)
        self.name = _safe(self.name)
        path = os.path.join(self.dir, self.name)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        if os.path.exists(path + ".qorvex"):
            self.path = path
            return os.open(path, flags)
        base, ext = os.path.splitext(path)
        for i in range(1000):
            candidate = path if i == 0 else f"{base} ({i}){ext}"
            try:
                fd = os.open(candidate, flags | os.O_EXCL)
            except FileExistsError:
                continue
            self.path = candidate
            return fd
        raise OSError("could not choose a free file name")

    # -- lifecycle
    def start(self):
        self.stop = self.cancel = False
        self.discard_on_close = False
        self.stream_penalty = 0
        self.cooldown_until = 0.0
        self.fatal, self.error = None, ""
        for state in self.ns.values():  # a resumed download gets a fresh chance per network
            state["down"] = False
            state["fails"] = 0
        self.status = "starting"
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _fail(self, msg):
        self.status, self.error = "error", msg
        self.finished = time.time()

    def _run(self):
        try:
            self._run_inner()
        except Exception as e:  # noqa: BLE001
            self._fail(str(e))
        finally:
            with self.lock:
                fd, self.fd = self.fd, None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if self.discard_on_close:
                self._discard()
                self.done = 0
                self.status = "cancelled"
            if self.status == "pausing":
                self.status = "paused"

    def _run_inner(self):
        nets = [n for n in self.mgr.usable() if self.allowed(n)]
        if not nets:
            return self._fail("no network enabled")
        # probe every network in parallel: a slow server must not multiply by
        # the number of networks (20s timeout each, sequentially = minutes)
        info, last = None, None
        for _ in range(4):  # a 429 during the probe is temporary, not a dead network
            if self.stop:
                break
            results = {}
            threads = []
            for n in nets:
                def _probe_net(n=n):
                    try:
                        results[n.name] = probe(self.url_for(n), n, self.headers, verify=self._verify())
                    except RateLimited as e:
                        results[n.name] = e
                        self.cooldown_until = max(self.cooldown_until, time.monotonic() + e.delay)
                    except Exception as e:  # noqa: BLE001
                        results[n.name] = e
                t = threading.Thread(target=_probe_net, daemon=True)
                t.start()
                threads.append(t)
            deadline = time.monotonic() + 25
            while any(t.is_alive() for t in threads) and time.monotonic() < deadline and not self.stop:
                time.sleep(0.1)
            for n in nets:
                r = results.get(n.name)
                if isinstance(r, RateLimited):
                    last = r
                elif isinstance(r, Exception):
                    last = r
                elif r is not None:
                    info = r
                    break
            if info or self.stop:
                break
            if not isinstance(last, RateLimited):
                break
            self._wait_cooldown()
        if not info:
            return self._fail(f"probe failed: {last}")
        if not self.net_urls:
            self.url = info["url"]
        self.size, self.ranged = info["size"], info["ranged"]
        self.etag, self.last_modified = info["etag"], info["last_modified"]
        self.name = _safe(self.name or info["name"])
        if not self.category:
            self.category = self.mgr.category_for(self.name)
        if self.directory is None and self._cfg("categorize", True):
            self.dir = os.path.join(self.mgr.dir, self.category)
        if self.path is None:
            self.fd = self._open_file()
        else:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        self.name = os.path.basename(self.path)
        resumed = self._load_resume()
        if not self.ranged or not self.size:
            return self._run_single(nets[0])
        if not resumed:
            with self.lock:
                self.done = 0
                self.active = []
                streams = max(1, len(nets) * self._streams())
                chunk_min = int(self._cfg("chunk_min_mb", 1)) * MiB
                chunk_max = int(self._cfg("chunk_max_mb", 8)) * MiB
                cs = min(chunk_max, max(chunk_min, self.size // (streams * 6)))
                self.pending = deque(Chunk(s, min(s + cs, self.size) - 1) for s in range(0, self.size, cs))
        # leave "starting" before the (potentially slow) file allocation so the
        # UI stops showing a frozen state; workers only spawn below, after the
        # truncate has completed, so no chunk can write before it is done
        self.status = "downloading"
        if not resumed:
            os.ftruncate(self.fd, self.size)
        workers, last_save = {}, 0.0
        no_networks = False
        while not self.stop and not self.fatal:
            with self.lock:
                if not self.pending and not self.active:
                    break
            enabled = [n for n in self.mgr.networks if n.enabled and self.allowed(n)]
            if not enabled:
                no_networks = True
                break
            for n in enabled:
                if self.st(n)["down"]:
                    continue
                for i in range(self._active_streams()):
                    key = (n.name, i)
                    t = workers.get(key)
                    if t is None or not t.is_alive():
                        t = threading.Thread(target=self._worker, args=(n, i), daemon=True)
                        workers[key] = t
                        t.start()
            if all(self.st(n)["down"] for n in enabled):
                self.fatal = "all networks failed"
                break
            if time.monotonic() - last_save > 1:
                self._save_state()
                last_save = time.monotonic()
            time.sleep(0.25)
        for t in workers.values():
            t.join(timeout=30)
        if self.fatal:
            self._save_state()
            return self._fail(self.fatal)
        if self.stop:
            self._save_state()
            if self.cancel:
                self.discard_on_close = True  # fd is still open: delete after it closes
            else:
                self.status = "paused"
            return
        if no_networks:
            self._save_state()
            self.status, self.error = "paused", "all networks are disabled"
            return
        self.done = self.size
        self.finished = time.time()
        try:
            os.remove(self.path + ".qorvex")
        except OSError:
            pass
        self.status = "done"

    def _load_resume(self):
        state_path = self.path + ".qorvex"
        try:
            with open(state_path, encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s, dict) or s.get("size") != self.size:
                return False
            if s.get("url") and s["url"] != self.url:
                return False
            if s.get("etag") and self.etag and s["etag"] != self.etag:
                return False
            if s.get("last_modified") and self.last_modified and s["last_modified"] != self.last_modified:
                return False
            if os.fstat(self.fd).st_size < self.size:
                return False
            chunks = []
            for row in s.get("remaining") or []:
                a, b = int(row[0]), int(row[1])
                if 0 <= a <= b < self.size:
                    chunks.append(Chunk(a, b))
            self.pending = deque(chunks)
            self.done = self.size - sum(c.end - c.pos + 1 for c in chunks)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _discard(self):
        for p in (self.path, self.path + ".qorvex", self.path + ".qorvex.tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    def _run_single(self, net):
        """Server without Range support: one stream, no resume."""
        self.status = "downloading"
        self.done = 0
        conn, r, _ = open_url(self.url_for(net), net, self.headers, verify=self._verify())
        try:
            if r.status != 200:
                return self._fail(f"HTTP {r.status}")
            os.ftruncate(self.fd, 0)
            pos = 0
            while not self.stop:
                data = r.read(READ)
                if not data:
                    self.status = "done"
                    self.finished = time.time()
                    return
                self._write(pos, data)
                pos += len(data)
                self._account(net, len(data))
                self.mgr.throttle(net, len(data))
            if self.cancel:
                self.discard_on_close = True
            else:
                self.status, self.done = "paused", 0  # cannot resume: restarts from zero
        finally:
            conn.close()

    # -- workers
    def _take(self):
        with self.lock:
            if self.pending:
                ch = self.pending.popleft()
            else:  # tail: split the biggest in-flight chunk so idle connections help finish it
                victim = max(self.active, key=lambda c: c.end - c.pos, default=None)
                if victim is None or victim.end - victim.pos < 2 * MIN_STEAL:
                    return None
                mid = (victim.pos + victim.end) // 2 + 1
                ch = Chunk(mid, victim.end)
                victim.end = mid - 1
            self.active.append(ch)
            return ch

    def _worker(self, net, index=0):
        st = self.st(net)
        retry_limit = int(self._cfg("retry_limit", 12))
        fail_limit = int(self._cfg("network_fail_limit", 4))
        while not self.stop and not self.fatal and net.enabled and not st["down"]:
            if index >= self._active_streams():
                return  # the server asked us to use fewer connections
            ch = self._take()
            if ch is None:
                with self.lock:
                    if not self.pending and not self.active:
                        return
                time.sleep(0.2)
                continue
            try:
                self._wait_cooldown()
                self._fetch(net, ch)
                st["fails"] = 0
            except RateLimited as e:
                ch.retries += 1
                self.stream_penalty = min(self.stream_penalty + 1, self._streams() - 1)
                self.cooldown_until = max(self.cooldown_until, time.monotonic() + e.delay)
                if ch.retries > retry_limit * 4:
                    self.fatal = f"server kept rate limiting: {e}"
                time.sleep(min(1.0, e.delay))
            except Exception as e:  # noqa: BLE001
                st["fails"] += 1
                ch.retries += 1
                if ch.retries > retry_limit:
                    self.fatal = f"chunk failed: {e}"
                if st["fails"] >= fail_limit:
                    st["down"] = True
                time.sleep(min(2 ** st["fails"], 8) * 0.25)

    def _fetch(self, net, ch):
        conn = None
        try:
            conn, r, _ = open_url(
                self.url_for(net),
                net,
                {**self.headers, "Range": f"bytes={ch.pos}-{ch.end}"},
                verify=self._verify(),
            )
            if r.status in (429, 503):
                delay = 2.0
                raw = r.getheader("Retry-After") or ""
                try:
                    delay = max(1.0, min(60.0, float(raw)))
                except ValueError:
                    pass
                raise RateLimited(delay)
            if r.status != 206:
                raise OSError(f"HTTP {r.status}")
            match = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", r.getheader("Content-Range") or "")
            if not match or int(match.group(1)) != ch.pos:
                raise OSError("server returned the wrong byte range")
            if match.group(3) != "*" and self.size and int(match.group(3)) != self.size:
                raise OSError("remote size changed during download")
            while ch.pos <= ch.end:
                if self.stop or not net.enabled:
                    break
                want = min(READ, ch.end - ch.pos + 1)
                if want <= 0:
                    break
                data = r.read(want)
                if not data:
                    raise OSError("connection closed early")
                n = min(len(data), ch.end - ch.pos + 1)
                if n <= 0:
                    break
                self._write(ch.pos, data[:n])
                ch.pos += n
                self._account(net, n)
                self.mgr.throttle(net, n)
        finally:
            if conn:
                conn.close()
            with self.lock:
                if ch in self.active:
                    self.active.remove(ch)
                if ch.pos <= ch.end:        # interrupted -> put the rest back
                    self.pending.appendleft(ch)

    def snapshot(self):
        size, done = self.size, min(self.done, self.size or self.done)
        sp = self.speed
        eta = (size - done) / sp if size and sp > 1 else None
        with self.lock:
            connections = len(self.active)
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name or self.url,
            "url": self.url,
            "status": self.status,
            "error": self.error,
            "size": size,
            "done": done,
            "speed": sp,
            "eta": eta,
            "nets": {k: v["bytes"] for k, v in list(self.ns.items())},
            "down": [k for k, v in list(self.ns.items()) if v["down"]],
            "path": self.path,
            "dir": self.dir,
            "category": self.category,
            "added": self.added,
            "finished": self.finished,
            "connections": connections,
        }


# ───────────────────────── manager ─────────────────────────
class Manager:
    def __init__(self, directory=None, networks=None, max_active=None, config_path=None, settings=None):
        self.settings = settings
        self.dir = os.path.abspath(os.path.expanduser(directory or self.setting("download_dir", default_download_dir())))
        self.networks = list(networks) if networks else detect_networks(
            manual=(settings.manual_networks() if settings else [])
        )
        self._manual_names = {
            str(m).partition("=")[0].strip() for m in (settings.manual_networks() if settings else [])
        }
        self.max_active = int(max_active or self.setting("max_active", 3))
        self.config_path = config_path
        self.queue_path = None
        if settings is not None:
            self.queue_path = os.path.join(os.path.dirname(os.path.abspath(settings.path)), "queue.json")
        elif config_path:
            self.queue_path = os.path.join(os.path.dirname(os.path.abspath(config_path)), "queue.json")
        self.downloads = {}
        self.ytdlp = {}   # options for yt-dlp, e.g. cookies_from_browser
        self._torrents = None   # TorrentEngine, created lazily on the first torrent
        self.limiter = RateLimiter(float(self.setting("global_limit_mb", 0) or 0) * MiB)
        self._closed = threading.Event()
        self._queue_lock = threading.Lock()
        self._load_config()
        self._restore_queue()
        threading.Thread(target=self._scheduler, name="qorvex-scheduler", daemon=True).start()
        threading.Thread(target=self._net_refresher, name="qorvex-netrefresh", daemon=True).start()

    # -- network auto-refresh ------------------------------------------------
    def refresh_networks(self):
        """Re-detect interfaces so adapters that appear or vanish (Wi-Fi,
        VPN, ...) are picked up without a restart. Manual entries and the
        saved enabled/limit settings survive the refresh."""
        manual_specs = self.settings.manual_networks() if self.settings is not None else []
        # with manual networks configured, detection stays manual-only (as at startup)
        detected = detect_networks(manual=()) if not manual_specs else []
        saved_cfg = self.settings.networks() if self.settings is not None else {}
        if not isinstance(saved_cfg, dict):
            saved_cfg = {}
        old = {n.name: n for n in self.networks}
        merged = []
        merged_names = set()
        # manual entries first, in their current order
        for n in self.networks:
            if n.name in self._manual_names:
                merged.append(n)
                merged_names.add(n.name)
        for n in detected:  # then live interfaces, skipping names a manual entry owns
            if n.name in self._manual_names or n.name in merged_names:
                continue
            prev = old.get(n.name)
            if prev is not None and prev.ip == n.ip and prev.device == n.device:
                merged.append(prev)  # keep session totals / live state
            else:
                saved = saved_cfg.get(n.name)
                if isinstance(saved, dict):
                    n.enabled = bool(saved.get("enabled", True))
                    n.limit = max(0, int(saved.get("limit", 0) or 0))
                merged.append(n)
            merged_names.add(n.name)
        if merged:
            self.networks[:] = merged
        self.update_torrent_interfaces()

    def _net_refresher(self):
        interval = max(2.0, float(self.setting("network_refresh_s", 5) or 5))
        while not self._closed.wait(interval):
            try:
                self.refresh_networks()
            except Exception:
                pass

    # -- settings helpers
    def setting(self, key, default=None):
        if self.settings is not None:
            return self.settings.get(key, default)
        return default

    def category_for(self, name):
        return category_for(name)

    def usable(self):
        return [n for n in self.networks if n.enabled]

    # -- persisted queue ----------------------------------------------------
    def _persist_queue(self):
        """Remember unfinished downloads so a restart can continue them.

        Cookies and Authorization headers are deliberately not written to disk;
        re-add such a download if the server needs them after a restart.
        """
        if not self.queue_path:
            return
        with self._queue_lock:
            self._write_queue()

    def _write_queue(self):
        rows = []
        for download in list(self.downloads.values()):
            if getattr(download, "kind", "file") != "file" or getattr(download, "hidden", False):
                continue
            if download.status in ("done", "cancelled"):
                continue
            if not str(download.url).lower().startswith(("http://", "https://")):
                continue
            headers = {
                key: value
                for key, value in download.headers.items()
                if key.lower() not in ("cookie", "authorization")
            }
            rows.append(
                {
                    "id": download.id,
                    "url": download.url,
                    "name": download.requested_name or "",
                    "dir": download.directory or "",
                    "category": download.category or "",
                    "path": download.path or "",
                    "headers": headers,
                    "status": download.status,
                }
            )
        document = {"version": 1, "downloads": rows}
        tmp = self.queue_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.queue_path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.queue_path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _restore_queue(self):
        if not self.queue_path:
            return
        try:
            with open(self.queue_path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(document, dict):
            return
        resume = bool(self.setting("resume_on_start", True))
        for row in document.get("downloads") or []:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "")
            if not url.lower().startswith(("http://", "https://")):
                continue
            headers = row.get("headers") if isinstance(row.get("headers"), dict) else {}
            download = self.add(
                url,
                headers,
                str(row.get("name") or ""),
                str(row.get("dir") or "") or None,
                str(row.get("category") or "") or None,
            )
            saved_path = str(row.get("path") or "")
            if saved_path and os.path.isabs(saved_path):
                # Reuse the exact partial file from the last session instead of
                # allocating a second "name (1)" copy and orphaning the first.
                download.path = saved_path
            download.status = "queued" if resume else "paused"
            if not resume:
                download.error = "restored from the last session"

    def _load_config(self):
        cfg = self.settings.networks() if self.settings is not None else {}
        if not cfg and self.config_path:
            try:
                with open(self.config_path, encoding="utf-8") as f:
                    cfg = json.load(f).get("networks", {})
            except (OSError, ValueError, TypeError):
                cfg = {}
        if not isinstance(cfg, dict):
            return
        for n in self.networks:
            saved = cfg.get(n.name)
            if isinstance(saved, dict):
                n.enabled = bool(saved.get("enabled", True))
                n.limit = max(0, int(saved.get("limit", 0) or 0))

    def _save_config(self):
        value = {n.name: {"enabled": n.enabled, "limit": n.limit} for n in self.networks}
        if self.settings is not None:
            self.settings.set_networks(value)
        elif self.config_path:
            try:
                with open(self.config_path, "w", encoding="utf-8") as f:
                    json.dump({"networks": value}, f)
            except OSError:
                pass

    def set_network(self, name, enabled=None, limit=None):
        for n in self.networks:
            if n.name == name:
                if enabled is not None:
                    n.enabled = bool(enabled)
                if limit is not None:
                    n.limit = max(0, int(limit))
                self._save_config()
                self.update_torrent_interfaces()
                return True
        return False

    def add_network(self, spec):
        name, _, ip = str(spec).partition("=")
        name, ip = name.strip(), ip.strip()
        if not name or not ip:
            raise ValueError("use NAME=IP")
        if any(n.name == name for n in self.networks):
            raise ValueError(f"network {name!r} already exists")
        self.networks.append(Network(name, ip, device_for_ip(ip)))
        self._manual_names.add(name)
        self._save_config()
        if self.settings is not None:
            manual = [m for m in self.settings.manual_networks() if not m.startswith(name + "=")]
            manual.append(f"{name}={ip}")
            self.settings.set_manual_networks(manual)
        return True

    def remove_network(self, name):
        for i, n in enumerate(self.networks):
            if n.name == name:
                del self.networks[i]
                self._manual_names.discard(name)
                if self.settings is not None:
                    manual = [m for m in self.settings.manual_networks() if not m.startswith(name + "=")]
                    self.settings.set_manual_networks(manual)
                self._save_config()
                return True
        return False

    def set_global_limit(self, megabytes_per_second):
        self.limiter.configure(float(megabytes_per_second or 0) * MiB)
        torrents = self._torrents
        if torrents is not None:
            torrents.set_rate_limit(float(megabytes_per_second or 0) * MiB)

    # -- torrents --------------------------------------------------------------
    def torrent_engine(self):
        """The TorrentEngine, created on first use (libtorrent is optional)."""
        if self._torrents is None:
            from .torrent import TorrentEngine

            self._torrents = TorrentEngine(self)
        return self._torrents

    @property
    def torrents(self):
        return self.torrent_engine()

    def add_torrent(self, source, directory=None, category=None):
        from .torrent import TorrentJob, torrents_available

        if not self.setting("torrent_enabled", True):
            raise ValueError("torrents are disabled in settings")
        if not torrents_available():
            from .torrent import missing_error

            raise ValueError(missing_error())
        job = TorrentJob(self, source, directory or None, category or None)
        self.downloads[job.id] = job
        return job

    def update_torrent_interfaces(self):
        torrents = self._torrents
        if torrents is not None:
            try:
                torrents.update_interfaces()
            except Exception:  # noqa: BLE001 - never break network handling
                pass

    def throttle(self, net, n):
        net.throttle(n)
        self.limiter.wait(n)

    def add(self, url, headers=None, name="", directory=None, category=None):
        target_dir = directory or self.dir
        for existing in self.downloads.values():
            if getattr(existing, "kind", "file") != "file" or getattr(existing, "hidden", False):
                continue
            if existing.status in ("done", "cancelled") or existing.url != url:
                continue
            if (existing.directory or self.dir) != target_dir:
                continue
            if name and existing.requested_name and existing.requested_name != name:
                continue
            return existing  # same URL already queued: never write two workers to one file
        h = {"User-Agent": self.setting("user_agent", UA), "Accept": "*/*", "Accept-Encoding": "identity", **(headers or {})}
        d = Download(self, url, h, name, directory or None, category or None)
        self.downloads[d.id] = d
        self._persist_queue()
        return d

    def add_many(self, urls, headers=None, directory=None, category=None):
        return [self.add(url, headers, "", directory, category) for url in urls]

    def add_media(self, page_url, quality="best", directory=None, category=None):
        from .media import MediaJob

        job = MediaJob(self, page_url, quality, directory, category)
        self.downloads[job.id] = job
        return job

    def action(self, did, act):
        d = self.downloads.get(did)
        if not d:
            return False
        if hasattr(d, "act"):      # MediaJob
            return d.act(act)
        running = d.thread is not None and d.thread.is_alive()
        if act == "pause":
            if running:
                d.stop = True
                d.status = "pausing"
            elif d.status in ("queued", "downloading", "starting", "pausing"):
                d.status = "paused"
        elif act in ("resume", "retry") and not running and d.status in (
            "paused", "error", "cancelled", "pausing"
        ):
            d.error = ""
            d.status = "queued"
        elif act == "cancel":
            if running:
                d.cancel = d.stop = True
            else:
                if d.path:
                    d._discard()
                d.status = "cancelled"
                d.done = 0
        elif act == "remove" and not running:
            self.downloads.pop(did, None)
        elif act == "delete" and not running:
            if d.path:
                d._discard()
            self.downloads.pop(did, None)
        self._persist_queue()
        return True

    def clear_finished(self):
        for did, d in list(self.downloads.items()):
            if getattr(d, "kind", "file") == "file" and d.status in ("done", "cancelled", "error"):
                if not (d.thread and d.thread.is_alive()):
                    self.downloads.pop(did, None)
        self._persist_queue()

    def _scheduler(self):
        ticks = 0
        while not self._closed.wait(0.3):
            ticks += 1
            live = [d for d in self.downloads.values() if d.thread and d.thread.is_alive()]
            free = self.max_active - len(live)
            for d in list(self.downloads.values()):
                if free <= 0:
                    break
                if d.status == "queued" and not (d.thread and d.thread.is_alive()):
                    d.start()
                    free -= 1
            if ticks % 10 == 0:
                self._persist_queue()

    def close(self):
        self._persist_queue()
        self._closed.set()
        torrents = self._torrents
        if torrents is not None:
            torrents.close()

    def state(self):
        return {
            "networks": [n.snapshot() for n in self.networks],
            "downloads": [d.snapshot() for d in list(self.downloads.values()) if not getattr(d, "hidden", False)],
            "max_active": self.max_active,
            "global_limit": self.limiter.limit,
            "dir": self.dir,
        }
