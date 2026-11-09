"""Fixed provenance diagnostics; never carry model text or raw exception messages."""

from reviewflow.errors import CollectionError

EVIDENCE_CODES = frozenset({
    "evidence_path_outside_scope", "evidence_snapshot_mismatch",
    "evidence_hash_mismatch", "evidence_line_range_invalid",
    "verification_finding_mismatch", "source_changed",
})
RECOVERABLE_EVIDENCE_CODES = frozenset({
    "verification_finding_mismatch", "evidence_snapshot_mismatch",
    "evidence_hash_mismatch", "evidence_line_range_invalid",
})


class EvidenceValidationError(CollectionError):
    def __init__(self, code):
        if code not in EVIDENCE_CODES:
            raise ValueError("Unknown evidence diagnostic")
        self.code = code
        super().__init__(code)


def evidence_error_code(error):
    if isinstance(error, EvidenceValidationError) and error.code in EVIDENCE_CODES:
        return error.code
    return "invalid_evidence"
