"""
Name: 01_stages_actions_and_tools.py
Scope: Actions taken and tools/concepts used in each engineering stage (Stage 1 - Stage 4)
"""

from dataclasses import dataclass
from typing import List


@dataclass
class StageBlueprint:
    stage_number: int
    stage_title: str
    what_we_did: str
    tools_and_concepts_used: List[str]


def get_engineering_stages() -> List[StageBlueprint]:
    return [
        StageBlueprint(
            stage_number=1,
            stage_title="Problem Framing & Edge NAS Alignment",
            what_we_did=(
                "Defined multi-node state drift, duplicate data ingestion, and network bottlenecks "
                "across UGREEN AI NAS and edge nodes, establishing the core SmartDataSynchronizer scope."
            ),
            tools_and_concepts_used=[
                "Distributed Systems Principles",
                "Local-First Storage Concepts",
                "UGREEN AI NAS Workflow Alignment",
                "Single-Responsibility Python Engine",
            ],
        ),
        StageBlueprint(
            stage_number=2,
            stage_title="Data Integrity & Deduplication Layer",
            what_we_did=(
                "Implemented deterministic payload hashing to detect whether incoming data has changed "
                "before mutating cached state."
            ),
            tools_and_concepts_used=[
                "Python hashlib (SHA-256)",
                "Canonical JSON Serialization (sort_keys=True, ensure_ascii=False, default=str)",
                "Checksum Deduplication (unchanged_cached)",
            ],
        ),
        StageBlueprint(
            stage_number=3,
            stage_title="Distributed State Tracking & Conflict Resolution",
            what_we_did=(
                "Added logical Vector Clocks and conflict detection/merging for concurrent or "
                "out-of-order updates across nodes such as nas-node-01 and remote-edge-02."
            ),
            tools_and_concepts_used=[
                "Logical Vector Clocks per record_id and node_id",
                "Causal Divergence Detection (_detect_conflict)",
                "Deterministic Field Merging (_merge_conflict_data)",
                "Conflict Metadata (_conflict_resolved: True)",
            ],
        ),
        StageBlueprint(
            stage_number=4,
            stage_title="Local Caching, Offline Readability & Verification",
            what_we_did=(
                "Built local in-memory lookup, full state/log export, and a reproducible verification "
                "batch to trace every state transition."
            ),
            tools_and_concepts_used=[
                "In-Memory Cache Store (get_record)",
                "State and Audit Log Exporter (export_state)",
                "UTC ISO-8601 Timestamps (datetime.now(timezone.utc))",
                "Structured Event Logging (sync_log)",
            ],
        ),
    ]


if __name__ == "__main__":
    for stage in get_engineering_stages():
        print(f"=== Stage {stage.stage_number}: {stage.stage_title} ===")
        print("What We Did:", stage.what_we_did)
        print("Tools & Concepts Used:", ", ".join(stage.tools_and_concepts_used))
        print()
