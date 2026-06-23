"""Build a Linux-friendly deployment zip for Azure App Service.

PowerShell's `Compress-Archive` writes zip entries with Windows backslashes
(`src\\bot\\atlas_bot.py`) which Linux treats as a single filename with
literal backslashes, breaking the `src` package import. Python's `zipfile`
uses POSIX forward slashes, which is what Azure's Oryx + gunicorn need.

Includes: bot_server.py, hello.py, requirements.txt, startup.sh, src/**
Excludes: __pycache__, .pyc files, .venv, .env, tests, build artifacts
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

_OUTPUT = Path("atlas-deploy.zip")

_TOP_LEVEL_FILES = (
    "bot_server.py",
    "hello.py",
    "requirements.txt",
    "startup.sh",
)
_SRC_DIR = Path("src")

_EXCLUDED_DIRS = {"__pycache__"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def main() -> int:
    """Build the deployment zip with forward-slash entry paths."""
    if _OUTPUT.exists():
        _OUTPUT.unlink()

    file_count = 0
    with zipfile.ZipFile(_OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in _TOP_LEVEL_FILES:
            path = Path(name)
            if not path.exists():
                print(f"[WARN] missing: {name}", file=sys.stderr)
                continue
            zf.write(path, arcname=name)
            file_count += 1

        if _SRC_DIR.exists():
            for root, dirs, files in os.walk(_SRC_DIR):
                dirs[:] = [d for d in dirs if d not in _EXCLUDED_DIRS]
                for filename in files:
                    if Path(filename).suffix in _EXCLUDED_SUFFIXES:
                        continue
                    abs_path = Path(root) / filename
                    # arcname uses forward slashes so Linux treats the
                    # entries as a real directory hierarchy.
                    arc = abs_path.as_posix()
                    zf.write(abs_path, arcname=arc)
                    file_count += 1

    size_kb = _OUTPUT.stat().st_size / 1024
    print(f"Built {_OUTPUT} - {file_count} files, {size_kb:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
