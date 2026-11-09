"""Fixed role/skill bindings, loaded once into an immutable per-run snapshot."""

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

# Product-owned bindings. No target paths, environment overrides or model-selected skills.
ROLE_SKILLS = (
    ("planner", ("planning",)),
    ("correctness", ("correctness",)),
    ("security", ("security",)),
    ("verifier", ("verification",)),
    ("patch", ("patching",)),
    ("general", ("correctness", "security")),
)
MAX_RESOURCE_BYTES = 8192
MAX_SYSTEM_BYTES = 16384


class ProfileError(ValueError):
    """Fixed messages only; resource contents are not diagnostics."""


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Resource:
    path: str
    text: str

    def manifest(self):
        return {"path": self.path, "sha256": _digest(self.text)}


@dataclass(frozen=True)
class Skill:
    identity: str
    resource: Resource


@dataclass(frozen=True)
class AgentProfile:
    role: str
    agent: Resource
    skills: tuple[Skill, ...]


@dataclass(frozen=True)
class PromptBundle:
    shared: Resource
    profiles: tuple[AgentProfile, ...]

    def system_for(self, stage, role=None):
        if stage == "reviewer" and role in ("correctness", "security", "general"):
            selected = role
        elif stage in ("planner", "verifier", "patch"):
            selected = stage
        else:
            raise ProfileError("Invalid agent profile")
        profile = next((p for p in self.profiles if p.role == selected), None)
        if profile is None:
            raise ProfileError("Invalid agent profile")
        return "\n\n".join(
            [self.shared.text, profile.agent.text] + [s.resource.text for s in profile.skills]
        )

    def manifest(self):
        manifest = {
            "schema_version": 1,
            "shared": self.shared.manifest(),
            "profiles": {
                profile.role: {
                    "agent": profile.agent.manifest(),
                    "skills": [
                        {"id": skill.identity, **skill.resource.manifest()}
                        for skill in profile.skills
                    ],
                }
                for profile in self.profiles
            },
        }
        manifest["bundle_digest"] = _digest(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        )
        return manifest


def _read(root, relative):
    resource = root
    if isinstance(resource, Path) and resource.is_symlink():
        raise ValueError()
    for part in relative.split("/"):
        resource = resource.joinpath(part)
        if isinstance(resource, Path) and resource.is_symlink():
            raise ValueError()
    with resource.open("rb") as stream:
        raw = stream.read(MAX_RESOURCE_BYTES + 1)
    if len(raw) > MAX_RESOURCE_BYTES:
        raise ValueError()
    text = raw.decode("utf-8")
    if not text.strip() or "\0" in text:
        raise ValueError()
    return Resource(relative, text)


def load_profiles():
    """Load product package resources, independent of the working/target directory.

    No process-global cache: later previews see edits. An approved run retains
    its existing frozen bundle even if package files change during that run.
    """
    try:
        root = files("reviewflow.agents")
        shared = _read(root, "SOUL.md")
        skills = {}
        profiles = []
        for role, identities in ROLE_SKILLS:
            for identity in identities:
                if identity not in skills:
                    skills[identity] = Skill(identity, _read(root, f"skills/{identity}/SKILL.md"))
            profiles.append(
                AgentProfile(
                    role,
                    _read(root, f"roles/{role}/AGENT.md"),
                    tuple(skills[identity] for identity in identities),
                )
            )
        bundle = PromptBundle(shared, tuple(profiles))
        for profile in profiles:
            stage = (
                "reviewer"
                if profile.role in ("correctness", "security", "general")
                else profile.role
            )
            if len(bundle.system_for(stage, profile.role).encode("utf-8")) > MAX_SYSTEM_BYTES:
                raise ValueError()
        return bundle
    except (OSError, ValueError, TypeError):
        raise ProfileError("Agent profile resources unavailable or invalid") from None
