import hashlib
import json
from datetime import datetime, timezone


class SmartDataSynchronizer:
    def __init__(self, node_id: str = "nas-node-01"):
        self.node_id = node_id
        self.cache_store: dict[str, dict] = {}
        self.vector_clocks: dict[str, dict[str, int]] = {}
        self.sync_log: list[dict] = []

    @staticmethod
    def _compute_checksum(payload: dict) -> str:
        """Compute deterministic SHA-256 checksum for data integrity & differential sync."""
        serialized = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def _tick_vector_clock(self, record_id: str, incoming_clock: dict[str, int] | None = None) -> dict[str, int]:
        """Advance logical Vector Clock for conflict-aware multi-node state tracking."""
        current = dict(self.vector_clocks.get(record_id, {}))

        if incoming_clock:
            for node, counter in incoming_clock.items():
                current[node] = max(current.get(node, 0), int(counter))

        current[self.node_id] = current.get(self.node_id, 0) + 1
        self.vector_clocks[record_id] = current
        return dict(current)

    def _detect_conflict(self, record_id: str, incoming_clock: dict[str, int] | None) -> bool:
        """Check if incoming version is concurrent/divergent compared to cached version."""
        if not incoming_clock or record_id not in self.vector_clocks:
            return False

        local_clock = self.vector_clocks[record_id]
        local_ahead = any(local_clock.get(k, 0) > incoming_clock.get(k, 0) for k in set(local_clock) | set(incoming_clock))
        remote_ahead = any(incoming_clock.get(k, 0) > local_clock.get(k, 0) for k in set(local_clock) | set(incoming_clock))
        return local_ahead and remote_ahead

    def ingest_and_sync(self, record: dict) -> dict:
        """Validate, deduplicate via checksum, resolve conflicts, and cache synchronized record."""
        record_id = str(record.get("id") or "").strip()
        if not record_id:
            raise ValueError("Record must include a non-empty 'id'.")

        data_body = record.get("data", {})
        if not isinstance(data_body, dict):
            raise ValueError("Record 'data' must be a JSON object/dictionary.")

        source = record.get("source", "edge_stream")
        incoming_clock = record.get("vector_clock")
        checksum = self._compute_checksum(data_body)

        existing = self.cache_store.get(record_id)
        conflict_detected = self._detect_conflict(record_id, incoming_clock)

        # Differential check: skip redundant write if checksum matches cached state
        if existing and existing["checksum"] == checksum and not conflict_detected:
            status = "unchanged_cached"
            updated_clock = dict(existing["vector_clock"])
            synced_record = existing
        else:
            updated_clock = self._tick_vector_clock(record_id, incoming_clock)
            merged_data = dict(data_body)

            if conflict_detected and existing:
                # Deterministic tie-breaker: preserve local fields if incoming omits them
                merged_data = {**existing["data"], **data_body, "_conflict_resolved": True}
                status = "conflict_merged"
            elif existing:
                status = "updated_synced"
            else:
                status = "created_synced"

            synced_record = {
                "id": record_id,
                "source": source,
                "data": merged_data,
                "checksum": checksum,
                "vector_clock": updated_clock,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
            self.cache_store[record_id] = synced_record

        event = {
            "record_id": record_id,
            "status": status,
            "conflict_detected": conflict_detected,
            "checksum": checksum[:12],
            "vector_clock": updated_clock,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.sync_log.append(event)
        return {"status": status, "conflict_detected": conflict_detected, "record": synced_record}

    def get_record(self, record_id: str) -> dict | None:
        """Retrieve cached record (works offline from local cache)."""
        return self.cache_store.get(record_id)

    def export_state(self) -> dict:
        """Export current synchronized cache and audit log."""
        return {
            "node_id": self.node_id,
            "total_cached_records": len(self.cache_store),
            "records": list(self.cache_store.values()),
            "sync_log": self.sync_log,
        }


if __name__ == "__main__":
    engine = SmartDataSynchronizer(node_id="ugreen-nas-01")

    batch = [
        {
            "id": "sensor-zone-a",
            "source": "nas_local_ingest",
            "data": {"temperature_c": 27.4, "humidity_pct": 68, "status": "healthy"},
        },
        {
            # Identical payload -> should be detected as unchanged_cached
            "id": "sensor-zone-a",
            "source": "nas_local_ingest",
            "data": {"temperature_c": 27.4, "humidity_pct": 68, "status": "healthy"},
        },
        {
            # Concurrent/divergent vector clock update -> conflict_merged
            "id": "sensor-zone-a",
            "source": "remote_edge_node",
            "vector_clock": {"remote-edge-02": 2, "ugreen-nas-01": 0},
            "data": {"temperature_c": 28.1, "status": "warm_alert"},
        },
    ]

    for item in batch:
        res = engine.ingest_and_sync(item)
        print(f"Record {res['record']['id']} -> {res['status']} (conflict={res['conflict_detected']})")

    print("\nExported Synchronized State:")
    print(json.dumps(engine.export_state(), indent=2))
