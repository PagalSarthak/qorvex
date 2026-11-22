"""Builds extension/chrome (Chrome, Edge, Brave) and extension/firefox from extension/src.

The manifest version is taken from the Python package so the two never drift.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "extension", "src")
FILES = ("background.js", "content.js", "popup.html", "popup.js")

sys.path.insert(0, ROOT)
from qorvex import __version__  # noqa: E402


def build(target: str) -> str:
    out = os.path.join(ROOT, "extension", target)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    for name in FILES:
        shutil.copy(os.path.join(SRC, name), out)
    with open(os.path.join(SRC, f"manifest.{target}.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest["version"] = __version__
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    print(f"built {out} (v{__version__})")
    return out


if __name__ == "__main__":
    for name in ("chrome", "firefox"):
        build(name)
