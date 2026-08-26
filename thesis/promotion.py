from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from .candidate_repository import SQLiteCandidateRepository
from .candidate_research_adapter import CandidateResearchAdapter
from .candidates import CandidateCard, CandidateDecision
from .gate3_generator import RecordedProposalGenerator
from .gate3_graph import Gate3OfflineWorkflow
from .gate3_models import Gate3RunStatus
from .gate3_tools import ReadOnlyMarketTools
from .models import DiscoverySource, RevisionType
from .openai_provider import OpenAIProviderAdapter, RequestsOpenAITransport
from .openai_workflow import OpenAIInitialResearchWorkflow
from .repository import SQLiteThesisRepository
from .semantic_reviewer import RecordedSemanticReviewer


class ResearchMode(StrEnum):
    OPENAI_LIVE = "openai-live"
    RECORDED = "recorded"


class PromotionResearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResearchExecution:
    thesis_id: UUID
    proposal_revision_id: UUID
    mode: ResearchMode


@dataclass(frozen=True)
class PromotionOutcome:
    thesis_id: UUID
    proposal_revision_id: UUID | None
    mode: ResearchMode
    reused_existing: bool


ResearchRunner = Callable[[CandidateCard], ResearchExecution]


class CandidatePromotionService:
    """Connect a PROMOTE decision to one persisted, human-reviewable thesis."""

    def __init__(
        self,
        candidate_repository: SQLiteCandidateRepository,
        thesis_repository: SQLiteThesisRepository,
        *,
        runner: ResearchRunner | None = None,
    ) -> None:
        self.candidate_repository = candidate_repository
        self.thesis_repository = thesis_repository
        self.runner = runner or self._run_default

    def promote(self, candidate_id: str) -> PromotionOutcome:
        candidate = self.candidate_repository.get(candidate_id)
        existing = self.thesis_repository.find_active_card_by_instrument(
            candidate.instrument_id
        )
        if existing is not None:
            proposal_id, mode = self._existing_research(existing.thesis_id)
            self.candidate_repository.set_decision(candidate_id, CandidateDecision.PROMOTE)
            return PromotionOutcome(
                thesis_id=existing.thesis_id,
                proposal_revision_id=proposal_id,
                mode=mode,
                reused_existing=True,
            )

        try:
            execution = self.runner(candidate)
        except PromotionResearchError:
            raise
        except Exception as exc:
            raise PromotionResearchError(
                f"research workflow failed: {type(exc).__name__}"
            ) from exc

        self.candidate_repository.set_decision(candidate_id, CandidateDecision.PROMOTE)
        return PromotionOutcome(
            thesis_id=execution.thesis_id,
            proposal_revision_id=execution.proposal_revision_id,
            mode=execution.mode,
            reused_existing=False,
        )

    def _run_default(self, candidate: CandidateCard) -> ResearchExecution:
        adapter = CandidateResearchAdapter(candidate)
        source = (
            DiscoverySource.WATCHLIST
            if {"RESEARCH_FOCUS", "WATCHLIST_ACTIVITY"} & set(candidate.trigger_rules)
            else DiscoverySource.MARKET_ACTIVITY
        )
        note = f"Candidate PROMOTE: {candidate.reason_text}"
        if os.environ.get("OPENAI_API_KEY"):
            tools = ReadOnlyMarketTools(
                adapter,
                self.thesis_repository,
                default_instruments=[candidate.instrument_id],
            )
            provider = OpenAIProviderAdapter(RequestsOpenAITransport(), tools)
            result = OpenAIInitialResearchWorkflow(
                self.thesis_repository,
                provider,
                reviewer=RecordedSemanticReviewer(),
            ).start_initial_thesis(
                adapter.instrument,
                candidate.trade_date,
                source,
                note,
            )
            return ResearchExecution(
                thesis_id=result.thesis_id,
                proposal_revision_id=result.proposal_revision_id,
                mode=ResearchMode.OPENAI_LIVE,
            )

        result = Gate3OfflineWorkflow(
            self.thesis_repository,
            adapter,
            RecordedProposalGenerator(),
            RecordedSemanticReviewer(),
        ).start_initial_thesis(
            adapter.instrument,
            candidate.trade_date,
            source,
            note,
        )
        if (
            result.status is not Gate3RunStatus.READY_FOR_HUMAN_REVIEW
            or result.proposal_revision_id is None
        ):
            raise PromotionResearchError("recorded research did not produce a reviewable proposal")
        return ResearchExecution(
            thesis_id=result.thesis_id,
            proposal_revision_id=result.proposal_revision_id,
            mode=ResearchMode.RECORDED,
        )

    def _existing_research(self, thesis_id: UUID) -> tuple[UUID | None, ResearchMode]:
        pending = self.thesis_repository.list_pending_proposals(thesis_id)
        if pending:
            proposal = pending[-1]
        else:
            accepted = self.thesis_repository.get_current_accepted_revision(thesis_id)
            if accepted is None:
                return None, ResearchMode.RECORDED
            proposal = accepted
            if proposal.revision_type is RevisionType.USER_REVISION:
                if proposal.derived_from_revision_id is None:
                    return proposal.revision_id, ResearchMode.RECORDED
                proposal = self.thesis_repository.get_revision(
                    proposal.derived_from_revision_id
                )
        review = self.thesis_repository.get_proposal_review(proposal.revision_id)
        mode = (
            ResearchMode.OPENAI_LIVE
            if review.generator_kind.startswith("openai:")
            else ResearchMode.RECORDED
        )
        return proposal.revision_id, mode
