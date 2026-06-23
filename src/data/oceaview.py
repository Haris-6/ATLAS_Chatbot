"""Oceaview environmental sensor data.

Sensor data is stored as JSON Lines under Hive-style date partitions:

    oceaview/<Sensor>/year=YYYY/month=MM/day=DD/<filename>.json

Sensors (folder casing varies in the lake):
- Humidity   (unit: %RH)
- Pressure
- temperature

Each line is: { "index": N, "date": "ISO datetime", "value": float, "unit": str }
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timezone
from typing import Any, Optional

# Python 3.10's datetime.fromisoformat caps fractional seconds at 6 digits,
# but the sensor data carries 7-digit precision (e.g. ".0000000Z"). We trim
# anything past 6 digits before parsing so the values round-trip cleanly.
_FRACTIONAL_SECONDS_RE = re.compile(r"\.(\d{6})\d+")

from src.data.datalake_client import DataLakeClient
from src.utils.logger import get_logger

# Map user-supplied sensor name (lowercase) -> actual folder name in ADLS.
_SENSOR_FOLDERS: dict[str, str] = {
    "humidity": "Humidity",
    "pressure": "Pressure",
    "temperature": "temperature",
}


class OceaviewError(ValueError):
    """Raised when an invalid sensor name is supplied."""


class OceaviewStore:
    """Reader for Oceaview sensor timeseries data."""

    def __init__(self, client: DataLakeClient, log_level: str = "INFO") -> None:
        self._client = client
        self._log = get_logger(__name__, log_level)

    def get_readings(
        self,
        sensor: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        sample_limit: int = 100,
    ) -> dict[str, Any]:
        """Fetch readings for one sensor, optionally bounded by date.

        Args:
            sensor: One of "humidity", "pressure", "temperature".
            date_from: Inclusive lower bound (YYYY-MM-DD or ISO 8601).
            date_to: Inclusive upper bound (YYYY-MM-DD or ISO 8601).
            sample_limit: Max number of raw readings to return in the
                response. Aggregate stats are computed over ALL readings,
                not just the sample.

        Returns:
            Dict with sensor info, total count, min/max/mean stats,
            and a truncated sample of readings.

        Raises:
            OceaviewError: If `sensor` is not a recognised name.
        """
        key = _normalize_sensor(sensor)
        folder = f"oceaview/{_SENSOR_FOLDERS[key]}"

        df = _parse_date(date_from) if date_from else None
        dt = _parse_date(date_to) if date_to else None

        blobs = self._client.list_blobs(folder)

        all_readings: list[dict[str, Any]] = []
        for blob in blobs:
            try:
                records = self._client.download_jsonl(blob["name"])
            except Exception as exc:  # noqa: BLE001 - skip bad file, continue
                self._log.warning("Failed to read %s: %s", blob["name"], exc)
                continue
            for rec in records:
                if not _in_range(rec.get("date"), df, dt):
                    continue
                all_readings.append(rec)

        all_readings.sort(key=lambda r: r.get("date", ""))

        values = [
            r["value"] for r in all_readings
            if isinstance(r.get("value"), (int, float))
        ]
        stats: dict[str, Any] = {}
        if values:
            unit = next(
                (r.get("unit", "") for r in all_readings if r.get("unit")),
                "",
            )
            stats = {
                "min": min(values),
                "max": max(values),
                "mean": round(statistics.mean(values), 4),
                "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
                "count": len(values),
                "unit": unit,
            }

        return {
            "sensor": key,
            "date_from": date_from,
            "date_to": date_to,
            "total_readings": len(all_readings),
            "stats": stats,
            "sample_readings": all_readings[:sample_limit],
            "source": folder,
        }


def _normalize_sensor(sensor: str) -> str:
    """Validate and normalize the sensor name."""
    key = sensor.strip().lower()
    if key not in _SENSOR_FOLDERS:
        raise OceaviewError(
            f"Unknown sensor: {sensor!r}. Valid: {sorted(_SENSOR_FOLDERS)}"
        )
    return key


def _parse_date(raw: str) -> datetime:
    """Parse YYYY-MM-DD or ISO 8601 into a timezone-aware datetime (UTC)."""
    s = _normalize_iso(raw.strip())
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.strptime(s, "%Y-%m-%d")
    except ValueError as exc:
        raise OceaviewError(
            f"Invalid date {raw!r}. Use YYYY-MM-DD or ISO 8601 (e.g. 2026-05-23)."
        ) from exc
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _in_range(
    iso_date: Optional[str],
    df: Optional[datetime],
    dt: Optional[datetime],
) -> bool:
    """Check whether `iso_date` is between `df` and `dt` (both inclusive)."""
    if df is None and dt is None:
        return True
    if not iso_date:
        return False
    try:
        d = datetime.fromisoformat(_normalize_iso(str(iso_date)))
    except ValueError:
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    if df is not None and d < df:
        return False
    if dt is not None and d > dt:
        return False
    return True


def _normalize_iso(s: str) -> str:
    """Adjust an ISO 8601 string so Python 3.10's `fromisoformat` accepts it.

    - Replaces a trailing `Z` with `+00:00`.
    - Truncates fractional seconds beyond 6 digits (.NNNNNNN -> .NNNNNN).
    """
    return _FRACTIONAL_SECONDS_RE.sub(r".\1", s.replace("Z", "+00:00"))
