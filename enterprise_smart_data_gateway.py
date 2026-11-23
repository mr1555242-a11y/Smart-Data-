from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import ssl
from typing import Any, Dict, List, Optional, Tuple
import unittest


@dataclass(frozen=True)
class SecurityStandardsSpec:
    """
    Documents international security and systems engineering patterns applied
    across transport, key management, cryptographic integrity, and hardware I/O.
    """

    key_derivation: str = "HKDF-SHA256 / PBKDF2-HMAC-SHA256 domain separation"
    authenticated_encryption: str = "AES-256-GCM (NIST SP 800-38D) with deterministic fallback"
    transport_security: str = "TLS 1.2+ via Python ssl.SSLContext & certificate verification"
    auth_comparison: str = "Constant-time hmac.compare_digest to mitigate timing side channels"
    hardware_abstraction: str = "Hardware-agnostic driver contract with sysfs/device & fallback support"


class SecretVaultProvider:
    """
    Resolves cryptographic keys and node credentials from environment variables
    or injected secret stores (such as Vault/KMS/secrets manager) rather than
    hardcoding sensitive values inside business logic.
    """

    def __init__(self, external_store: Optional[Dict[str, str]] = None) -> None:
        self._external_store = dict(external_store) if external_store else {}

    def get_secret(self, key_name: str, default_value: Optional[str] = None) -> str:
        env_val = os.environ.get(key_name)
        if env_val and env_val.strip():
            return env_val.strip()
        if key_name in self._external_store and self._external_store[key_name].strip():
            return self._external_store[key_name].strip()
        if default_value is not None:
            return default_value
        raise KeyError(f"Required secret '{key_name}' is not configured in environment or store")

    def derive_subkey(self, root_secret: str, salt: bytes, info_context: bytes, length: int = 32) -> bytes:
        prk = hmac.new(salt, root_secret.encode("utf-8"), hashlib.sha256).digest()
        okm = bytearray()
        prev = b""
        counter = 1
        while len(okm) < length:
            prev = hmac.new(prk, prev + info_context + bytes([counter]), hashlib.sha256).digest()
            okm.extend(prev)
            counter += 1
        return bytes(okm[:length])


class EnterpriseCryptoEngine:
    """
    Provides AES-256-GCM authenticated encryption (when 'cryptography' is available),
    TLS context construction, and constant-time node token verification.
    """

    def __init__(self, vault: SecretVaultProvider, master_key_env: str = "SMART_DATA_MASTER_KEY") -> None:
        self.vault = vault
        master_secret = vault.get_secret(master_key_env, default_value="ephemeral-dev-master-key")
        self._enc_key = vault.derive_subkey(
            master_secret,
            salt=b"ugreen-smart-data-salt-v1",
            info_context=b"aes-256-gcm-payload-key",
            length=32,
        )
        self._mac_key = vault.derive_subkey(
            master_secret,
            salt=b"ugreen-smart-data-salt-v1",
            info_context=b"hmac-sha256-auth-key",
            length=32,
        )
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

            self._aesgcm_cls = AESGCM
        except ImportError:
            self._aesgcm_cls = None

    def create_tls_context(
        self,
        certfile: Optional[str] = None,
        keyfile: Optional[str] = None,
        cafile: Optional[str] = None,
        require_client_auth: bool = False,
    ) -> ssl.SSLContext:
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cafile)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if certfile and keyfile:
            ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
        if require_client_auth:
            ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    def sign_node_auth(self, node_id: str, nonce: str, timestamp_iso: str) -> str:
        message = f"{node_id}|{nonce}|{timestamp_iso}".encode("utf-8")
        return hmac.new(self._mac_key, message, hashlib.sha256).hexdigest()

    def verify_node_auth(self, node_id: str, nonce: str, timestamp_iso: str, signature: str) -> bool:
        if not node_id or not nonce or not timestamp_iso or not signature:
            return False
        expected = self.sign_node_auth(node_id, nonce, timestamp_iso)
        return hmac.compare_digest(expected, signature)

    def encrypt_message(self, data: Dict[str, Any], aad: str = "ugreen-nas-smart-data") -> Dict[str, str]:
        raw_bytes = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
        aad_bytes = aad.encode("utf-8")
        if self._aesgcm_cls is not None:
            nonce = os.urandom(12)
            cipher = self._aesgcm_cls(self._enc_key)
            ct = cipher.encrypt(nonce, raw_bytes, aad_bytes)
            return {
                "algorithm": "AES-256-GCM",
                "nonce_hex": nonce.hex(),
                "ciphertext_hex": ct.hex(),
                "aad": aad,
            }
        nonce = os.urandom(16)
        stream = bytearray()
        idx = 0
        while len(stream) < len(raw_bytes):
            stream.extend(hashlib.sha256(self._enc_key + nonce + idx.to_bytes(4, "big")).digest())
            idx += 1
        ct = bytes(b ^ k for b, k in zip(raw_bytes, stream[: len(raw_bytes)]))
        tag = hmac.new(self._mac_key, nonce + ct + aad_bytes, hashlib.sha256).hexdigest()
        return {
            "algorithm": "HKDF-CTR-HMAC-SHA256-FALLBACK",
            "nonce_hex": nonce.hex(),
            "ciphertext_hex": ct.hex(),
            "tag_hex": tag,
            "aad": aad,
        }

    def decrypt_message(self, packet: Dict[str, str]) -> Dict[str, Any]:
        nonce = bytes.fromhex(packet["nonce_hex"])
        ct = bytes.fromhex(packet["ciphertext_hex"])
        aad_bytes = packet.get("aad", "ugreen-nas-smart-data").encode("utf-8")
        algo = packet.get("algorithm", "HKDF-CTR-HMAC-SHA256-FALLBACK")

        if algo == "AES-256-GCM" and self._aesgcm_cls is not None:
            cipher = self._aesgcm_cls(self._enc_key)
            plain = cipher.decrypt(nonce, ct, aad_bytes)
            return json.loads(plain.decode("utf-8"))

        expected_tag = hmac.new(self._mac_key, nonce + ct + aad_bytes, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_tag, packet.get("tag_hex", "")):
            raise ValueError("Cryptographic integrity verification failed")
        stream = bytearray()
        idx = 0
        while len(stream) < len(ct):
            stream.extend(hashlib.sha256(self._enc_key + nonce + idx.to_bytes(4, "big")).digest())
            idx += 1
        plain = bytes(b ^ k for b, k in zip(ct, stream[: len(ct)]))
        return json.loads(plain.decode("utf-8"))


class BaseStorageDriver(ABC):
    """
    Abstract hardware and storage interface for UGREEN AI NAS integration.
    """

    @abstractmethod
    def get_hardware_status(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def persist_snapshot(self, record_id: str, record: Dict[str, Any]) -> None:
        raise NotImplementedError


class UGREENHardwareAdapter(BaseStorageDriver):
    """
    Detects host hardware/sysfs paths when running on target NAS appliances and
    gracefully switches to virtualized mode on non-appliance development machines.
    """

    def __init__(self, device_node: str = "/dev/ugreen_ai_nas", sysfs_thermal_path: str = "/sys/class/thermal/thermal_zone0/temp") -> None:
        self.device_node = device_node
        self.sysfs_thermal_path = sysfs_thermal_path
        self.snapshots: Dict[str, Dict[str, Any]] = {}

    def _read_host_temperature_c(self) -> float:
        if os.path.exists(self.sysfs_thermal_path):
            try:
                with open(self.sysfs_thermal_path, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                    millideg = float(raw)
                    return round(millideg / 1000.0 if millideg > 200 else millideg, 2)
            except (OSError, ValueError):
                pass
        return 38.5

    def get_hardware_status(self) -> Dict[str, Any]:
        has_device = os.path.exists(self.device_node)
        return {
            "device_node": self.device_node,
            "hardware_attached": has_device,
            "mode": "native_hardware_driver" if has_device else "virtualized_driver_mode",
            "temperature_c": self._read_host_temperature_c(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def persist_snapshot(self, record_id: str, record: Dict[str, Any]) -> None:
        self.snapshots[record_id] = deepcopy(record)


class EnterpriseSmartDataGateway:
    """
    End-to-end secure synchronization gateway combining canonical SHA-256
    deduplication, Vector Clock conflict resolution, authenticated encryption,
    and hardware driver abstraction.
    """

    def __init__(self, node_id: str, crypto: EnterpriseCryptoEngine, hardware: BaseStorageDriver) -> None:
        self.node_id = node_id
        self.crypto = crypto
        self.hardware = hardware
        self.records: Dict[str, Dict[str, Any]] = {}
        self.audit_trail: List[Dict[str, Any]] = []

    @staticmethod
    def _checksum(payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_concurrent_conflict(clock_a: Dict[str, int], clock_b: Dict[str, int]) -> bool:
        keys = set(clock_a.keys()) | set(clock_b.keys())
        a_ahead = any(clock_a.get(k, 0) > clock_b.get(k, 0) for k in keys)
        b_ahead = any(clock_b.get(k, 0) > clock_a.get(k, 0) for k in keys)
        return a_ahead and b_ahead

    def process_secure_sync(
        self,
        record_id: str,
        sender_node_id: str,
        nonce: str,
        timestamp_iso: str,
        auth_signature: str,
        encrypted_packet: Dict[str, str],
        incoming_clock: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        if not self.crypto.verify_node_auth(sender_node_id, nonce, timestamp_iso, auth_signature):
            raise PermissionError(f"Unauthorized node signature from '{sender_node_id}'")

        payload = self.crypto.decrypt_message(encrypted_packet)
        digest = self._checksum(payload)
        now_iso = datetime.now(timezone.utc).isoformat()
        existing = self.records.get(record_id)

        if existing is None:
            clock = dict(incoming_clock) if incoming_clock else {}
            clock[sender_node_id] = clock.get(sender_node_id, 0) + 1
            record = {
                "record_id": record_id,
                "payload": deepcopy(payload),
                "checksum": digest,
                "vector_clock": clock,
                "last_node": sender_node_id,
                "updated_at": now_iso,
            }
            status = "created_synced"
            conflict = False
            final_clock = clock
            final_digest = digest
        else:
            ex_clock = existing["vector_clock"]
            ex_digest = existing["checksum"]
            cand_clock = dict(incoming_clock) if incoming_clock else dict(ex_clock)
            cand_clock[sender_node_id] = cand_clock.get(sender_node_id, 0) + 1
            conflict = self._is_concurrent_conflict(ex_clock, cand_clock)

            if conflict:
                merged_clock = {
                    k: max(ex_clock.get(k, 0), cand_clock.get(k, 0))
                    for k in set(ex_clock.keys()) | set(cand_clock.keys())
                }
                merged_payload = deepcopy(existing["payload"])
                for k, v in payload.items():
                    merged_payload[k] = deepcopy(v)
                merged_payload["_conflict_resolved"] = True
                merged_payload["_merged_vector_clock"] = merged_clock
                merged_digest = self._checksum(merged_payload)
                record = {
                    "record_id": record_id,
                    "payload": merged_payload,
                    "checksum": merged_digest,
                    "vector_clock": merged_clock,
                    "last_node": sender_node_id,
                    "updated_at": now_iso,
                }
                status = "conflict_merged"
                final_clock = merged_clock
                final_digest = merged_digest
            elif digest == ex_digest:
                record = deepcopy(existing)
                status = "unchanged_cached"
                final_clock = dict(ex_clock)
                final_digest = ex_digest
            else:
                upd_clock = {
                    k: max(ex_clock.get(k, 0), cand_clock.get(k, 0))
                    for k in set(ex_clock.keys()) | set(cand_clock.keys())
                }
                record = {
                    "record_id": record_id,
                    "payload": deepcopy(payload),
                    "checksum": digest,
                    "vector_clock": upd_clock,
                    "last_node": sender_node_id,
                    "updated_at": now_iso,
                }
                status = "updated_synced"
                final_clock = upd_clock
                final_digest = digest

        self.records[record_id] = record
        self.hardware.persist_snapshot(record_id, record)

        event = {
            "record_id": record_id,
            "sender_node_id": sender_node_id,
            "status": status,
            "conflict_detected": conflict,
            "checksum_prefix": final_digest[:12],
            "vector_clock": final_clock,
            "hardware": self.hardware.get_hardware_status(),
            "processed_at": now_iso,
        }
        self.audit_trail.append(event)
        return event


class EnterpriseGatewayTestSuite(unittest.TestCase):
    def test_authenticated_encrypted_sync_and_conflict(self) -> None:
        vault = SecretVaultProvider({"SMART_DATA_MASTER_KEY": "strong-international-benchmark-key"})
        crypto = EnterpriseCryptoEngine(vault)
        gateway = EnterpriseSmartDataGateway("ugreen-nas-01", crypto, UGREENHardwareAdapter())

        ts = datetime.now(timezone.utc).isoformat()
        nonce = "n-101"
        sig = crypto.sign_node_auth("ugreen-nas-01", nonce, ts)
        pkt = crypto.encrypt_message({"volume": "pool-1", "iops": 1200})

        ev1 = gateway.process_secure_sync("vol-1", "ugreen-nas-01", nonce, ts, sig, pkt, {"ugreen-nas-01": 1})
        self.assertEqual(ev1["status"], "created_synced")

        nonce2 = "n-102"
        sig2 = crypto.sign_node_auth("remote-edge-02", nonce2, ts)
        pkt2 = crypto.encrypt_message({"volume": "pool-1", "tier": "nvme-cache"})

        ev2 = gateway.process_secure_sync("vol-1", "remote-edge-02", nonce2, ts, sig2, pkt2, {"remote-edge-02": 2})
        self.assertEqual(ev2["status"], "conflict_merged")
        self.assertTrue(gateway.records["vol-1"]["payload"].get("_conflict_resolved"))

        with self.assertRaises(PermissionError):
            gateway.process_secure_sync("vol-1", "remote-edge-02", "n-103", ts, "bad-sig", pkt2)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(EnterpriseGatewayTestSuite)
    res = unittest.TextTestRunner(verbosity=0).run(suite)
    print(
        json.dumps(
            {
                "standards": SecurityStandardsSpec().__dict__,
                "tests_passed": res.wasSuccessful(),
            },
            indent=2,
        )
    )
