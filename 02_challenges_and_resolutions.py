"""
Name: 02_challenges_and_resolutions.py
Scope: Engineering challenges encountered and mechanisms used to resolve them
"""

from typing import Dict, List


CHALLENGE_RESOLUTION_MATRIX: List[Dict[str, str]] = [
    {
        "stage": "Stage 1",
        "challenge": (
            "Balancing rich state tracking and conflict awareness with low complexity so the "
            "synchronization logic remains predictable and easy to audit."
        ),
        "solution_mechanism": (
            "Structured SmartDataSynchronizer around a focused class design with explicit status codes "
            "(created_synced, updated_synced, unchanged_cached, conflict_merged) and internal sync_log."
        ),
    },
    {
        "stage": "Stage 2",
        "challenge": (
            "Dictionary key ordering or formatting differences can make identical payloads look "
            "different if not normalized consistently before hashing."
        ),
        "solution_mechanism": (
            "Normalized payloads with json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False) "
            "and computed SHA-256 digests via hashlib.sha256 to flag identical states as unchanged_cached."
        ),
    },
    {
        "stage": "Stage 3",
        "challenge": (
            "When nodes update records independently or over intermittent links, naive "
            "last-write-wins logic without version awareness can obscure concurrent edits."
        ),
        "solution_mechanism": (
            "Added _detect_conflict across Vector Clocks and _merge_conflict_data to deterministically "
            "merge fields while preserving prior data and setting _conflict_resolved: True."
        ),
    },
    {
        "stage": "Stage 4",
        "challenge": (
            "Ensuring every state transition—created, updated, unchanged, or conflict_merged—is "
            "traceable for debugging, testing, and verification."
        ),
        "solution_mechanism": (
            "Recorded structured events in sync_log containing record_id, status, conflict_detected, "
            "12-character checksum_prefix, vector_clock, and UTC timestamp via export_state()."
        ),
    },
]


def print_challenges_and_fixes() -> None:
    for item in CHALLENGE_RESOLUTION_MATRIX:
        print(f"[{item['stage']}]")
        print(f"  Challenge: {item['challenge']}")
        print(f"  Resolution: {item['solution_mechanism']}")
        print("-" * 60)


if __name__ == "__main__":
    print_challenges_and_fixes()
