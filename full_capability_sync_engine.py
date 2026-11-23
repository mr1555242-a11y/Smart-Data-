from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import socket
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple
import unittest


class AtomicDiskStorage:
    """
    Provides crash-safe atomic JSON persistence on disk using temporary files
    and atomic rename replacement.
    """

    def __init__(self, filepath: str) -> None:
        self.filepath = filepath

    def save_state(self, state: Dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self.filepath))
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".smart_data_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.filepath)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def load_state(self) -> Optional[Dict[str, Any]]:
        if not os.path.exists(self.filepath):
            return None
        with open(self.filepath, "r", encoding="utf-8") as handle:
            return json.load(handle)


class SmartDataCoreEngine:
    """
    Core synchronization engine with canonical SHA-256 deduplication,
    Vector Clock conflict detection/merging, atomic disk persistence,
    and network message synchronization.
    """

    def __init__(self, node_id: str, storage: Optional[AtomicDiskStorage] = None) -> None:
        if not node_id or not isinstance(node_id, str):
            raise ValueError("node_id must be a non-empty string")
        self.node_id = node_id
        self.storage = storage
        self.cache_store: Dict[str, Dict[str, Any]] = {}
        self.sync_log: List[Dict[str, Any]] = []

        if self.storage is not None:
            loaded = self.storage.load_state()
            if loaded is not None:
                self.cache_store = loaded.get("cache_store", {})
                self.sync_log = loaded.get("sync_log", [])

    @staticmethod
    def compute_checksum(payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def detect_conflict(existing_clock: Dict[str, int], incoming_clock: Dict[str, int]) -> bool:
        all_nodes = set(existing_clock.keys()) | set(incoming_clock.keys())
        existing_ahead = any(existing_clock.get(n, 0) > incoming_clock.get(n, 0) for n in all_nodes)
        incoming_ahead = any(incoming_clock.get(n, 0) > existing_clock.get(n, 0) for n in all_nodes)
        return existing_ahead and incoming_ahead

    @staticmethod
    def merge_payloads(
        existing_payload: Dict[str, Any],
        incoming_payload: Dict[str, Any],
        merged_clock: Dict[str, int],
    ) -> Dict[str, Any]:
        merged = deepcopy(existing_payload)
        for key, value in incoming_payload.items():
            merged[key] = deepcopy(value)
        merged["_conflict_resolved"] = True
        merged["_merged_vector_clock"] = merged_clock
        return merged

    def _persist_if_configured(self) -> None:
        if self.storage is not None:
            self.storage.save_state(
                {
                    "node_id": self.node_id,
                    "cache_store": self.cache_store,
                    "sync_log": self.sync_log,
                }
            )

    def ingest_record(
        self,
        record_id: str,
        source_node_id: str,
        payload: Dict[str, Any],
        incoming_clock: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        if not record_id or not isinstance(record_id, str):
            raise ValueError("record_id must be a non-empty string")
        if not source_node_id or not isinstance(source_node_id, str):
            raise ValueError("source_node_id must be a non-empty string")
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dictionary")

        checksum = self.compute_checksum(payload)
        timestamp = datetime.now(timezone.utc).isoformat()
        existing = self.cache_store.get(record_id)

        if existing is None:
            clock = dict(incoming_clock) if incoming_clock else {}
            clock[source_node_id] = clock.get(source_node_id, 0) + 1
            self.cache_store[record_id] = {
                "record_id": record_id,
                "payload": deepcopy(payload),
                "checksum": checksum,
                "vector_clock": clock,
                "last_node": source_node_id,
                "updated_at": timestamp,
            }
            status = "created_synced"
            conflict_detected = False
            final_clock = clock
            final_checksum = checksum
        else:
            ex_payload = existing["payload"]
            ex_checksum = existing["checksum"]
            ex_clock = existing["vector_clock"]

            cand_clock = dict(incoming_clock) if incoming_clock else dict(ex_clock)
            cand_clock[source_node_id] = cand_clock.get(source_node_id, 0) + 1
            conflict_detected = self.detect_conflict(ex_clock, cand_clock)

            if conflict_detected:
                merged_clock = {
                    n: max(ex_clock.get(n, 0), cand_clock.get(n, 0))
                    for n in set(ex_clock.keys()) | set(cand_clock.keys())
                }
                merged_payload = self.merge_payloads(ex_payload, payload, merged_clock)
                merged_checksum = self.compute_checksum(merged_payload)
                self.cache_store[record_id] = {
                    "record_id": record_id,
                    "payload": merged_payload,
                    "checksum": merged_checksum,
                    "vector_clock": merged_clock,
                    "last_node": source_node_id,
                    "updated_at": timestamp,
                }
                status = "conflict_merged"
                final_clock = merged_clock
                final_checksum = merged_checksum
            elif checksum == ex_checksum:
                status = "unchanged_cached"
                final_clock = dict(ex_clock)
                final_checksum = ex_checksum
            else:
                upd_clock = {
                    n: max(ex_clock.get(n, 0), cand_clock.get(n, 0))
                    for n in set(ex_clock.keys()) | set(cand_clock.keys())
                }
                self.cache_store[record_id] = {
                    "record_id": record_id,
                    "payload": deepcopy(payload),
                    "checksum": checksum,
                    "vector_clock": upd_clock,
                    "last_node": source_node_id,
                    "updated_at": timestamp,
                }
                status = "updated_synced"
                final_clock = upd_clock
                final_checksum = checksum

        event = {
            "record_id": record_id,
            "node_id": source_node_id,
            "status": status,
            "conflict_detected": conflict_detected,
            "checksum_prefix": final_checksum[:12],
            "vector_clock": final_clock,
            "timestamp": timestamp,
        }
        self.sync_log.append(event)
        self._persist_if_configured()
        return event

    def build_sync_batch(self) -> List[Dict[str, Any]]:
        batch: List[Dict[str, Any]] = []
        for record_id, entry in self.cache_store.items():
            batch.append(
                {
                    "record_id": record_id,
                    "source_node_id": entry["last_node"],
                    "payload": deepcopy(entry["payload"]),
                    "vector_clock": deepcopy(entry["vector_clock"]),
                }
            )
        return batch

    def apply_remote_batch(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for msg in messages:
            ev = self.ingest_record(
                record_id=msg["record_id"],
                source_node_id=msg["source_node_id"],
                payload=msg["payload"],
                incoming_clock=msg.get("vector_clock"),
            )
            events.append(ev)
        return events


class TCPNetworkSyncServer:
    """
    Lightweight TCP socket server that receives JSON synchronization batches
    and applies them to a SmartDataCoreEngine instance.
    """

    def __init__(self, engine: SmartDataCoreEngine, host: str = "127.0.0.1", port: int = 0) -> None:
        self.engine = engine
        self.host = host
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, port))
        self._sock.listen(5)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _serve_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                chunks: List[bytes] = []
                while True:
                    data = conn.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
                if chunks:
                    raw = b"".join(chunks).decode("utf-8")
                    messages = json.loads(raw)
                    applied = self.engine.apply_remote_batch(messages)
                    response = json.dumps({"applied_count": len(applied), "events": applied}).encode("utf-8")
                    conn.sendall(response)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def send_sync_batch_over_tcp(host: str, port: int, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload_bytes = json.dumps(messages, ensure_ascii=False).encode("utf-8")
    with socket.create_connection((host, port), timeout=2.0) as client:
        client.sendall(payload_bytes)
        client.shutdown(socket.SHUT_WR)
        resp_chunks: List[bytes] = []
        while True:
            part = client.recv(65536)
            if not part:
                break
            resp_chunks.append(part)
    return json.loads(b"".join(resp_chunks).decode("utf-8"))


class ComprehensiveSmartDataTests(unittest.TestCase):
    def test_end_to_end_persistence_and_tcp_conflict_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            file_a = os.path.join(tmpdir, "node_a_state.json")
            file_b = os.path.join(tmpdir, "node_b_state.json")

            engine_a = SmartDataCoreEngine("nas-node-01", AtomicDiskStorage(file_a))
            engine_b = SmartDataCoreEngine("remote-edge-02", AtomicDiskStorage(file_b))

            e1 = engine_a.ingest_record("rec-100", "nas-node-01", {"temp": 37.0, "zone": "edge-1"}, {"nas-node-01": 1})
            self.assertEqual(e1["status"], "created_synced")

            e2 = engine_a.ingest_record("rec-100", "nas-node-01", {"zone": "edge-1", "temp": 37.0}, {"nas-node-01": 1})
            self.assertEqual(e2["status"], "unchanged_cached")

            engine_b.ingest_record("rec-100", "remote-edge-02", {"temp": 42.5, "alert": "high"}, {"remote-edge-02": 2})

            server = TCPNetworkSyncServer(engine_a)
            server.start()
            try:
                ack = send_sync_batch_over_tcp(server.host, server.port, engine_b.build_sync_batch())
            finally:
                server.stop()

            self.assertEqual(ack["applied_count"], 1)
            self.assertEqual(ack["events"][0]["status"], "conflict_merged")
            self.assertTrue(engine_a.cache_store["rec-100"]["payload"].get("_conflict_resolved"))

            reloaded_a = SmartDataCoreEngine("nas-node-01", AtomicDiskStorage(file_a))
            self.assertIn("rec-100", reloaded_a.cache_store)


def run_full_suite() -> Tuple[Dict[str, Any], bool]:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ComprehensiveSmartDataTests)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    return {
        "module": "full_capability_sync_engine.py",
        "capabilities": [
            "AtomicDiskStorage (fsync + atomic os.replace)",
            "TCPNetworkSyncServer & send_sync_batch_over_tcp",
            "SHA-256 Deduplication & Vector Clock Conflict Merge",
            "Automated unittest Verification",
        ],
        "tests_passed": result.wasSuccessful(),
    }, result.wasSuccessful()


if __name__ == "__main__":
    report, passed = run_full_suite()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print("All checks passed:", passed)
