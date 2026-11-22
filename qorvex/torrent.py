"""BitTorrent downloads (magnet: links and .torrent files) via libtorrent.

libtorrent is an optional dependency (``pip install libtorrent``), handled the
same way as yt-dlp in :mod:`qorvex.media`: the rest of Qorvex works without it
and adding a torrent without it reports a clear error.

One :class:`TorrentEngine` owns the libtorrent session. Outgoing peer
connections are bound to every enabled network's IP (``outgoing_interfaces``),
so peer traffic spreads across the same interfaces the HTTP engine uses.
Incoming connections arrive on one listen port; binding those per-interface is
not portable, so bonding here is on the outbound side only.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid

from .engine import _safe, open_url


def _lt():
    try:
        import libtorrent

        return libtorrent
    except ImportError:
        return None


def torrents_available() -> bool:
    return _lt() is not None


def missing_error() -> str:
    return "libtorrent is not installed. Run: pip install -U libtorrent"


def is_torrent_source(source: str) -> bool:
    s = str(source or "").strip()
    return s.lower().startswith("magnet:") or s.lower().split("?")[0].endswith(".torrent")


class TorrentError(Exception):
    pass


# ───────────────────────── session owner ─────────────────────────
class TorrentEngine:
    """Owns the libtorrent session and maps per-torrent handles for jobs."""

    def __init__(self, mgr):
        self.mgr = mgr
        self.lt = _lt()
        self._session = None
        self._lock = threading.Lock()

    # -- session ------------------------------------------------------------
    def _ensure(self):
        if self._session is not None:
            return self._session
        lt = self.lt
        if lt is None:
            raise TorrentError(missing_error())
        rate = int(float(self.mgr.setting("global_limit_mb", 0) or 0) * (1 << 20))
        with self._lock:
            if self._session is None:
                self._session = lt.session({
                    "listen_interfaces": f"0.0.0.0:{int(self.mgr.setting('torrent_port', 6881) or 6881)}",
                    "enable_dht": True,
                    "enable_upnp": True,
                    "enable_natpmp": True,
                    "download_rate_limit": rate,   # 0 = unlimited
                    "upload_rate_limit": rate,
                })
                self.update_interfaces()
        return self._session

    def update_interfaces(self):
        """Bind outgoing peer connections to every enabled network IP."""
        if self._session is None:
            return
        ips = [n.ip for n in self.mgr.networks if n.enabled and n.ip]
        self._session.apply_settings({"outgoing_interfaces": ",".join(ips)})

    def set_rate_limit(self, bytes_per_second: float):
        if self._session is None:
            return
        limit = max(0, int(bytes_per_second or 0))
        self._session.apply_settings({
            "download_rate_limit": limit,
            "upload_rate_limit": limit,
        })

    # -- torrent lifecycle ---------------------------------------------------
    def attach(self, job):
        """Add the job's torrent to the session and return its handle."""
        session = self._ensure()
        lt = self.lt
        atp = self._params(job)
        return session.add_torrent(atp)

    def _params(self, job):
        lt = self.lt
        source = job.source
        if source.lower().startswith("magnet:"):
            atp = lt.parse_magnet_uri(source)
            atp.save_path = job.dir
            return atp
        path = self._torrent_file(job)
        atp = lt.add_torrent_params()
        try:
            atp.ti = lt.torrent_info(path)  # reads the file into memory
        except Exception:
            if self._is_temp_file(path):
                try:
                    os.unlink(path)  # corrupt upload: do not leave the copy behind
                except OSError:
                    pass
            raise
        atp.save_path = job.dir
        if self._is_temp_file(path):
            try:
                os.unlink(path)  # drag & drop / fetched copies land in the temp dir
            except OSError:
                pass
        return atp

    @staticmethod
    def _is_temp_file(path):
        tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        return os.path.normcase(os.path.abspath(path)).startswith(tmp + os.sep)

    def _torrent_file(self, job):
        """Return a local .torrent path, fetching http(s) ones over a bound net."""
        source = job.source
        if os.path.isfile(source):
            return source
        if not source.lower().startswith(("http://", "https://")):
            raise TorrentError("not a magnet link, .torrent file or .torrent URL")
        nets = [n for n in self.mgr.networks if n.enabled and n.ip] or [None]
        last = None
        for net in nets:
            try:
                conn, r, final = open_url(
                    source, net,
                    {"User-Agent": self.mgr.setting("user_agent", ""), "Accept": "*/*",
                     "Accept-Encoding": "identity"},
                    verify=bool(self.mgr.setting("verify_tls", True)),
                )
                try:
                    if r.status != 200:
                        raise TorrentError(f"HTTP {r.status} fetching .torrent")
                    data = r.read(16 * (1 << 20))
                    if not data:
                        raise TorrentError("empty .torrent response")
                finally:
                    conn.close()
                fd, path = tempfile.mkstemp(suffix=".torrent")
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                return path
            except TorrentError:
                raise
            except Exception as e:  # noqa: BLE001 - try the next interface
                last = e
        raise TorrentError(f"could not fetch .torrent: {last}")

    def detach(self, job, delete_files=False):
        handle = job.handle
        job.handle = None
        if handle is None or self._session is None:
            return
        try:
            try:  # libtorrent >= 2.0 flags form
                option = self.lt.session.delete_files if delete_files else None
                if option is None:
                    self._session.remove_torrent(handle)
                else:
                    self._session.remove_torrent(handle, option)
            except (AttributeError, TypeError):  # older bindings: keyword form
                self._session.remove_torrent(handle, delete_files=delete_files)
        except Exception:  # noqa: BLE001 - removal is best-effort on shutdown
            pass

    def close(self):
        session, self._session = self._session, None
        if session is not None:
            try:
                session.pause()
            except Exception:  # noqa: BLE001
                pass


# ───────────────────────── one torrent ─────────────────────────
class TorrentJob:
    """One row in the UI backed by a libtorrent handle."""

    kind = "torrent"

    def __init__(self, mgr, source, directory=None, category=None):
        self.id = uuid.uuid4().hex[:8]
        self.mgr = mgr
        self.source = str(source).strip()
        self.name = ""
        self.dir = directory or mgr.dir
        self.directory = directory
        self.category = category
        self.path = None
        self.error = ""
        self.status = "queued"
        self.size = None
        self.done = 0
        self.speed = 0.0
        self.seeds = 0
        self.connections = 0
        self.added = time.time()
        self.finished = None
        self.handle = None
        self._paused = False
        self._stopping = False
        self._cancelled = False
        self._peer_seen = {}   # (remote ip, port) -> last total_download
        self._net_bytes = {}   # network name -> download bytes this session
        self._net_last = {}    # network name -> monotonic time of last traffic
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name=f"qorvex-torrent-{self.id}", daemon=True)
        self.thread.start()

    # -- helpers
    def _fail(self, msg):
        self.status, self.error = "error", msg
        self.finished = time.time()

    def engine(self):
        return self.mgr.torrents

    def _seed_ratio(self):
        return max(0.0, float(self.mgr.setting("torrent_seed_ratio", 0) or 0))

    # -- main loop
    def _run(self):
        try:
            handle = self.engine().attach(self)
        except Exception as e:  # noqa: BLE001 - surfaced in the UI
            return self._fail(str(e).removeprefix("TorrentError: ") or str(e))
        with self._lock:
            self.handle = handle
        self.status = "starting"
        lt = self.engine().lt
        try:
            while not self._stopping:
                with self._lock:
                    handle = self.handle
                if handle is None:
                    return
                try:
                    st = handle.status()
                except Exception as e:  # noqa: BLE001 - session is closing
                    if self._stopping:
                        return
                    return self._fail(str(e))
                self._absorb(handle, st, lt)
                if self._finished_seeding(st, lt):
                    self.engine().detach(self)  # keep the files
                    self.path = self._path_of(st)
                    self.status = "done"
                    self.finished = time.time()
                    return
                if self._paused:
                    try:
                        handle.pause()
                    except Exception:  # noqa: BLE001
                        pass
                time.sleep(0.5)
        finally:
            if self._cancelled:
                self.engine().detach(self, delete_files=True)
                self.status, self.finished = "cancelled", time.time()
                self.done = 0
            elif self._stopping and self.status == "seeding":
                # user stopped seeding: keep the files, show the row as done
                self.engine().detach(self)
                self.done = self.size if self.size is not None else self.done
                self.status, self.finished = "done", time.time()

    def _absorb(self, handle, st, lt):
        """Copy libtorrent status into UI-facing fields."""
        self._account_peers(handle)
        with self._lock:
            self.size = st.total_wanted or None
            self.done = st.total_wanted_done
            self.seeds = st.num_seeds
            self.connections = st.num_peers
            if not self.name:
                self.name = _safe(st.name or self._fallback_name())
            self.path = self._path_of(st)
            if self._paused:
                self.status = "paused"
                self.speed = 0.0
                return
            err = ""
            try:
                if st.errc.value():
                    err = st.errc.message()
            except Exception:  # noqa: BLE001 - errc is absent on old bindings
                pass
            if err:
                self.status, self.error = "error", err
                self.finished = time.time()
                return
            self.error = ""
            state = st.state
            if lt is None or state in (lt.torrent_status.checking_files,
                                       lt.torrent_status.downloading_metadata):
                self.status = "starting"
                self.speed = st.download_payload_rate
            elif state in (lt.torrent_status.finished, lt.torrent_status.seeding):
                self.status = "seeding"
                self.speed = st.upload_payload_rate
                self.done = self.size if self.size is not None else self.done
            else:
                self.status = "downloading"
                self.speed = st.download_payload_rate

    def _fallback_name(self):
        if self.source.lower().startswith("magnet:"):
            from urllib.parse import parse_qs, urlsplit

            qs = parse_qs(urlsplit(self.source).query)
            if qs.get("dn"):
                return _safe(qs["dn"][0])
            return "magnet"
        return _safe(os.path.basename(self.source.split("?")[0])) or "torrent"

    # -- per-network accounting -----------------------------------------------
    @staticmethod
    def _value(x):
        """Some bindings expose endpoints as methods: call them."""
        if callable(x):
            try:
                return x()
            except Exception:  # noqa: BLE001
                return None
        return x

    @staticmethod
    def _endpoint(ep):
        """libtorrent endpoints arrive as (address, port); be forgiving anyway."""
        try:
            if isinstance(ep, tuple):
                return (str(ep[0]), int(ep[1]))
        except Exception:  # noqa: BLE001
            pass
        try:
            return (str(ep), 0)
        except Exception:  # noqa: BLE001
            return ("", 0)

    def _net_for(self, local_ip):
        if not local_ip:
            return None
        for n in self.mgr.networks:
            if n.ip == local_ip:
                return n.name
        return None

    def _account_peers(self, handle):
        """Split peer traffic across networks using each connection's local
        endpoint, and feed the deltas to the network cards so Wi-Fi and
        Ethernet show torrent bytes like any other download."""
        try:
            peers = handle.get_peer_info()
        except Exception:  # noqa: BLE001 - no peers / closing session
            return
        deltas, live = {}, set()
        for p in peers:
            try:
                remote = self._endpoint(self._value(p.ip))
                total_dl = int(p.total_download)
                local_ip = self._endpoint(self._value(getattr(p, "local_endpoint", None)))[0]
            except Exception:  # noqa: BLE001
                continue
            live.add(remote)
            prev = self._peer_seen.get(remote)
            delta = max(0, total_dl - prev) if prev is not None else 0
            self._peer_seen[remote] = total_dl
            name = self._net_for(local_ip)
            if name is None:
                continue
            if delta:
                deltas[name] = deltas.get(name, 0) + delta
                self._net_bytes[name] = self._net_bytes.get(name, 0) + delta
                self._net_last[name] = time.monotonic()
        # forget departed peers so their counters cannot leak memory
        if len(self._peer_seen) > 2 * (len(peers) + 16):
            self._peer_seen = {k: v for k, v in self._peer_seen.items() if k in live}
        nets = {n.name: n for n in self.mgr.networks}
        for name, delta in deltas.items():
            net = nets.get(name)
            if net is not None:
                net.record(delta)

    def _finished_seeding(self, st, lt) -> bool:
        complete = self.size and self.done >= self.size
        if not complete:
            return False
        if lt is not None and st.state not in (
            lt.torrent_status.finished, lt.torrent_status.seeding
        ):
            return False  # still checking/allocating
        wanted = self._seed_ratio()
        if wanted <= 0:
            return True
        try:
            uploaded = st.all_time_upload
        except AttributeError:
            uploaded = 0
        return uploaded / max(1, self.size) >= wanted

    def _path_of(self, st):
        """File to open for done/seeding rows: the file, or folder for multi-file."""
        base = self.dir
        try:
            ti = st.torrent_file if st.torrent_file else self.handle.torrent_file() if self.handle else None
        except Exception:  # noqa: BLE001
            ti = None
        if ti is None:
            return None
        fs = ti.files()
        if fs.num_files() == 1:
            return os.path.join(base, _safe(fs.file_path(0)))
        return base

    # -- actions (Manager.action routes here like MediaJob.act) ---------------
    def act(self, action: str) -> bool:
        if action == "pause":
            if self.status == "seeding":
                self._stopping = True  # stop seeding, keep the files
                return True
            if self.status in ("queued", "starting", "downloading"):
                self._paused = True
                return True
            return True
        if action in ("resume", "retry"):
            self.error = ""
            self._paused = False
            self._stopping = False
            with self._lock:
                handle = self.handle
            if handle is not None:
                try:
                    handle.resume()
                except Exception:  # noqa: BLE001 - handled on the next poll
                    pass
                self.status = "queued"
                return True
            if self.status == "error":  # never attached: try from scratch
                self.status = "queued"
                self.thread = threading.Thread(
                    target=self._run, name=f"qorvex-torrent-{self.id}", daemon=True
                )
                self.thread.start()
            return True
        if action == "cancel":
            with self._lock:
                handle = self.handle
            if handle is not None:
                self._cancelled = True
                self._stopping = True
            else:
                self.status, self.finished = "cancelled", time.time()
            return True
        if action == "remove":
            if self.status in ("done", "error", "cancelled"):
                self.engine().detach(self)
                self.mgr.downloads.pop(self.id, None)
            return True
        if action == "delete":
            if self.status in ("done", "error", "cancelled"):
                self.engine().detach(self, delete_files=True)
                self.mgr.downloads.pop(self.id, None)
            return True
        return False

    # -- UI ------------------------------------------------------------------
    def snapshot(self) -> dict:
        eta = None
        if self.status == "downloading" and self.size and self.speed > 1:
            eta = (self.size - self.done) / self.speed
        return {
            "id": self.id,
            "kind": "torrent",
            "name": self.name or self.source,
            "url": self.source,
            "status": self.status,
            "error": self.error,
            "size": self.size,
            "done": self.done,
            "speed": self.speed if self.status in ("downloading", "starting", "seeding") else 0,
            "eta": eta,
            "nets": dict(self._net_bytes),
            "down": [name for name, when in self._net_last.items()
                     if time.monotonic() - when <= 2.5],
            "path": self.path,
            "dir": self.dir,
            "category": self.category or "torrents",
            "added": self.added,
            "finished": self.finished,
            "connections": self.connections,
            "seeds": self.seeds,
            "hidden": False,
        }
