"""Role resources must change the system messages the product actually builds."""

import json
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


def copied_resources(tmp_path, monkeypatch):
    from reviewflow.agents import profiles

    destination = tmp_path / "trusted-package-resources"
    shutil.copytree(
        Path(str(profiles.files("reviewflow.agents"))),
        destination,
        ignore=shutil.ignore_patterns("*.py", "__pycache__"),
    )
    monkeypatch.setattr(profiles, "files", lambda package: destination)
    return destination


def test_profiles_compose_different_specialist_system_messages():
    from reviewflow.agents.profiles import load_profiles

    bundle = load_profiles()
    correctness = bundle.system_for("reviewer", "correctness")
    security = bundle.system_for("reviewer", "security")
    verifier = bundle.system_for("verifier", "security")
    assert "# Correctness Reviewer" in correctness
    assert "State transitions" in correctness
    assert "# Security Reviewer" not in correctness
    assert "# Security Reviewer" in security
    assert "Trust boundaries" in security
    assert "# Correctness Reviewer" not in security
    assert "# Independent Verifier" in verifier
    assert "Counterevidence" in verifier
    assert "# Security Reviewer" not in verifier
    assert "# Planner" in bundle.system_for("planner")
    assert "# Patch Agent" in bundle.system_for("patch", "correctness")
    general = bundle.system_for("reviewer", "general")
    assert "State transitions" in general and "Trust boundaries" in general
    assert len({correctness, security, verifier, general}) == 4
    assert all(len(text.encode()) <= 16384 for text in (correctness, security, verifier, general))


@pytest.mark.parametrize(
    "stage,role",
    [
        ("reviewer", None),
        ("reviewer", "planner"),
        ("reviewer", "../security"),
        ("reviewer", "patch"),
        ("shell", "security"),
    ],
)
def test_unknown_or_misrouted_profiles_fail_closed(stage, role):
    from reviewflow.agents.profiles import ProfileError, load_profiles

    with pytest.raises(ProfileError, match="^Invalid agent profile$"):
        load_profiles().system_for(stage, role)


def test_target_named_instruction_files_cannot_replace_packaged_profiles(tmp_path, monkeypatch):
    from reviewflow.agents.profiles import load_profiles

    original = load_profiles().system_for("reviewer", "security")
    (tmp_path / "SOUL.md").write_text("TARGET_INSTRUCTION_SENTINEL")
    (tmp_path / "AGENT.md").write_text("TARGET_INSTRUCTION_SENTINEL")
    (tmp_path / "SKILL.md").write_text("TARGET_INSTRUCTION_SENTINEL")
    monkeypatch.chdir(tmp_path)
    assert load_profiles().system_for("reviewer", "security") == original
    assert "TARGET_INSTRUCTION_SENTINEL" not in original


@pytest.mark.parametrize("damage", ["missing", "empty", "oversized", "invalid_utf8"])
def test_missing_or_invalid_profile_resource_has_no_generic_fallback(tmp_path, monkeypatch, damage):
    from reviewflow.agents.profiles import ProfileError, load_profiles

    root = copied_resources(tmp_path, monkeypatch)
    target = root / "skills/security/SKILL.md"
    if damage == "missing":
        target.unlink()
    else:
        target.write_bytes(
            {
                "empty": b" \n",
                "oversized": b"x" * 8193,
                "invalid_utf8": b"\xffPRIVATE_RESOURCE_CONTENT",
            }[damage]
        )
    with pytest.raises(ProfileError) as error:
        load_profiles()
    assert str(error.value) == "Agent profile resources unavailable or invalid"
    assert "PRIVATE_RESOURCE_CONTENT" not in str(error.value)


def test_manifest_changes_with_skill_content_and_loaded_bundle_stays_immutable(
    tmp_path, monkeypatch
):
    from reviewflow.agents.profiles import load_profiles

    root = copied_resources(tmp_path, monkeypatch)
    original = load_profiles()
    original_system = original.system_for("reviewer", "security")
    old_manifest = original.manifest()
    with (root / "skills/security/SKILL.md").open("a") as stream:
        stream.write("\nTRUSTED_RESOURCE_REVISION_SENTINEL\n")
    new = load_profiles()
    assert new.manifest()["bundle_digest"] != old_manifest["bundle_digest"]
    assert original.system_for("reviewer", "security") == original_system
    assert "TRUSTED_RESOURCE_REVISION_SENTINEL" in new.system_for("reviewer", "security")
    assert "TRUSTED_RESOURCE_REVISION_SENTINEL" not in original_system
    assert old_manifest["profiles"]["correctness"]["skills"][0]["id"] == "correctness"
    assert old_manifest["profiles"]["security"]["skills"][0]["id"] == "security"
    # Caller changes to a returned manifest cannot mutate the approved bundle.
    old_manifest["profiles"]["security"]["skills"].clear()
    assert len(original.manifest()["profiles"]["security"]["skills"]) == 1
    with pytest.raises(FrozenInstanceError):
        original.shared = None


@pytest.mark.parametrize("interactive,mode", [(False, "multi"), (True, "multi"), (True, "single")])
def test_graph_routes_actual_system_messages_by_stage_and_validated_role(
    tmp_path, interactive, mode
):
    from test_development_controls import development_fixture

    from reviewflow.models import ScanPolicy
    from reviewflow.review.demo import DemoBackend
    from reviewflow.review.interactive import SessionDemoBackend, run_interactive_review
    from reviewflow.review.workflow import run_review
    from reviewflow.snapshot import build_inventory
    from reviewflow.sources.directory import DirectorySource

    _, source, _ = development_fixture(tmp_path)
    (source.info.root / "a.py").write_text("# REVIEWFLOW_DEMO_FINDING\nx = 1\n")
    source = DirectorySource(source.info.root)
    inventory = build_inventory(source, ScanPolicy())
    requests = []
    delegate = SessionDemoBackend() if interactive else DemoBackend()

    class Backend:
        def complete(self, request):
            requests.append(request)
            return delegate.complete(request)

    runner = run_interactive_review if interactive else run_review
    result = runner(source, inventory, Backend(), mode=mode)
    report = result.report if interactive else result
    assert report.status == "complete"
    reviewer_roles = set()
    for request in requests:
        data = json.loads(request.payload)
        if request.stage == "reviewer":
            role = data["task"]["role"]
            reviewer_roles.add(role)
            heading, skill = {
                "correctness": ("# Correctness Reviewer", "State transitions"),
                "security": ("# Security Reviewer", "Trust boundaries"),
                "general": ("# General Reviewer", "State transitions"),
            }[role]
            assert heading in request.system and skill in request.system
            if role == "general":
                assert "Trust boundaries" in request.system
            assert "# Independent Verifier" not in request.system
        elif request.stage == "verifier":
            assert "# Independent Verifier" in request.system
            assert "Counterevidence" in request.system
            assert "# Correctness Reviewer" not in request.system
            assert "# Security Reviewer" not in request.system
        else:
            assert "# Planner" in request.system
    assert reviewer_roles == ({"general"} if mode == "single" else {"correctness", "security"})
    if mode == "multi":
        assert any(request.stage == "verifier" for request in requests)
        if interactive:
            assert any(json.loads(r.payload).get("operation") == "deduplicate" for r in requests)
