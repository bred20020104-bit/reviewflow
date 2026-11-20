"""Explicit unverified source input for a NEW run; never deserialize authority."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from reviewflow.backend.approval import fingerprint
from reviewflow.backend.live_patch import live_editable
from reviewflow.backend.nofollow import mode_matches
from reviewflow.backend.repair import RepairError, Replacement
from reviewflow.backend.repair_candidate import _identity, _read, _sha
from reviewflow.backend.validation import content_digest

SEED_POLICY = {"version": 1, "max_depth": 2, "max_report_bytes": 262144,
               "max_changed_files": 20, "max_file_bytes": 65536, "max_total_bytes": 131072,
               "max_reported_calls": 144, "max_reported_patch_attempts": 24,
               "validation": "fresh_tests_and_full_review", "historical_accounting": "report_supplied"}


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate seed field")
        value[key] = item
    return value


def _absolute_read(path):
    path = Path(path).absolute()
    return _read(Path(path.anchor), path.relative_to(path.anchor).as_posix())


def _count(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("Invalid seed count")
    return value


@dataclass(frozen=True)
class SeedPlan:
    report_path: Path
    report_sha256: str
    candidate_path: Path
    candidate_identity: tuple[int, int]
    content_digest: str
    replacements: tuple[Replacement, ...]
    depth: int
    reported_prior_calls: int
    reported_prior_patch_attempts: int

    @classmethod
    def load(cls, plan, report_path):
        try:
            plan.check_origin()
            report_path = Path(report_path).absolute()
            raw, _, _ = _absolute_read(report_path)
            report = json.loads(raw, object_pairs_hook=_object)
            if (report["report_type"] != "live_development_repair" or report["status"] != "blocked"
                    or report["application"] is not None or report["preserve_candidate"] is not True
                    or not report["candidate_diff"] or report["accounting"]["usage_complete"] is not True):
                raise ValueError("Ineligible seed report")
            old = report["preview"]["candidate"]
            current = plan.preview()
            if any(old[k] != current[k] for k in (
                    "root", "base", "branch", "selected_paths", "files", "modes", "original_state")):
                raise ValueError("Seed origin differs")
            candidate_path = Path(report["candidate_path"])
            if (not candidate_path.is_absolute() or str(candidate_path) != old["destination"]
                    or candidate_path == plan.root or plan.root in candidate_path.parents
                    or candidate_path == plan.destination or candidate_path in plan.destination.parents):
                raise ValueError("Invalid seed source")
            identity = _identity(candidate_path)
            lineage = report.get("seed")
            if lineage is None:
                lineage = {}
            if type(lineage) is not dict:
                raise ValueError("Invalid seed lineage")
            prior_depth = _count(lineage.get("depth", 0), SEED_POLICY["max_depth"] - 1)
            old_calls = _count(lineage.get("reported_prior_calls", 0), 48 * prior_depth)
            old_attempts = _count(lineage.get("reported_prior_patch_attempts", 0), 8 * prior_depth)
            current_calls = _count(report["accounting"]["requests_attempted"], 48)
            records = report["accounting"]["records"]
            if (type(records) is not list or len(records) != current_calls
                    or any(type(r) is not dict or r.get("status") != "complete"
                           or r.get("error_code") is not None or type(r.get("usage")) is not dict
                           or any(type(r["usage"].get(k)) is not int or r["usage"][k] < 0
                                  for k in ("input_tokens", "output_tokens"))
                           for r in records)):
                raise ValueError("Seed transport did not complete")
            calls = old_calls + current_calls
            attempts = report["patch_attempts"]
            if type(attempts) is not list or len(attempts) > 8:
                raise ValueError("Invalid seed attempts")
            attempts = old_attempts + len(attempts)
            if calls + 48 > SEED_POLICY["max_reported_calls"] or attempts + 8 > SEED_POLICY["max_reported_patch_attempts"]:
                raise ValueError("Seed chain ceiling")
            last = report["test_runs"][-1]
            if (last["status"] not in {"passed", "failed"} or last["execution"] != "container"
                    or last["suite"] != "repository"):
                raise ValueError("Seed has no usable tested snapshot")
            baseline, originals, modes = dict(plan.baseline), dict(plan.originals), dict(plan.modes)
            actual, replacements, total = {}, [], 0
            for path, before in plan.baseline:
                data, _, signature = _absolute_read(candidate_path / path)
                if not mode_matches(signature[2], modes[path]):
                    raise ValueError("Seed mode changed")
                actual[path] = data
                if data == before:
                    continue
                if (path not in plan.selected or not live_editable(path) or originals[path] != before
                        or len(data) > SEED_POLICY["max_file_bytes"]):
                    raise ValueError("Protected seed source")
                total += len(data)
                replacements.append(Replacement(path=path, before_sha256=_sha(before), content=data.decode("utf-8")))
            seen = set()
            expected_directories = {parent.as_posix() for path in baseline
                                    for parent in Path(path).parents if parent != Path(".")}
            for root, directories, files in os.walk(candidate_path, followlinks=False):
                if any((Path(root) / name).relative_to(candidate_path).as_posix()
                       not in expected_directories for name in directories):
                    raise ValueError("Seed contains unexpected directory")
                if any((Path(root) / name).is_symlink() for name in directories + files):
                    raise ValueError("Seed contains symlink")
                for name in files:
                    relative = (Path(root) / name).relative_to(candidate_path).as_posix()
                    if relative != ".git":
                        seen.add(relative)
            if (seen != set(baseline) or not 1 <= len(replacements) <= SEED_POLICY["max_changed_files"]
                    or total > SEED_POLICY["max_total_bytes"]
                    or content_digest(actual) != last["content_digest"]
                    or _identity(candidate_path) != identity):
                raise ValueError("Seed snapshot differs")
            plan.check_origin()
            return cls(report_path, _sha(raw), candidate_path, identity, content_digest(actual),
                       tuple(replacements), prior_depth + 1, calls, attempts)
        except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError):
            raise RepairError("Seed input rejected; no historical authority restored") from None

    def preview(self):
        return {"report_path": str(self.report_path), "report_sha256": self.report_sha256,
                "candidate_path": str(self.candidate_path), "candidate_identity": self.candidate_identity,
                "content_digest": self.content_digest,
                "files": {r.path: _sha(r.content.encode("utf-8")) for r in self.replacements},
                "depth": self.depth, "reported_prior_calls": self.reported_prior_calls,
                "reported_prior_patch_attempts": self.reported_prior_patch_attempts,
                "validation": "unverified_input", "historical_accounting": "report_supplied"}

    def check(self, plan):
        if fingerprint(self.load(plan, self.report_path).preview()) != fingerprint(self.preview()):
            raise RepairError("Seed input changed after preview")

    def apply(self, candidate, authority):
        authority.check(candidate)
        if (fingerprint(self.preview()) != fingerprint(authority.preview.get("seed"))
                or candidate.current_digest() != candidate.baseline_digest):
            raise RepairError("Seed requires fresh approved candidate")
        self.check(candidate.plan)
        candidate._apply_replacements(self.replacements)
        if candidate.current_digest() != self.content_digest:
            candidate.restore_attempt()
            raise RepairError("Seed application digest differs")
