"""Bind workflow requests to checked source, explicit approval and transport budgets.

The injected adapter/counter/authorization UI are trusted application components,
never dynamic plugins or target configuration. This module resolves no credentials.
"""

import json
from pathlib import Path

from reviewflow.backend.diagnostics import SafeTransportError, safe_error_code
from reviewflow.backend.transport import TransportError
from reviewflow.errors import CollectionError
from reviewflow.review.context import ReviewContext
from reviewflow.review.contracts import EvidenceRef, ReviewTask
from reviewflow.review.workflow_models import ModelRequest


class WorkflowGateway:
    def __init__(self, source, inventory, transport, input_upper_bound, authorize):
        self.source = source
        self.inventory = inventory
        self.context = ReviewContext(source, inventory)
        self.transport = transport
        self.input_upper_bound = input_upper_bound
        self.authorize = authorize
        self.records = {r.path: r for r in inventory.snapshot.records if r.status == "included"}

    def complete(self, request: ModelRequest) -> str:
        try:
            config = self.transport.config
            if (
                self.source.info.root.resolve()
                != Path(config.privacy.allowed_project_root).resolve()
            ):
                raise ValueError()
            payload = json.loads(request.payload)
            if not isinstance(payload, dict):
                raise ValueError()
            files = {}

            def evidence(value):
                ref = EvidenceRef.model_validate(value)
                if (
                    ref.path not in config.privacy.allowed_paths
                    or ref.path not in self.records
                    or ref.snapshot_id != self.inventory.snapshot.snapshot_id
                    or ref.content_sha256 != self.records[ref.path].sha256
                ):
                    raise ValueError()
                text = self.context.read_lines(ref.path, ref.start_line, ref.end_line)
                files[ref.path] = ref.content_sha256
                return text

            def visit(value):
                if isinstance(value, list):
                    for item in value:
                        visit(item)
                elif isinstance(value, dict):
                    if "content_sha256" in value and "path" in value:
                        evidence(value)
                    if isinstance(value.get("evidence"), dict):
                        if value.get("text") != evidence(value["evidence"]):
                            raise ValueError()
                    for item in value.values():
                        visit(item)

            visit(payload)
            if "task" in payload:
                task = ReviewTask.model_validate_json(json.dumps(payload["task"]))
                self.context.validate_task(task)
                if not set(task.paths) <= set(config.privacy.allowed_paths):
                    raise ValueError()
            if not files:
                raise ValueError()
            wire = json.dumps(
                {
                    "stage": request.stage,
                    "system": request.system,
                    "payload": payload,
                    "response_schema": request.response_schema,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            bound = self.input_upper_bound(wire)
        except TransportError as exc:
            raise SafeTransportError(safe_error_code(exc)) from None
        except (ValueError, TypeError, KeyError, CollectionError, RecursionError):
            raise TransportError("request_evidence_or_scope") from None
        intent = self.transport.intent(self.inventory.snapshot.snapshot_id, files, wire, bound)
        grant = self.authorize(intent)
        return self.transport.call(self.inventory.snapshot.snapshot_id, files, wire, bound, grant)
