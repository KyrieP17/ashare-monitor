from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from .adapters import MarketDataAdapter
from .models import (
    CreateThesisRequest,
    DecisionEvent,
    DiscoverySource,
    InstrumentRef,
    ReviewDecision,
    RevisionChanges,
    RevisionType,
    ThesisCard,
    ThesisLifecycleStatus,
    ThesisRevision,
)
from .proposal_builders import ProposalBuilder
from .provenance import validate_revision_provenance
from .repository import DuplicateActiveThesisError, SQLiteThesisRepository
from .symbols import normalize_symbol


class ProposalValidationError(ValueError):
    pass


class ThesisWorkflow:
    """Application service enforcing the human-in-the-loop thesis lifecycle."""

    def __init__(
        self,
        repository: SQLiteThesisRepository,
        adapter: MarketDataAdapter,
        proposal_builder: ProposalBuilder,
    ) -> None:
        self.repository = repository
        self.adapter = adapter
        self.proposal_builder = proposal_builder

    def start_thesis(
        self,
        symbol: str | InstrumentRef,
        trade_date: date,
        discovery_source: DiscoverySource,
        discovery_note: str | None = None,
    ) -> tuple[ThesisCard, ThesisRevision]:
        instrument = normalize_symbol(symbol) if isinstance(symbol, str) else symbol
        request = CreateThesisRequest(
            instrument=instrument,
            trade_date=trade_date,
            discovery_source=discovery_source,
            discovery_note=discovery_note,
        )
        existing = self.repository.find_active_card_by_instrument(instrument.instrument_id)
        if existing is not None:
            raise DuplicateActiveThesisError(
                f"an active or draft thesis already exists for {instrument.instrument_id}"
            )
        snapshot = self.adapter.get_market_snapshot(request.trade_date, [request.instrument])
        now = datetime.now(UTC)
        card = ThesisCard(
            thesis_id=uuid4(),
            instrument=request.instrument,
            lifecycle_status=ThesisLifecycleStatus.DRAFT,
            discovery_source=request.discovery_source,
            discovery_note=request.discovery_note,
            created_from_snapshot_id=snapshot.snapshot_id,
            created_at=now,
        )
        proposal = self.proposal_builder.build_proposal(
            thesis_id=card.thesis_id,
            snapshot=snapshot,
            instrument=request.instrument,
            version=1,
            derived_from_revision_id=None,
            previous_revision=None,
        )
        self._validate_proposal(proposal, snapshot, card.thesis_id, 1, None)
        return self.repository.create_thesis_bundle(snapshot, card, proposal)[:2]

    def propose_update(
        self,
        thesis_id: UUID,
        trade_date: date,
        *,
        proposed_lifecycle_status: ThesisLifecycleStatus | None = None,
    ) -> ThesisRevision:
        card = self.repository.get_card(thesis_id)
        current = self.repository.get_current_accepted_revision(thesis_id)
        if current is None:
            raise ValueError("an update requires a current accepted revision")
        snapshot = self.adapter.get_market_snapshot(trade_date, [card.instrument])
        version = self.repository.next_version(card.thesis_id)
        proposal = self.proposal_builder.build_proposal(
            thesis_id=card.thesis_id,
            snapshot=snapshot,
            instrument=card.instrument,
            version=version,
            derived_from_revision_id=current.revision_id,
            previous_revision=current,
            proposed_lifecycle_status=proposed_lifecycle_status,
        )
        self._validate_proposal(
            proposal,
            snapshot,
            card.thesis_id,
            version,
            current.revision_id,
        )
        return self.repository.add_update_bundle(snapshot, proposal)[0]

    def decide(
        self,
        proposal_revision_id: UUID,
        decision: ReviewDecision,
        *,
        changes: RevisionChanges | None = None,
        user_comment: str | None = None,
    ) -> tuple[DecisionEvent, ThesisRevision | None]:
        return self.repository.review_proposal(
            proposal_revision_id,
            decision,
            changes=changes,
            user_comment=user_comment,
        )

    @staticmethod
    def _validate_proposal(
        proposal: ThesisRevision,
        snapshot,
        thesis_id: UUID,
        version: int,
        derived_from_revision_id: UUID | None,
    ) -> None:
        if (
            proposal.thesis_id != thesis_id
            or proposal.based_on_snapshot_id != snapshot.snapshot_id
            or proposal.version != version
            or proposal.derived_from_revision_id != derived_from_revision_id
            or proposal.revision_type is not RevisionType.AGENT_PROPOSAL
            or proposal.accepted
        ):
            raise ProposalValidationError("proposal builder returned invalid revision identity or state")
        report = validate_revision_provenance(proposal, snapshot)
        if not report.is_valid:
            details = "; ".join(f"{issue.code.value}:{issue.path}" for issue in report.issues)
            raise ProposalValidationError(f"proposal provenance validation failed: {details}")
