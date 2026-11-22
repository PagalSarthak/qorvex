"""User settings: one validated, atomically saved JSON document.

The settings spec below is the single source of truth for defaults, bounds and
the settings form in the web UI. Add a field here and it shows up everywhere.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import sys
import tempfile
import threading
from typing import Any

APP_NAME = "Qorvex"
def _default_ua() -> str:
    platform = {
        "win32": "Windows NT 10.0; Win64; x64",
        "darwin": "Macintosh; Intel Mac OS X 10_15_7",
    }.get(sys.platform, "X11; Linux x86_64")
    return f"Mozilla/5.0 ({platform}) Qorvex/1.0"


DEFAULT_UA = _default_ua()


def app_dir() -> str:
    """Per-user state directory (config, log, optional portable data)."""
    return os.environ.get("QORVEX_HOME") or os.path.join(os.path.expanduser("~"), ".qorvex")


def config_path() -> str:
    return os.path.join(app_dir(), "config.json")


def log_path() -> str:
    return os.path.join(app_dir(), "qorvex.log")


def ensure_app_dir() -> str:
    path = app_dir()
    os.makedirs(path, exist_ok=True)
    return path


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))


#: Every user-facing setting. `group` drives the settings UI, `restart` marks
#: changes that only apply after a restart, and `managed` fields get a dedicated
#: control instead of a generic form row.
SETTINGS_SPEC: tuple[dict[str, Any], ...] = (
    # ---- General -----------------------------------------------------------
    dict(
        key="download_dir",
        type="path",
        default="~/Downloads",
        group="General",
        label="Download folder",
        help="Base folder for new downloads. Categories create subfolders here.",
    ),
    dict(
        key="categorize",
        type="bool",
        default=True,
        group="General",
        label="Sort downloads into category folders",
        help="video/, audio/, documents/, archives/, programs/ under the download folder.",
    ),
    dict(
        key="max_active",
        type="int",
        default=3,
        min=1,
        max=16,
        group="General",
        label="Simultaneous downloads",
        help="How many downloads run at once. Each one can use several connections.",
    ),
    dict(
        key="port",
        type="int",
        default=6805,
        min=1024,
        max=65535,
        group="General",
        label="Local port",
        restart=True,
        help="The browser extension and UI connect to 127.0.0.1 on this port.",
    ),
    dict(
        key="open_ui_on_start",
        type="bool",
        default=True,
        group="General",
        label="Open the web UI on start",
        help="Also disabled automatically for login/autostart launches.",
    ),
    dict(
        key="resume_on_start",
        type="bool",
        default=True,
        group="General",
        label="Resume unfinished downloads on start",
        help="Restores the queue and continues where it left off. "
        "Downloads that need cookies must be added again.",
    ),
    dict(
        key="theme",
        type="choice",
        default="auto",
        choices=["auto", "dark", "light"],
        group="General",
        label="Theme",
    ),
    dict(
        key="api_token",
        type="secret",
        default="",
        group="General",
        label="Extension token",
        help="Optional. When set, browser extensions must send this token. "
        "The web UI keeps working without it. Generate one below.",
    ),
    # ---- Engine ------------------------------------------------------------
    dict(
        key="streams_per_network",
        type="int",
        default=4,
        min=1,
        max=16,
        group="Engine",
        label="Connections per network",
        help="Parallel ranged requests per interface. More can help, but some "
        "servers rate-limit per IP.",
    ),
    dict(
        key="chunk_min_mb",
        type="int",
        default=1,
        min=1,
        max=32,
        group="Engine",
        label="Smallest chunk (MB)",
    ),
    dict(
        key="chunk_max_mb",
        type="int",
        default=8,
        min=1,
        max=64,
        group="Engine",
        label="Largest chunk (MB)",
    ),
    dict(
        key="retry_limit",
        type="int",
        default=12,
        min=1,
        max=100,
        group="Engine",
        label="Chunk retries",
        help="Retries before a chunk is reported as failed.",
    ),
    dict(
        key="network_fail_limit",
        type="int",
        default=4,
        min=1,
        max=20,
        group="Engine",
        label="Network failures before it is dropped",
    ),
    dict(
        key="global_limit_mb",
        type="float",
        default=0.0,
        min=0.0,
        max=100000.0,
        group="Engine",
        label="Total speed limit (MB/s)",
        help="0 means unlimited. Applies across every network and download.",
    ),
    dict(
        key="verify_tls",
        type="bool",
        default=True,
        group="Engine",
        label="Verify HTTPS certificates",
        help="Leave on. Disabling is only for debugging a local server.",
    ),
    dict(
        key="user_agent",
        type="str",
        default=DEFAULT_UA,
        max_len=300,
        group="Engine",
        label="User agent",
    ),
    dict(
        key="torrent_enabled",
        type="bool",
        default=True,
        group="Engine",
        label="Torrents",
        help="BitTorrent via libtorrent (pip install libtorrent). magnet: links and .torrent files are detected automatically.",
    ),
    dict(
        key="torrent_seed_ratio",
        type="float",
        default=0.0,
        min=0.0,
        max=1000.0,
        group="Engine",
        label="Seed torrents until ratio",
        help="Keep uploading after a torrent finishes until this upload/download ratio is reached. 0 stops as soon as the download completes.",
    ),
    dict(
        key="torrent_port",
        type="int",
        default=6881,
        min=1,
        max=65535,
        group="Engine",
        label="Torrent listen port",
        restart=True,
        help="Incoming peer connections arrive here. Change needs a restart.",
    ),
    # ---- Browser -----------------------------------------------------------
    dict(
        key="intercept",
        type="bool",
        default=True,
        group="Browser",
        label="Take over browser downloads",
        help="The extension cancels normal downloads and hands them to Qorvex.",
    ),
    dict(
        key="intercept_min_mb",
        type="float",
        default=0.0,
        min=0.0,
        max=100000.0,
        group="Browser",
        label="Only take over files larger than (MB)",
        help="0 takes over every download.",
    ),
    dict(
        key="intercept_confirm",
        type="bool",
        default=False,
        group="Browser",
        label="Ask before each captured download",
        help="Captured downloads wait in a prompt above the list — click Download to start them. Nothing runs automatically.",
    ),
    dict(
        key="clipboard_watch",
        type="bool",
        default=True,
        group="Browser",
        label="Watch the clipboard for links",
        help="Shows a one-click download prompt when you copy a file or video link.",
    ),
    dict(
        key="clipboard_auto_add",
        type="bool",
        default=False,
        group="Browser",
        label="Start copied links automatically",
        help="Skip the prompt and queue the link as soon as it is copied.",
    ),
    # ---- Startup -----------------------------------------------------------
    dict(
        key="autostart",
        type="bool",
        default=False,
        group="Startup",
        label="Start Qorvex when you sign in",
        managed="autostart",
        help="Registers a per-user login item. No administrator rights needed.",
    ),
)

SPEC_BY_KEY: dict[str, dict[str, Any]] = {item["key"]: item for item in SETTINGS_SPEC}
DEFAULTS: dict[str, Any] = {item["key"]: item["default"] for item in SETTINGS_SPEC}


class SettingsError(ValueError):
    """A rejected setting value."""


def coerce(item: dict[str, Any], value: Any) -> Any:
    """Validate and normalize one value. Raises SettingsError."""
    kind = item["type"]
    key = item["key"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("0", "false", "no", "off", ""):
            return False
        raise SettingsError(f"{key} must be true or false")

    if kind in ("int", "float"):
        if isinstance(value, bool):
            raise SettingsError(f"{key} must be a number")
        try:
            number = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{key} must be a number") from None
        low, high = item.get("min"), item.get("max")
        if low is not None and number < low:
            raise SettingsError(f"{key} must be at least {low}")
        if high is not None and number > high:
            raise SettingsError(f"{key} must be at most {high}")
        return number

    if kind == "choice":
        text = str(value)
        if text not in item.get("choices", ()):
            raise SettingsError(f"{key} must be one of {', '.join(item['choices'])}")
        return text

    if kind == "path":
        text = str(value).strip()
        if not text:
            raise SettingsError(f"{key} cannot be empty")
        if "\x00" in text:
            raise SettingsError(f"{key} contains an invalid character")
        return _expand(text)

    # str / secret
    text = "" if value is None else str(value)
    if kind == "secret":
        text = text.strip()
    limit = item.get("max_len")
    if limit is not None and len(text) > limit:
        raise SettingsError(f"{key} must be at most {limit} characters")
    if "\x00" in text:
        raise SettingsError(f"{key} contains an invalid character")
    return text


class Settings:
    """Thread-safe settings store with atomic saves and forward-compatible extras.

    Unknown top-level keys (for example ``networks`` and ``manual_networks``)
    are preserved so newer and older versions can share one file.
    """

    def __init__(self, path: str | None = None):
        self.path = path or config_path()
        self._lock = threading.RLock()
        self._raw: dict[str, Any] = {}
        self._values: dict[str, Any] = dict(DEFAULTS)
        self._load()

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        raw: dict[str, Any] = {}
        try:
            with open(self.path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, ValueError):
            raw = {}
        self._raw = raw
        values = dict(DEFAULTS)
        for key, item in SPEC_BY_KEY.items():
            if key in raw:
                try:
                    values[key] = coerce(item, raw[key])
                except SettingsError:
                    values[key] = item["default"]
        self._values = values
        # Normalize a default that was never persisted.
        if "download_dir" in self._values:
            self._values["download_dir"] = _expand(self._values["download_dir"])

    def save(self) -> None:
        with self._lock:
            document = dict(self._raw)
            document.update(self._values)
            document["version"] = 1
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(document, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if os.name != "nt":
                    os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    # -- access -------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if key in self._values:
                return self._values[key]
            return self._raw.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def values(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._values)

    def spec(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(SETTINGS_SPEC))

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial update. Valid keys are saved even if others fail."""
        if not isinstance(patch, dict):
            return {"changed": [], "errors": {"": "expected an object"}}
        changed: list[str] = []
        errors: dict[str, str] = {}
        with self._lock:
            for key, value in patch.items():
                item = SPEC_BY_KEY.get(key)
                if item is None:
                    errors[key] = "unknown setting"
                    continue
                try:
                    normalized = coerce(item, value)
                except SettingsError as exc:
                    errors[key] = str(exc)
                    continue
                if self._values.get(key) != normalized:
                    self._values[key] = normalized
                    changed.append(key)
            if changed:
                self.save()
        return {"changed": changed, "errors": errors}

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._values = dict(DEFAULTS)
            self._values["download_dir"] = _expand(DEFAULTS["download_dir"])
            self.save()
            return self.values()

    # -- extras (networks, manual interfaces) -------------------------------
    def extra(self, key: str, default: Any = None) -> Any:
        with self._lock:
            value = self._raw.get(key, default)
            return copy.deepcopy(value)

    def set_extra(self, key: str, value: Any) -> None:
        with self._lock:
            self._raw[key] = value
            self.save()

    # -- network preferences ------------------------------------------------
    def networks(self) -> dict[str, dict[str, Any]]:
        value = self.extra("networks", {})
        return value if isinstance(value, dict) else {}

    def set_networks(self, value: dict[str, dict[str, Any]]) -> None:
        self.set_extra("networks", value)

    def manual_networks(self) -> list[str]:
        value = self.extra("manual_networks", [])
        return [str(item) for item in value] if isinstance(value, list) else []

    def set_manual_networks(self, value: list[str]) -> None:
        self.set_extra("manual_networks", list(value))

    def new_token(self) -> str:
        return secrets.token_urlsafe(24)
