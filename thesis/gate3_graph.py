from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, TypedDict
from uuid import UUID, uuid4

from langgraph.graph import END, START, StateGraph

from .adapters import MarketDataAdapter
from .gate3_generator import GenerationRequest, ProposalGenerator
from .gate3_models import (
    Gate3RunResult,
    Gate3RunStatus,
    HardValidationResult,
    IssueSeverity,
    ProposalReviewRecord,
    SemanticReviewResult,
    StructuredValidationIssue,
)
from .gate3_validation import HardProposalValidator
from .models import (
    DiscoverySource,
    InstrumentRef,
    MarketSnapshot,
    ThesisCard,
    ThesisLifecycleStatus,
    ThesisRevision,
)
from .repository import DuplicateActiveThesisError, SQLiteThesisRepository
from .semantic_reviewer import SemanticReviewer
from .symbols import normalize_symbol


class Gate3State(TypedDict, total=False):
    thesis_id: UUID
    instrument: InstrumentRef
    trade_date: date
    discovery_source: DiscoverySource
    discovery_note: str | None
    snapshot: MarketSnapshot
    card: ThesisCard
    raw_proposal: dict[str, Any]
    original_failed_proposal: dict[str, Any]
    proposal: ThesisRevision | None
    hard_validation: HardValidationResult
    semantic_review: SemanticReviewResult
    attempt: int
    repair_count: int
    trace: list[str]
    failed_outputs: list[dict[str, Any]]
    first_revision_id: str | None


class Gate3OfflineWorkflow:
    """Offline Gate 3A graph. No live model or network behavior is hidden here."""

    def __init__(
        self,
        repository: SQLiteThesisRepository,
        adapter: MarketDataAdapter,
        generator: ProposalGenerator,
        reviewer: SemanticReviewer,
        validator: HardProposalValidator | None = None,
    ) -> None:
        self.repository = repository
        self.adapter = adapter
        self.generator = generator
        self.reviewer = reviewer
        self.validator = validator or HardProposalValidator()
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(Gate3State)
        graph.add_node("generator", self._generate)
        graph.add_node("hard_validation", self._hard_validate)
        graph.add_node("repair_generator", self._repair_generate)
        graph.add_node("repair_hard_validation", self._repair_hard_validate)
        graph.add_node("semantic_reviewer", self._semantic_review)
        graph.add_node("ready_for_human_review", self._ready)
        graph.add_node("validation_failed", self._validation_failed)
        graph.add_edge(START, "generator")
        graph.add_edge("generator", "hard_validation")
        graph.add_conditional_edges(
            "hard_validation",
            self._route_first_validation,
            {
                "review": "semantic_reviewer",
                "repair": "repair_generator",
            },
        )
        graph.add_edge("repair_generator", "repair_hard_validation")
        graph.add_conditional_edges(
            "repair_hard_validation",
            self._route_repair_validation,
            {
                "review": "semantic_reviewer",
                "failed": "validation_failed",
            },
        )
        graph.add_edge("semantic_reviewer", "ready_for_human_review")
        graph.add_edge("ready_for_human_review", END)
        graph.add_edge("validation_failed", END)
        return graph.compile()

    def start_initial_thesis(
        self,
        symbol: str | InstrumentRef,
        trade_date: date,
        discovery_source: DiscoverySource,
        discovery_note: str | None = None,
    ) -> Gate3RunResult:
        instrument = normalize_symbol(symbol) if isinstance(symbol, str) else symbol
        if self.repository.find_active_card_by_instrument(instrument.instrument_id) is not None:
            raise DuplicateActiveThesisError(
                f"an active or draft thesis already exists for {instrument.instrument_id}"
            )
        snapshot = self.adapter.get_market_snapshot(trade_date, [instrument])
        thesis_id = uuid4()
        card = ThesisCard(
            thesis_id=thesis_id,
            instrument=instrument,
            lifecycle_status=ThesisLifecycleStatus.DRAFT,
            discovery_source=discovery_source,
            discovery_note=discovery_note,
            created_from_snapshot_id=snapshot.snapshot_id,
            created_at=datetime.now(UTC),
        )
        call_start = len(self.generator.calls)
        final = self.graph.invoke(
            Gate3State(
                thesis_id=thesis_id,
                instrument=instrument,
                trade_date=trade_date,
                discovery_source=discovery_source,
                discovery_note=discovery_note,
                snapshot=snapshot,
                card=card,
                attempt=1,
                repair_count=0,
                trace=[],
                failed_outputs=[],
            )
        )
        hard = final["hard_validation"]
        proposal = final.get("proposal")
        status = (
            Gate3RunStatus.READY_FOR_HUMAN_REVIEW
            if final["trace"][-1] == "ready_for_human_review"
            else Gate3RunStatus.VALIDATION_FAILED
        )
        return Gate3RunResult(
            status=status,
            thesis_id=thesis_id,
            snapshot_id=snapshot.snapshot_id,
            proposal_revision_id=proposal.revision_id if proposal and status is Gate3RunStatus.READY_FOR_HUMAN_REVIEW else None,
            hard_validation=hard,
            semantic_review=final.get("semantic_review"),
            graph_trace=final["trace"],
            generator_calls=self.generator.calls[call_start:],
            repair_count=final["repair_count"],
            failed_outputs=final["failed_outputs"],
        )

    def _generate(self, state: Gate3State) -> Gate3State:
        raw = self.generator.generate(
            GenerationRequest(
                thesis_id=state["thesis_id"],
                snapshot=state["snapshot"],
                instrument=state["instrument"],
                version=1,
                derived_from_revision_id=None,
                previous_revision=None,
                attempt=1,
            )
        )
        first_id = raw.get("revision_id")
        return {
            "raw_proposal": raw,
            "first_revision_id": str(first_id) if first_id is not None else None,
            "trace": [*state["trace"], "generator"],
        }

    def _hard_validate(self, state: Gate3State) -> Gate3State:
        proposal, result = self.validator.validate(
            state["raw_proposal"],
            snapshot=state["snapshot"],
            thesis_id=state["thesis_id"],
            version=1,
            derived_from_revision_id=None,
        )
        return {
            "proposal": proposal,
            "hard_validation": result,
            "trace": [*state["trace"], "hard_validation"],
        }

    @staticmethod
    def _route_first_validation(state: Gate3State) -> str:
        return "review" if state["hard_validation"].is_valid else "repair"

    def _repair_generate(self, state: Gate3State) -> Gate3State:
        raw = self.generator.generate(
            GenerationRequest(
                thesis_id=state["thesis_id"],
                snapshot=state["snapshot"],
                instrument=state["instrument"],
                version=1,
                derived_from_revision_id=None,
                previous_revision=None,
                attempt=2,
                original_proposal=state["raw_proposal"],
                validation_issues=tuple(state["hard_validation"].issues),
            )
        )
        return {
            "original_failed_proposal": state["raw_proposal"],
            "raw_proposal": raw,
            "repair_count": 1,
            "failed_outputs": [*state["failed_outputs"], state["raw_proposal"]],
            "trace": [*state["trace"], "repair_generator"],
        }

    def _repair_hard_validate(self, state: Gate3State) -> Gate3State:
        proposal, result = self.validator.validate(
            state["raw_proposal"],
            snapshot=state["snapshot"],
            thesis_id=state["thesis_id"],
            version=1,
            derived_from_revision_id=None,
        )
        if proposal is not None and str(proposal.revision_id) == state.get("first_revision_id"):
            result = HardValidationResult(
                issues=[
                    *result.issues,
                    StructuredValidationIssue(
                        issue_code="repair.identity_reused",
                        issue_path="revision_id",
                        severity=IssueSeverity.ERROR,
                        message="Repair must create a complete proposal with a new revision identity.",
                        expected_constraint="Second generator output must use a new revision_id.",
                    ),
                ]
            )
        return {
            "proposal": proposal,
            "hard_validation": result,
            "trace": [*state["trace"], "repair_hard_validation"],
        }

    @staticmethod
    def _route_repair_validation(state: Gate3State) -> str:
        return "review" if state["hard_validation"].is_valid else "failed"

    def _semantic_review(self, state: Gate3State) -> Gate3State:
        proposal = state["proposal"]
        assert proposal is not None
        review = self.reviewer.review(proposal, state["snapshot"])
        return {
            "semantic_review": review,
            "trace": [*state["trace"], "semantic_reviewer"],
        }

    def _ready(self, state: Gate3State) -> Gate3State:
        proposal = state["proposal"]
        assert proposal is not None
        trace = [*state["trace"], "ready_for_human_review"]
        review_record = ProposalReviewRecord(
            proposal_revision_id=proposal.revision_id,
            semantic_review=state["semantic_review"],
            graph_trace=trace,
            generator_kind=self.generator.kind,
            repair_count=state["repair_count"],
        )
        self.repository.create_thesis_bundle(state["snapshot"], state["card"], proposal, review_record)
        return {"trace": trace}

    @staticmethod
    def _validation_failed(state: Gate3State) -> Gate3State:
        return {
            "failed_outputs": [*state["failed_outputs"], state["raw_proposal"]],
            "trace": [*state["trace"], "validation_failed"],
        }
