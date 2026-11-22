from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any


class SmartDataSynchronizer:
    """Edge-oriented synchronization, deduplication, and conflict-handling engine."""

    def __init__(self, default_node_id: str = "nas-node-01") -> None:
        if not isinstance(default_node_id, str) or not default_node_id.strip():
            raise ValueError("default_node_id must be a non-empty string.")
        self.default_node_id: str = default_node_id.strip()
        self.cache_store: dict[str, dict[str, Any]] = {}
        self.vector_clocks: dict[str, dict[str, int]] = {}
        self.sync_log: list[dict[str, Any]] = []

    def _compute_checksum(self, payload: Any) -> str:
        """Compute deterministic SHA-256 checksum using canonical JSON serialization."""
        canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _tick_vector_clock(
        self,
        record_id: str,
        node_id: str,
        incoming_clock: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """Advance and merge logical Vector Clock counters for a record."""
        current = dict(self.vector_clocks.get(record_id, {}))

        if isinstance(incoming_clock, dict):
            for peer_node, count in incoming_clock.items():
                if isinstance(peer_node, str) and isinstance(count, int) and count >= 0:
                    current[peer_node] = max(current.get(peer_node, 0), count)

        current[node_id] = current.get(node_id, 0) + 1
        self.vector_clocks[record_id] = current
        return dict(current)

    def _detect_conflict(
        self,
        existing_clock: dict[str, int],
        incoming_clock: dict[str, int] | None,
    ) -> bool:
        """Detect concurrent divergence across Vector Clocks when both sides advance."""
        if not existing_clock or not isinstance(incoming_clock, dict) or not incoming_clock:
            return False

        all_nodes = set(existing_clock.keys()) | {
            k for k, v in incoming_clock.items() if isinstance(k, str) and isinstance(v, int)
        }
        existing_ahead = False
        incoming_ahead = False

        for node in all_nodes:
            ex_val = existing_clock.get(node, 0)
            in_val = incoming_clock.get(node, 0) if isinstance(incoming_clock.get(node, 0), int) else 0
            if ex_val > in_val:
                existing_ahead = True
            elif in_val > ex_val:
                incoming_ahead = True

        return existing_ahead and incoming_ahead

    def _merge_conflict_data(
        self,
        existing_payload: dict[str, Any],
        incoming_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Deterministically merge conflicting dictionaries while marking resolution."""
        merged = dict(existing_payload)
        for key in sorted(incoming_payload.keys()):
            val = incoming_payload[key]
            if key in merged and merged[key] != val:
                merged[key] = val
            elif key not in merged:
                merged[key] = val
        merged["_conflict_resolved"] = True
        return merged

    def ingest_and_sync(
        self,
        record_id: str,
        payload: dict[str, Any],
        node_id: str | None = None,
        incoming_clock: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Validate, deduplicate, detect/merge conflicts, cache, and log a record."""
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("record_id must be a non-empty string.")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dictionary.")

        active_node = (
            node_id.strip()
            if isinstance(node_id, str) and node_id.strip()
            else self.default_node_id
        )
        checksum = self._compute_checksum(payload)
        previous_entry = self.cache_store.get(record_id)
        previous_clock = dict(self.vector_clocks.get(record_id, {}))

        conflict_detected = (
            previous_entry is not None
            and self._detect_conflict(previous_clock, incoming_clock)
        )

        if (
            previous_entry is not None
            and not conflict_detected
            and previous_entry.get("checksum") == checksum
        ):
            updated_clock = self._tick_vector_clock(record_id, active_node, incoming_clock)
            status = "unchanged_cached"
            final_payload = dict(previous_entry["data"])
            final_checksum = previous_entry["checksum"]
        elif conflict_detected and previous_entry is not None:
            merged_payload = self._merge_conflict_data(previous_entry["data"], payload)
            updated_clock = self._tick_vector_clock(record_id, active_node, incoming_clock)
            final_checksum = self._compute_checksum(merged_payload)
            status = "conflict_merged"
            final_payload = merged_payload
            self.cache_store[record_id] = {
                "record_id": record_id,
                "data": dict(final_payload),
                "checksum": final_checksum,
                "node_id": active_node,
                "vector_clock": dict(updated_clock),
            }
        else:
            updated_clock = self._tick_vector_clock(record_id, active_node, incoming_clock)
            final_checksum = checksum
            status = "created_synced" if previous_entry is None else "updated_synced"
            final_payload = dict(payload)
            self.cache_store[record_id] = {
                "record_id": record_id,
                "data": dict(final_payload),
                "checksum": final_checksum,
                "node_id": active_node,
                "vector_clock": dict(updated_clock),
            }

        event = {
            "record_id": record_id,
            "status": status,
            "conflict_detected": conflict_detected,
            "checksum_prefix": final_checksum[:12],
            "vector_clock": dict(updated_clock),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.sync_log.append(event)

        return {
            "status": status,
            "record_id": record_id,
            "conflict_detected": conflict_detected,
            "checksum": final_checksum,
            "vector_clock": dict(updated_clock),
            "data": dict(final_payload),
        }

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        """Retrieve a cached record by ID if present."""
        entry = self.cache_store.get(record_id)
        if entry is None:
            return None
        return {
            "record_id": entry["record_id"],
            "data": dict(entry["data"]),
            "checksum": entry["checksum"],
            "node_id": entry["node_id"],
            "vector_clock": dict(entry["vector_clock"]),
        }

    def export_state(self) -> dict[str, Any]:
        """Export current cached records and full structured audit log."""
        return {
            "default_node_id": self.default_node_id,
            "cache_store": {
                k: {
                    "record_id": v["record_id"],
                    "data": dict(v["data"]),
                    "checksum": v["checksum"],
                    "node_id": v["node_id"],
                    "vector_clock": dict(v["vector_clock"]),
                }
                for k, v in self.cache_store.items()
            },
            "sync_log": [
                {
                    **item,
                    "vector_clock": dict(item["vector_clock"]),
                }
                for item in self.sync_log
            ],
        }
