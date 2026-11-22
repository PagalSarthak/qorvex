# Qorvex

A download manager that **grabs links and videos from your browser** (IDM / Ghost Downloader style)
and **downloads them over every internet connection at once** (Plexo style).

```
 Browser extension ──► Qorvex (local, 127.0.0.1) ──► Wi-Fi ┐
  • takes over downloads            • splits file into     ├─► one file on disk
  • right-click "Download with…"      chunks               │
  • button on videos / media list   • each chunk goes out    Ethernet / USB tether /
  • clipboard link catcher            over a chosen network  phone hotspot ┘
```

Version 1.0 adds the things that make it usable day to day: a settings screen,
start-at-login, category folders, speed limits per network and overall, batch
downloads, a clipboard catcher, a real Windows executable, and a much better
failure story (pause when every network is off, recover from dead networks,
validate resumes).

## Feature map

| IDM-style | Plexo-style |
|---|---|
| Takes over browser downloads | Splits each file into chunks across interfaces |
| Hover button on any video | Per-network connection count and speed caps |
| Right-click → Download with Qorvex | Global speed limit |
| Clipboard link catcher | Per-network enable/disable, live |
| Category folders (video/audio/docs/…) | Bonding verified by checksum in tests |
| Pause / resume / retry / cancel | Per-network yt-dlp resolution for IP-locked media |
| Batch add (paste many links) | Torrents via libtorrent: magnet links and .torrent files | Network failure isolation + automatic recovery |
| Start at login, tray-free background server | IPv4 source-IP + device binding |
| Open / reveal finished files | Resume validated by URL, size, ETag, Last-Modified |

## Run it (from source)

```bash
pip install psutil pywebview
pip install -U "yt-dlp[default]"   # only needed for YouTube / video-site downloads
pip install -U libtorrent          # only needed for magnet: / .torrent downloads
python -m qorvex serve             # opens as a desktop window
```

Without `pywebview` the dashboard still works — it just opens in your browser at
http://127.0.0.1:6805. `python -m qorvex serve --no-window` forces browser mode.

`python run.py serve` works too. The UI is a single HTML file with no build step.

Useful commands:

```bash
python -m qorvex add https://example.com/file.iso
python -m qorvex add https://youtu.be/... --media --quality 720
python -m qorvex nets
python -m qorvex log -n 200
python -m qorvex open                # download folder
python -m qorvex extension           # extension folder
python -m qorvex autostart on|off|status
python -m qorvex settings            # print config path
```

`qorvex --port 7000` is shorthand for `qorvex serve --port 7000`.

## Single-file app (no Python needed to run)

- **Windows:** run `build.bat` → `dist\qorvex.exe`. Double-click it: Qorvex opens as a desktop
  window. Use `qorvex.exe serve --no-window` for browser-only mode, and `--no-open` to start
  without opening anything (used by autostart).
- **Linux:** run `./build.sh` → `dist/qorvex` (single file). The desktop window uses Qt via
  `pywebview[qt]` (installed by build.sh automatically) or GTK webkit if present — otherwise it
  falls back to the browser UI. A `qorvex.desktop` menu entry is included.
- **macOS:** run `./build.sh` with `--onedir` replaced in mind → `dist/qorvex.app`; CI produces
  the proper `.app` bundle automatically. The desktop window uses the native WKWebView. Gatekeeper
  will ask to confirm apps from an unnotarized developer — right-click → Open.
- **No build tools?** Push this folder to GitHub, run the **Build Qorvex** workflow
  (Actions tab) and download the Windows/Linux/macOS artifacts. The workflow also runs the tests
  and builds both browser extensions.

The executable bundles `ui.html`, `logo.png`, the extension folders, `psutil`, `pywebview`, `yt-dlp` and
`libtorrent` (when available at build time). It builds **windowed** with the logo as icon:
double-click it and Qorvex opens as a real desktop window (Edge WebView2 — no browser tabs or
address bar). Closing the window quits the app; **Quit Qorvex** in Settings closes the window too.
If the WebView2 runtime is missing it falls back to the browser UI.
`ffmpeg` is not bundled: install it for merged video+audio (otherwise both files are kept).

## First run

1. Start Qorvex. The dashboard lists every IPv4 interface it found, with a live speed per interface.
2. Paste links (one per line) or let the extension/clipboard feed it.
3. Press **Add**. Downloads are scheduled up to *Simultaneous downloads*.
4. Each row shows progress, speed, ETA, connections, per-network byte counts and its category.
5. **Settings** covers everything else; changes apply live unless marked as restart-only.

### Settings

| Group | What you can change |
|---|---|
| General | download folder, category folders, simultaneous downloads, port, open UI on start, resume unfinished downloads, theme, extension token |
| Engine | connections per network, min/max chunk size, chunk retries, network failure limit, global speed limit, TLS verification, user agent |
| Browser | take over browser downloads, minimum size to intercept, clipboard watching, auto-add copied links |
| Startup | start Qorvex when you sign in (per-user; no admin rights) |

Autostart is implemented per OS, not with a Windows service:

- **Windows:** `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
- **Linux:** `~/.config/autostart/qorvex.desktop`
- **macOS:** `~/Library/LaunchAgents/com.qorvex.agent.plist`

The registered command is refreshed on each start, so moving the executable does not
leave a dead startup entry. Autostart launches with `--no-open`, so login stays quiet.

### Clipboard catcher

Copy any `http(s)` link and the dashboard offers a one-click download (or queues it
immediately if *Start copied links automatically* is on). Qorvex only reads the clipboard;
it never writes to it. Its own UI/API links are ignored.

## Browser extension

Two ready folders are built from one source:

- **Chrome / Edge / Brave:** `chrome://extensions` → Developer mode → Load unpacked → `extension/chrome`
- **Firefox:** `about:debugging#/runtime/this-firefox` → Load Temporary Add-on → pick
  `extension/firefox/manifest.json`. Temporary until restart; permanent installs need signing
  on addons.mozilla.org (or Firefox Developer/Nightly with `xpinstall.signatures.required=false`).

In the one-file executable the extension is copied to `~/.qorvex/extension` (Settings →
**Open extension folder**) so it keeps working after the temporary unpack folder is deleted.

Features:

- Normal browser downloads are taken over automatically. If Qorvex is not running, the browser
  downloads as usual. A minimum-size filter avoids stealing tiny files.
- **Hover any video** → **⬇ Qorvex** button. YouTube-style players get a quality menu
  (Best, 1080p, 720p, 480p, 360p, Audio only); direct `mp4` links download immediately.
- Right-click → *Download with Qorvex* / *Download video with Qorvex (YouTube etc.)* / *Open Qorvex*.
- Toolbar popup: connection status, capture settings, current-page video download, detected media list.
- Optional **extension token** (Settings → Generate). When set, extension requests must send it;
  the local UI keeps working. This stops other installed extensions from queueing downloads.

Edit `extension/src/` then run `python tools/build_extension.py`; the manifest version is taken
from the Python package so the two never drift.

## How video sites work

Players like YouTube use `blob:` streams, so the button sends the **page URL** to Qorvex, which
resolves it with **yt-dlp**. Those direct links are usually locked to the IP that requested them,
so Qorvex resolves **once per network** and each network downloads with its own link. Video and
audio arrive as two hidden parts and are merged with **ffmpeg**.

YouTube also needs a JavaScript runtime for yt-dlp (Deno is recommended; see yt-dlp's EJS wiki)
and a recent yt-dlp. Streaming-only formats (HLS/DASH playlists) are rejected up front with a
clear error instead of failing late.

## How torrents work

Paste a `magnet:` link or a `.torrent` URL (or a local `.torrent` path), or **drag & drop a
`.torrent` file** anywhere onto the dashboard — there is also a *+ .torrent file* picker button
next to Add. Dropped files go to the server as base64, are parsed by libtorrent and the temp copy
is removed afterwards. Qorvex queues each one as a
**torrent** row. libtorrent (an optional dependency, like yt-dlp) handles DHT, trackers, peers and
piece management; the session binds outgoing peer connections to every enabled network's IP so
peer traffic spreads across your interfaces. Pause/resume, retry, cancel (deletes partial files)
and delete work like any other row. Settings → Engine controls seeding: *Seed torrents until
ratio* — `0` finishes the moment the download completes, otherwise the row stays **seeding**
(with an upload rate and a Stop seeding button) until the ratio is reached.

Incoming peer connections arrive on one listen port (Settings → Engine → Torrent listen port), so
bonding here is on the outbound side only. Torrent bytes are attributed to the network each peer
connection actually uses (read from the connection's local endpoint every poll) and fed into the
network cards, so Wi-Fi and Ethernet show torrent traffic like any other download. A few hundred
kilobytes per new peer connection can land in the first statistics sample and go unattributed.

## How the download engine works

- Probes the URL, splits the file into 1–8 MB chunks in a shared queue; each enabled network runs
  N connections (default 4) pulling chunks, so faster networks carry more. Sockets are pinned to
  each interface (`SO_BINDTODEVICE` on Linux, `IP_BOUND_IF` on macOS, source-IP bind elsewhere).
- Idle connections split the tail of the largest in-flight chunk, so no worker sits idle at the end.
- Pause/resume survives restarts: the queue and `file.qorvex` sidecars are restored on start
  (turn *Resume unfinished downloads on start* off to restore them paused). Resume is refused when
  the URL, total size, `ETag` or `Last-Modified` changed, so a changed remote file cannot silently
  corrupt output. Cookies are never written to the queue file; authenticated downloads must be
  added again after a restart.
- Every ranged response is checked: status must be `206`, `Content-Range` must start at the
  requested offset and report the same total size.
- A server without Range support gets one stream; pause then restarts from zero (and says so).
- If every network is disabled mid-download the job pauses instead of spinning; re-enable and resume.
- A network that fails repeatedly is dropped for that job; **retry** clears that state so a fixed
  network (VPN reconnect, cable replug) works without restarting Qorvex.
- Filenames are allocated with `O_EXCL`, sanitized, length-limited and checked against Windows
  reserved names (`CON`, `NUL`, `COM1`, …).
- Writes use `pwrite` where available, otherwise a locked seek+write.

## Security model

The server listens on **127.0.0.1 only** and:

- rejects any request whose `Host` is not `127.0.0.1`/`localhost` (blocks DNS rebinding),
- rejects web-page `Origin`s; only its own UI and `chrome-extension://`/`moz-extension://` are allowed,
- requires `Content-Type: application/json` for POST, so a cross-site HTML form cannot drive the API,
- optionally requires a token for extension-origin requests,
- accepts only `http(s)` URLs,
- only opens/reveals paths under the download folder, the state folder or the extension folder,
- caps request bodies at 1 MB.

Anything already running on your PC can still call the API, as with most local download managers.
The UI ships no inline third-party scripts and sets `nosniff`/`no-referrer`.

## Tests

```bash
python -m pytest -q
```

67 tests, most checksum-verified against a real ranged HTTP server:

- bonding across two loopback "interfaces", single network, network disabled mid-flight,
  all networks disabled (pauses, then resumes), pause/resume, resume with a changed URL,
  wrong `Content-Range` rejection and recovery, all-networks-failed then retry, no-Range fallback,
  filename collisions, cancel cleanup, Windows reserved names, queue persistence and restart,
- `MediaJob`: per-network IP-locked URLs, two-part and single-part jobs, part errors, cancel while
  resolving, HLS rejection,
- torrents: magnet/`.torrent` detection, missing-libtorrent errors, progress polling, seed ratio,
  pause/resume, cancel/delete cleanup, per-peer network attribution, upload validation,
- settings validation/bounds/atomic save/unknown-key preservation, clipboard parsing and dedupe,
  autostart helpers, app state redaction,
- HTTP surface: foreign Origin/Host, token enforcement, JSON-only POSTs, settings roundtrip,
  network add/remove, path-limited open, clipboard endpoints, quit.

Torrent tests use a fake libtorrent session so they run without a network or the real package;
CI runs the whole suite on Windows and Linux.

## Honest limits

- **Real multi-NIC speed-up needs real hardware.** Each interface must have its own uplink; a Wi-Fi
  and Ethernet port on the same router often share one WAN and will not add bandwidth. Some servers
  also rate-limit per IP.
- **Real YouTube resolution** depends on yt-dlp, a JS runtime and your cookies; it is not exercised
  by the offline test suite (the tests use a fake resolver with IP-locked URLs).
- The extension is syntax-checked and served by the app, but this build environment has no Chrome/
  Firefox profile to load it into. Hover-button placement may need tweaks on unusual players.
- IPv4 and `http(s)` only. No HLS/DASH muxing, no proxy support, no tray icon.
- Torrents download bonded on outgoing connections; incoming peers and per-network speed caps do
  not apply per-interface, and torrent jobs are not persisted across restarts (finished files are).
- `ffmpeg` is required for merged media; without it video and audio are kept as separate files.
- macOS builds are produced by CI but were not run here.
- Linux desktop window needs Qt (`pip install "pywebview[qt]"`) or GTK webkit; the browser UI works
  everywhere without any of that.

## Layout

```
qorvex/
  config.py      settings spec, validation, atomic JSON store
  engine.py      networks, bound sockets, chunk queue, workers, resume, scheduler
  media.py       per-network yt-dlp resolution, hidden parts, ffmpeg merge
  torrent.py     magnet / .torrent downloads via a shared libtorrent session
  server.py      local HTTP API + UI host, security guards
  app.py         wiring: settings, engine, clipboard, autostart, lifecycle
  ui.html        dashboard (single file, no build step)
  clipboard.py   platform clipboard readers + watcher
  autostart.py   HKCU Run / XDG autostart / LaunchAgent
  system.py      open/reveal files, app id, hide console
  __main__.py    CLI
extension/
  src/           one source for both browsers
  chrome/        built MV3
  firefox/       built MV2
tests/           pytest suite
tools/           extension builder
```
