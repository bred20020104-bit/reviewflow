"""Owned HEAD-derived candidates for live repair; not an execution sandbox.

No checkout filters, target commands, commits, pushes or implicit cleanup. Local
export policy is deliberately separate from model-upload policy. Original dirty
bytes are used only for conflict detection, never as model context.
"""

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from reviewflow.backend.approval import fingerprint
from reviewflow.backend.live_patch import live_editable
from reviewflow.backend.live_review import selected_inventory
from reviewflow.backend.repair import (
    CheckedPatch,
    PartialApplicationError,
    RepairError,
    validate_patch,
)
from reviewflow.backend.validation import content_digest
from reviewflow.backend.volume_runner import _export_manifest
from reviewflow.backend.workspace import _git, _replace
from reviewflow.errors import CollectionError
from reviewflow.models import ScanPolicy
from reviewflow.policy import path_reason
from reviewflow.review.context import ReviewContext
from reviewflow.snapshot import build_inventory
from reviewflow.sources.directory import DirectorySource


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _identity(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RepairError("Directory identity changed")
    return info.st_dev, info.st_ino


def _read(root, path):
    """Read a bounded ordinary file without following any component symlink."""
    fds, parents = [], []
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(fd)
        info = os.fstat(fd)
        parents.append((info.st_dev, info.st_ino))
        parts = path.split("/")
        for part in parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            fds.append(fd)
            info = os.fstat(fd)
            parents.append((info.st_dev, info.st_ino))
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        fds.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 262144:
            raise RepairError("File changed or unsupported")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(262145)
        after = os.fstat(fd)
        def signature(s):
            return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

        if signature(before) != signature(after) or len(data) != before.st_size:
            raise RepairError("File changed during read")
        return data, tuple(parents), signature(before)
    except OSError:
        raise RepairError("File changed or inaccessible") from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _git_state(root):
    identity = _identity(root)
    marker = (root / ".git").lstat()
    if stat.S_ISDIR(marker.st_mode):
        marker_identity = (marker.st_dev, marker.st_ino)
    elif stat.S_ISREG(marker.st_mode):
        marker_identity = (marker.st_dev, marker.st_ino, _sha(_read(root, ".git")[0]))
    else:
        raise RepairError("Git metadata changed")
    return (
        identity, marker_identity,
        _git(root, ["rev-parse", "HEAD"]).decode().strip(),
        _git(root, ["symbolic-ref", "--short", "HEAD"]).decode().strip(),
        _sha(_git(root, ["ls-files", "--stage", "-z"])),
    )


def _origin_state(root, paths):
    git_state = _git_state(root)
    originals, signatures = [], []
    for path in paths:
        data, parents, signature = _read(root, path)
        originals.append((path, data))
        signatures.append((path, _sha(data), parents, signature))
    return fingerprint({"git": git_state, "files": signatures}), tuple(originals), git_state


@dataclass(frozen=True)
class CandidatePlan:
    root: Path
    destination: Path
    base: str
    branch: str
    selected: tuple[str, ...]
    baseline: tuple[tuple[str, bytes], ...]
    originals: tuple[tuple[str, bytes], ...]
    modes: tuple[tuple[str, int], ...]
    origin_digest: str
    storage_identity: tuple[int, int]

    @classmethod
    def from_head(cls, source, selected_paths, destination):
        try:
            if source.info.mode != "head" or not source.info.commit or not source.info.branch:
                raise RepairError("Live repair requires a named branch with committed HEAD")
            root = source.info.root
            destination = Path(destination).absolute()
            parent = destination.parent
            if parent.is_symlink() or not parent.is_dir():
                raise RepairError("Trusted existing candidate parent required")
            destination = parent.resolve(strict=True) / destination.name
            if os.path.lexists(destination) or destination == root or root in destination.parents:
                raise RepairError("Candidate must be new and outside original repository")
            selected = tuple(sorted(selected_paths))
            if not selected or len(selected) > 20 or len(set(selected)) != len(selected):
                raise RepairError("Invalid candidate review selection")
            entries = tuple(source.entries())
            # Validate metadata and path policy before reading any blobs, including secrets.
            _export_manifest({entry.path: b"" for entry in entries})
            if any(entry.entry_type != "file" or entry.size_bytes is None
                   or entry.size_bytes > 262144 for entry in entries):
                raise RepairError("Unsupported candidate Git tree")
            if sum(entry.size_bytes for entry in entries) > 8388608:
                raise RepairError("Candidate export exceeds budget")
            names = {entry.path for entry in entries}
            if not set(selected) <= names or any(path_reason(p, "file") for p in selected):
                raise RepairError("Selected source cannot be uploaded")
            baseline = tuple(sorted((entry.path, source.read_bytes(entry, max_bytes=262144))
                                    for entry in entries))
            _export_manifest(dict(baseline))
            modes = []
            tree = _git(root, ["ls-tree", "-r", "-z", source.info.commit])
            for record in tree.split(b"\0"):
                if record:
                    metadata, path = record.split(b"\t", 1)
                    mode = metadata.split(b" ", 1)[0]
                    if mode not in (b"100644", b"100755"):
                        raise RepairError("Unsupported candidate mode")
                    modes.append((path.decode("utf-8"), 0o755 if mode == b"100755" else 0o644))
            digest, originals, state = _origin_state(root, selected)
            if state[2:4] != (source.info.commit, source.info.branch):
                raise RepairError("Original branch or HEAD changed")
            return cls(root, destination, source.info.commit, source.info.branch, selected,
                       baseline, originals, tuple(sorted(modes)), digest, _identity(parent.resolve()))
        except (OSError, ValueError, CollectionError):
            raise RepairError("Candidate plan cannot safely represent this repository") from None

    def preview(self):
        baseline = dict(self.baseline)
        return {
            "action": "create-live-repair-candidate",
            "root": str(self.root), "destination": str(self.destination),
            "base": self.base, "branch": self.branch,
            "selected_paths": list(self.selected),
            "files": {p: _sha(data) for p, data in self.baseline},
            "modes": dict(self.modes),
            "export_digest": content_digest(baseline),
            "original_state": self.origin_digest,
            "storage_identity": self.storage_identity,
            "dirty_selected_paths": [p for p, data in self.originals if data != baseline[p]],
            "local_only_paths": [p for p, _ in self.baseline if path_reason(p, "file")],
        }

    def check_origin(self):
        try:
            if _origin_state(self.root, self.selected)[0] != self.origin_digest:
                raise RepairError("Original workspace changed")
        except (OSError, ValueError, CollectionError):
            raise RepairError("Original workspace changed") from None


class RepairCandidate:
    @classmethod
    def create(cls, plan, gate, grant):
        plan.check_origin()
        if (_identity(plan.destination.parent) != plan.storage_identity
                or os.path.lexists(plan.destination)):
            raise RepairError("Candidate destination changed")
        gate.consume(grant, plan.preview())
        _git(plan.root, ["worktree", "add", "--detach", "--no-checkout",
                         str(plan.destination), plan.base])
        _git(plan.destination, ["read-tree", plan.base])
        modes = dict(plan.modes)
        try:
            for relative, data in plan.baseline:
                path = plan.destination / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(data)
                path.chmod(modes[relative])
            candidate = cls()
            candidate.plan = plan
            candidate.path = plan.destination
            candidate._root_identity = _identity(candidate.path)
            pointer, _, signature = _read(candidate.path, ".git")
            candidate._pointer = pointer, signature
            candidate._index = _sha(_git(candidate.path, ["ls-files", "--stage", "-z"]))
            candidate._expected = dict(plan.baseline)
            candidate._last_before = None
            candidate.baseline_digest = content_digest(candidate._expected)
            candidate.files()
            return candidate
        except (OSError, ValueError, CollectionError):
            raise RepairError("Candidate creation incomplete; preserve workspace") from None

    def files(self):
        try:
            if _identity(self.path) != self._root_identity:
                raise RepairError("Candidate root changed")
            pointer, _, signature = _read(self.path, ".git")
            if (
                (pointer, signature) != self._pointer
                or _git(self.path, ["rev-parse", "HEAD"]).decode().strip() != self.plan.base
                or _git(self.path, ["rev-parse", "--abbrev-ref", "HEAD"]) != b"HEAD\n"
                or _sha(_git(self.path, ["ls-files", "--stage", "-z"])) != self._index
            ):
                raise RepairError("Candidate Git metadata changed")
            source = DirectorySource(self.path)
            entries = {entry.path: entry for entry in source.entries()}
            if set(entries) != set(self._expected) | {".git"}:
                raise RepairError("Unknown candidate entries")
            modes = dict(self.plan.modes)
            result = {}
            for path, expected in self._expected.items():
                data, _, signature = _read(self.path, path)
                if data != expected or stat.S_IMODE(signature[2]) != modes[path]:
                    raise RepairError("Candidate files changed")
                result[path] = data
            return result
        except (OSError, ValueError, CollectionError):
            raise RepairError("Candidate files or metadata changed") from None

    def current_digest(self):
        return content_digest(self.files())

    def source_inventory(self, paths):
        self.files()
        if not paths or not set(paths) <= set(self.plan.selected):
            raise RepairError("Candidate review scope exceeds approval")
        source = DirectorySource(self.path)
        inventory = selected_inventory(build_inventory(source, ScanPolicy()), tuple(paths))
        return source, inventory

    def apply(self, patch, context, task, finding):
        self.plan.check_origin()
        self.files()
        if not isinstance(patch, CheckedPatch):
            raise RepairError("Checked patch required")
        scope = tuple(context._records)
        source, inventory = self.source_inventory(scope)
        if task.snapshot_id != inventory.snapshot.snapshot_id:
            raise RepairError("Patch is not for the current candidate")
        current = validate_patch(ReviewContext(source, inventory), task, finding, patch.proposal)
        if current.sha256 != patch.sha256:
            raise RepairError("Checked patch changed")
        if any(not live_editable(r.path) for r in current.proposal.replacements):
            raise RepairError("Live patch path protected")
        self._apply_replacements(current.proposal.replacements)

    def apply_group(self, patch, context, members):
        from reviewflow.backend.grouped_patch import CheckedPatchGroup, validate_group

        self.plan.check_origin()
        self.files()
        if not isinstance(patch, CheckedPatchGroup):
            raise RepairError("Checked group required")
        source, inventory = self.source_inventory(tuple(context._records))
        if any(t.snapshot_id != inventory.snapshot.snapshot_id for t, _ in members):
            raise RepairError("Group is not for current candidate")
        current = validate_group(ReviewContext(source, inventory), members, patch.proposal)
        if current.sha256 != patch.sha256:
            raise RepairError("Checked group changed")
        self._apply_replacements(current.replacements)

    def _apply_replacements(self, replacements):
        before, applied = dict(self._expected), []
        try:
            for replacement in replacements:
                self.plan.check_origin()
                data = replacement.content.encode("utf-8")
                _replace(self.path, replacement.path, self._expected[replacement.path], data)
                self._expected[replacement.path] = data
                applied.append(replacement.path)
            self.files()
        except Exception:
            for path in reversed(applied):
                try:
                    _replace(self.path, path, self._expected[path], before[path])
                    self._expected[path] = before[path]
                except Exception:
                    raise PartialApplicationError("Candidate rollback failed; preserve workspace") from None
            raise RepairError("Candidate patch application failed") from None
        self._last_before = before

    def restore_attempt(self):
        self.files()
        if self._last_before is None:
            raise RepairError("No owned candidate attempt to restore")
        for path, before in self._last_before.items():
            if before != self._expected[path]:
                _replace(self.path, path, self._expected[path], before)
                self._expected[path] = before
        self._last_before = None
        self.files()

    def writeback(self, authority, permit):
        """Apply only validated changes; never stage, commit or reset user work.

        This detects observed conflicts, not arbitrary concurrent-writer races.
        The tested tree is HEAD plus patches, not the user's dirty working tree.
        """
        from reviewflow.backend.repair_authority import ApplicationAuthority

        if type(authority) is not ApplicationAuthority:
            raise RepairError("Trusted application authority required")
        digest = authority.consume(self, permit)
        root = self.plan.root
        baseline = dict(self.plan.baseline)
        files = self.files()
        changed = sorted(p for p, data in files.items() if data != baseline[p])
        if not changed or any(p not in self.plan.selected or not live_editable(p) for p in changed):
            raise RepairError("Application scope invalid")
        states = {p: _read(root, p) for p in self.plan.selected}
        git_state = _git_state(root)
        self.plan.check_origin()
        if any(states[p][0] != baseline[p] for p in changed):
            raise RepairError("Originally dirty destination cannot be overwritten")
        originals = states.copy()
        applied = []
        attempted = None

        def check():
            if _git_state(root) != git_state or self.current_digest() != digest:
                raise RepairError("Application workspace changed")
            if any(_read(root, p) != state for p, state in states.items()):
                raise RepairError("Application source changed")

        try:
            for path in changed:
                check()
                attempted = path
                _replace(root, path, states[path][0], files[path])
                applied.append(path)
                current = _read(root, path)
                if (current[0] != files[path] or current[1] != states[path][1]
                        or current[2][2] != states[path][2][2]):
                    raise RepairError("Application destination changed during write")
                states[path] = current
                attempted = None
            check()
        except Exception:
            try:
                # Do not undo another actor's staging, branch switch or moved parent.
                if _git_state(root) != git_state:
                    raise RepairError("Original Git state changed during application")
                if attempted and attempted not in applied and _read(root, attempted) != states[attempted]:
                    raise RepairError("Uncertain write outcome; preserve original workspace")
                for path in reversed(applied):
                    if _read(root, path) != states[path] or states[path][0] != files[path]:
                        raise RepairError("Cannot restore externally changed destination")
                    _replace(root, path, files[path], originals[path][0])
            except Exception:
                raise PartialApplicationError("Writeback rollback blocked; preserve both workspaces") from None
            raise RepairError("Writeback failed; owned writes restored") from None
        return {
            "applied_files": changed,
            "candidate_digest": digest,
            "file_hashes": {p: _sha(files[p]) for p in changed},
            "commit_ready": False,
            "validated_tree": "HEAD_plus_candidate_changes",
            "uncommitted_changes_tested": False,
        }
