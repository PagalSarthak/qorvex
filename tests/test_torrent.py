"""Torrent support tests. libtorrent itself is stubbed, so these run anywhere."""
from __future__ import annotations

import time
import types

import pytest

from qorvex.torrent import is_torrent_source


# ───────────────────────── fake libtorrent ─────────────────────────
class FakeFileStorage:
    def __init__(self, paths):
        self._paths = paths

    def num_files(self):
        return len(self._paths)

    def file_path(self, index):
        return self._paths[index]


class FakeTorrentInfo:
    def __init__(self, source=None, paths=None):
        if isinstance(source, (list, tuple)):  # tests pass a paths tuple directly
            self._paths = list(source)
            return
        self._paths = list(paths or ("movie.mkv",))
        if isinstance(source, str):
            with open(source, "rb") as handle:
                data = handle.read()
        elif isinstance(source, (bytes, bytearray)):
            data = bytes(source)
        else:
            return  # no file content to validate
        _bdecode_validate(data)  # real libtorrent rejects non-bencoded .torrent files

    def files(self):
        return FakeFileStorage(self._paths)


def _bdecode_validate(data):
    """Minimal bencode structural check: raises ValueError on garbage."""
    def decode(index):
        if index >= len(data):
            raise ValueError("truncated bencode")
        head = data[index:index + 1]
        if head == b"i":
            end = data.index(b"e", index)
            int(data[index + 1:end])
            return end + 1
        if head in b"0123456789":
            colon = data.index(b":", index)
            length = int(data[index:colon])
            start = colon + 1
            if start + length > len(data):
                raise ValueError("short string")
            return start + length
        if head == b"l":
            index += 1
            while data[index:index + 1] != b"e":
                index = decode(index)
            return index + 1
        if head == b"d":
            index += 1
            while data[index:index + 1] != b"e":
                index = decode(index)  # key
                index = decode(index)  # value
            return index + 1
        raise ValueError("not bencoded")

    decode(0)


class FakeErrc:
    def value(self):
        return 0

    def message(self):
        return ""


class FakeStatus:
    def __init__(self, state=2, total=0, done=0, dl=0.0, ul=0.0, seeds=0, peers=0,
                 name="", torrent_file=None, uploaded=0):
        self.state = state
        self.total_wanted = total
        self.total_wanted_done = done
        self.download_payload_rate = dl
        self.upload_payload_rate = ul
        self.num_seeds = seeds
        self.num_peers = peers
        self.name = name
        self.torrent_file = torrent_file
        self.all_time_upload = uploaded
        self.errc = FakeErrc()


class FakePeerInfo:
    def __init__(self, remote, local, total_dl=0, total_ul=0, payload=0):
        self.ip = remote
        self.local_endpoint = local
        self.total_download = total_dl
        self.total_upload = total_ul
        self.payload_down_speed = payload


class FakeHandle:
    def __init__(self, atp, status):
        self.atp = atp
        self._status = status
        self.paused = False
        self.resumed = 0
        self.peers = []

    def status(self):
        return self._status

    def get_peer_info(self):
        return list(self.peers)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False
        self.resumed += 1

    def torrent_file(self):
        return self._status.torrent_file


class FakeSession:
    delete_files = 1
    instances: list["FakeSession"] = []

    def __init__(self, settings):
        self.settings = dict(settings or {})
        self.packs: list[dict] = []
        self.handles: list[FakeHandle] = []
        self.removed = []
        self.paused = False
        FakeSession.instances.append(self)

    def apply_settings(self, pack):
        self.packs.append(dict(pack))

    def add_torrent(self, atp):
        handle = FakeHandle(atp, atp.test_status)
        self.handles.append(handle)
        return handle

    def remove_torrent(self, handle, delete_files=False):
        self.removed.append((handle, bool(delete_files)))

    def pause(self):
        self.paused = True


class FakeATP:
    def __init__(self):
        self.ti = None
        self.save_path = ""
        self.test_status = FakeStatus(state=2, total=1000, done=100, dl=2048.0)


def fake_lt(monkeypatch):
    lt = types.SimpleNamespace()
    lt.settings_pack = FakeATP  # unused on the dict API; kept for older bindings
    lt.session = FakeSession
    lt.add_torrent_params = FakeATP
    lt.torrent_info = FakeTorrentInfo
    lt.torrent_status = types.SimpleNamespace(
        checking_files=0, downloading_metadata=1, downloading=2, finished=3, seeding=4,
    )

    def parse_magnet_uri(uri):
        # resolved at call time so tests can swap in a populated params class
        atp = lt.add_torrent_params()
        atp.test_status.name = "Ubuntu"
        return atp

    lt.parse_magnet_uri = parse_magnet_uri
    FakeSession.instances = []
    monkeypatch.setitem(__import__("sys").modules, "libtorrent", lt)
    return lt


# ───────────────────────── helpers ─────────────────────────
def wait_for(job, status, timeout=5.0):
    deadline = time.time() + timeout
    while job.status != status and time.time() < deadline:
        time.sleep(0.02)
    return job.status


def cancel_quietly(mgr, job):
    try:
        mgr.action(job.id, "cancel")
    except Exception:  # noqa: BLE001
        pass


# ───────────────────────── tests ─────────────────────────
def test_source_detection():
    assert is_torrent_source("magnet:?xt=urn:btih:abc")
    assert is_torrent_source("MAGNET:?xt=urn:btih:abc")
    assert is_torrent_source("https://example.org/ubuntu.iso.torrent?token=1")
    assert is_torrent_source("C:/downloads/something.torrent")
    assert not is_torrent_source("https://example.org/file.iso")
    assert not is_torrent_source("")


def test_missing_libtorrent_gives_clear_error(manager, monkeypatch):
    import qorvex.torrent as torrent

    monkeypatch.setattr(torrent, "_lt", lambda: None)
    with pytest.raises(ValueError, match="libtorrent"):
        manager.add_torrent("magnet:?xt=urn:btih:abc")


def test_disabled_in_settings(manager):
    manager.settings.update({"torrent_enabled": False})
    with pytest.raises(ValueError, match="disabled"):
        manager.add_torrent("magnet:?xt=urn:btih:abc")


def test_magnet_adds_and_polls(manager, monkeypatch):
    fake_lt(monkeypatch)
    job = manager.add_torrent("magnet:?xt=urn:btih:abc&dn=Ubuntu")
    try:
        assert job.id in manager.downloads
        assert wait_for(job, "downloading") == "downloading"
        snap = job.snapshot()
        assert snap["kind"] == "torrent"
        assert snap["name"] == "Ubuntu"
        assert snap["size"] == 1000
        assert snap["done"] == 100
        assert snap["speed"] > 0
    finally:
        cancel_quietly(manager, job)
        wait_for(job, "cancelled")


def test_seed_ratio_zero_completes_and_detaches(manager, monkeypatch, tmp_path):
    lt = fake_lt(monkeypatch)
    status = FakeStatus(state=3, total=1000, done=1000, ul=512.0,
                        torrent_file=FakeTorrentInfo(("movie.mkv",)))

    class PopulatedATP(FakeATP):
        def __init__(self):
            super().__init__()
            self.test_status = status

    lt.add_torrent_params = PopulatedATP
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    try:
        assert wait_for(job, "done") == "done"
        assert job.done == 1000
        assert job.path.endswith("movie.mkv")
        session = FakeSession.instances[-1]
        assert session.removed and session.removed[0][1] is False  # files kept
    finally:
        if job.status != "done":
            cancel_quietly(manager, job)


def test_stop_seeding_keeps_files(manager, monkeypatch):
    lt = fake_lt(monkeypatch)
    manager.settings.update({"torrent_seed_ratio": 5.0})
    status = FakeStatus(state=4, total=1000, done=1000,
                        torrent_file=FakeTorrentInfo(("movie.mkv",)))

    class PopulatedATP(FakeATP):
        def __init__(self):
            super().__init__()
            self.test_status = status

    lt.add_torrent_params = PopulatedATP
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    assert wait_for(job, "seeding") == "seeding"
    assert manager.action(job.id, "pause")
    assert wait_for(job, "done") == "done"
    assert job.done == 1000
    session = FakeSession.instances[-1]
    assert session.removed and session.removed[0][1] is False  # files kept


def test_cancel_deletes_files(manager, monkeypatch):
    lt = fake_lt(monkeypatch)
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    wait_for(job, "downloading")
    assert manager.action(job.id, "cancel")
    assert wait_for(job, "cancelled") == "cancelled"
    session = FakeSession.instances[-1]
    assert session.removed and session.removed[0][1] is True


def test_outgoing_interfaces_follow_enabled_networks(manager, monkeypatch):
    fake_lt(monkeypatch)
    manager.update_torrent_interfaces()  # engine not created yet: no-op, no error
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    try:
        wait_for(job, "downloading")
        session = FakeSession.instances[-1]
        packs = session.packs
        assert any(
            p.get("outgoing_interfaces") == "127.0.0.1,127.0.0.2" for p in packs
        )
        manager.action(job.id, "pause")  # disable netB? simpler: re-check after toggle
        manager.set_network("netB", enabled=False)
        manager.update_torrent_interfaces()
        packs = session.packs
        assert any(
            p.get("outgoing_interfaces") == "127.0.0.1" for p in packs
        )
    finally:
        cancel_quietly(manager, job)


def test_global_limit_reaches_session(manager, monkeypatch):
    fake_lt(monkeypatch)
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    try:
        wait_for(job, "downloading")
        manager.set_global_limit(2)  # 2 MB/s
        session = FakeSession.instances[-1]
        limits = session.packs
        assert any(
            p.get("download_rate_limit") == 2 * (1 << 20) and
            p.get("upload_rate_limit") == 2 * (1 << 20)
            for p in limits
        )
    finally:
        cancel_quietly(manager, job)


def test_pause_resume(manager, monkeypatch):
    fake_lt(monkeypatch)
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    try:
        assert wait_for(job, "downloading") == "downloading"
        assert manager.action(job.id, "pause")
        assert wait_for(job, "paused") == "paused"
        handle = FakeSession.instances[-1].handles[-1]
        assert handle.paused
        assert manager.action(job.id, "resume")
        handle = FakeSession.instances[-1].handles[-1]
        assert not handle.paused and handle.resumed == 1
    finally:
        cancel_quietly(manager, job)


def test_remove_and_delete(manager, monkeypatch, tmp_path):
    lt = fake_lt(monkeypatch)
    status = FakeStatus(state=3, total=1000, done=1000,
                        torrent_file=FakeTorrentInfo(("movie.mkv",)))

    class PopulatedATP(FakeATP):
        def __init__(self):
            super().__init__()
            self.test_status = status

    lt.add_torrent_params = PopulatedATP
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    wait_for(job, "done")
    assert manager.action(job.id, "remove")
    assert job.id not in manager.downloads


def test_multi_file_path_is_folder(manager, monkeypatch):
    lt = fake_lt(monkeypatch)
    status = FakeStatus(state=3, total=1000, done=1000,
                        torrent_file=FakeTorrentInfo(("a.mkv", "b.mkv")))

    class PopulatedATP(FakeATP):
        def __init__(self):
            super().__init__()
            self.test_status = status

    lt.add_torrent_params = PopulatedATP
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    try:
        assert wait_for(job, "done") == "done"
        assert job.path == job.dir  # multi-file: open the folder
    finally:
        if job.status != "done":
            cancel_quietly(manager, job)


# ───────────────────────── /api/torrents/add (drag & drop) ─────────────────
import base64 as _b64  # noqa: E402
import os as _os  # noqa: E402
import threading as _threading  # noqa: E402
import urllib.error as _uerror  # noqa: E402
import urllib.request as _urequest  # noqa: E402

import pytest as _pytest  # noqa: E402

from qorvex.app import App as _App  # noqa: E402
from qorvex.config import Settings as _Settings  # noqa: E402
from qorvex.engine import Network as _Network  # noqa: E402
from qorvex.server import serve as _serve


def _bencode(obj) -> bytes:
    if isinstance(obj, bool):
        raise TypeError("bencode has no bool")
    if isinstance(obj, int):
        return f"i{obj}e".encode()
    if isinstance(obj, bytes):
        return str(len(obj)).encode() + b":" + obj
    if isinstance(obj, str):
        return _bencode(obj.encode())
    if isinstance(obj, list):
        return b"l" + b"".join(_bencode(x) for x in obj) + b"e"
    if isinstance(obj, dict):
        return b"d" + b"".join(
            _bencode(k.encode()) + _bencode(v) for k, v in sorted(obj.items())
        ) + b"e"
    raise TypeError(obj)


def _torrent_bytes(name="a.bin", length=5):
    return _bencode({
        "announce": "http://tracker/announce",
        "info": {
            "name": name,
            "length": length,
            "piece length": 16384,
            "pieces": b"\x00" * 20,
        },
    })


@_pytest.fixture
def http_app(tmp_path):
    settings = _Settings(str(tmp_path / "config.json"))
    settings.update({
        "download_dir": str(tmp_path / "downloads"),
        "clipboard_watch": False,
        "open_ui_on_start": False,
        "categorize": False,
    })
    application = _App(settings=settings, networks=[_Network("netA", "127.0.0.1")])
    httpd = _serve(application, 0)
    thread = _threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield application, httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        application.close()


def _post(port, path, body):
    import json

    data = _urequest.Request(
        f"http://127.0.0.1:{port}{path}",
        json.dumps(body).encode(),
        {"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urequest.urlopen(data, timeout=10) as response:
            return response.status, response.read()
    except _uerror.HTTPError as exc:
        return exc.code, exc.read()


def test_upload_torrent_file(http_app, monkeypatch):
    fake_lt(monkeypatch)
    app, port = http_app
    code, raw = _post(port, "/api/torrents/add", {
        "name": "a.bin.torrent",
        "data": _b64.b64encode(_torrent_bytes()).decode(),
    })
    assert code == 200
    result = __import__("json").loads(raw)
    assert result["id"] in app.mgr.downloads
    job = app.mgr.downloads[result["id"]]
    try:
        assert wait_for(job, "downloading") == "downloading"
        # the server-side temp copy is deleted once libtorrent has read it
        assert not _os.path.exists(job.source)
    finally:
        cancel_quietly(app.mgr, job)


def test_upload_rejects_bad_base64(http_app):
    app, port = http_app
    code, raw = _post(port, "/api/torrents/add", {"data": "not base64!!"})
    assert code == 400
    assert b"base64" in raw


def test_upload_rejects_empty(http_app):
    app, port = http_app
    code, raw = _post(port, "/api/torrents/add", {"name": "x.torrent"})
    assert code == 400


def test_upload_missing_libtorrent(http_app, monkeypatch):
    import tempfile as _tempfile
    import qorvex.torrent as torrent

    monkeypatch.setattr(torrent, "_lt", lambda: None)
    app, port = http_app
    tmp = _tempfile.gettempdir()
    before = set(f for f in _os.listdir(tmp) if f.startswith("qorvex-"))
    code, raw = _post(port, "/api/torrents/add", {
        "name": "x.torrent",
        "data": _b64.b64encode(_torrent_bytes()).decode(),
    })
    assert code == 400
    assert b"libtorrent" in raw
    after = set(f for f in _os.listdir(tmp) if f.startswith("qorvex-"))
    assert before == after  # rejected upload leaves no temp file behind


def test_upload_corrupt_torrent_fails_cleanly(http_app, monkeypatch):
    fake_lt(monkeypatch)
    import tempfile as _tempfile

    app, port = http_app
    code, raw = _post(port, "/api/torrents/add", {
        "name": "broken.torrent",
        "data": _b64.b64encode(b"this is not a bencoded torrent").decode(),
    })
    assert code == 200
    result = __import__("json").loads(raw)
    job = app.mgr.downloads[result["id"]]
    try:
        deadline = __import__("time").time() + 5
        while job.status not in ("error", "cancelled") and __import__("time").time() < deadline:
            __import__("time").sleep(0.05)
        assert job.status == "error"
        assert not _os.path.exists(job.source)  # temp copy cleaned up
    finally:
        cancel_quietly(app.mgr, job)


# ───────────────────────── per-network accounting ─────────────────────────
def test_peer_bytes_attributed_to_networks(manager, monkeypatch):
    fake_lt(monkeypatch)
    netA = manager.networks[0]
    netB = manager.networks[1]
    before_a, before_b = netA.total, netB.total
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    try:
        assert wait_for(job, "downloading") == "downloading"
        handle = FakeSession.instances[-1].handles[-1]
        peerA = FakePeerInfo(("1.1.1.1", 5000), ("127.0.0.1", 40000), total_dl=1000, payload=800)
        peerB = FakePeerInfo(("2.2.2.2", 5001), ("127.0.0.2", 40001), total_dl=2000, payload=400)
        unknown = FakePeerInfo(("3.3.3.3", 5002), ("9.9.9.9", 40002), total_dl=5000, payload=999)
        handle.peers = [peerA, peerB, unknown]
        time.sleep(0.6)  # one poll: baselines recorded, delta 0
        peerA.total_download += 1500
        peerB.total_download += 2500
        deadline = time.time() + 3
        while time.time() < deadline:
            time.sleep(0.05)
            snap = job.snapshot()
            if snap["nets"].get("netA") and snap["nets"].get("netB"):
                break
        snap = job.snapshot()
        assert snap["nets"]["netA"] >= 1500
        assert snap["nets"]["netB"] >= 2500
        assert "9.9.9.9" not in snap["nets"]  # unknown local IP is not attributed
        assert set(snap["down"]) == {"netA", "netB"}  # traffic within the last 2.5s
        assert netA.total > before_a  # network cards now see torrent bytes
        assert netB.total > before_b
    finally:
        cancel_quietly(manager, job)


def test_departed_peers_do_not_leak_or_regress(manager, monkeypatch):
    fake_lt(monkeypatch)
    job = manager.add_torrent("magnet:?xt=urn:btih:abc")
    try:
        wait_for(job, "downloading")
        handle = FakeSession.instances[-1].handles[-1]
        peer = FakePeerInfo(("1.1.1.1", 5000), ("127.0.0.1", 40000), total_dl=0)
        handle.peers = [peer]
        time.sleep(0.6)
        peer.total_download = 9999  # bytes arrived
        time.sleep(0.6)
        assert job.snapshot()["nets"]["netA"] >= 9999
        handle.peers = []  # everyone left
        time.sleep(3.0)    # longer than the 2.5s activity window
        snap = job.snapshot()
        assert snap["nets"]["netA"] >= 9999  # totals never go backwards
        assert snap["down"] == []            # nothing is moving any more
        assert len(job._peer_seen) <= 2 * 16  # pruned
    finally:
        cancel_quietly(manager, job)
