from __future__ import annotations

import json
from python_smart_data_manager import SmartDataSynchronizer


def run_verification_batch() -> dict[str, object]:
    """Execute sample batch demonstrating created_synced, unchanged_cached, and conflict_merged."""
    syncer = SmartDataSynchronizer(default_node_id="nas-node-01")

    # 1. Create initial record on nas-node-01
    step_1 = syncer.ingest_and_sync(
        record_id="sensor-batch-001",
        payload={"temperature": 26.5, "status": "ok", "zone": "rack-a"},
        node_id="nas-node-01",
    )

    # 2. Re-submit identical payload (different key ordering to verify canonical checksum)
    step_2 = syncer.ingest_and_sync(
        record_id="sensor-batch-001",
        payload={"zone": "rack-a", "status": "ok", "temperature": 26.5},
        node_id="nas-node-01",
    )

    # 3. Advance nas-node-01 again, then simulate divergent clock from remote-edge-02
    syncer.ingest_and_sync(
        record_id="sensor-batch-001",
        payload={"temperature": 27.0, "status": "ok", "zone": "rack-a"},
        node_id="nas-node-01",
    )
    step_3_conflict = syncer.ingest_and_sync(
        record_id="sensor-batch-001",
        payload={"temperature": 29.2, "status": "alert", "edge_flag": True},
        node_id="remote-edge-02",
        incoming_clock={"nas-node-01": 1, "remote-edge-02": 2},
    )

    cached_record = syncer.get_record("sensor-batch-001")
    exported = syncer.export_state()

    return {
        "step_1_create": step_1["status"],
        "step_2_dedup": step_2["status"],
        "step_3_conflict": step_3_conflict["status"],
        "conflict_flag_in_cache": bool(cached_record and cached_record["data"].get("_conflict_resolved")),
        "log_count": len(exported["sync_log"]),
        "exported_state": exported,
    }


if __name__ == "__main__":
    result = run_verification_batch()
    print(json.dumps(result, indent=2, ensure_ascii=False))
