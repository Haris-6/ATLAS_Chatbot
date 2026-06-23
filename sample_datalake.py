"""Sample one JSON file from each top-level folder of Galaxy's data lake.

Goal: see the schema/shape of each data source so we can design tools.
Downloads one blob per source folder, parses the JSON, and prints a
truncated, pretty-formatted preview plus the top-level keys.

Run from the `atlas/` directory:

    python sample_datalake.py
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

from azure.core.exceptions import AzureError
from azure.storage.blob import BlobServiceClient, ContainerClient
from dotenv import load_dotenv

from src.utils.console import ensure_utf8_stdout

_CONTAINER = "raw-data"
_TOP_FOLDERS = ["DataNinja", "NetSuite", "netsuite", "mastercontrol", "oceaview"]
_PREVIEW_CHARS = 1200  # truncate body preview to keep output readable


def main() -> int:
    """Sample one file from each source folder. Returns shell exit code."""
    ensure_utf8_stdout()
    load_dotenv()

    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn_str:
        print("[ERROR] AZURE_STORAGE_CONNECTION_STRING is missing.", file=sys.stderr)
        return 1

    try:
        service = BlobServiceClient.from_connection_string(conn_str)
        container = service.get_container_client(_CONTAINER)
    except AzureError as exc:
        print(f"[AZURE ERROR] {exc}", file=sys.stderr)
        return 1

    failures = 0
    for folder in _TOP_FOLDERS:
        ok = _sample_folder(container, folder)
        if not ok:
            failures += 1

    print(f"\nSampled {len(_TOP_FOLDERS) - failures}/{len(_TOP_FOLDERS)} folders successfully.")
    return 0 if failures == 0 else 1


def _sample_folder(container: ContainerClient, folder: str) -> bool:
    """Find and print the first JSON blob inside `folder`. Returns success bool."""
    print("\n" + "=" * 78)
    print(f"SOURCE: {folder}/")
    print("=" * 78)

    blob = _first_real_json_blob(container, folder)
    if blob is None:
        print(f"   (no .json files found)")
        return False

    print(f"Sampled blob: {blob['name']}  ({blob['size']:,} bytes)")

    try:
        data = container.download_blob(blob["name"]).readall()
        text = data.decode("utf-8", errors="replace")
    except AzureError as exc:
        print(f"   [download error] {exc}")
        return False

    parsed: Optional[Any]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        parsed = None
        print(f"   [JSON parse warning] {exc} - showing raw text instead")

    if parsed is not None:
        _print_json_shape(parsed)
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False, default=str)
        _print_truncated("Pretty preview", pretty)
    else:
        _print_truncated("Raw preview", text)

    return True


def _first_real_json_blob(
    container: ContainerClient,
    folder: str,
) -> Optional[dict[str, Any]]:
    """Return the first non-empty .json blob inside `folder/`.

    Azure shows virtual folder entries with size=0; we skip those.
    """
    prefix = folder + "/"
    for blob in container.list_blobs(name_starts_with=prefix):
        if blob.size and blob.size > 0 and blob.name.lower().endswith(".json"):
            return {"name": blob.name, "size": blob.size}
    return None


def _print_json_shape(parsed: Any) -> None:
    """Print a one-line shape hint about the parsed JSON."""
    if isinstance(parsed, dict):
        keys = list(parsed.keys())
        print(f"   Top-level type: dict with {len(keys)} keys: {keys[:12]}"
              + (" ..." if len(keys) > 12 else ""))
    elif isinstance(parsed, list):
        sample_type = type(parsed[0]).__name__ if parsed else "(empty)"
        print(f"   Top-level type: list of {len(parsed)} items (first item: {sample_type})")
        if parsed and isinstance(parsed[0], dict):
            keys = list(parsed[0].keys())
            print(f"   First item keys: {keys[:12]}"
                  + (" ..." if len(keys) > 12 else ""))
    else:
        print(f"   Top-level type: {type(parsed).__name__}")


def _print_truncated(label: str, text: str) -> None:
    """Print `text` truncated to `_PREVIEW_CHARS` chars with a clear marker."""
    print(f"   --- {label} ---")
    if len(text) <= _PREVIEW_CHARS:
        print(text)
    else:
        print(text[:_PREVIEW_CHARS])
        print(f"\n   ... [truncated, {len(text) - _PREVIEW_CHARS:,} more chars]")


if __name__ == "__main__":
    sys.exit(main())
