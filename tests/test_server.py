"""HTTP surface: origin/host guards, token, JSON-only POSTs and routes."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from qorvex.app import App
from qorvex.config import Settings
from qorvex.engine import Network
from qorvex.server import serve


@pytest.fixture
def app(tmp_path):
    settings = Settings(str(tmp_path / "config.json"))
    settings.update(
        {
            "download_dir": str(tmp_path / "downloads"),
            "clipboard_watch": False,
            "open_ui_on_start": False,
            "categorize": False,
        }
    )
    application = App(settings=settings, networks=[Network("netA", "127.0.0.1")])
    yield application
    application.close()


@pytest.fixture
def server(app):
    httpd = serve(app, 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


def call(port, path, body=None, *, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data,
        {"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def payload(result):
    status, body = result
    return status, json.loads(body or b"{}")


def test_ui_and_state(server, app):
    status, body = call(server, "/")
    assert status == 200 and b"Qorvex" in body

    status, data = payload(call(server, "/api/state"))
    assert status == 200
    assert data["version"]
    assert "api_token" not in data["settings"]
    assert data["networks"][0]["name"] == "netA"


def test_foreign_origin_and_host_are_rejected(server):
    assert call(server, "/api/state", headers={"Origin": "https://evil.example"})[0] == 403
    assert call(server, "/api/state", headers={"Host": "evil.example"})[0] == 403
    assert call(server, "/api/add", {"url": "https://x.test/a"}, headers={"Origin": "https://evil.example"})[0] == 403


def test_extension_token(server, app):
    assert call(server, "/api/state", headers={"Origin": "chrome-extension://abc"})[0] == 200
    app.settings.update({"api_token": "tok123"})
    assert call(server, "/api/state", headers={"Origin": "chrome-extension://abc"})[0] == 403
    assert call(server, "/api/state", headers={"Origin": "chrome-extension://abc",
                                               "X-Qorvex-Token": "tok123"})[0] == 200
    assert call(server, "/api/state?token=tok123", headers={"Origin": "moz-extension://abc"})[0] == 200
    # The local UI keeps working without the token.
    assert call(server, "/api/state")[0] == 200
    app.settings.update({"api_token": ""})


def test_post_requires_json(server):
    request = urllib.request.Request(
        f"http://127.0.0.1:{server}/api/add",
        b"url=https://x.test/a",
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("should have been rejected")
    except urllib.error.HTTPError as exc:
        assert exc.code == 415


def test_add_rejects_bad_scheme(server):
    assert payload(call(server, "/api/add", {"url": "file:///etc/passwd"}))[0] == 400
    assert payload(call(server, "/api/add", {"urls": ["javascript:alert(1)"]}))[0] == 400


def test_settings_roundtrip(server, app):
    status, data = payload(call(server, "/api/settings", {"max_active": 6, "theme": "dark", "bogus": 1}))
    assert status == 200
    assert data["changed"] == ["max_active", "theme"]
    assert "bogus" in data["errors"]
    assert app.settings.get("max_active") == 6
    status, data = payload(call(server, "/api/settings"))
    assert status == 200 and data["values"]["theme"] == "dark"
    assert any(item["key"] == "autostart" for item in data["spec"])


def test_networks_add_and_remove(server, app):
    status, data = payload(call(server, "/api/networks/add", {"spec": "lab=127.0.0.5"}))
    assert status == 200
    assert any(net["name"] == "lab" for net in data["networks"])
    assert payload(call(server, "/api/network", {"name": "lab", "enabled": False}))[1]["ok"]
    assert payload(call(server, "/api/networks/remove", {"name": "lab"}))[1]["ok"]
    assert not any(net.name == "lab" for net in app.mgr.networks)


def test_open_is_limited_to_owned_paths(server, app, tmp_path, monkeypatch):
    import qorvex.server as server_module

    opened: list[str] = []
    monkeypatch.setattr(server_module, "open_path", lambda path: opened.append(path) or True)
    outside = "C:\\Windows\\win.ini"
    assert payload(call(server, "/api/open", {"path": outside}))[1]["ok"] is False
    inside = tmp_path / "downloads"
    inside.mkdir(parents=True, exist_ok=True)
    status, data = payload(call(server, "/api/open", {"path": str(inside)}))
    assert status == 200 and data["ok"] is True
    assert opened and opened[0].endswith("downloads")


def test_clipboard_endpoints(server, app):
    app._on_clipboard("https://example.com/file.zip")
    status, data = payload(call(server, "/api/clipboard", {"action": "take"}))
    assert status == 200 and data["urls"][0]["url"] == "https://example.com/file.zip"
    assert payload(call(server, "/api/clipboard", {"action": "take"}))[1]["urls"] == []
    assert payload(call(server, "/api/clipboard", {"action": "nope"}))[0] == 400


def test_quit_sets_the_flag(server, app):
    assert payload(call(server, "/api/quit", {}))[1]["ok"]
    assert app.quitting


def test_frozen_extension_is_copied_to_the_state_folder(tmp_path, monkeypatch):
    import sys

    import qorvex.server as server_module

    bundled = tmp_path / "bundle" / "extension"
    (bundled / "chrome").mkdir(parents=True)
    (bundled / "chrome" / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setenv("QORVEX_HOME", str(tmp_path / "home"))

    target = server_module.extension_dir()
    assert target == str(tmp_path / "home" / "extension")
    assert (tmp_path / "home" / "extension" / "chrome" / "manifest.json").exists()
    assert server_module.extension_dir() == target  # copied once per version


def test_extension_announce_detected(app, server):
    """The extension's marker header marks it as installed; the hint can hide."""
    before = app.state()["extension_last_seen"]
    status, _ = call(server, "/api/state", method="GET", headers={"X-Qorvex-Extension": "1"})
    assert status == 200
    assert app.state()["extension_last_seen"] not in (None, before)


def test_extension_launch_unknown_browser(app, server):
    """Launch with a browser that isn't installed fails cleanly."""
    status, _ = call(server, "/api/extension/launch", {"browser": "netscape"})
    assert status == 400


def test_extension_info_lists_browsers(app, server):
    status, body = call(server, "/api/extension", method="GET")
    assert status == 200
    assert "browsers" in json.loads(body)
