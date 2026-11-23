from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
from typing import Any, Dict, List, Optional, Tuple
import unittest


class TokenAuthManager:
    """
    Validates node authentication tokens using constant-time HMAC verification.
    """

    def __init__(self, node_secrets: Dict[str, str]) -> None:
        if not node_secrets:
            raise ValueError("node_secrets must contain at least one node credential")
        self.node_secrets = dict(node_secrets)

    def sign_token(self, node_id: str, nonce: str) -> str:
        secret = self.node_secrets.get(node_id)
        if not secret:
            raise PermissionError(f"Unknown node_id: {node_id}")
        message = f"{node_id}:{nonce}".encode("utf-8")
        return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    def verify_token(self, node_id: str, nonce: str, provided_signature: str) -> bool:
        secret = self.node_secrets.get(node_id)
        if not secret or not provided_signature:
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{node_id}:{nonce}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, provided_signature)


class PayloadCipher:
    """
    Provides authenticated integrity signing and XOR keystream encryption
    suitable for prototype transport security; can be backed by TLS or
    cryptography/AES in production deployments.
    """

    def __init__(self, master_key: str) -> None:
        if not master_key:
            raise ValueError("master_key must be a non-empty string")
        self.key_bytes = hashlib.sha256(master_key.encode("utf-8")).digest()

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        stream = bytearray()
        counter = 0
        while len(stream) < length:
            block = hashlib.sha256(self.key_bytes + nonce + counter.to_bytes(4, "big")).digest()
            stream.extend(block)
            counter += 1
        return bytes(stream[:length])

    def encrypt_payload(self, payload: Dict[str, Any], nonce: bytes) -> Dict[str, str]:
        raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
        ks = self._keystream(nonce, len(raw))
        cipher_bytes = bytes(b ^ k for b, k in zip(raw, ks))
        mac = hmac.new(self.key_bytes, nonce + cipher_bytes, hashlib.sha256).hexdigest()
        return {
            "nonce_hex": nonce.hex(),
            "ciphertext_hex": cipher_bytes.hex(),
            "hmac_sha256": mac,
        }

    def decrypt_payload(self, envelope: Dict[str, str]) -> Dict[str, Any]:
        nonce = bytes.fromhex(envelope["nonce_hex"])
        cipher_bytes = bytes.fromhex(envelope["ciphertext_hex"])
        provided_mac = envelope["hmac_sha256"]
        expected_mac = hmac.new(self.key_bytes, nonce + cipher_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_mac, provided_mac):
            raise ValueError("Payload integrity check failed: HMAC mismatch")
        ks = self._keystream(nonce, len(cipher_bytes))
        plain_bytes = bytes(b ^ k for b, k in zip(cipher_bytes, ks))
        return json.loads(plain_bytes.decode("utf-8"))


class SQLiteStateStore:
    """
    Persists synchronized records and audit logs inside a relational SQLite database.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    record_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    vector_clock_json TEXT NOT NULL,
                    last_node TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    conflict_detected INTEGER NOT NULL,
                    checksum_prefix TEXT NOT NULL,
                    vector_clock_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )

    def upsert_record(self, record: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO records (record_id, payload_json, checksum, vector_clock_json, last_node, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(record_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    checksum = excluded.checksum,
                    vector_clock_json = excluded.vector_clock_json,
                    last_node = excluded.last_node,
                    updated_at = excluded.updated_at
                """,
                (
                    record["record_id"],
                    json.dumps(record["payload"], sort_keys=True, ensure_ascii=False),
                    record["checksum"],
                    json.dumps(record["vector_clock"], sort_keys=True),
                    record["last_node"],
                    record["updated_at"],
                ),
            )

    def append_log(self, event: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_logs (record_id, node_id, status, conflict_detected, checksum_prefix, vector_clock_json, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["record_id"],
                    event["node_id"],
                    event["status"],
                    1 if event["conflict_detected"] else 0,
                    event["checksum_prefix"],
                    json.dumps(event["vector_clock"], sort_keys=True),
                    event["timestamp"],
                ),
            )

    def load_all_records(self) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_id, payload_json, checksum, vector_clock_json, last_node, updated_at FROM records"
            ).fetchall()
        for record_id, payload_json, checksum, vc_json, last_node, updated_at in rows:
            result[record_id] = {
                "record_id": record_id,
                "payload": json.loads(payload_json),
                "checksum": checksum,
                "vector_clock": json.loads(vc_json),
                "last_node": last_node,
                "updated_at": updated_at,
            }
        return result


class UGREENHardwareDriverInterface:
    """
    Abstract driver contract for direct UGREEN AI NAS hardware integration.
    Replace or subclass with vendor kernel/sysfs/API bindings on target hardware.
    """

    def read_telemetry(self) -> Dict[str, Any]:
        raise NotImplementedError("Subclasses must implement read_telemetry")

    def write_block_snapshot(self, snapshot_id: str, data: Dict[str, Any]) -> bool:
        raise NotImplementedError("Subclasses must implement write_block_snapshot")


class SimulatedUGREENNASDriver(UGREENHardwareDriverInterface):
    """
    Simulated direct UGREEN hardware driver exposing thermal/SMART telemetry
    and direct snapshot hooks until native hardware bindings are attached.
    """

    def __init__(self, model_name: str = "UGREEN-DXP-AI-NAS") -> None:
        self.model_name = model_name
        self.snapshots: Dict[str, Dict[str, Any]] = {}

    def read_telemetry(self) -> Dict[str, Any]:
        return {
            "hardware_model": self.model_name,
            "driver_mode": "simulated_direct_driver",
            "thermal_c": 39.0,
            "smart_health": "PASSED",
        }

    def write_block_snapshot(self, snapshot_id: str, data: Dict[str, Any]) -> bool:
        self.snapshots[snapshot_id] = deepcopy(data)
        return True


class ProductionHardenedSmartDataEngine:
    """
    Combines node token authentication, encrypted payload envelopes,
    SQLite database persistence, Vector Clock conflict resolution, and
    UGREEN hardware driver hooks.
    """

    def __init__(
        self,
        node_id: str,
        auth_manager: TokenAuthManager,
        cipher: PayloadCipher,
        db_store: SQLiteStateStore,
        hardware_driver: Optional[UGREENHardwareDriverInterface] = None,
    ) -> None:
        self.node_id = node_id
        self.auth_manager = auth_manager
        self.cipher = cipher
        self.db_store = db_store
        self.hardware_driver = hardware_driver or SimulatedUGREENNASDriver()
        self.cache_store: Dict[str, Dict[str, Any]] = self.db_store.load_all_records()

    @staticmethod
    def compute_checksum(payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def detect_conflict(existing_clock: Dict[str, int], incoming_clock: Dict[str, int]) -> bool:
        nodes = set(existing_clock.keys()) | set(incoming_clock.keys())
        ex_ahead = any(existing_clock.get(n, 0) > incoming_clock.get(n, 0) for n in nodes)
        in_ahead = any(incoming_clock.get(n, 0) > existing_clock.get(n, 0) for n in nodes)
        return ex_ahead and in_ahead

    @staticmethod
    def merge_payloads(
        existing_payload: Dict[str, Any],
        incoming_payload: Dict[str, Any],
        merged_clock: Dict[str, int],
    ) -> Dict[str, Any]:
        merged = deepcopy(existing_payload)
        for k, v in incoming_payload.items():
            merged[k] = deepcopy(v)
        merged["_conflict_resolved"] = True
        merged["_merged_vector_clock"] = merged_clock
        return merged

    def ingest_secure_record(
        self,
        record_id: str,
        source_node_id: str,
        nonce: str,
        auth_token: str,
        encrypted_envelope: Dict[str, str],
        incoming_clock: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        if not self.auth_manager.verify_token(source_node_id, nonce, auth_token):
            raise PermissionError(f"Authentication failed for node {source_node_id}")

        payload = self.cipher.decrypt_payload(encrypted_envelope)
        checksum = self.compute_checksum(payload)
        timestamp = datetime.now(timezone.utc).isoformat()
        existing = self.cache_store.get(record_id)

        if existing is None:
            clock = dict(incoming_clock) if incoming_clock else {}
            clock[source_node_id] = clock.get(source_node_id, 0) + 1
            record = {
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
                record = {
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
                record = deepcopy(existing)
                status = "unchanged_cached"
                final_clock = dict(ex_clock)
                final_checksum = ex_checksum
            else:
                upd_clock = {
                    n: max(ex_clock.get(n, 0), cand_clock.get(n, 0))
                    for n in set(ex_clock.keys()) | set(cand_clock.keys())
                }
                record = {
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

        self.cache_store[record_id] = record
        self.db_store.upsert_record(record)
        self.hardware_driver.write_block_snapshot(record_id, record)

        event = {
            "record_id": record_id,
            "node_id": source_node_id,
            "status": status,
            "conflict_detected": conflict_detected,
            "checksum_prefix": final_checksum[:12],
            "vector_clock": final_clock,
            "hardware_telemetry": self.hardware_driver.read_telemetry(),
            "timestamp": timestamp,
        }
        self.db_store.append_log(event)
        return event


class ProductionSecurityAndHardwareTests(unittest.TestCase):
    def test_auth_encryption_sqlite_and_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "smart_data.db")
            auth = TokenAuthManager({"nas-node-01": "secret-key-nas-01"})
            cipher = PayloadCipher("master-encryption-key")
            store = SQLiteStateStore(db_path)
            driver = SimulatedUGREENNASDriver()
            engine = ProductionHardenedSmartDataEngine("nas-node-01", auth, cipher, store, driver)

            nonce_str = "nonce-001"
            token = auth.sign_token("nas-node-01", nonce_str)
            envelope = cipher.encrypt_payload({"zone": "rack-1", "temp_c": 38.0}, b"0123456789abcdef")

            event = engine.ingest_secure_record(
                record_id="snap-01",
                source_node_id="nas-node-01",
                nonce=nonce_str,
                auth_token=token,
                encrypted_envelope=envelope,
                incoming_clock={"nas-node-01": 1},
            )
            self.assertEqual(event["status"], "created_synced")
            self.assertIn("snap-01", driver.snapshots)

            reloaded_store = SQLiteStateStore(db_path)
            reloaded_records = reloaded_store.load_all_records()
            self.assertIn("snap-01", reloaded_records)

            with self.assertRaises(PermissionError):
                engine.ingest_secure_record(
                    record_id="snap-01",
                    source_node_id="nas-node-01",
                    nonce="nonce-002",
                    auth_token="invalid-token",
                    encrypted_envelope=envelope,
                )


def run_production_checks() -> Tuple[Dict[str, Any], bool]:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProductionSecurityAndHardwareTests)
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    return {
        "module": "production_security_and_hardware_engine.py",
        "added_layers": [
            "TokenAuthManager (constant-time HMAC authentication)",
            "PayloadCipher (authenticated HMAC-SHA256 integrity + encryption)",
            "SQLiteStateStore (relational database persistence)",
            "UGREENHardwareDriverInterface & SimulatedUGREENNASDriver",
            "ProductionSecurityAndHardwareTests",
        ],
        "tests_passed": result.wasSuccessful(),
    }, result.wasSuccessful()


if __name__ == "__main__":
    summary, ok = run_production_checks()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print("Production checks passed:", ok)
