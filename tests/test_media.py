"""MediaJob: per-network URL resolution, part handling and terminal states."""
from __future__ import annotations

import os
import time

import pytest

from qorvex import media
from tests import util


def fake_extract(port, parts=2):
    def extract(page_url, net, quality, extra):
        urls = []
        if parts >= 1:
            urls.append({"id": "v", "url": f"http://127.0.0.1:{port}/locked?ip={net.ip}", "ext": "mp4", "headers": {}})
        if parts >= 2:
            urls.append({"id": "a", "url": f"http://127.0.0.1:{port}/locked?ip={net.ip}", "ext": "m4a", "headers": {}})
        return {"title": "My: Video?", "parts": urls}

    return extract


def wait_media(job, statuses, timeout=60):
    deadline = time.time() + timeout
    while job.status not in statuses and time.time() < deadline:
        time.sleep(0.05)
    return job.status


@pytest.fixture(autouse=True)
def no_ffmpeg(monkeypatch):
    """Keep the suite hermetic: CI runs tests before the ffmpeg bundle step."""
    monkeypatch.setattr(media, "_ffmpeg_path", lambda: None)


def test_two_parts_use_each_networks_own_url(manager, httpd, monkeypatch):
    monkeypatch.setattr(media, "_extract", fake_extract(httpd, parts=2))
    job = manager.add_media("https://example.test/watch?v=1", "720")
    assert wait_media(job, {"done", "error"}) == "done", job.error
    assert "ffmpeg not found" in job.error
    assert util.STATE.seen == {"127.0.0.1", "127.0.0.2"}
    files = sorted(name for name in os.listdir(job.dir) if name.startswith("My_ Video_"))
    assert files == ["My_ Video_.part0.mp4", "My_ Video_.part1.m4a"]
    for name in files:
        assert util.digest(os.path.join(job.dir, name)) == util.SHA
    snapshot = job.snapshot()
    assert snapshot["kind"] == "media" and snapshot["path"].endswith("part0.mp4")
    # Hidden parts never show up as separate rows.
    assert [d["id"] for d in manager.state()["downloads"]] == [job.id]


def test_single_part(manager, httpd, monkeypatch):
    monkeypatch.setattr(media, "_extract", fake_extract(httpd, parts=1))
    job = manager.add_media("https://example.test/watch?v=1", "best")
    assert wait_media(job, {"done", "error"}) == "done", job.error
    assert os.path.exists(os.path.join(job.dir, "My_ Video_.mp4"))
    assert job.snapshot()["path"].endswith("My_ Video_.mp4")


def test_part_error_ends_the_job(manager, httpd, monkeypatch):
    def failing(page_url, net, quality, extra):
        return {"title": "Broken", "parts": [
            {"id": "v", "url": f"http://127.0.0.1:{httpd}/404?ip={net.ip}", "ext": "mp4", "headers": {}}]}

    monkeypatch.setattr(media, "_extract", failing)
    job = manager.add_media("https://example.test/broken", "best")
    assert wait_media(job, {"error"}, timeout=30) == "error"
    assert not job.parts or all(part.id not in manager.downloads for part in job.parts)


def test_cancel_while_resolving(manager, httpd, monkeypatch):
    def slow(page_url, net, quality, extra):
        time.sleep(0.4)
        return fake_extract(httpd, parts=1)(page_url, net, quality, extra)

    monkeypatch.setattr(media, "_extract", slow)
    job = manager.add_media("https://example.test/watch?v=1", "best")
    assert job.act("cancel")
    assert wait_media(job, {"cancelled"}, timeout=30) == "cancelled"


def test_unsupported_streaming_formats(manager, httpd, monkeypatch):
    class FakeYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=False):
            return {"title": "HLS", "requested_formats": [
                {"format_id": "hls", "url": "https://example.test/x.m3u8", "protocol": "m3u8_native",
                 "ext": "mp4", "http_headers": {}}]}

    import sys
    import types

    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)
    job = manager.add_media("https://example.test/hls", "best")
    assert wait_media(job, {"error"}, timeout=30) == "error"
    assert "streaming format" in job.error
