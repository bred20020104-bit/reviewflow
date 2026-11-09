"""Deterministic bounded collection; neither review judgments nor source storage."""

import hashlib
import json
from collections import Counter
from dataclasses import asdict

from reviewflow.errors import CollectionError
from reviewflow.models import (
    Coverage,
    FileRecord,
    Inventory,
    ScanPolicy,
    Snapshot,
    SourceInfo,
    SourceReader,
)
from reviewflow.policy import classify_kind, path_reason


def compute_snapshot_id(
    source: SourceInfo, policy: ScanPolicy, records: tuple[FileRecord, ...]
) -> str:
    payload = {
        "mode": source.mode,
        "commit": source.commit,
        "policy": asdict(policy),
        "records": [asdict(record) for record in records],
    }
    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    except UnicodeError as exc:
        raise CollectionError("File metadata is not valid UTF-8.") from exc
    return hashlib.sha256(encoded).hexdigest()


def build_inventory(source: SourceReader, policy: ScanPolicy) -> Inventory:
    entries = sorted(source.entries(), key=lambda entry: entry.path)
    if len({entry.path for entry in entries}) != len(entries):
        raise CollectionError("Source returned duplicate paths.")
    records: list[FileRecord] = []
    included_files = included_bytes = excluded_directories = 0
    counts: Counter[str] = Counter()
    for entry in entries:
        reason = path_reason(entry.path, entry.entry_type)
        digest = None
        if reason is None:
            size = entry.size_bytes
            if size is None or size < 0:
                reason = "unsupported_type"
            elif size > policy.max_file_bytes:
                reason = "file_too_large"
            elif included_files >= policy.max_files:
                reason = "file_limit"
            elif included_bytes + size > policy.max_total_bytes:
                reason = "total_bytes_limit"
            else:
                content = source.read_bytes(entry, max_bytes=policy.max_file_bytes)
                if len(content) != size:
                    raise CollectionError("Content length changed since enumeration.")
                if b"\0" in content:
                    reason = "binary"
                else:
                    try:
                        content.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        reason = "invalid_utf8"
                if reason is None:
                    digest = hashlib.sha256(content).hexdigest()
                    included_files += 1
                    included_bytes += size
        if reason:
            counts[reason] += 1
            if entry.entry_type == "directory" and reason == "excluded_directory":
                excluded_directories += 1
        records.append(
            FileRecord(
                entry.path,
                "skipped" if reason else "included",
                reason,
                entry.size_bytes,
                digest,
                classify_kind(entry.path),
            )
        )
    budget_limited = bool(counts["file_limit"] or counts["total_bytes_limit"])
    warnings = list(source.info.warnings)
    if counts:
        warnings.append("Not all paths were included; inspect skipped entries and policy limits.")
    if excluded_directories:
        warnings.append(
            "Excluded directory contents were not enumerated; their file counts are unknown."
        )
    if budget_limited:
        warnings.append(
            "Collection budget reached; this inventory does not cover all eligible files."
        )
    if not included_files:
        warnings.append(
            "No files were included. This is not evidence that the project has no problems."
        )
    ordered = tuple(records)
    snapshot = Snapshot(
        1,
        source.info,
        policy,
        ordered,
        compute_snapshot_id(source.info, policy, ordered),
        tuple(warnings),
    )
    coverage = Coverage(
        included_files,
        included_bytes,
        sum(counts.values()) - excluded_directories,
        excluded_directories,
        dict(sorted(counts.items())),
        budget_limited,
    )
    return Inventory(snapshot, coverage)
