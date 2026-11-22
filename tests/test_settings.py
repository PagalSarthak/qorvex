"""Settings validation, persistence and related small modules."""
from __future__ import annotations

import json
import os

import pytest

from qorvex import autostart, clipboard
from qorvex.app import App
from qorvex.config import DEFAULTS, SETTINGS_SPEC, Settings, SettingsError, coerce


def test_defaults_are_complete_and_unique():
    keys = [item["key"] for item in SETTINGS_SPEC]
    assert len(keys) == len(set(keys))
    assert set(DEFAULTS) == set(keys)
    assert DEFAULTS["port"] == 6805
    assert os.path.isabs(DEFAULTS["download_dir"]) is False  # expanded on load


def test_validation_bounds_and_types():
    item = next(i for i in SETTINGS_SPEC if i["key"] == "max_active")
    assert coerce(item, "5") == 5
    with pytest.raises(SettingsError):
        coerce(item, True)  # bools are not silently accepted as numbers
    with pytest.raises(SettingsError):
        coerce(item, 0)
    with pytest.raises(SettingsError):
        coerce(item, 99)
    with pytest.raises(SettingsError):
        coerce(item, "many")

    flag = next(i for i in SETTINGS_SPEC if i["key"] == "categorize")
    assert coerce(flag, "yes") is True
    assert coerce(flag, 0) is False

    choice = next(i for i in SETTINGS_SPEC if i["key"] == "theme")
    assert coerce(choice, "dark") == "dark"
    with pytest.raises(SettingsError):
        coerce(choice, "neon")


def test_update_reports_errors_and_keeps_good_values(tmp_path):
    settings = Settings(str(tmp_path / "config.json"))
    result = settings.update({"max_active": 5, "port": 70000, "nope": 1})
    assert result["changed"] == ["max_active"]
    assert set(result["errors"]) == {"port", "nope"}
    assert settings.get("max_active") == 5
    assert settings.get("port") == 6805


def test_save_is_atomic_and_keeps_unknown_keys(tmp_path):
    path = tmp_path / "config.json"
    settings = Settings(str(path))
    settings.set_extra("networks", {"wifi": {"enabled": False}})
    settings.update({"theme": "light"})
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["theme"] == "light"
    assert document["networks"] == {"wifi": {"enabled": False}}
    assert document["version"] == 1
    assert not [name for name in os.listdir(tmp_path) if name.startswith(".config-")]

    reloaded = Settings(str(path))
    assert reloaded.get("theme") == "light"
    assert reloaded.networks() == {"wifi": {"enabled": False}}


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    settings = Settings(str(path))
    assert settings.get("max_active") == DEFAULTS["max_active"]


def test_reset_restores_defaults(tmp_path):
    settings = Settings(str(tmp_path / "config.json"))
    settings.update({"max_active": 9, "theme": "light"})
    values = settings.reset()
    assert values["max_active"] == DEFAULTS["max_active"]
    assert values["theme"] == DEFAULTS["theme"]


def test_token_generation():
    settings = Settings.__new__(Settings)
    first, second = settings.new_token(), settings.new_token()
    assert len(first) > 20 and first != second


def test_app_state_hides_token(tmp_path):
    settings = Settings(str(tmp_path / "config.json"))
    settings.update({"download_dir": str(tmp_path / "dl"), "clipboard_watch": False, "api_token": "secret"})
    app = App(settings=settings, networks=[])
    try:
        assert "api_token" not in app.state()["settings"]
        assert app.settings.values()["api_token"] == "secret"
    finally:
        app.close()


def test_clipboard_url_detection():
    assert clipboard.find_url("look at https://example.com/a.zip).") == "https://example.com/a.zip"
    assert clipboard.find_url("no links here") is None
    assert clipboard.find_url("https://x.test/" + "a" * 200_000) is None
    assert clipboard.is_local("http://127.0.0.1:6805/api/state")
    assert clipboard.is_local("http://localhost:6805/")
    assert not clipboard.is_local("https://example.com/file.zip")


def test_clipboard_watcher_deduplicates():
    import time

    seen: list[str] = []
    values = ["https://example.com/one.zip", "https://example.com/one.zip", "https://example.com/two.zip"]
    watcher = clipboard.ClipboardWatcher(lambda url: seen.append(url), interval=0.02,
                                         reader=lambda: values.pop(0) if values else None)
    watcher.start()
    deadline = time.time() + 3
    while len(seen) < 2 and time.time() < deadline:
        time.sleep(0.02)
    watcher.stop()
    watcher.join(timeout=2)
    assert seen == ["https://example.com/one.zip", "https://example.com/two.zip"]


def test_autostart_helpers():
    command = autostart.launch_command()
    assert command and "serve" in command
    assert "--no-open" in command
    entry = autostart.desktop_entry()
    assert entry.startswith("[Desktop Entry]")
    assert "Exec=" in entry and "Terminal=false" in entry
    status = autostart.status()
    assert set(status) == {"supported", "enabled", "method", "command"}
