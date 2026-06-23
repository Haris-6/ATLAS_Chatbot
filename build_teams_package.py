"""Generate the Teams app package (manifest + icons + zip).

Output: `atlas-teams.zip` at the project root, ready to install via
   Teams -> Apps -> Manage your apps -> Upload a custom app
or via Teams Admin Center for org-wide publishing.

Reads `BOT_APP_ID` from `.env` so the manifest points at the right bot.
Icons are generated programmatically (solid Galaxy-blue color, simple
white center for the outline) so this script has zero external deps
beyond what ATLAS already uses.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import zipfile
import zlib
from pathlib import Path

from dotenv import load_dotenv

from src.utils.console import ensure_utf8_stdout

# App-level GUID identifying THIS Teams app package. Different from the
# bot's App ID. Kept stable across re-runs so Teams treats updates as
# updates rather than a brand-new app.
_APP_GUID = "8c4f3b1d-2a5e-4f6b-9c0d-1e2f3a4b5c6d"

_OUTPUT_DIR = Path("teams_package")
_OUTPUT_ZIP = Path("atlas-teams.zip")

# Galaxy brand-ish blue used for the color icon and accent color.
_BRAND_RGB = (0, 120, 212)


def main() -> int:
    """Build the manifest, generate icons, and zip the package."""
    ensure_utf8_stdout()
    load_dotenv()

    bot_app_id = os.getenv("BOT_APP_ID", "").strip()
    if not bot_app_id:
        print("[ERROR] BOT_APP_ID is missing from .env", file=sys.stderr)
        return 1

    _OUTPUT_DIR.mkdir(exist_ok=True)

    manifest_path = _OUTPUT_DIR / "manifest.json"
    color_icon = _OUTPUT_DIR / "icon-color.png"
    outline_icon = _OUTPUT_DIR / "icon-outline.png"

    manifest_path.write_text(
        json.dumps(_build_manifest(bot_app_id), indent=2),
        encoding="utf-8",
    )
    _write_solid_png(color_icon, 192, 192, (*_BRAND_RGB, 255))
    _write_outline_png(outline_icon, 32)

    with zipfile.ZipFile(_OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in (manifest_path, color_icon, outline_icon):
            zf.write(path, arcname=path.name)

    size_kb = _OUTPUT_ZIP.stat().st_size / 1024
    print(f"Created {_OUTPUT_ZIP} ({size_kb:.1f} KB)")
    print("Contents:")
    print(f"  - manifest.json (bot id: {bot_app_id})")
    print(f"  - icon-color.png (192x192)")
    print(f"  - icon-outline.png (32x32)")
    print()
    print("Next: open Microsoft Teams -> Apps -> Manage your apps")
    print("      -> Upload a custom app -> select atlas-teams.zip")
    return 0


def _build_manifest(bot_app_id: str) -> dict:
    """Construct the v1.16 manifest dict pointing at our bot."""
    return {
        "$schema": (
            "https://developer.microsoft.com/en-us/json-schemas/"
            "teams/v1.16/MicrosoftTeams.schema.json"
        ),
        "manifestVersion": "1.16",
        "version": "0.1.0",
        "id": _APP_GUID,
        "packageName": "com.galaxypharma.atlas",
        "developer": {
            "name": "Galaxy Pharmaceuticals",
            "websiteUrl": "https://galaxypharma.net",
            "privacyUrl": "https://galaxypharma.net/privacy",
            "termsOfUseUrl": "https://galaxypharma.net/terms",
        },
        "icons": {
            "color": "icon-color.png",
            "outline": "icon-outline.png",
        },
        "name": {
            "short": "ATLAS",
            "full": "ATLAS - Galaxy AI Assistant",
        },
        "description": {
            "short": "Galaxy's AI workspace assistant.",
            "full": (
                "ATLAS retrieves information from Galaxy's MasterControl, "
                "NetSuite, Oceaview, and DataNinja systems via chat. "
                "Ask in plain English, get cited answers."
            ),
        },
        "accentColor": "#0078D4",
        "bots": [
            {
                "botId": bot_app_id,
                "scopes": ["personal", "team", "groupchat"],
                "supportsFiles": False,
                "isNotificationOnly": False,
            }
        ],
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": [],
    }


# ---------------------------------------------------------------------------
# Minimal PNG writer (stdlib only)
# ---------------------------------------------------------------------------


def _png_chunk(type_bytes: bytes, data: bytes) -> bytes:
    """Build a single PNG chunk: length, type, data, CRC32."""
    crc = zlib.crc32(type_bytes + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + type_bytes + data + struct.pack(">I", crc)


def _write_png(path: Path, width: int, height: int, rgba_pixels: bytes) -> None:
    """Write an RGBA PNG using only stdlib (zlib + struct)."""
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0),
    )

    # PNG requires a filter byte at the start of each scanline; 0 = none.
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)
        raw.extend(rgba_pixels[y * stride:(y + 1) * stride])

    idat = _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    iend = _png_chunk(b"IEND", b"")
    path.write_bytes(signature + ihdr + idat + iend)


def _write_solid_png(path: Path, width: int, height: int, rgba: tuple) -> None:
    """Write a flat-color PNG of `width x height` filled with `rgba`."""
    pixels = bytes(rgba) * (width * height)
    _write_png(path, width, height, pixels)


def _write_outline_png(path: Path, size: int) -> None:
    """Outline icon: transparent background with a white square in the centre.

    Teams requires the outline icon to be predominantly transparent but NOT
    entirely empty. A simple white block satisfies validation cleanly.
    """
    pixels = bytearray()
    inner_start = size // 4
    inner_end = 3 * size // 4
    for y in range(size):
        for x in range(size):
            if inner_start <= x < inner_end and inner_start <= y < inner_end:
                pixels.extend((255, 255, 255, 255))
            else:
                pixels.extend((0, 0, 0, 0))
    _write_png(path, size, size, bytes(pixels))


if __name__ == "__main__":
    sys.exit(main())
