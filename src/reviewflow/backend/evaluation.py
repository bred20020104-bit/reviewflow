"""Deterministic scoring of human-adjudicated root causes, not an LLM judge."""

import json
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field

from reviewflow.backend.config import Money
from reviewflow.review.contracts import Identifier, ReviewMessage


class EvaluationError(ValueError):
    pass


class Case(ReviewMessage):
    case_id: Identifier
    group: Identifier
    split: Literal["dev", "test"]
    truth_ids: tuple[Identifier, ...]


class Manifest(ReviewMessage):
    cases: Annotated[tuple[Case, ...], Field(min_length=1, max_length=1000)]


class Match(ReviewMessage):
    finding_id: Identifier
    truth_id: Identifier | None
    verdict: Literal["accepted", "rejected", "unresolved", "unverified"]


class Run(ReviewMessage):
    case_id: Identifier
    candidates: Annotated[tuple[Match, ...], Field(max_length=100)]
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    cost_usd: Money
    seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    repair_attempted: bool = False
    repair_validated: bool = False
    introduced_regression: bool = False


class Results(ReviewMessage):
    execution: Literal["real"]
    mode: Literal["single", "multi-no-verifier", "multi", "multi-no-retry"]
    split: Literal["dev", "test"]
    runs: Annotated[tuple[Run, ...], Field(min_length=1, max_length=1000)]


def score(manifest: dict, results: dict) -> dict:
    """Input claims are human supplied; this function cannot attest experiment provenance."""
    try:
        spec = Manifest.model_validate_json(json.dumps(manifest, allow_nan=False))
        data = Results.model_validate_json(json.dumps(results, allow_nan=False))
        ids, groups = set(), {}
        for case in spec.cases:
            if case.case_id in ids or len(set(case.truth_ids)) != len(case.truth_ids):
                raise ValueError()
            ids.add(case.case_id)
            if case.group in groups and groups[case.group] != case.split:
                raise ValueError()
            groups[case.group] = case.split
        cases = {c.case_id: c for c in spec.cases if c.split == data.split}
        if len({r.case_id for r in data.runs}) != len(data.runs):
            raise ValueError()
        if {r.case_id for r in data.runs} != cases.keys():
            raise ValueError()
        tp = fp = duplicate = clean_fp = correct_reject = wrong_reject = 0
        for run in data.runs:
            case = cases[run.case_id]
            if len({m.finding_id for m in run.candidates}) != len(run.candidates):
                raise ValueError()
            if run.repair_validated and (not run.repair_attempted or run.introduced_regression):
                raise ValueError()
            found = set()
            predicted = []
            for match in run.candidates:
                if match.truth_id is not None and match.truth_id not in case.truth_ids:
                    raise ValueError()
                if data.mode in {"single", "multi-no-verifier"}:
                    if match.verdict != "unverified":
                        raise ValueError()
                    keep = True
                else:
                    if match.verdict == "unverified":
                        raise ValueError()
                    keep = match.verdict == "accepted"
                if match.verdict == "rejected":
                    correct_reject += match.truth_id is None
                    wrong_reject += match.truth_id is not None
                if keep:
                    predicted.append(match)
                    if match.truth_id is None:
                        fp += 1
                    elif match.truth_id in found:
                        duplicate += 1
                        fp += 1
                    else:
                        found.add(match.truth_id)
                        tp += 1
            if not case.truth_ids and predicted:
                clean_fp += 1
        positives = sum(len(c.truth_ids) for c in cases.values())
        clean = sum(not c.truth_ids for c in cases.values())
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / positives if positives else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if recall is not None and precision + recall
            else 0.0
        )
        attempted = sum(r.repair_attempted for r in data.runs)
        return {
            "execution": "real",
            "provenance": "human-supplied; not independently attested",
            "mode": data.mode,
            "split": data.split,
            "cases": len(cases),
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": positives - tp,
            "duplicate_findings": duplicate,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "clean_case_false_positive_rate": clean_fp / clean if clean else None,
            "verifier_correct_rejections": correct_reject,
            "verifier_wrong_rejections": wrong_reject,
            "repair_success_rate": (
                sum(r.repair_validated for r in data.runs) / attempted if attempted else None
            ),
            "introduced_regressions": sum(r.introduced_regression for r in data.runs),
            "input_tokens": sum(r.input_tokens for r in data.runs),
            "output_tokens": sum(r.output_tokens for r in data.runs),
            "cost_usd": str(sum((Decimal(r.cost_usd) for r in data.runs), Decimal(0))),
            "seconds": sum(r.seconds for r in data.runs),
        }
    except (ValueError, TypeError, OverflowError):
        raise EvaluationError(
            "Invalid evaluation: check coverage, split, matches and real execution."
        ) from None
