"""
Name: 06_technical_sync_engine.py
Scope: SmartDataSynchronizer technical engine, Vector Clocks, checksums, and verification batch
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, List, Optional


class SmartDataSynchronizer:
    """
    Deterministic edge synchronization, SHA-256 deduplication, Vector Clock
    conflict detection/merging, and local caching engine for UGREEN AI NAS.
    """

    def __init__(self) -> None:
        self.cache_store: Dict[str, Dict[str, Any]] = {}
        self.sync_log: List[Dict[str, Any]] = []

    @staticmethod
    def _compute_checksum(payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _tick_vector_clock(current_clock: Optional[Dict[str, int]], node_id: str) -> Dict[str, int]:
        updated = dict(current_clock) if current_clock else {}
        updated[node_id] = updated.get(node_id, 0) + 1
        return updated

    @staticmethod
    def _detect_conflict(
        existing_clock: Dict[str, int],
        incoming_clock: Dict[str, int],
    ) -> bool:
        all_nodes = set(existing_clock.keys()) | set(incoming_clock.keys())
        existing_ahead = False
        incoming_ahead = False

        for node in all_nodes:
            ex_val = existing_clock.get(node, 0)
            in_val = incoming_clock.get(node, 0)
            if ex_val > in_val:
                existing_ahead = True
            elif in_val > ex_val:
                incoming_ahead = True

        return existing_ahead and incoming_ahead

    @staticmethod
    def _merge_conflict_data(
        existing_payload: Dict[str, Any],
        incoming_payload: Dict[str, Any],
        existing_clock: Dict[str, int],
        incoming_clock: Dict[str, int],
    ) -> Dict[str, Any]:
        merged_payload = deepcopy(existing_payload)
        for key, value in incoming_payload.items():
            merged_payload[key] = deepcopy(value)

        merged_clock: Dict[str, int] = {}
        for node in set(existing_clock.keys()) | set(incoming_clock.keys()):
            merged_clock[node] = max(existing_clock.get(node, 0), incoming_clock.get(node, 0))

        merged_payload["_conflict_resolved"] = True
        merged_payload["_merged_vector_clock"] = merged_clock
        return merged_payload

    def ingest_and_sync(
        self,
        record_id: str,
        node_id: str,
        payload: Dict[str, Any],
        incoming_vector_clock: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        if not record_id or not isinstance(record_id, str):
            raise ValueError("record_id must be a non-empty string.")
        if not node_id or not isinstance(node_id, str):
            raise ValueError("node_id must be a non-empty string.")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dictionary.")

        incoming_checksum = self._compute_checksum(payload)
        timestamp = datetime.now(timezone.utc).isoformat()
        existing_entry = self.cache_store.get(record_id)

        if existing_entry is None:
            new_clock = self._tick_vector_clock(incoming_vector_clock, node_id)
            self.cache_store[record_id] = {
                "record_id": record_id,
                "payload": deepcopy(payload),
                "checksum": incoming_checksum,
                "vector_clock": new_clock,
                "last_node": node_id,
                "updated_at": timestamp,
            }
            status = "created_synced"
            conflict_detected = False
            final_clock = new_clock
            final_checksum = incoming_checksum

        else:
            existing_payload = existing_entry["payload"]
            existing_checksum = existing_entry["checksum"]
            existing_clock = existing_entry["vector_clock"]

            candidate_clock = (
                dict(incoming_vector_clock)
                if incoming_vector_clock is not None
                else dict(existing_clock)
            )
            candidate_clock[node_id] = candidate_clock.get(node_id, 0) + 1

            conflict_detected = self._detect_conflict(existing_clock, candidate_clock)

            if conflict_detected:
                merged_payload = self._merge_conflict_data(
                    existing_payload=existing_payload,
                    incoming_payload=payload,
                    existing_clock=existing_clock,
                    incoming_clock=candidate_clock,
                )
                merged_clock = {
                    node: max(existing_clock.get(node, 0), candidate_clock.get(node, 0))
                    for node in set(existing_clock.keys()) | set(candidate_clock.keys())
                }
                merged_checksum = self._compute_checksum(merged_payload)

                self.cache_store[record_id] = {
                    "record_id": record_id,
                    "payload": merged_payload,
                    "checksum": merged_checksum,
                    "vector_clock": merged_clock,
                    "last_node": node_id,
                    "updated_at": timestamp,
                }
                status = "conflict_merged"
                final_clock = merged_clock
                final_checksum = merged_checksum

            elif incoming_checksum == existing_checksum:
                status = "unchanged_cached"
                final_clock = dict(existing_clock)
                final_checksum = existing_checksum

            else:
                updated_clock = {
                    node: max(existing_clock.get(node, 0), candidate_clock.get(node, 0))
                    for node in set(existing_clock.keys()) | set(candidate_clock.keys())
                }
                self.cache_store[record_id] = {
                    "record_id": record_id,
                    "payload": deepcopy(payload),
                    "checksum": incoming_checksum,
                    "vector_clock": updated_clock,
                    "last_node": node_id,
                    "updated_at": timestamp,
                }
                status = "updated_synced"
                final_clock = updated_clock
                final_checksum = incoming_checksum

        log_entry = {
            "record_id": record_id,
            "node_id": node_id,
            "status": status,
            "conflict_detected": conflict_detected,
            "checksum_prefix": final_checksum[:12],
            "vector_clock": final_clock,
            "timestamp": timestamp,
        }
        self.sync_log.append(log_entry)
        return log_entry

    def get_record(self, record_id: str) -> Optional[Dict[str, Any]]:
        record = self.cache_store.get(record_id)
        return deepcopy(record) if record is not None else None

    def export_state(self) -> Dict[str, Any]:
        return {
            "cache_store": deepcopy(self.cache_store),
            "sync_log": deepcopy(self.sync_log),
        }


def run_verification_batch() -> Dict[str, Any]:
    sync = SmartDataSynchronizer()

    sync.ingest_and_sync(
        record_id="sensor-temp-01",
        node_id="nas-node-01",
        payload={"zone": "rack-a", "temp_c": 38.5},
        incoming_vector_clock={"nas-node-01": 1},
    )

    sync.ingest_and_sync(
        record_id="sensor-temp-01",
        node_id="nas-node-01",
        payload={"temp_c": 38.5, "zone": "rack-a"},
        incoming_vector_clock={"nas-node-01": 1},
    )

    sync.ingest_and_sync(
        record_id="sensor-temp-01",
        node_id="remote-edge-02",
        payload={"temp_c": 41.2, "alert": "elevated_thermal"},
        incoming_vector_clock={"remote-edge-02": 2},
    )

    return sync.export_state()


if __name__ == "__main__":
    exported = run_verification_batch()
    print(json.dumps(exported, indent=2, ensure_ascii=False))
