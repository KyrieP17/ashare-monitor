from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Gate3Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Gate3RunStatus(StrEnum):
    READY_FOR_HUMAN_REVIEW = "ready_for_human_review"
    VALIDATION_FAILED = "validation_failed"


class ToolName(StrEnum):
    GET_MARKET_SNAPSHOT = "get_market_snapshot"
    GET_STOCK_OBSERVATION = "get_stock_observation"
    GET_SECTOR_OBSERVATIONS = "get_sector_observations"
    GET_FUND_FLOW_OBSERVATIONS = "get_fund_flow_observations"
    GET_CATALYST_CONTEXT = "get_catalyst_context"


class ToolInvocationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ToolInvocation(Gate3Model):
    """Audit event for one typed-tool execution.

    observation references are stable data identities. ``llm_tool_call_id`` is
    optional and reserved for a real model provider's per-call event identity.
    """

    invocation_id: UUID = Field(default_factory=uuid4)
    llm_tool_call_id: str | None = None
    tool_name: ToolName
    arguments: dict[str, Any]
    started_at: datetime
    completed_at: datetime
    status: ToolInvocationStatus
    error: str | None = None
    snapshot_id: UUID | None = None
    instrument_coverage: list[str] = Field(default_factory=list)
    returned_observation_ref_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_terminal_result(self) -> ToolInvocation:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        if self.status is ToolInvocationStatus.SUCCEEDED and self.error is not None:
            raise ValueError("successful tool invocation cannot contain an error")
        if self.status is ToolInvocationStatus.SUCCEEDED and self.snapshot_id is None:
            raise ValueError("successful tool invocation requires snapshot_id")
        if self.status is ToolInvocationStatus.FAILED:
            if not self.error:
                raise ValueError("failed tool invocation requires an error")
            if self.snapshot_id is not None:
                raise ValueError("failed tool invocation cannot claim a persisted snapshot_id")
            if self.returned_observation_ref_ids:
                raise ValueError("failed tool invocation cannot claim returned observations")
        return self


class StructuredValidationIssue(Gate3Model):
    issue_code: str
    issue_path: str
    severity: IssueSeverity
    message: str
    related_evidence_id: UUID | None = None
    related_observation_ref_id: str | None = None
    expected_constraint: str


class HardValidationResult(Gate3Model):
    issues: list[StructuredValidationIssue] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.issues


class SemanticReviewIssue(Gate3Model):
    issue_code: str
    issue_path: str
    severity: IssueSeverity
    message: str
    related_evidence_id: UUID | None = None
    requires_human_judgment: bool = True


class SemanticReviewResult(Gate3Model):
    reviewer_kind: str = "recorded"
    summary: str
    issues: list[SemanticReviewIssue] = Field(default_factory=list)


class ProposalReviewRecord(Gate3Model):
    review_id: UUID = Field(default_factory=uuid4)
    proposal_revision_id: UUID
    semantic_review: SemanticReviewResult
    graph_trace: list[str]
    generator_kind: str
    repair_count: int = Field(ge=0, le=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GeneratorCallAudit(Gate3Model):
    attempt: int = Field(ge=1, le=2)
    generator_kind: str
    repair: bool
    input_snapshot_id: UUID
    input_observation_ref_ids: list[str]
    validation_issues: list[StructuredValidationIssue] = Field(default_factory=list)
    output_revision_id: UUID | None = None


class Gate3RunResult(Gate3Model):
    status: Gate3RunStatus
    thesis_id: UUID
    snapshot_id: UUID
    proposal_revision_id: UUID | None = None
    hard_validation: HardValidationResult
    semantic_review: SemanticReviewResult | None = None
    graph_trace: list[str]
    generator_calls: list[GeneratorCallAudit]
    repair_count: int = Field(ge=0, le=1)
    failed_outputs: list[dict[str, Any]] = Field(default_factory=list)
