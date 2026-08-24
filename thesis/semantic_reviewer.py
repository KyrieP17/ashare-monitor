from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .gate3_models import IssueSeverity, SemanticReviewIssue, SemanticReviewResult
from .models import MarketSnapshot, ThesisRevision


class SemanticReviewer(Protocol):
    kind: str

    def review(self, proposal: ThesisRevision, snapshot: MarketSnapshot) -> SemanticReviewResult: ...


@dataclass
class RecordedSemanticReviewer:
    """Offline structured reviewer double; it cannot accept/reject or mutate lifecycle."""

    kind: str = "recorded"
    calls: list[tuple[str, str]] = field(default_factory=list)

    def review(self, proposal: ThesisRevision, snapshot: MarketSnapshot) -> SemanticReviewResult:
        issues: list[SemanticReviewIssue] = []
        for index, evidence in enumerate(proposal.counter_evidence):
            if any(ref.endswith(":in_limit_up_pool") for ref in evidence.metric_refs):
                issues.append(
                    SemanticReviewIssue(
                        issue_code="pool_absence_requires_human_interpretation",
                        issue_path=f"counter_evidence[{index}]",
                        severity=IssueSeverity.WARNING,
                        message=(
                            "Absence from the dated limit-up pool is counter-evidence only; it does not by itself "
                            "prove thesis invalidation, a price decline, or lack of capital participation."
                        ),
                        related_evidence_id=evidence.evidence_id,
                    )
                )
        if proposal.proposed_lifecycle_status is not None:
            issues.append(
                SemanticReviewIssue(
                    issue_code="lifecycle_change_requires_user_decision",
                    issue_path="proposed_lifecycle_status",
                    severity=IssueSeverity.WARNING,
                    message="A proposed lifecycle change is advisory and requires an explicit user decision.",
                )
            )
        self.calls.append((str(proposal.revision_id), str(snapshot.snapshot_id)))
        return SemanticReviewResult(
            reviewer_kind=self.kind,
            summary=(
                "Structured semantic review completed; the proposal remains a pending suggestion for human review."
            ),
            issues=issues,
        )
