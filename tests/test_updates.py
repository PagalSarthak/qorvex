"""Update checker: version comparison and watcher lifecycle (all offline)."""
from __future__ import annotations

from unittest import mock

import qorvex.updates as updates


def test_version_comparison():
    assert updates.newer("v1.0.2", "1.0.1")
    assert updates.newer("1.10.0", "1.9.9")   # numeric, not lexicographic
    assert not updates.newer("1.0.1", "1.0.1")
    assert not updates.newer("v1.0.0", "1.0.1")
    assert not updates.newer("garbage", "1.0.1")


def test_latest_release_parses_tag():
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"tag_name": "v9.9.9", "html_url": "https://x"}'

    with mock.patch.object(updates.urllib.request, "urlopen", return_value=R()):
        assert updates.latest_release() == ("v9.9.9", updates._RELEASE_PAGE)


def test_latest_release_survives_errors():
    with mock.patch.object(
        updates.urllib.request, "urlopen", side_effect=OSError("offline")
    ):
        assert updates.latest_release() is None


def test_watcher_reports_newer_version():
    app = type("AppStub", (), {})()
    seen = []
    watcher = updates.UpdateWatcher(app, on_found=lambda v, u: seen.append((v, u)))
    with mock.patch.object(updates, "latest_release", return_value=("v1.0.2", "u")):
        watcher._check_once()
    assert seen == [("v1.0.2", "u")]
    assert watcher.state() == {"version": "v1.0.2", "url": "u", "current": updates.qorvex.__version__}

    # a second pass with the same result must not re-notify
    with mock.patch.object(updates, "latest_release", return_value=("v1.0.2", "u")):
        watcher._check_once()
    assert seen == [("v1.0.2", "u")]


def test_watcher_silent_when_no_update():
    app = type("AppStub", (), {})()
    seen = []
    watcher = updates.UpdateWatcher(app, on_found=lambda v, u: seen.append((v, u)))
    with mock.patch.object(updates, "latest_release", return_value=("v1.0.1", "u")):
        watcher._check_once()
    assert seen == [] and watcher.state() is None
