"""Update checker: compares the running version against the latest GitHub release.

Everything is best-effort: any failure (offline, rate limit, blocked API)
leaves the app silent rather than nagging.
"""

import json
import re
import threading
import urllib.request

import qorvex

_RELEASES_URL = "https://api.github.com/repos/PagalSarthak/qorvex/releases/latest"
_RELEASE_PAGE = "https://github.com/PagalSarthak/qorvex/releases/latest"
_CHECK_INTERVAL = 12 * 60 * 60  # twice a day
_FIRST_DELAY = 45.0  # let the app settle before the first check


def _parts(version: str):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", str(version).strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newer(latest: str, current: str) -> bool:
    a, b = _parts(latest), _parts(current)
    return bool(a and b and a > b)


def latest_release(timeout: float = 10.0):
    """Return (version, url) of the newest GitHub release, or None."""
    try:
        req = urllib.request.Request(
            _RELEASES_URL,
            headers={
                "User-Agent": "qorvex/" + qorvex.__version__,
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "")
        if not tag:
            return None
        return (tag, _RELEASE_PAGE)
    except Exception:  # noqa: BLE001 - offline / rate limited / blocked: stay silent
        return None


class UpdateWatcher(threading.Thread):
    """Background checker; exposes the result through `app.state` and notifies."""

    def __init__(self, app, on_found=None, interval: float = _CHECK_INTERVAL):
        super().__init__(name="qorvex-updates", daemon=True)
        self.app = app
        self.on_found = on_found
        self.interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()  # not Thread._stop; renamed intentionally
        self.found: tuple | None = None

    def stop(self):
        self._stop.set()
        self._wake.set()

    def check_now(self):
        """Force an immediate check; called from the UI's Check now button."""
        self._wake.set()

    def run(self):
        self._wake.wait(_FIRST_DELAY)
        while not self._stop.is_set():
            self._check_once()
            self._wake.wait(self.interval)

    def _check_once(self):
        result = latest_release()
        if result and newer(result[0], qorvex.__version__):
            first_time = self.found != result
            self.found = result
            if first_time and self.on_found:
                try:
                    self.on_found(result[0], result[1])
                except Exception:  # noqa: BLE001
                    pass

    def state(self) -> dict | None:
        return (
            {"version": self.found[0], "url": self.found[1], "current": qorvex.__version__}
            if self.found
            else None
        )
