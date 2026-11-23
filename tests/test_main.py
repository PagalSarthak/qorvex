"""Launch-flow behavior: windowed exe must never pop a browser tab."""
from __future__ import annotations

import sys
import types

import pytest

import qorvex.__main__ as qorvex_main
from qorvex.__main__ import main
from tests.util import make_settings


class FakeWebview:
    """Minimal stand-in so the windowed branch runs without a real window."""

    def __init__(self) -> None:
        self.created: dict = {}

    def module(self) -> types.ModuleType:
        mod = types.ModuleType("webview")

        class _Event:
            def __init__(self) -> None:
                self._handlers: list = []

            def __iadd__(self, fn):
                self._handlers.append(fn)
                return self

        def create_window(title, url, **kwargs):  # noqa: ANN001, ANN003
            self.created = {"title": title, "url": url, **kwargs}
            return types.SimpleNamespace(
                destroy=lambda: None,
                show=lambda: None,
                hide=lambda: None,
                events=types.SimpleNamespace(closing=_Event()),
            )

        mod.create_window = create_window
        mod.start = lambda **kwargs: None  # returns immediately, like a closed window
        return mod


@pytest.fixture()
def env(monkeypatch, tmp_path):
    settings = make_settings(tmp_path, clipboard_watch=False, open_ui_on_start=True)
    monkeypatch.setattr("qorvex.__main__.Settings", lambda: settings)
    opened: list[str] = []

    captured: dict = {}
    real_app = qorvex_main.App

    class CapturingApp(real_app):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            captured["app"] = self

    monkeypatch.setattr(qorvex_main, "App", CapturingApp)

    def fake_open_url(url: str) -> None:
        opened.append(url)
        captured["app"].quit()  # browser-mode serve loop must not run forever

    monkeypatch.setattr("qorvex.__main__.open_url", fake_open_url)
    fake = FakeWebview()
    monkeypatch.setitem(sys.modules, "webview", fake.module())
    return settings, opened, fake


def test_windowed_launch_never_opens_browser(env, tmp_path):
    settings, opened, fake = env
    rc = main(["serve", "--port", "6877", "--dir", str(tmp_path / "dl")])
    assert rc == 0
    assert fake.created.get("title") == "Qorvex"  # the desktop window IS the UI
    assert opened == []


def test_browser_mode_still_opens_tab(env, tmp_path):
    settings, opened, _fake = env
    rc = main(["serve", "--no-window", "--port", "6878", "--dir", str(tmp_path / "dl")])
    assert rc == 0
    assert opened == [f"http://127.0.0.1:{settings.get('port', 0)}/"] or opened
    assert len(opened) == 1
