"""Discover the structure of Galaxy's Data Lake (`raw-data` container).

Connects with `AZURE_STORAGE_CONNECTION_STRING` from `.env`, walks every
blob under `raw-data/`, and prints a tree grouped by top-level folder.
For each folder it lists file names, sizes, and extensions, plus a tally.

Read-only - the script only LISTs blobs, never reads, writes, or deletes.

Run from the `atlas/` directory:

    python discover_datalake.py
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict

from azure.core.exceptions import AzureError
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

from src.utils.console import ensure_utf8_stdout

_CONTAINER = "raw-data"
_MAX_FILES_PER_FOLDER = 15  # truncate noisy folders to keep output readable


def main() -> int:
    """Walk the data lake and print a structured summary. Returns shell exit code."""
    ensure_utf8_stdout()
    load_dotenv()

    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if not conn_str:
        print(
            "[ERROR] AZURE_STORAGE_CONNECTION_STRING is missing from .env",
            file=sys.stderr,
        )
        return 1

    try:
        service = BlobServiceClient.from_connection_string(conn_str)
        container = service.get_container_client(_CONTAINER)
        blobs = list(container.list_blobs())
    except AzureError as exc:
        print(f"[AZURE ERROR] Could not list blobs: {exc}", file=sys.stderr)
        return 1

    if not blobs:
        print(f"Container '{_CONTAINER}' is empty.")
        return 0

    # Group by top-level folder and collect per-extension stats.
    by_folder: dict[str, list[tuple[str, int]]] = defaultdict(list)
    by_extension: dict[str, int] = defaultdict(int)
    total_size = 0

    for blob in blobs:
        name = blob.name
        size = blob.size or 0
        top = name.split("/", 1)[0] if "/" in name else "(root)"
        by_folder[top].append((name, size))
        by_extension[_extension_of(name)] += 1
        total_size += size

    _print_summary(by_folder, by_extension, total_size, blob_count=len(blobs))
    return 0


def _extension_of(name: str) -> str:
    """Return a lowercase file extension, or '(no ext)' if none."""
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return "(no ext)"
    return "." + base.rsplit(".", 1)[-1].lower()


def _print_summary(
    by_folder: dict[str, list[tuple[str, int]]],
    by_extension: dict[str, int],
    total_size: int,
    blob_count: int,
) -> None:
    """Pretty-print the discovery tree + summary statistics."""
    print("=" * 78)
    print(f"Galaxy Data Lake — container '{_CONTAINER}'")
    print("=" * 78)

    for folder in sorted(by_folder):
        files = sorted(by_folder[folder], key=lambda kv: kv[0])
        folder_size_mb = sum(s for _, s in files) / (1024 * 1024)
        print(f"\n[FOLDER] {folder}/  ({len(files)} files, {folder_size_mb:.2f} MB)")

        for name, size in files[:_MAX_FILES_PER_FOLDER]:
            rel = name[len(folder) + 1:] if name.startswith(folder + "/") else name
            size_kb = size / 1024
            print(f"    {rel}   [{size_kb:,.1f} KB]")

        if len(files) > _MAX_FILES_PER_FOLDER:
            extra = len(files) - _MAX_FILES_PER_FOLDER
            print(f"    ... and {extra} more")

    print("\n" + "=" * 78)
    print("Summary")
    print("=" * 78)
    print(f"Total files: {blob_count}")
    print(f"Total size:  {total_size / (1024 * 1024):.2f} MB")
    print("By extension:")
    for ext, count in sorted(by_extension.items(), key=lambda kv: -kv[1]):
        print(f"   {ext:<12} {count} file(s)")


if __name__ == "__main__":
    sys.exit(main())
