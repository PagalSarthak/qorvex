"""Command line entry point.

``qorvex`` with no arguments starts the manager and opens the UI. Everything
after ``serve`` is also accepted directly, so ``qorvex --port 7000`` works.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__, autostart
from .app import App, tail_log
from .config import Settings, ensure_app_dir
from .engine import detect_networks
from .server import extension_dir, serve
from .system import hide_console, open_path, open_url, prepend_path, set_app_id

COMMANDS = {"serve", "add", "nets", "log", "open", "extension", "settings", "autostart"}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="qorvex",
        description="Qorvex: a multi-network download manager (IDM-style capture, bonded connections).",
    )
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the manager (default)")
    s.add_argument("--port", type=int, help="local port (default: from settings, 6805)")
    s.add_argument("--dir", help="download folder (default: from settings)")
    s.add_argument("--network", action="append", default=[], metavar="NAME=IP",
                   help="use these interfaces instead of auto-detecting (repeatable)")
    s.add_argument("--loopback", action="store_true", help="include 127.x addresses (testing)")
    s.add_argument("--max-active", type=int, help="simultaneous downloads")
    s.add_argument("--cookies-from-browser", metavar="BROWSER",
                   help="let yt-dlp use this browser's login (firefox, chrome, edge, brave...)")
    s.add_argument("--no-open", action="store_true", help="do not open the browser UI")
    s.add_argument("--no-window", action="store_true",
                   help="skip the desktop window; open the browser UI instead (needs --no-open to suppress it)")
    s.add_argument("--console", action="store_true", help="keep the console window of a frozen build")

    a = sub.add_parser("add", help="queue one or more URLs on a running Qorvex")
    a.add_argument("urls", nargs="+")
    a.add_argument("--media", action="store_true", help="page URL (YouTube etc.): resolve with yt-dlp")
    a.add_argument("--torrent", action="store_true", help="force torrent mode (magnet:/ .torrent are detected anyway)")
    a.add_argument("--quality", default="best", help="best | 1080 | 720 | 480 | 360 | audio")
    a.add_argument("--dir", help="save into this folder")
    a.add_argument("--port", type=int)
    a.add_argument("--token", help="extension token, if one is configured")

    sub.add_parser("nets", help="list detected network interfaces")

    lg = sub.add_parser("log", help="show the tail of the log file")
    lg.add_argument("-n", "--lines", type=int, default=100)

    sub.add_parser("open", help="open the download folder")
    sub.add_parser("extension", help="open the folder holding the browser extension")
    sub.add_parser("settings", help="print the settings file path")
    au = sub.add_parser("autostart", help="show or change start-at-login")
    au.add_argument("state", nargs="?", choices=["on", "off", "status"], default="status")
    return ap


def _normalize(argv: list[str]) -> list[str]:
    """Allow ``qorvex --port 7000`` as shorthand for ``qorvex serve --port 7000``."""
    if argv and argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        return ["serve", *argv]
    return argv


def _request(port: int, path: str, body: dict | None = None, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Qorvex-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data, headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def main(argv: list[str] | None = None) -> int:
    argv = _normalize(list(sys.argv[1:] if argv is None else argv))
    args = _parser().parse_args(argv)
    if args.cmd is None:
        args = _parser().parse_args(["serve"])

    if args.cmd == "nets":
        for net in detect_networks():
            print(f"{net.name:<20} {net.ip}")
        return 0

    if args.cmd == "log":
        sys.stdout.write(tail_log(args.lines))
        return 0

    if args.cmd == "open":
        return 0 if open_path(Settings().get("download_dir")) else 1

    if args.cmd == "extension":
        return 0 if open_path(extension_dir()) else 1

    if args.cmd == "settings":
        print(Settings().path)
        return 0

    if args.cmd == "autostart":
        if args.state == "status":
            print(json.dumps(autostart.status(), indent=2))
        else:
            autostart.set_enabled(args.state == "on")
            print(json.dumps(autostart.status(), indent=2))
        return 0

    settings = Settings()
    if args.cmd == "add":
        port = args.port or int(settings.get("port", 6805))
        try:
            result = _request(
                port,
                "/api/add",
                {"urls": args.urls, "media": args.media, "quality": args.quality,
                 "dir": args.dir or "", "torrent": args.torrent},
                token=args.token or "",
            )
        except (urllib.error.URLError, OSError) as exc:
            print(f"Qorvex is not running on port {port}: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result))
        return 0

    # ---- serve -----------------------------------------------------------
    ensure_app_dir()
    set_app_id()
    # bundled helpers first on PATH so yt-dlp finds its JS runtime (deno)
    _bundled = getattr(sys, "_MEIPASS", None)
    _frozen_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else ""
    prepend_path(
        _bundled,
        _frozen_dir,
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build_cache"),
    )
    frozen = bool(getattr(sys, "frozen", False))
    if frozen and not args.console:
        hide_console()

    port = int(args.port or settings.get("port", 6805))
    if _port_open(port):
        # already running: if the first instance is minimized to the tray, bring
        # its window back instead of doing nothing (or opening another tab)
        if not args.no_open:
            try:
                _request(port, "/api/show", {}, token=settings.get("api_token", ""))
            except (urllib.error.URLError, OSError):
                pass
        return 0

    app = App(
        settings,
        directory=args.dir,
        networks=detect_networks(args.loopback, args.network) if (args.network or args.loopback) else None,
        max_active=args.max_active,
    )
    app.mgr.ytdlp["cookies_from_browser"] = args.cookies_from_browser

    from .tray import TrayController, tray_available

    tray = TrayController(app)

    server = serve(app, port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Qorvex {__version__} running at {url}  (Ctrl+C to quit)")
    print("networks:", ", ".join(f"{n.name}({n.ip})" for n in app.mgr.networks) or "none")
    print("downloads:", app.mgr.dir)

    server.timeout = 0.4

    def serve_loop():
        try:
            while not app.quitting:
                server.handle_request()
        except KeyboardInterrupt:
            pass

    windowed = not args.no_window
    webview = None
    if windowed:
        try:
            import webview  # pywebview: renders the UI in a native window
        except ImportError:
            webview = None
            windowed = False

    if windowed:
        worker = threading.Thread(target=serve_loop, name="qorvex-server", daemon=True)
        worker.start()
        window = webview.create_window(
            "Qorvex", url, width=1180, height=820, min_size=(860, 540),
        )

        def show_window():
            try:
                window.show()
            except Exception:
                pass

        tray.show_window = show_window
        if tray_available() and (
            settings.get("close_to_tray", True) or settings.get("notify_complete", True)
        ):
            tray.start()

            def show_watcher():
                """Re-launching the exe un-hides a tray-minimized window."""
                while not app.quitting:
                    if app.show_requested.wait(0.5):
                        app.show_requested.clear()
                        show_window()

            threading.Thread(target=show_watcher, daemon=True, name="qorvex-show").start()

        def on_closing():
            """Close-to-tray: hide instead of quitting (unless disabled/quitting)."""
            if settings.get("close_to_tray", True) and not app.quitting:
                window.hide()
                tray.notify("Qorvex is still running", "Downloads continue in the tray.")
                return False  # cancel the close

        window.events.closing += on_closing

        def quit_watcher():
            """UI “Quit Qorvex” must also close the window."""
            while not app.quitting:
                time.sleep(0.25)
            try:
                window.destroy()
            except Exception:  # noqa: BLE001 - window may already be gone
                pass

        threading.Thread(target=quit_watcher, name="qorvex-quitwatch", daemon=True).start()
        try:
            webview.start()  # blocks until the window is closed
        except Exception as e:  # noqa: BLE001 - no display / broken runtime
            print(f"desktop window failed ({e}); falling back to the browser")
            if not args.no_open and settings.get("open_ui_on_start", True):
                open_url(url)
            serve_loop()
        finally:
            tray.stop()
            app.quit()
        worker.join(timeout=5)
    else:
        if not args.no_open and settings.get("open_ui_on_start", True):
            open_url(url)
        if tray_available() and settings.get("notify_complete", True):
            tray.start()
        try:
            serve_loop()
        finally:
            tray.stop()

    server.server_close()
    app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
