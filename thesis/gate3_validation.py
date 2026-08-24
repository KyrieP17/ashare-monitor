from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import ValidationError

from .gate3_models import HardValidationResult, IssueSeverity, StructuredValidationIssue
from .models import MarketSnapshot, RevisionType, ThesisRevision
from .provenance import validate_revision_provenance


class HardProposalValidator:
    """Run full Pydantic/schema, identity, and provenance validation."""

    def validate(
        self,
        raw_proposal: dict[str, Any],
        *,
        snapshot: MarketSnapshot,
        thesis_id: UUID,
        version: int,
        derived_from_revision_id: UUID | None,
    ) -> tuple[ThesisRevision | None, HardValidationResult]:
        try:
            proposal = ThesisRevision.model_validate(raw_proposal)
        except ValidationError as exc:
            issues = [
                StructuredValidationIssue(
                    issue_code=f"schema.{error['type']}",
                    issue_path=".".join(str(item) for item in error["loc"]),
                    severity=IssueSeverity.ERROR,
                    message=error["msg"],
                    expected_constraint="Proposal must satisfy the complete ThesisRevision schema.",
                )
                for error in exc.errors()
            ]
            return None, HardValidationResult(issues=issues)

        identity_checks = (
            (proposal.thesis_id == thesis_id, "thesis_id", "identity.thesis_id", "Expected requested thesis_id."),
            (
                proposal.based_on_snapshot_id == snapshot.snapshot_id,
                "based_on_snapshot_id",
                "identity.snapshot_id",
                "Expected the supplied immutable snapshot_id.",
            ),
            (proposal.version == version, "version", "identity.version", "Expected next repository version."),
            (
                proposal.derived_from_revision_id == derived_from_revision_id,
                "derived_from_revision_id",
                "identity.parent",
                "Expected current accepted revision as parent.",
            ),
            (
                proposal.revision_type is RevisionType.AGENT_PROPOSAL,
                "revision_type",
                "identity.revision_type",
                "Generator may only submit an agent_proposal.",
            ),
            (not proposal.accepted, "accepted", "state.accepted", "Generator output must be unaccepted."),
        )
        issues = [
            StructuredValidationIssue(
                issue_code=code,
                issue_path=path,
                severity=IssueSeverity.ERROR,
                message=expected,
                expected_constraint=expected,
            )
            for valid, path, code, expected in identity_checks
            if not valid
        ]
        provenance = validate_revision_provenance(proposal, snapshot)
        evidence_by_path = {
            **{f"support_evidence[{i}]": item for i, item in enumerate(proposal.support_evidence)},
            **{f"counter_evidence[{i}]": item for i, item in enumerate(proposal.counter_evidence)},
        }
        for issue in provenance.issues:
            evidence_path = issue.path.split(".")[0]
            evidence = evidence_by_path.get(evidence_path)
            related_ref = None
            if evidence is not None and evidence.observation_ref_ids:
                related_ref = evidence.observation_ref_ids[0]
            issues.append(
                StructuredValidationIssue(
                    issue_code=f"provenance.{issue.code.value}",
                    issue_path=issue.path,
                    severity=IssueSeverity.ERROR,
                    message=issue.message,
                    related_evidence_id=evidence.evidence_id if evidence else None,
                    related_observation_ref_id=related_ref,
                    expected_constraint="All facts and numbers must resolve to usable observations in the supplied snapshot.",
                )
            )
        return proposal, HardValidationResult(issues=issues)
