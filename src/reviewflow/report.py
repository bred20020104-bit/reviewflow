"""Render metadata only; filenames and repository metadata are untrusted text."""

import html
import json
import unicodedata
from dataclasses import asdict

from reviewflow.models import Inventory

NOTICE = "Inventory only: no semantic code review or target tests have been performed."


def safe_text(value: object) -> str:
    """Make control and formatting characters visible rather than executable."""
    text = str(value)
    return "".join(
        char.encode("unicode_escape").decode("ascii")
        if unicodedata.category(char).startswith("C") or char in "  "
        else char
        for char in text
    )


def _markdown(value: object) -> str:
    text = "".join(
        f"&#{ord(char)};" if char in "\\`|*[]{}()!#~_" else html.escape(char, quote=True)
        for char in safe_text(value)
    )
    # Visible newline escapes are ordinary text, never actual table row breaks.
    return text.replace("&#92;n", "\\n").replace("&#92;r", "\\r").replace("&#92;t", "\\t")


def render_json(inventory: Inventory) -> str:
    data = asdict(inventory)
    source = data["snapshot"]["source"]
    source["root"] = str(inventory.snapshot.source.root)
    source["git_repair_eligible"] = inventory.snapshot.source.git_repair_eligible
    data["notice"] = NOTICE
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True)


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.1f} KiB"
    return f"{count / (1024 * 1024):.1f} MiB"


def _noun(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def render_text(inventory: Inventory, *, verbose: bool = False) -> str:
    snapshot, coverage = inventory.snapshot, inventory.coverage
    source = snapshot.source
    origin = (
        f"committed HEAD ({source.commit}); uncommitted changes are excluded"
        if source.mode == "head"
        else "current files (includes uncommitted changes; not eligible for Git repair)"
    )
    lines = [
        "ReviewFlow · repository inventory",
        "",
        f"Project   {safe_text(source.root)}",
        f"Source    {origin}",
        "Result    inventory complete; this is not a passed code review",
        "",
        f"Included  {_noun(coverage.included_files, 'file', 'files')} · {_size(coverage.included_bytes)}",
        f"Skipped   {_noun(coverage.skipped_files, 'file/entry', 'files/entries')} · "
        f"{coverage.excluded_directories} directories (nested file count unknown)",
        f"Budget    {'limit reached; coverage is incomplete' if coverage.budget_limited else 'within the limit'}",
        f"Limits    {snapshot.policy.max_files} files · "
        f"{_size(snapshot.policy.max_file_bytes)} per file · "
        f"{_size(snapshot.policy.max_total_bytes)} total",
        f"Snapshot  {snapshot.snapshot_id}",
        "",
        "Notes",
        "  · No semantic review or target tests have been run",
    ]
    lines.extend(f"  · {safe_text(warning)}" for warning in snapshot.warnings)
    if verbose:
        for title, status in (("Included files", "included"), ("Skipped entries", "skipped")):
            lines += ["", title]
            for record in snapshot.records:
                if record.status == status:
                    lines.append(f"  {safe_text(record.path)}")
                    lines.append(
                        f"    kind={record.kind} bytes={record.size_bytes} reason={record.reason or '—'}"
                    )
                    if record.sha256:
                        lines.append(f"    SHA-256={record.sha256}")
    else:
        lines += ["", "Use --verbose for the full file list."]
    lines += ["", NOTICE]
    return "\n".join(lines)


def render_markdown(inventory: Inventory) -> str:
    snapshot, coverage = inventory.snapshot, inventory.coverage
    source = snapshot.source
    lines = ["# Repository inventory", "", NOTICE, "", "## Source", ""]
    for label, value in (
        ("Mode", source.mode),
        ("Root", source.root),
        ("Commit", source.commit),
        ("Branch", source.branch),
        ("Dirty", source.dirty),
        (
            "Git repair eligible (source metadata only; inventory does not authorize repair)",
            source.git_repair_eligible,
        ),
        ("Snapshot ID", snapshot.snapshot_id),
        ("Schema version", snapshot.schema_version),
    ):
        lines.append(
            f"- {label}: {_markdown(value if value is not None else 'unknown / not applicable')}"
        )
    lines += ["", "## Coverage and limits", ""]
    for label, value in (
        ("Included files", coverage.included_files),
        ("Included bytes", coverage.included_bytes),
        ("Skipped files / non-directory entries", coverage.skipped_files),
        ("Excluded directories (inner file counts unknown)", coverage.excluded_directories),
        ("Budget limited", coverage.budget_limited),
        ("Semantic review performed", coverage.semantic_review_performed),
        ("Target tests executed", coverage.target_tests_executed),
        ("Maximum included files", snapshot.policy.max_files),
        ("Maximum bytes per file", snapshot.policy.max_file_bytes),
        ("Maximum included bytes", snapshot.policy.max_total_bytes),
    ):
        lines.append(f"- {label}: {value}")
    for reason, count in sorted(coverage.skip_counts.items()):
        lines.append(f"- Skipped ({_markdown(reason)}): {count}")
    lines += ["", "## Warnings", ""]
    lines.extend(f"- {_markdown(warning)}" for warning in snapshot.warnings)
    if not snapshot.warnings:
        lines.append("- None. This does not imply the code is safe.")
    for heading, status in (("Included files", "included"), ("Skipped entries", "skipped")):
        lines += [
            "",
            f"## {heading}",
            "",
            "| Path | Kind | Bytes | Reason | SHA-256 |",
            "| --- | --- | ---: | --- | --- |",
        ]
        for record in snapshot.records:
            if record.status == status:
                values = (record.path, record.kind, record.size_bytes, record.reason, record.sha256)
                lines.append(
                    "| " + " | ".join(_markdown(v) if v is not None else "—" for v in values) + " |"
                )
    return "\n".join(lines)
