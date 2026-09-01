from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from .catalyst import CATALYST_METRIC_KEY
from .models import (
    ClaimType,
    DataStatus,
    EvidenceDirection,
    EvidenceItem,
    EvidenceQuality,
    InstrumentRef,
    MarketSnapshot,
    RevisionType,
    ThesisAssessment,
    ThesisLifecycleStatus,
    ThesisRevision,
)


class IncompatibleProposalBuilderError(ValueError):
    pass


class ProposalBuilder(Protocol):
    """Boundary for proposal generation; implementations never persist state."""

    def build_proposal(
        self,
        *,
        thesis_id: UUID,
        snapshot: MarketSnapshot,
        instrument: InstrumentRef,
        version: int,
        derived_from_revision_id: UUID | None,
        previous_revision: ThesisRevision | None,
        proposed_lifecycle_status: ThesisLifecycleStatus | None = None,
    ) -> ThesisRevision: ...


class LLMProposalBuilder(Protocol):
    """Gate 3 interface concept only; no implementation exists in Gate 2.1."""

    def build_proposal(
        self,
        *,
        thesis_id: UUID,
        snapshot: MarketSnapshot,
        instrument: InstrumentRef,
        version: int,
        derived_from_revision_id: UUID | None,
        previous_revision: ThesisRevision | None,
        proposed_lifecycle_status: ThesisLifecycleStatus | None = None,
    ) -> ThesisRevision: ...


class MockProposalBuilder:
    """Preserves Gate 1's deterministic mock proposal semantics."""

    def build_proposal(
        self,
        *,
        thesis_id: UUID,
        snapshot: MarketSnapshot,
        instrument: InstrumentRef,
        version: int,
        derived_from_revision_id: UUID | None,
        previous_revision: ThesisRevision | None,
        proposed_lifecycle_status: ThesisLifecycleStatus | None = None,
    ) -> ThesisRevision:
        del previous_revision
        observation = next(
            (
                item
                for item in snapshot.stock_observations
                if item.instrument.instrument_id == instrument.instrument_id
            ),
            None,
        )
        if observation is None:
            raise IncompatibleProposalBuilderError(
                f"MockProposalBuilder requires a stock observation for {instrument.instrument_id}"
            )
        price = next(
            (item for item in observation.price_metrics if item.metric_key == "close_price"),
            None,
        )
        if price is None:
            raise IncompatibleProposalBuilderError(
                "MockProposalBuilder requires close_price; use DeterministicReplayProposalBuilder "
                "for ExistingJsonAdapter snapshots"
            )
        fact = EvidenceItem(
            evidence_id=uuid4(),
            claim=f"Mock fixture close price is {price.value} {price.unit} as of {snapshot.trade_date.isoformat()}.",
            claim_type=ClaimType.FACT,
            direction=EvidenceDirection.SUPPORT,
            evidence_quality=EvidenceQuality.HIGH,
            quality_reason="Directly copied from the adapter's typed price observation.",
            observation_ref_ids=[price.observation_ref_id],
            source_refs=[price.source],
            metric_refs=[f"stock:{instrument.instrument_id}:close_price"],
            observed_at=price.observed_at,
            known_limitations=["Synthetic fixture; not live market evidence."],
        )
        return ThesisRevision(
            revision_id=uuid4(),
            thesis_id=thesis_id,
            based_on_snapshot_id=snapshot.snapshot_id,
            derived_from_revision_id=derived_from_revision_id,
            revision_type=RevisionType.AGENT_PROPOSAL,
            version=version,
            market_expectation="Record the short-term expectation and challenge it against new sourced observations.",
            assessment=ThesisAssessment.PENDING if version == 1 else ThesisAssessment.UNCHANGED,
            support_evidence=[fact],
            counter_evidence=[],
            price_in_risks=["The observed price may already reflect the tracked expectation."],
            invalidation_conditions=["User judges that the original market expectation no longer explains the setup."],
            invalidation_response="Pause and let the user decide whether to invalidate or close the thesis.",
            proposed_lifecycle_status=proposed_lifecycle_status,
            accepted=False,
            created_at=datetime.now(UTC),
        )


class DeterministicReplayProposalBuilder:
    """Build neutral replay proposals from usable observations without recommendations."""

    def build_proposal(
        self,
        *,
        thesis_id: UUID,
        snapshot: MarketSnapshot,
        instrument: InstrumentRef,
        version: int,
        derived_from_revision_id: UUID | None,
        previous_revision: ThesisRevision | None,
        proposed_lifecycle_status: ThesisLifecycleStatus | None = None,
    ) -> ThesisRevision:
        stock = next(
            (
                item
                for item in snapshot.stock_observations
                if item.instrument.instrument_id == instrument.instrument_id
            ),
            None,
        )
        membership = None
        catalyst = None
        if stock is not None:
            membership = next(
                (
                    metric
                    for metric in stock.membership_metrics
                    if metric.metric_key == "in_limit_up_pool"
                    and metric.status is DataStatus.AVAILABLE
                    and isinstance(metric.value, bool)
                ),
                None,
            )
            catalyst = next(
                (
                    metric
                    for metric in stock.membership_metrics
                    if metric.metric_key == CATALYST_METRIC_KEY
                    and metric.status is DataStatus.AVAILABLE
                    and isinstance(metric.value, str)
                    and metric.value.strip()
                ),
                None,
            )

        support: list[EvidenceItem] = []
        counter: list[EvidenceItem] = []
        assessment = ThesisAssessment.PENDING if previous_revision is None else ThesisAssessment.UNCHANGED
        if membership is not None:
            present = bool(membership.value)
            evidence = EvidenceItem(
                evidence_id=uuid4(),
                claim=(
                    f"The stock was present in the successfully loaded limit-up pool for "
                    f"{snapshot.trade_date.isoformat()}."
                    if present
                    else f"The stock was not present in the successfully loaded limit-up pool for "
                    f"{snapshot.trade_date.isoformat()}."
                ),
                claim_type=ClaimType.FACT,
                direction=EvidenceDirection.SUPPORT if present else EvidenceDirection.COUNTER,
                evidence_quality=EvidenceQuality.HIGH,
                quality_reason="Deterministic membership query against the complete dated limit-up pool.",
                observation_ref_ids=[membership.observation_ref_id],
                source_refs=[membership.source],
                metric_refs=[f"stock:{instrument.instrument_id}:in_limit_up_pool"],
                observed_at=membership.observed_at,
                known_limitations=[
                    "Pool absence does not imply a price decline, thesis invalidation, or lack of capital participation."
                ] if not present else [],
            )
            if present:
                support.append(evidence)
            else:
                counter.append(evidence)
                previous_was_present = bool(
                    previous_revision
                    and any(
                        ref.endswith(":in_limit_up_pool")
                        for item in previous_revision.support_evidence
                        for ref in item.metric_refs
                    )
                )
                if previous_was_present:
                    assessment = ThesisAssessment.WEAKENING
            market_expectation = (
                "Record the dated limit-up-pool membership observation and let the user evaluate the thesis."
            )
        else:
            market_expectation = (
                "Available data is insufficient to establish dated limit-up-pool membership; "
                "no positive market fact is proposed."
            )

        if catalyst is not None:
            support.append(
                EvidenceItem(
                    evidence_id=uuid4(),
                    claim=(
                        "The dated limit-up data provider associated the stock with the raw reason text: "
                        f"{catalyst.value}"
                    ),
                    claim_type=ClaimType.FACT,
                    direction=EvidenceDirection.SUPPORT,
                    evidence_quality=EvidenceQuality.MEDIUM,
                    quality_reason=(
                        "The text is copied from the provider's persisted reason field, but the underlying "
                        "event has not been independently verified against an announcement."
                    ),
                    observation_ref_ids=[catalyst.observation_ref_id],
                    source_refs=[catalyst.source],
                    metric_refs=[f"stock:{instrument.instrument_id}:{CATALYST_METRIC_KEY}"],
                    observed_at=catalyst.observed_at,
                    known_limitations=[
                        "Provider reason/theme text is not company-announcement evidence.",
                        "This proposal records what the source associated with the stock, not that every tag is true.",
                    ],
                )
            )
            market_expectation += " Preserve the provider reason as an unverified catalyst hypothesis."

        return ThesisRevision(
            revision_id=uuid4(),
            thesis_id=thesis_id,
            based_on_snapshot_id=snapshot.snapshot_id,
            derived_from_revision_id=derived_from_revision_id,
            revision_type=RevisionType.AGENT_PROPOSAL,
            version=version,
            market_expectation=market_expectation,
            assessment=assessment,
            support_evidence=support,
            counter_evidence=counter,
            price_in_risks=[],
            invalidation_conditions=[
                "The user decides that the original expectation no longer explains the setup."
            ],
            invalidation_response="Pause and require an explicit user lifecycle decision.",
            proposed_lifecycle_status=proposed_lifecycle_status,
            accepted=False,
            created_at=datetime.now(UTC),
        )
