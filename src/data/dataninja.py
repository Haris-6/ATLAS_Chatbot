"""DataNinja parts / inventory accessor.

The DataNinja export lives at `DataNinja/data.json`. Despite the `.json`
extension, the file is actually JSON Lines - one part record per line.
We load it once and cache the resulting list of records in memory.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from src.data.datalake_client import DataLakeClient
from src.utils.logger import get_logger

_BLOB_NAME = "DataNinja/data.json"


class DataNinjaStore:
    """In-memory reader for the DataNinja data dump."""

    def __init__(self, client: DataLakeClient, log_level: str = "INFO") -> None:
        self._client = client
        self._log = get_logger(__name__, log_level)
        self._cache: Optional[Any] = None

    def warmup(self) -> None:
        """Force the data to load. Safe to call multiple times."""
        self._ensure_loaded()

    def search(self, query: str, limit: int = 20) -> dict[str, Any]:
        """Free-text search across DataNinja records (case-insensitive).

        Args:
            query: Substring to find anywhere in the record JSON.
            limit: Max number of matching records to return.

        Returns:
            Dict with `query`, `matches`, `results`, `source`.
        """
        data = self._ensure_loaded()
        records = _records_of(data)
        q = query.strip().lower()

        results: list[Any] = []
        for record in records:
            haystack = json.dumps(record, default=str).lower()
            if q in haystack:
                results.append(record)
                if len(results) >= limit:
                    break

        return {
            "query": query,
            "matches": len(results),
            "results": results,
            "source": _BLOB_NAME,
        }

    def get_summary(self) -> dict[str, Any]:
        """Return a top-level summary: record count + observed field names."""
        data = self._ensure_loaded()
        records = _records_of(data)
        sample_fields: list[str] = []
        if records and isinstance(records[0], dict):
            sample_fields = list(records[0].keys())
        return {
            "record_count": len(records),
            "fields": sample_fields,
            "source": _BLOB_NAME,
        }

    def _ensure_loaded(self) -> list[dict[str, Any]]:
        """Lazy-load and cache DataNinja records on first access."""
        if self._cache is not None:
            return self._cache

        self._log.info("Loading DataNinja data (JSON Lines)...")
        # The file extension is .json but the content is JSON Lines -
        # one part record per line. download_jsonl skips blank lines and
        # logs (rather than aborts on) malformed lines.
        self._cache = self._client.download_jsonl(_BLOB_NAME)
        self._log.info("DataNinja loaded: %d records", len(self._cache))
        return self._cache


def _records_of(data: Any) -> list[Any]:
    """Normalize the cached data into a list of records.

    `_ensure_loaded` returns a list, but this helper stays defensive
    in case the export format changes back to a single object.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []
