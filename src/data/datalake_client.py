"""Shared Azure Data Lake (Blob Storage) client for ATLAS.

Wraps `BlobServiceClient` with format-aware helpers that handle the quirks
discovered during data-lake exploration:
  - UTF-8 BOM in some files (`utf-8-sig` decoder)
  - JSON Lines format (one record per line) in oceaview sensor data
  - Virtual folder entries with size=0 (skipped during listing)

All operations are read-only. There is no `upload` / `delete` API here on
purpose - ATLAS never writes to the data lake.
"""

from __future__ import annotations

import json
from typing import Any

from azure.storage.blob import BlobServiceClient, ContainerClient

from src.utils.logger import get_logger


class DataLakeError(Exception):
    """Raised when a data lake operation fails in a non-retryable way."""


class DataLakeClient:
    """Read-only client over a single ADLS container."""

    def __init__(
        self,
        connection_string: str,
        container_name: str = "raw-data",
        log_level: str = "INFO",
    ) -> None:
        """Initialize the client.

        Args:
            connection_string: Azure Storage connection string from `.env`.
            container_name: Container under the storage account to read.
            log_level: Logger verbosity.

        Raises:
            DataLakeError: If `connection_string` is empty.
        """
        if not connection_string:
            raise DataLakeError(
                "AZURE_STORAGE_CONNECTION_STRING is required for data tools."
            )
        self._service = BlobServiceClient.from_connection_string(connection_string)
        self._container: ContainerClient = self._service.get_container_client(
            container_name
        )
        self._log = get_logger(__name__, log_level)

    @property
    def container(self) -> ContainerClient:
        """Direct access to the underlying ContainerClient for advanced ops."""
        return self._container

    def list_blobs(self, prefix: str) -> list[dict[str, Any]]:
        """List non-empty blobs whose names start with `prefix/`.

        Args:
            prefix: Folder path inside the container (e.g. "mastercontrol").

        Returns:
            List of `{"name": str, "size": int}` dicts, sorted by name.
        """
        normalized = prefix.rstrip("/") + "/"
        blobs = [
            {"name": b.name, "size": b.size or 0}
            for b in self._container.list_blobs(name_starts_with=normalized)
            if (b.size or 0) > 0
        ]
        blobs.sort(key=lambda b: b["name"])
        return blobs

    def download_text(self, blob_name: str) -> str:
        """Download `blob_name` and return its content as UTF-8 text.

        `utf-8-sig` is used to transparently strip any byte-order mark.
        """
        raw = self._container.download_blob(blob_name).readall()
        return raw.decode("utf-8-sig", errors="replace")

    def download_json(self, blob_name: str) -> Any:
        """Download and parse a JSON blob (BOM-safe).

        Returns:
            The parsed JSON value (dict, list, or scalar).

        Raises:
            json.JSONDecodeError: If the content is not valid JSON.
        """
        return json.loads(self.download_text(blob_name))

    def download_jsonl(self, blob_name: str) -> list[dict[str, Any]]:
        """Download a JSON Lines blob and return a list of parsed records.

        Blank lines are skipped. Malformed lines are logged and skipped
        rather than aborting the whole batch.
        """
        text = self.download_text(blob_name)
        records: list[dict[str, Any]] = []
        for line_num, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                self._log.warning(
                    "Skipping malformed JSONL line %d in %s: %s",
                    line_num, blob_name, exc,
                )
        return records
