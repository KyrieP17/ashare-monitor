from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from .gate3_models import GeneratorCallAudit, StructuredValidationIssue
from .models import InstrumentRef, MarketSnapshot, ThesisRevision
from .proposal_builders import DeterministicReplayProposalBuilder


@dataclass(frozen=True)
class GenerationRequest:
    thesis_id: UUID
    snapshot: MarketSnapshot
    instrument: InstrumentRef
    version: int
    derived_from_revision_id: UUID | None
    previous_revision: ThesisRevision | None
    attempt: int
    original_proposal: dict[str, Any] | None = None
    validation_issues: tuple[StructuredValidationIssue, ...] = ()


class ProposalGenerator(Protocol):
    kind: str
    calls: list[GeneratorCallAudit]

    def generate(self, request: GenerationRequest) -> dict[str, Any]: ...


def _observation_refs(snapshot: MarketSnapshot) -> list[str]:
    refs: list[str] = []
    for stock in snapshot.stock_observations:
        for metric in stock.membership_metrics + stock.price_metrics + stock.fund_flow_metrics:
            refs.append(metric.observation_ref_id)
        for sector in stock.sectors:
            refs.extend(metric.observation_ref_id for metric in sector.metrics)
    refs.extend(metric.observation_ref_id for metric in snapshot.market_metrics)
    return refs


@dataclass
class RecordedProposalGenerator:
    """Offline model-boundary double. It is never presented as a live LLM call."""

    fail_first: bool = False
    kind: str = "recorded"
    calls: list[GeneratorCallAudit] = field(default_factory=list)

    def generate(self, request: GenerationRequest) -> dict[str, Any]:
        proposal = DeterministicReplayProposalBuilder().build_proposal(
            thesis_id=request.thesis_id,
            snapshot=request.snapshot,
            instrument=request.instrument,
            version=request.version,
            derived_from_revision_id=request.derived_from_revision_id,
            previous_revision=request.previous_revision,
        )
        payload = proposal.model_dump(mode="json")
        if self.fail_first and request.attempt == 1:
            evidence = payload["support_evidence"] or payload["counter_evidence"]
            if evidence:
                evidence[0]["observation_ref_ids"] = ["obs_not_in_snapshot"]

        self.calls.append(
            GeneratorCallAudit(
                attempt=request.attempt,
                generator_kind=self.kind,
                repair=request.attempt == 2,
                input_snapshot_id=request.snapshot.snapshot_id,
                input_observation_ref_ids=_observation_refs(request.snapshot),
                validation_issues=list(request.validation_issues),
                output_revision_id=proposal.revision_id,
            )
        )
        return deepcopy(payload)
