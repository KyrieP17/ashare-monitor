from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID
import warnings

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimType(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    ASSUMPTION = "assumption"


class EvidenceDirection(StrEnum):
    SUPPORT = "support"
    COUNTER = "counter"
    NEUTRAL = "neutral"


class EvidenceQuality(StrEnum):
    UNKNOWN = "unknown"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RevisionType(StrEnum):
    AGENT_PROPOSAL = "agent_proposal"
    USER_REVISION = "user_revision"


class ReviewDecision(StrEnum):
    ACCEPT = "accept"
    MODIFY = "modify"
    REJECT = "reject"


class DiscoverySource(StrEnum):
    WATCHLIST = "watchlist"
    MARKET_ACTIVITY = "market_activity"
    EXTERNAL_PLATFORM = "external_platform"
    FRIEND_REFERRAL = "friend_referral"
    MANUAL_SEARCH = "manual_search"
    OTHER = "other"


class DataStatus(StrEnum):
    AVAILABLE = "available"
    MISSING = "missing"
    STALE = "stale"
    CONFLICTED = "conflicted"
    ERROR = "error"


class Exchange(StrEnum):
    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"


class ThesisLifecycleStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    INVALIDATED = "invalidated"
    CLOSED = "closed"
    REJECTED = "rejected"


class ThesisAssessment(StrEnum):
    PENDING = "pending"
    STRENGTHENING = "strengthening"
    UNCHANGED = "unchanged"
    WEAKENING = "weakening"
    CONFLICTED = "conflicted"


class MarketRegime(StrEnum):
    UNKNOWN = "unknown"
    ATTACK = "attack"
    DIVERGENCE = "divergence"
    RETREAT = "retreat"


class InstrumentRef(DomainModel):
    market: Literal["CN"] = "CN"
    exchange: Exchange
    code: str = Field(pattern=r"^\d{6}$")
    name: str | None = None

    @property
    def instrument_id(self) -> str:
        return f"{self.market}.{self.exchange.value}.{self.code}"


class SourceRef(DomainModel):
    source_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    endpoint_or_dataset: str | None = None
    retrieved_at: datetime
    data_as_of: datetime | date | None = None
    definition: str | None = None
    known_limitations: list[str] = Field(default_factory=list)


MetricValue = Decimal | int | str | bool | None


class MetricObservation(DomainModel):
    metric_key: str = Field(min_length=1)
    value: MetricValue
    unit: str | None = None
    status: DataStatus
    source: SourceRef
    observation_ref_id: str = Field(
        min_length=1,
        validation_alias=AliasChoices("observation_ref_id", "tool_call_id"),
    )
    observed_at: datetime
    raw_reference: str | None = None

    @property
    def tool_call_id(self) -> str:
        """Deprecated Gate 1 alias; this was never an LLM invocation ID."""
        warnings.warn(
            "tool_call_id is deprecated; use observation_ref_id",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.observation_ref_id


class SectorObservation(DomainModel):
    sector_id: str | None = None
    sector_name: str = Field(min_length=1)
    taxonomy: str = Field(min_length=1)
    relation: str | None = None
    observation_ref_id: str | None = None
    raw_reference: str | None = None
    metrics: list[MetricObservation] = Field(default_factory=list)
    status: DataStatus
    source: SourceRef


class StockObservation(DomainModel):
    instrument: InstrumentRef
    trade_date: date
    name: str | None = None
    membership_metrics: list[MetricObservation] = Field(default_factory=list)
    price_metrics: list[MetricObservation] = Field(default_factory=list)
    fund_flow_metrics: list[MetricObservation] = Field(default_factory=list)
    sectors: list[SectorObservation] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)


class MarketSnapshot(DomainModel):
    # Pydantic frozen is shallow: repository constraints provide the MVP's
    # append-only semantics; nested lists/models are not deeply immutable.
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: UUID
    trade_date: date
    created_at: datetime
    market_regime: MarketRegime = MarketRegime.UNKNOWN
    market_metrics: list[MetricObservation] = Field(default_factory=list)
    sector_observations: list[SectorObservation] = Field(default_factory=list)
    stock_observations: list[StockObservation] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    raw_data_hash: str = Field(min_length=1)


class EvidenceItem(DomainModel):
    evidence_id: UUID
    claim: str = Field(min_length=1)
    claim_type: ClaimType
    direction: EvidenceDirection
    evidence_quality: EvidenceQuality
    quality_reason: str = Field(min_length=1)
    observation_ref_ids: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("observation_ref_ids", "tool_call_ids"),
    )
    source_refs: list[SourceRef] = Field(default_factory=list)
    metric_refs: list[str] = Field(default_factory=list)
    observed_at: datetime
    known_limitations: list[str] = Field(default_factory=list)

    @property
    def tool_call_ids(self) -> list[str]:
        """Deprecated Gate 1 alias; values are stable observation references."""
        warnings.warn(
            "tool_call_ids is deprecated; use observation_ref_ids",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.observation_ref_ids

    @model_validator(mode="after")
    def validate_provenance(self) -> EvidenceItem:
        if self.claim_type is ClaimType.FACT:
            if not self.source_refs or not self.observation_ref_ids:
                raise ValueError("fact evidence requires source_refs and observation_ref_ids")
        if self.claim_type is ClaimType.INFERENCE and not self.metric_refs:
            raise ValueError("inference evidence requires metric_refs to factual observations")
        return self


class ThesisCard(DomainModel):
    thesis_id: UUID
    instrument: InstrumentRef
    lifecycle_status: ThesisLifecycleStatus
    discovery_source: DiscoverySource
    discovery_note: str | None = None
    created_from_snapshot_id: UUID
    created_at: datetime
    current_accepted_revision_id: UUID | None = None


class ThesisRevision(DomainModel):
    revision_id: UUID
    thesis_id: UUID
    based_on_snapshot_id: UUID
    derived_from_revision_id: UUID | None = None
    revision_type: RevisionType
    version: int = Field(ge=1)
    market_expectation: str = Field(min_length=1)
    assessment: ThesisAssessment
    support_evidence: list[EvidenceItem] = Field(default_factory=list)
    counter_evidence: list[EvidenceItem] = Field(default_factory=list)
    price_in_risks: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    invalidation_response: str | None = None
    proposed_lifecycle_status: ThesisLifecycleStatus | None = None
    accepted: bool = False
    created_at: datetime

    @model_validator(mode="after")
    def validate_evidence_directions(self) -> ThesisRevision:
        if any(item.direction is not EvidenceDirection.SUPPORT for item in self.support_evidence):
            raise ValueError("support_evidence items must have support direction")
        if any(item.direction is not EvidenceDirection.COUNTER for item in self.counter_evidence):
            raise ValueError("counter_evidence items must have counter direction")
        return self


class DecisionEvent(DomainModel):
    decision_id: UUID
    thesis_id: UUID
    proposal_revision_id: UUID
    decision: ReviewDecision
    resulting_revision_id: UUID | None = None
    user_comment: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_result(self) -> DecisionEvent:
        if self.decision is ReviewDecision.MODIFY and self.resulting_revision_id is None:
            raise ValueError("modify decision requires resulting_revision_id")
        if self.decision is not ReviewDecision.MODIFY and self.resulting_revision_id is not None:
            raise ValueError("only modify decision may have resulting_revision_id")
        return self


class CreateThesisRequest(DomainModel):
    instrument: InstrumentRef
    trade_date: date
    discovery_source: DiscoverySource
    discovery_note: str | None = None


class RevisionChanges(DomainModel):
    """User-editable fields when turning a proposal into a user revision."""

    market_expectation: str | None = None
    assessment: ThesisAssessment | None = None
    support_evidence: list[EvidenceItem] | None = None
    counter_evidence: list[EvidenceItem] | None = None
    price_in_risks: list[str] | None = None
    invalidation_conditions: list[str] | None = None
    invalidation_response: str | None = None
    proposed_lifecycle_status: ThesisLifecycleStatus | None = None
