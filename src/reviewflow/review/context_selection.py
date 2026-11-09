"""Deterministic prefix allocation shared by preview and interactive review."""

from reviewflow.backend.context_index import ContextIndex
from reviewflow.review.workflow_models import CodePacket


def initial_context(index, limits):
    """Reserve file starts first, then grow contiguous LF prefixes round-robin.

    Only the first indexed chunk of each file is eligible. A later, smaller
    chunk must never substitute for a first chunk that did not fit the budget.
    """
    first = {}
    for packet in index.packets:
        first.setdefault(packet.evidence.path, packet)
    candidates = list(first.values())[: limits.max_context_files]
    lines = []
    for packet in candidates:
        parts = packet.text.split("\n")
        lines.append([p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else []))
    counts = [0] * len(candidates)
    remaining = limits.max_context_bytes
    while True:
        advanced = False
        for i, parts in enumerate(lines):
            if counts[i] == len(parts):
                continue
            size = len(parts[counts[i]].encode("utf-8"))
            if size <= remaining:
                counts[i] += 1
                remaining -= size
                advanced = True
        if not advanced:
            break
    packets, omissions = [], list(index.omissions)
    for i, packet in enumerate(first.values()):
        count = counts[i] if i < len(counts) else 0
        if count:
            packets.append(
                CodePacket(
                    packet.evidence.model_copy(update={"end_line": count}),
                    "".join(lines[i][:count]),
                )
            )
        if not count or count < packet.evidence.end_line:
            item = (packet.evidence.path, "context_budget")
            if item not in omissions:
                omissions.append(item)
    return tuple(packets), tuple(omissions)


def context_preflight(source, inventory, limits):
    """Local-only coverage facts, not a promise of semantic review or wire fit."""
    index = ContextIndex(source, inventory, max_bytes=8388608, max_chunks=1000)
    packets, omissions = initial_context(index, limits)
    records = [r for r in inventory.snapshot.records if r.status == "included"]
    total = sum(r.size_bytes for r in records)
    # A full-selection index budget is reset by selecting a smaller batch;
    # only intrinsic content exclusions make a file unusable in every batch.
    unavailable = {path for path, reason in index.omissions if reason != "context_budget"}
    batches, batch, used, oversized = [], [], 0, []
    for record in records:
        if record.size_bytes > limits.max_context_bytes:
            oversized.append(record.path)
            continue
        if record.path in unavailable:
            continue
        if batch and (
            used + record.size_bytes > limits.max_context_bytes
            or len(batch) >= limits.max_context_files
        ):
            batches.append(batch)
            batch, used = [], 0
        batch.append(record.path)
        used += record.size_bytes
    if batch:
        batches.append(batch)
    return {
        "selected_source_bytes": total,
        "max_context_bytes": limits.max_context_bytes,
        "whole_selection_fits": bool(records)
        and total <= limits.max_context_bytes
        and len(records) <= limits.max_context_files
        and not index.omissions,
        "planner_ranges": [p.evidence.model_dump(mode="json") for p in packets],
        "initial_omissions": [{"path": path, "reason": reason} for path, reason in omissions],
        "suggested_include_batches": batches,
        "oversized_files": oversized,
        "notice": "Source-byte estimates only; schemas/escaping must also fit the HTTP body cap. "
        "If the selection does not fit, review is bounded and cannot claim full coverage. "
        "Suggested batches are not executed automatically and need separate approvals; "
        "separate runs do not establish cross-batch correctness.",
    }
