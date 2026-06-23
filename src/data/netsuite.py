"""NetSuite ERP data accessor.

Two folders:
- `NetSuite/` (capital S) - current snapshot
       * CustomerData.json
       * SalesOrderData.json
       * PurchaseOrderData.json
- `netsuite/history/` (lowercase) - timestamped historical exports

Each JSON file has shape: { "resultCount": N, "result": [...records...] }
"""

from __future__ import annotations

from typing import Any, Optional

from src.data.datalake_client import DataLakeClient
from src.utils.logger import get_logger

_CURRENT_PREFIX = "NetSuite"
_HISTORY_PREFIX = "netsuite/history"

# Map normalized entity name -> filename inside `NetSuite/`.
_ENTITY_FILES: dict[str, str] = {
    "customers": "CustomerData.json",
    "sales_orders": "SalesOrderData.json",
    "purchase_orders": "PurchaseOrderData.json",
}


class NetSuiteError(ValueError):
    """Raised when the caller asks for an unsupported entity."""


class NetSuiteStore:
    """Reader for NetSuite snapshots and history files."""

    def __init__(self, client: DataLakeClient, log_level: str = "INFO") -> None:
        self._client = client
        self._log = get_logger(__name__, log_level)

    def get_current(self, entity: str) -> dict[str, Any]:
        """Return the latest snapshot of one entity.

        Args:
            entity: One of "customers", "sales_orders", "purchase_orders".

        Returns:
            Dict with `count`, `records`, `source` fields.

        Raises:
            NetSuiteError: If `entity` is not a recognised name.
        """
        key = _normalize_entity(entity)
        filename = _ENTITY_FILES[key]
        blob_name = f"{_CURRENT_PREFIX}/{filename}"

        data = self._client.download_json(blob_name)
        records = data.get("result", []) if isinstance(data, dict) else []
        return {
            "entity": key,
            "count": data.get("resultCount", len(records)) if isinstance(data, dict) else len(records),
            "records": records,
            "source": blob_name,
        }

    def list_history(
        self,
        entity: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """List historical snapshot files (timestamped exports).

        Args:
            entity: Optional substring filter (e.g. "customers", "invoices").
            limit: Max number of files to return (most-recent suffix wins
                by lexicographic sort which matches ISO-style timestamps).

        Returns:
            List of `{name, size}` dicts.
        """
        blobs = self._client.list_blobs(_HISTORY_PREFIX)
        if entity:
            needle = entity.lower()
            blobs = [b for b in blobs if needle in b["name"].lower()]
        # Newest-first based on the embedded ISO timestamp in the filename.
        blobs.sort(key=lambda b: b["name"], reverse=True)
        return blobs[:limit]


def _normalize_entity(entity: str) -> str:
    """Normalize an entity string to one of the supported keys."""
    key = entity.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _ENTITY_FILES:
        raise NetSuiteError(
            f"Unknown NetSuite entity: {entity!r}. "
            f"Valid options: {sorted(_ENTITY_FILES)}"
        )
    return key
