import os
from dataclasses import replace
from hashlib import sha256

import pytest
from review_helpers import evidence, finding, task

from reviewflow.errors import CollectionError
from reviewflow.models import ScanPolicy
from reviewflow.review.context import MAX_EXCERPT_BYTES, MAX_EXCERPT_LINES, ReviewContext
from reviewflow.review.contracts import EvidenceRef, Finding, ReviewTask, VerificationResult
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource
from reviewflow.sources.git import open_source


def setup_context(tmp_path, content=b"first\r\nsecond\nlast", name="main.py"):
    (tmp_path / name).write_bytes(content)
    (tmp_path / "other.py").write_bytes(b"counterevidence\n")
    (tmp_path / ".env").write_bytes(b"SECRET=hidden")
    source = DirectorySource(tmp_path)
    inventory = build_inventory(source, ScanPolicy())
    return source, inventory, ReviewContext(source, inventory)


def messages(inventory, **evidence_changes):
    ref = EvidenceRef.model_validate(
        evidence(
            snapshot_id=inventory.snapshot.snapshot_id,
            path="main.py",
            content_sha256=next(
                r.sha256 for r in inventory.snapshot.records if r.path == "main.py"
            ),
            **evidence_changes,
        )
    )
    work = ReviewTask.model_validate(
        task(snapshot_id=inventory.snapshot.snapshot_id, paths=("main.py",))
    )
    candidate = Finding.model_validate(finding(evidence=(ref,)))
    return work, candidate


@pytest.mark.skipif(os.name != "posix", reason="Windows rejects newline characters in file names")
def test_lines_preserve_crlf_and_unicode_names(tmp_path):
    _, _, ctx = setup_context(tmp_path, name="模块\n.py")
    assert ctx.read_lines("模块\n.py", 1, 2) == "first\r\nsecond\n"
    assert ctx.read_lines("模块\n.py", 3, 3) == "last"


@pytest.mark.parametrize(
    "content,good,bad",
    [
        (b"a\n", (1, 1), (2, 2)),
        (b"\n", (1, 1), (2, 2)),
        ("a\rb\v ".encode(), (1, 1), (2, 2)),
        (b"", None, (1, 1)),
    ],
)
def test_lf_line_count(tmp_path, content, good, bad):
    _, _, ctx = setup_context(tmp_path, content)
    if good:
        assert ctx.read_lines("main.py", *good) == content.decode()
    with pytest.raises(CollectionError):
        ctx.read_lines("main.py", *bad)


@pytest.mark.parametrize("path", [".env", "missing.py", "../main.py", "/main.py"])
def test_disallowed_paths_rejected_before_read(tmp_path, monkeypatch, path):
    source, _, ctx = setup_context(tmp_path)
    monkeypatch.setattr(source, "read_bytes", lambda *a, **k: pytest.fail("forbidden read"))
    with pytest.raises(CollectionError):
        ctx.read_lines(path, 1, 1)


@pytest.mark.parametrize(
    "start,end", [(0, 1), (True, 1), ("1", 2), (2, 1), (1, MAX_EXCERPT_LINES + 1)]
)
def test_invalid_ranges_before_read(tmp_path, monkeypatch, start, end):
    source, _, ctx = setup_context(tmp_path)
    monkeypatch.setattr(source, "read_bytes", lambda *a, **k: pytest.fail("invalid range read"))
    with pytest.raises(CollectionError):
        ctx.read_lines("main.py", start, end)


@pytest.mark.parametrize(
    "content,end",
    [(b"x\n" * MAX_EXCERPT_LINES, MAX_EXCERPT_LINES), (b"x" * MAX_EXCERPT_BYTES, 1)],
)
def test_excerpt_limit_inclusive(tmp_path, content, end):
    _, _, ctx = setup_context(tmp_path, content)
    assert ctx.read_lines("main.py", 1, end) == content.decode()


def test_excerpt_byte_limit(tmp_path):
    content = "中".encode() * (MAX_EXCERPT_BYTES // 3 + 1)
    (tmp_path / "main.py").write_bytes(content)
    source = DirectorySource(tmp_path)
    inventory = build_inventory(
        source, ScanPolicy(max_file_bytes=len(content), max_total_bytes=len(content))
    )
    ctx = ReviewContext(source, inventory)
    with pytest.raises(CollectionError):
        ctx.read_lines("main.py", 1, 1)


def test_changed_directory_file(tmp_path):
    _, _, ctx = setup_context(tmp_path)
    (tmp_path / "main.py").write_bytes(b"different")
    with pytest.raises(CollectionError):
        ctx.read_lines("main.py", 1, 1)


@pytest.mark.parametrize("corruption", ["snapshot", "root", "mode", "commit"])
def test_context_rejects_wrong_identity(tmp_path, corruption):
    source, inventory, _ = setup_context(tmp_path)
    snap = inventory.snapshot
    if corruption == "snapshot":
        snap = replace(snap, snapshot_id="0" * 64)
    else:
        value = {"root": tmp_path / "other", "mode": "head", "commit": "0" * 40}[corruption]
        snap = replace(snap, source=replace(snap.source, **{corruption: value}))
    with pytest.raises(CollectionError):
        ReviewContext(source, replace(inventory, snapshot=snap))


@pytest.mark.parametrize("data", [b"x", b"OTHER\r\nsecond\nlast"])
def test_content_hash_and_size_checked(tmp_path, monkeypatch, data):
    source, _, ctx = setup_context(tmp_path)
    monkeypatch.setattr(source, "read_bytes", lambda *a, **k: data)
    with pytest.raises(CollectionError):
        ctx.read_lines("main.py", 1, 1)


def test_valid_relationships_and_counterevidence(tmp_path):
    _, inventory, ctx = setup_context(tmp_path)
    work, candidate = messages(inventory)
    ctx.validate_task(work)
    ctx.validate_finding(work, candidate)
    ref = EvidenceRef.model_validate(
        evidence(
            snapshot_id=inventory.snapshot.snapshot_id,
            path="other.py",
            content_sha256=sha256(b"counterevidence\n").hexdigest(),
            end_line=1,
        )
    )
    result = VerificationResult(
        finding_id=candidate.finding_id,
        verdict="rejected",
        rationale="Counterexample",
        evidence=(ref,),
    )
    ctx.validate_verification(work, candidate, result)


@pytest.mark.parametrize(
    "change",
    [
        "task_snapshot",
        "task_path",
        "task_id",
        "evidence_snapshot",
        "evidence_hash",
        "evidence_path",
        "evidence_range",
        "forged_path",
        "forged_range",
    ],
)
def test_finding_validates_entire_chain(tmp_path, change):
    _, inventory, ctx = setup_context(tmp_path)
    work, candidate = messages(inventory)
    ref = candidate.evidence[0]
    if change == "task_snapshot":
        work = work.model_copy(update={"snapshot_id": "0" * 64})
    elif change == "task_path":
        work = work.model_copy(update={"paths": (".env",)})
    elif change == "task_id":
        candidate = candidate.model_copy(update={"task_id": "another"})
    else:
        update = {
            "evidence_snapshot": {"snapshot_id": "0" * 64},
            "evidence_hash": {"content_sha256": "0" * 64},
            "evidence_path": {"path": "other.py"},
            "evidence_range": {"end_line": 4},
            "forged_path": {"path": "../main.py"},
            "forged_range": {"start_line": True},
        }[change]
        candidate = candidate.model_copy(update={"evidence": (ref.model_copy(update=update),)})
    with pytest.raises(CollectionError):
        ctx.validate_finding(work, candidate)
    result = VerificationResult(
        finding_id=candidate.finding_id, verdict="unresolved", rationale="Unknown", evidence=()
    )
    with pytest.raises(CollectionError):
        ctx.validate_verification(work, candidate, result)


@pytest.mark.parametrize("change", ["finding_id", "scope", "forged"])
def test_verification_rejects_bad_result(tmp_path, change):
    _, inventory, ctx = setup_context(tmp_path)
    work, candidate = messages(inventory)
    result = VerificationResult(
        finding_id=candidate.finding_id,
        verdict="accepted",
        rationale="Checked",
        evidence=candidate.evidence,
    )
    if change == "finding_id":
        result = result.model_copy(update={"finding_id": "wrong"})
    elif change == "scope":
        result = result.model_copy(
            update={"evidence": (candidate.evidence[0].model_copy(update={"path": ".env"}),)}
        )
    else:
        result = result.model_copy(update={"evidence": ()})
    with pytest.raises(CollectionError):
        ctx.validate_verification(work, candidate, result)


def test_git_head_ignores_dirty_worktree(dirty_repo):
    source = open_source(dirty_repo, "head")
    ctx = ReviewContext(source, build_inventory(source, ScanPolicy()))
    assert ctx.read_lines("a.py", 1, 1) == "old\n"
    (dirty_repo / "a.py").write_bytes(b"changed again")
    assert ctx.read_lines("a.py", 1, 1) == "old\n"
