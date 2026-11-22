"""Engine behaviour: bonding, resume, failure recovery, range validation."""
from __future__ import annotations

import json
import os
import time

import pytest

from qorvex.engine import _safe
from tests import util


def add(mgr, port, path="/f", **kwargs):
    return mgr.add(f"http://127.0.0.1:{port}{path}", **kwargs)


def test_bonded_download_uses_both_networks(manager, httpd):
    download = add(manager, httpd)
    assert util.wait_for(download, {"done", "error"}) == "done", download.error
    assert util.digest(download.path) == util.SHA
    assert util.STATE.seen == {"127.0.0.1", "127.0.0.2"}
    assert all(state["bytes"] > 0 for state in download.ns.values())
    assert not os.path.exists(download.path + ".qorvex")


def test_single_network(manager, httpd):
    manager.networks[1].enabled = False
    download = add(manager, httpd)
    assert util.wait_for(download, {"done", "error"}) == "done", download.error
    assert util.digest(download.path) == util.SHA
    assert util.STATE.seen == {"127.0.0.1"}


def test_disable_all_networks_pauses_instead_of_hanging(manager, httpd):
    download = add(manager, httpd)
    deadline = time.time() + 20
    while download.done < 1_000_000 and time.time() < deadline:
        time.sleep(0.05)
    for network in manager.networks:
        network.enabled = False
    assert util.wait_for(download, {"paused", "error", "done"}, timeout=20) == "paused"
    assert "disabled" in download.error
    assert os.path.exists(download.path + ".qorvex")

    for network in manager.networks:
        network.enabled = True
    assert manager.action(download.id, "resume")
    assert util.wait_for(download, {"done", "error"}) == "done", download.error
    assert util.digest(download.path) == util.SHA


def test_network_disabled_mid_download(manager, httpd):
    download = add(manager, httpd)

    def hook():
        if download.done > 2_000_000 and manager.networks[1].enabled:
            manager.networks[1].enabled = False

    deadline = time.time() + 60
    while download.status not in ("done", "error") and time.time() < deadline:
        hook()
        time.sleep(0.05)
    assert download.status == "done", download.error
    assert util.digest(download.path) == util.SHA


def test_pause_and_resume(manager, httpd):
    download = add(manager, httpd)
    deadline = time.time() + 30
    while download.done < 2_000_000 and time.time() < deadline:
        time.sleep(0.05)
    assert manager.action(download.id, "pause")
    assert util.wait_for(download, {"paused"}, timeout=30) == "paused"
    partial = download.done
    assert os.path.exists(download.path + ".qorvex")
    assert manager.action(download.id, "resume")
    assert util.wait_for(download, {"done", "error"}) == "done", download.error
    assert util.digest(download.path) == util.SHA
    assert download.done == download.size
    assert partial > 0


def test_resume_rejects_a_changed_url(manager, httpd):
    download = add(manager, httpd)
    deadline = time.time() + 30
    while download.done < 2_000_000 and time.time() < deadline:
        time.sleep(0.05)
    manager.action(download.id, "pause")
    util.wait_for(download, {"paused"}, timeout=30)
    sidecar = download.path + ".qorvex"
    with open(sidecar, encoding="utf-8") as handle:
        state = json.load(handle)
    state["url"] = "http://example.invalid/other"
    with open(sidecar, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    manager.action(download.id, "resume")
    assert util.wait_for(download, {"done", "error"}) == "done", download.error
    assert util.digest(download.path) == util.SHA  # restarted instead of mixing files


def test_wrong_content_range_is_rejected(manager, httpd):
    manager.settings.update({"retry_limit": 1, "network_fail_limit": 2, "streams_per_network": 1})
    util.STATE.lie = True
    download = add(manager, httpd, "/lie")
    assert util.wait_for(download, {"error"}, timeout=30) == "error"
    assert "wrong byte range" in download.error

    util.STATE.lie = False
    assert manager.action(download.id, "retry")
    assert util.wait_for(download, {"done", "error"}) == "done", download.error
    assert util.digest(download.path) == util.SHA


def test_retry_after_all_networks_failed(manager, httpd):
    manager.settings.update({"retry_limit": 1, "network_fail_limit": 1, "streams_per_network": 1})
    util.STATE.fail_until = time.time() + 3
    download = add(manager, httpd, "/failranges")
    assert util.wait_for(download, {"error"}, timeout=30) == "error"
    assert "network" in download.error.lower()
    assert all(state["down"] for state in download.ns.values())

    time.sleep(max(0, util.STATE.fail_until - time.time()) + 0.2)
    assert manager.action(download.id, "retry")
    assert util.wait_for(download, {"done", "error"}) == "done", download.error
    assert util.digest(download.path) == util.SHA


def test_rate_limited_server_gets_fewer_connections(manager, httpd):
    manager.settings.update({"retry_limit": 2, "streams_per_network": 4})
    util.STATE.limited = 8
    download = add(manager, httpd, "/limited")
    assert util.wait_for(download, {"done", "error"}, timeout=60) == "done", download.error
    assert util.digest(download.path) == util.SHA
    assert download.stream_penalty >= 1  # backed off instead of failing the job


def test_no_range_support(manager, httpd):
    download = add(manager, httpd, "/norange")
    assert util.wait_for(download, {"done", "error"}) == "done", download.error
    assert util.digest(download.path) == util.SHA


def test_filename_collision_gets_a_unique_name(manager, httpd):
    first = add(manager, httpd, "/f?a=1", name="dup.bin")
    second = add(manager, httpd, "/f?a=2", name="dup.bin")
    assert util.wait_for(first, {"done", "error"}) == "done", first.error
    assert util.wait_for(second, {"done", "error"}) == "done", second.error
    assert first.path != second.path
    assert util.digest(first.path) == util.SHA
    assert util.digest(second.path) == util.SHA


def test_cancel_discards_the_file(manager, httpd):
    download = add(manager, httpd)
    deadline = time.time() + 20
    while download.done < 500_000 and time.time() < deadline:
        time.sleep(0.05)
    manager.action(download.id, "cancel")
    assert util.wait_for(download, {"cancelled"}, timeout=20) == "cancelled"
    assert not os.path.exists(download.path)


def test_queue_survives_a_restart(tmp_path, httpd):
    settings = util.make_settings(tmp_path)
    first = util.make_manager(tmp_path, settings)
    download = first.add(f"http://127.0.0.1:{httpd}/f")
    deadline = time.time() + 30
    while download.done < 2_000_000 and time.time() < deadline:
        time.sleep(0.05)
    first.action(download.id, "pause")
    util.wait_for(download, {"paused"}, timeout=30)
    first.close()

    assert (tmp_path / "queue.json").exists()
    second = util.make_manager(tmp_path, settings)
    try:
        restored = [d for d in second.downloads.values() if d.url == download.url]
        assert len(restored) == 1
        assert restored[0].path == download.path  # reuse the partial file, no "(1)" copy
        assert util.wait_for(restored[0], {"done", "error"}) == "done", restored[0].error
        assert util.digest(restored[0].path) == util.SHA
        siblings = [p.name for p in (tmp_path / "downloads").iterdir() if p.is_file()]
        assert not any("(1)" in name for name in siblings), siblings
    finally:
        second.close()


def test_queue_can_wait_for_a_manual_resume(tmp_path, httpd):
    settings = util.make_settings(tmp_path, resume_on_start=False)
    first = util.make_manager(tmp_path, settings)
    first.max_active = 0
    download = first.add(f"http://127.0.0.1:{httpd}/f")
    first.close()

    second = util.make_manager(tmp_path, settings)
    try:
        restored = [d for d in second.downloads.values() if d.url == download.url][0]
        time.sleep(0.6)
        assert restored.status == "paused"
        assert "restored" in restored.error
        second.action(restored.id, "resume")
        assert util.wait_for(restored, {"done", "error"}) == "done", restored.error
        assert util.digest(restored.path) == util.SHA
    finally:
        second.close()


def test_queue_never_writes_cookies(tmp_path, httpd):
    manager = util.make_manager(tmp_path)
    manager.max_active = 0
    manager.add(f"http://127.0.0.1:{httpd}/f", {"Cookie": "session=very-secret", "Referer": "https://example.test/"})
    manager._persist_queue()
    document = (tmp_path / "queue.json").read_text(encoding="utf-8")
    assert "very-secret" not in document
    assert "example.test" in document


def test_adding_the_same_url_twice_reuses_the_job(manager, httpd):
    manager.max_active = 0
    url = f"http://127.0.0.1:{httpd}/f"
    first = manager.add(url)
    second = manager.add(url)
    assert first is second


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CON.txt", "_CON.txt"),
        ("nul", "_nul"),
        ("a:b?.txt", "a_b_.txt"),
        ("", "download"),
        ("  ..  ", "download"),
    ],
)
def test_safe_filenames(raw, expected):
    assert _safe(raw) == expected
