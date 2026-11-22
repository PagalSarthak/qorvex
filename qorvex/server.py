"""Local control server: web UI + JSON API for the browser extension.

Security model
--------------
* Bound to 127.0.0.1 only.
* ``Host`` must be 127.0.0.1/localhost (blocks DNS rebinding).
* A browser ``Origin`` must be our own UI or an extension; requests with no
  Origin (curl, CLI, the extension's background fetch) are allowed.
* POST bodies must be ``application/json``, so a cross-site HTML form cannot
  drive the API.
* Optional shared token: when set, extension-origin requests must present it.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, autostart
from .app import App, tail_log
from .config import app_dir
from .system import copy_clipboard, find_browsers, open_path, reveal_path

UI = os.path.join(os.path.dirname(__file__), "ui.html")
LOGO = os.path.join(os.path.dirname(__file__), "logo.png")
MAX_BODY = 1 << 20
MAX_TORRENT_BODY = 32 << 20   # .torrent uploads (drag & drop); libtorrent caps metadata at ~30 MB
ACTIONS = {"pause", "resume", "retry", "cancel", "remove", "delete"}
MEDIA_PAGE_RE = re.compile(r"youtu|vimeo|dailymotion|twitch\.tv/videos", re.I)


def extension_dir() -> str:
    """Where the browser extension lives, in a checkout or a frozen build.

    A one-file executable unpacks to a temporary folder that disappears on exit,
    so copy the extension to the per-user state folder and load it from there.
    """
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        bundled = os.path.join(bundled_root, "extension")
        if os.path.isdir(bundled):
            target = os.path.join(app_dir(), "extension")
            marker = os.path.join(target, ".version")
            try:
                with open(marker, encoding="utf-8") as handle:
                    current = handle.read().strip()
            except OSError:
                current = ""
            if current != __version__:
                try:
                    shutil.rmtree(target, ignore_errors=True)
                    shutil.copytree(bundled, target)
                    with open(marker, "w", encoding="utf-8") as handle:
                        handle.write(__version__ + "\n")
                except OSError:
                    return bundled
            return target
        return bundled
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "extension")


def make_handler(app: App, port: int):
    own = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "Qorvex"
        sys_version = ""

        def log_message(self, *args):  # keep the console quiet
            pass

        # -- request guards -------------------------------------------------
        def _origin(self) -> str:
            return self.headers.get("Origin", "")

        def _is_extension(self) -> bool:
            return self._origin().startswith(("chrome-extension://", "moz-extension://"))

        def _allowed(self) -> bool:
            host = self.headers.get("Host", "")
            if host.rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
                return False
            origin = self._origin()
            if origin and origin not in own and not self._is_extension():
                return False
            token = str(app.settings.get("api_token", "") or "")
            if token and self._is_extension():
                supplied = self.headers.get("X-Qorvex-Token") or urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query
                ).get("token", [""])[0]
                if supplied != token:
                    return False
            return True

        def _cors(self) -> None:
            origin = self._origin()
            if origin.startswith(("chrome-extension://", "moz-extension://")):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Qorvex-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Vary", "Origin")

        def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _json(self, payload, code: int = 200) -> None:
            self._send(code, json.dumps(payload, default=str).encode())

        def _error(self, message: str, code: int = 400) -> None:
            self._json({"error": message}, code)

        def _read_json(self, limit: int = MAX_BODY) -> dict | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > limit:
                self._error("request too large", 413)
                return None
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if length and ctype != "application/json":
                self._error("expected application/json", 415)
                return None
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._error("bad json")
                return None
            if not isinstance(payload, dict):
                self._error("expected an object")
                return None
            return payload

        # -- verbs ----------------------------------------------------------
        def do_OPTIONS(self):
            if not self._allowed():
                return self._send(403)
            self._send(204)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._allowed():
                return self._send(403)
            if self.headers.get("X-Qorvex-Extension"):
                app.note_extension()
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                try:
                    with open(UI, "rb") as handle:
                        body = handle.read()
                except OSError:
                    return self._error("ui.html is missing", 500)
                return self._send(200, body, "text/html; charset=utf-8")
            if path in ("/favicon.ico", "/logo.png"):
                try:
                    with open(LOGO, "rb") as handle:
                        body = handle.read()
                except OSError:
                    return self._send(204)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=3600")
                self._cors()
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            if path == "/favicon.ico":
                return self._send(204)
            if path == "/api/state":
                return self._json(app.state())
            if path == "/api/settings":
                return self._json({"values": app.settings.values(), "spec": app.settings.spec()})
            if path == "/api/log":
                lines = urllib.parse.parse_qs(parsed.query).get("lines", ["200"])[0]
                try:
                    count = int(lines)
                except ValueError:
                    count = 200
                return self._send(200, tail_log(count).encode("utf-8", "replace"), "text/plain; charset=utf-8")
            if path == "/api/extension":
                return self._json(
                    {
                        "dir": extension_dir(),
                        "chrome": os.path.join(extension_dir(), "chrome"),
                        "firefox": os.path.join(extension_dir(), "firefox"),
                        "exists": os.path.isdir(extension_dir()),
                        "browsers": find_browsers(),
                    }
                )
            if path == "/api/extension/open":
                return self._json({"ok": open_path(extension_dir())})
            self._error("not found", 404)

        def do_POST(self):
            if not self._allowed():
                return self._send(403)
            if self.headers.get("X-Qorvex-Extension"):
                app.note_extension()
            path_only = urllib.parse.urlsplit(self.path).path
            body = self._read_json(
                MAX_TORRENT_BODY if path_only == "/api/torrents/add" else MAX_BODY
            )
            if body is None:
                return
            path = urllib.parse.urlsplit(self.path).path
            try:
                return self._route(path, body)
            except ValueError as exc:
                return self._error(str(exc))
            except Exception as exc:  # noqa: BLE001 - never kill the server thread
                return self._error(f"internal error: {exc}", 500)

        # -- routes ---------------------------------------------------------
        def _route(self, path: str, body: dict):
            mgr = app.mgr
            if path == "/api/add":
                urls = body.get("urls")
                if urls is None:
                    urls = [body.get("url", "")]
                if not isinstance(urls, list) or not urls:
                    return self._error("no url given")
                headers = {
                    key: body[field]
                    for field, key in (("cookie", "Cookie"), ("referer", "Referer"), ("ua", "User-Agent"))
                    if body.get(field)
                }
                media = bool(body.get("media"))
                quality = str(body.get("quality", "best"))
                quality = quality if quality in ("best", "audio") or quality.isdigit() else "best"
                torrent = bool(body.get("torrent"))
                directory = str(body.get("dir") or "") or None
                filename = str(body.get("filename") or "")
                added, rejected, held = [], [], []
                for raw in urls[:100]:
                    url = str(raw).strip()
                    if not url:
                        continue
                    # extension captures wait for a click when “ask first” is on
                    if (body.get("intercepted") and app.settings.get("intercept_confirm")
                            and not body.get("confirm")):
                        app.stage_pending(url, name=filename, source="intercept")
                        held.append(url)
                        continue
                    try:
                        item = app.add_url(
                            url,
                            media=media,
                            quality=quality,
                            filename=filename if len(urls) == 1 else "",
                            directory=directory,
                            headers=headers,
                            torrent=torrent,
                        )
                        added.append(item.id)
                    except ValueError as exc:
                        rejected.append({"url": url, "error": str(exc)})
                if held:
                    return self._json({"held": held})
                if not added:
                    return self._error(rejected[0]["error"] if rejected else "no valid url")
                return self._json({"ids": added, "rejected": rejected})

            if path == "/api/network":
                ok = mgr.set_network(body.get("name"), body.get("enabled"), body.get("limit"))
                return self._json({"ok": ok})

            if path == "/api/networks/add":
                spec = str(body.get("spec") or "")
                if not spec and body.get("name"):
                    spec = f"{body['name']}={body.get('ip', '')}"
                mgr.add_network(spec)
                return self._json({"ok": True, "networks": [n.snapshot() for n in mgr.networks]})

            if path == "/api/networks/remove":
                return self._json({"ok": mgr.remove_network(str(body.get("name") or ""))})

            if path == "/api/torrents/add":
                """Drag & drop / file picker: base64 .torrent contents."""
                data = body.get("data")
                if not isinstance(data, str) or not data:
                    return self._error("no torrent data")
                try:
                    raw = base64.b64decode(data, validate=True)
                except (ValueError, TypeError):
                    return self._error("torrent data is not valid base64")
                if len(raw) > MAX_TORRENT_BODY:
                    return self._error("torrent file too large", 413)
                import tempfile

                fd, tmp = tempfile.mkstemp(prefix="qorvex-", suffix=".torrent")
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                directory = str(body.get("dir") or "") or None
                try:
                    job = mgr.add_torrent(tmp, directory=directory)
                except Exception:
                    try:
                        os.unlink(tmp)  # rejected (disabled / no libtorrent): no leftovers
                    except OSError:
                        pass
                    raise
                return self._json({"id": job.id, "name": job.name or ""})

            if path == "/api/settings":
                return self._json(app.update_settings(body))

            if path == "/api/settings/reset":
                return self._json({"values": app.reset_settings()})

            if path == "/api/autostart":
                enabled = bool(body.get("enabled"))
                app.settings.update({"autostart": enabled})
                return self._json({"autostart": autostart.status()})

            if path == "/api/token":
                action = str(body.get("action") or "new")
                token = app.settings.new_token() if action == "new" else ""
                app.settings.update({"api_token": token})
                return self._json({"token": token})

            if path == "/api/clipboard":
                action = str(body.get("action") or "")
                if action == "take":
                    return self._json({"urls": app.take_pending()})
                if action == "confirm":
                    url = str(body.get("url") or "")
                    if not url:
                        return self._error("no url given")
                    result = app.confirm_pending(url, media=bool(MEDIA_PAGE_RE.search(url)))
                    return self._json({"ok": True, **result})
                if action == "dismiss":
                    app.dismiss_pending(body.get("url") or None)
                    return self._json({"ok": True})
                return self._error("unknown clipboard action")

            if path == "/api/open":
                return self._json({"ok": _open_known(app, str(body.get("path") or ""), reveal=False)})

            if path == "/api/reveal":
                return self._json({"ok": _open_known(app, str(body.get("path") or ""), reveal=True)})

            if path == "/api/extension/launch":
                """One-click setup: open the browser at its extension page with
                the unpacked-extension folder already copied for pasting."""
                name = str(body.get("browser") or "")
                exe = find_browsers().get(name)
                if not exe:
                    return self._error(f"{name or 'browser'} is not installed")
                firefox = name == "firefox"
                folder = os.path.join(extension_dir(), "firefox" if firefox else "chrome")
                if not os.path.isdir(folder):
                    return self._error("extension folder is missing")
                setup_page = "about:debugging#/runtime/this-firefox" if firefox else "chrome://extensions/"
                copied = copy_clipboard(folder)
                open_path(folder)
                subprocess.Popen([exe, setup_page], close_fds=True,
                                 creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
                return self._json({"ok": True, "url": setup_page, "folder": folder, "copied": copied})

            if path == "/api/extension/open":
                target = os.path.join(extension_dir(), str(body.get("browser") or "chrome"))
                return self._json({"ok": open_path(target)})

            if path == "/api/quit":
                app.quit()
                return self._json({"ok": True})

            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "download"]:
                action = parts[3]
                if action not in ACTIONS:
                    return self._error("unknown action")
                return self._json({"ok": mgr.action(parts[2], action)})

            return self._error("not found", 404)

    return Handler


def _open_known(app: App, path: str, reveal: bool) -> bool:
    """Only open files the app owns: downloads and its own state directory."""
    if not path:
        return False
    target = os.path.realpath(os.path.expanduser(path))
    roots = {
        os.path.realpath(app.mgr.dir),
        os.path.realpath(app.settings.get("download_dir", "")),
        os.path.realpath(os.path.dirname(app.settings.path)),
        os.path.realpath(extension_dir()),
    }
    if not any(target == root or target.startswith(root + os.sep) for root in roots if root):
        return False
    return reveal_path(target) if reveal else open_path(target)


def serve(app: App, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server
