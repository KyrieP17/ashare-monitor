from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from .gate3_models import (
    Gate3RunStatus,
    HardValidationResult,
    ProposalReviewRecord,
    SemanticReviewResult,
    ToolInvocation,
)
from .gate3_validation import HardProposalValidator
from .models import (
    DiscoverySource,
    InstrumentRef,
    ThesisCard,
    ThesisLifecycleStatus,
)
from .openai_provider import OpenAIGenerationResult, OpenAIProviderAdapter, OpenAIResearchRequest
from .repository import DuplicateActiveThesisError, SQLiteThesisRepository
from .semantic_reviewer import RecordedSemanticReviewer, SemanticReviewer
from .symbols import normalize_symbol


class OpenAIProposalValidationError(ValueError):
    def __init__(self, validation: HardValidationResult) -> None:
        self.validation = validation
        details = "; ".join(f"{item.issue_code}:{item.issue_path}" for item in validation.issues)
        super().__init__(f"OpenAI proposal failed hard/provenance validation: {details}")


@dataclass(frozen=True)
class OpenAIResearchRunResult:
    status: Gate3RunStatus
    thesis_id: UUID
    snapshot_id: UUID
    proposal_revision_id: UUID
    hard_validation: HardValidationResult
    semantic_review: SemanticReviewResult
    graph_trace: list[str]
    tool_invocations: list[ToolInvocation]
    provider_response_ids: list[str]
    tool_rounds: int
    usage: dict[str, int]
    latency_seconds: float


class OpenAIInitialResearchWorkflow:
    """Initial live-model slice; proposals remain pending until explicit human review."""

    def __init__(
        self,
        repository: SQLiteThesisRepository,
        provider: OpenAIProviderAdapter,
        *,
        validator: HardProposalValidator | None = None,
        reviewer: SemanticReviewer | None = None,
    ) -> None:
        if provider.tools.repository is not repository:
            raise ValueError("provider tools and workflow must share one repository")
        self.repository = repository
        self.provider = provider
        self.validator = validator or HardProposalValidator()
        self.reviewer = reviewer or RecordedSemanticReviewer()

    def start_initial_thesis(
        self,
        symbol: str | InstrumentRef,
        trade_date: date,
        discovery_source: DiscoverySource,
        discovery_note: str | None = None,
    ) -> OpenAIResearchRunResult:
        instrument = normalize_symbol(symbol) if isinstance(symbol, str) else symbol
        if self.repository.find_active_card_by_instrument(instrument.instrument_id) is not None:
            raise DuplicateActiveThesisError(
                f"an active or draft thesis already exists for {instrument.instrument_id}"
            )

        thesis_id = uuid4()
        generation: OpenAIGenerationResult = self.provider.generate(
            OpenAIResearchRequest(
                thesis_id=thesis_id,
                instrument=instrument,
                trade_date=trade_date,
            )
        )
        proposal, validation = self.validator.validate(
            generation.raw_proposal,
            snapshot=generation.snapshot,
            thesis_id=thesis_id,
            version=1,
            derived_from_revision_id=None,
        )
        if proposal is None or not validation.is_valid:
            raise OpenAIProposalValidationError(validation)

        semantic_review = self.reviewer.review(proposal, generation.snapshot)
        trace = [
            "openai_function_calling",
            "hard_validation",
            "semantic_reviewer",
            "ready_for_human_review",
        ]
        card = ThesisCard(
            thesis_id=thesis_id,
            instrument=instrument,
            lifecycle_status=ThesisLifecycleStatus.DRAFT,
            discovery_source=discovery_source,
            discovery_note=discovery_note,
            created_from_snapshot_id=generation.snapshot.snapshot_id,
            created_at=datetime.now(UTC),
        )
        review = ProposalReviewRecord(
            proposal_revision_id=proposal.revision_id,
            semantic_review=semantic_review,
            graph_trace=trace,
            generator_kind=f"openai:{self.provider.model}",
            repair_count=0,
        )
        self.repository.create_thesis_bundle(generation.snapshot, card, proposal, review)
        return OpenAIResearchRunResult(
            status=Gate3RunStatus.READY_FOR_HUMAN_REVIEW,
            thesis_id=thesis_id,
            snapshot_id=generation.snapshot.snapshot_id,
            proposal_revision_id=proposal.revision_id,
            hard_validation=validation,
            semantic_review=semantic_review,
            graph_trace=trace,
            tool_invocations=generation.tool_invocations,
            provider_response_ids=generation.response_ids,
            tool_rounds=generation.tool_rounds,
            usage=generation.usage,
            latency_seconds=generation.latency_seconds,
        )
