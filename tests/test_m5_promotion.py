from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.candidate_research_adapter import CandidateResearchAdapter
from thesis.candidate_rules import RULE_LIMIT_UP
from thesis.candidates import (
    CandidateCard,
    CandidateDecision,
    CandidateObservation,
    candidate_id_for,
)
from thesis.models import (
    DataStatus,
    DiscoverySource,
    ReviewDecision,
    RevisionChanges,
    ThesisLifecycleStatus,
)
from thesis.gate3_generator import RecordedProposalGenerator
from thesis.gate3_graph import Gate3OfflineWorkflow
from thesis.promotion import (
    CandidatePromotionService,
    PromotionResearchError,
    ResearchExecution,
    ResearchMode,
)
from thesis.provenance import validate_snapshot_provenance
from thesis.repository import SQLiteThesisRepository
from thesis.semantic_reviewer import RecordedSemanticReviewer


ROOT = Path(__file__).resolve().parents[1]
DAY = date(2026, 8, 26)
NOW = datetime(2026, 8, 26, 7, 0, tzinfo=UTC)


def candidate(code: str, name: str = "测试候选") -> CandidateCard:
    instrument_id = f"CN.SZ.{code}"
    observation = CandidateObservation(
        instrument_id=instrument_id,
        instrument_name=name,
        source="public.ths.limit_up_pool",
        data_as_of=NOW,
        retrieved_at=NOW,
        status=DataStatus.AVAILABLE,
        coverage="full_limit_up_pool:1",
        observation_ref_id=f"obs:{code}",
        raw_reference=f"limit_up_pool[{code}]",
        source_snapshot_id="snapshot:m5",
        reason="普通首板测试证据",
        metrics={"boards": 1, "chg_pct": 10.0, "price": 12.34, "open_num": 0},
    )
    return CandidateCard(
        candidate_id=candidate_id_for(DAY, instrument_id),
        trade_date=DAY,
        instrument_id=instrument_id,
        instrument_name=name,
        first_seen_at=NOW,
        last_seen_at=NOW,
        hit_count=1,
        trigger_rules=[RULE_LIMIT_UP],
        reason_text="确定性规则 + 数据源原始 reason 字段拼接：普通首板测试证据",
        source_snapshot_ids=["snapshot:m5"],
        source_names=["public.ths.limit_up_pool"],
        data_as_of=NOW,
        freshness_status=DataStatus.AVAILABLE,
        observations=[observation],
    )


def recorded_runner(repository: SQLiteThesisRepository):
    """Explicit offline retry path; it must never branch on OPENAI_API_KEY."""

    def run(card: CandidateCard) -> ResearchExecution:
        adapter = CandidateResearchAdapter(card)
        result = Gate3OfflineWorkflow(
            repository,
            adapter,
            RecordedProposalGenerator(),
            RecordedSemanticReviewer(),
        ).start_initial_thesis(
            adapter.instrument,
            card.trade_date,
            DiscoverySource.MARKET_ACTIVITY,
            "Explicit Recorded runner for retry isolation test.",
        )
        assert result.proposal_revision_id is not None
        return ResearchExecution(
            thesis_id=result.thesis_id,
            proposal_revision_id=result.proposal_revision_id,
            mode=ResearchMode.RECORDED,
        )

    return run


def test_promote_runs_recorded_research_accepts_and_reopens(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    database = tmp_path / "m5.sqlite"
    with SQLiteCandidateRepository(database) as candidates, SQLiteThesisRepository(database) as theses:
        saved = candidates.upsert([candidate("002295", "首板案例")])[0]
        outcome = CandidatePromotionService(candidates, theses).promote(saved.candidate_id)

        assert outcome.mode is ResearchMode.RECORDED
        assert outcome.reused_existing is False
        assert outcome.proposal_revision_id is not None
        assert candidates.get(saved.candidate_id).user_decision is CandidateDecision.PROMOTE
        proposal = theses.get_revision(outcome.proposal_revision_id)
        review = theses.get_proposal_review(proposal.revision_id)
        assert review.generator_kind == "recorded"
        assert review.graph_trace[-1] == "ready_for_human_review"
        theses.review_proposal(proposal.revision_id, ReviewDecision.ACCEPT)

    with SQLiteCandidateRepository(database) as candidates, SQLiteThesisRepository(database) as reopened:
        card = reopened.get_card(outcome.thesis_id)
        proposal = reopened.get_revision(outcome.proposal_revision_id)
        assert card.lifecycle_status is ThesisLifecycleStatus.ACTIVE
        assert card.current_accepted_revision_id == proposal.revision_id
        assert proposal.accepted is True
        assert len(reopened.list_decisions(card.thesis_id)) == 1
        assert candidates.get(saved.candidate_id).user_decision is CandidateDecision.PROMOTE


def test_repeat_promote_reuses_active_thesis(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    database = tmp_path / "duplicate.sqlite"
    with SQLiteCandidateRepository(database) as candidates, SQLiteThesisRepository(database) as theses:
        saved = candidates.upsert([candidate("002296")])[0]
        service = CandidatePromotionService(candidates, theses)
        first = service.promote(saved.candidate_id)
        second = service.promote(saved.candidate_id)

        assert second.reused_existing is True
        assert second.thesis_id == first.thesis_id
        assert second.proposal_revision_id == first.proposal_revision_id
        assert len(theses.list_cards()) == 1
        assert len(theses.list_pending_proposals(first.thesis_id)) == 1


def test_promote_preserves_sector_metric_provenance(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    database = tmp_path / "sector.sqlite"
    card = candidate("002300", "板块共振案例")
    sector_observation = CandidateObservation(
        instrument_id=card.instrument_id,
        instrument_name=card.instrument_name,
        source="public.sina.board_flow",
        data_as_of=NOW,
        retrieved_at=NOW,
        status=DataStatus.AVAILABLE,
        coverage="board_flow:1",
        observation_ref_id="obs:sector:002300",
        raw_reference="board_flow[有色金属]",
        source_snapshot_id="snapshot:sector:m5",
        reason="有色金属板块共振",
        metrics={
            "board_name": "有色金属",
            "board_net_yi": 2.5,
            "board_chg_pct": 3.2,
            "lead_chg_pct": 9.9,
        },
    )
    card = card.model_copy(
        update={
            "observations": [*card.observations, sector_observation],
            "source_names": [*card.source_names, sector_observation.source],
            "source_snapshot_ids": [
                *card.source_snapshot_ids,
                sector_observation.source_snapshot_id,
            ],
        }
    )

    with SQLiteCandidateRepository(database) as candidates, SQLiteThesisRepository(database) as theses:
        saved = candidates.upsert([card])[0]
        outcome = CandidatePromotionService(candidates, theses).promote(saved.candidate_id)
        thesis_card = theses.get_card(outcome.thesis_id)
        snapshot = theses.get_snapshot(thesis_card.created_from_snapshot_id)

        assert validate_snapshot_provenance(snapshot).is_valid
        sector = snapshot.stock_observations[0].sectors[0]
        assert sector.sector_name == "有色金属"
        assert {metric.metric_key for metric in sector.metrics} == {
            "board_net_yi",
            "board_chg_pct",
            "lead_chg_pct",
        }


def test_research_failure_preserves_candidate_and_is_retryable(tmp_path):
    database = tmp_path / "failure.sqlite"
    with SQLiteCandidateRepository(database) as candidates, SQLiteThesisRepository(database) as theses:
        saved = candidates.upsert([candidate("002297")])[0]

        def fail(_candidate):
            raise RuntimeError("provider unavailable")

        with pytest.raises(PromotionResearchError, match="research workflow failed"):
            CandidatePromotionService(candidates, theses, runner=fail).promote(saved.candidate_id)

        assert candidates.get(saved.candidate_id).user_decision is CandidateDecision.PENDING
        assert theses.list_cards() == []

        retry = CandidatePromotionService(
            candidates,
            theses,
            runner=recorded_runner(theses),
        ).promote(saved.candidate_id)
        assert retry.mode is ResearchMode.RECORDED
        assert theses.get_card(retry.thesis_id).lifecycle_status is ThesisLifecycleStatus.DRAFT


def test_modify_and_reject_persist_after_reopen(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    database = tmp_path / "decisions.sqlite"
    with SQLiteCandidateRepository(database) as candidates, SQLiteThesisRepository(database) as theses:
        modified_candidate, rejected_candidate = candidates.upsert(
            [candidate("002298", "修改案例"), candidate("002299", "拒绝案例")]
        )
        service = CandidatePromotionService(candidates, theses)
        modified = service.promote(modified_candidate.candidate_id)
        rejected = service.promote(rejected_candidate.candidate_id)

        event, user_revision = theses.review_proposal(
            modified.proposal_revision_id,
            ReviewDecision.MODIFY,
            changes=RevisionChanges(market_expectation="用户修改后的研究预期"),
            user_comment="保留证据，修改判断",
        )
        theses.review_proposal(
            rejected.proposal_revision_id,
            ReviewDecision.REJECT,
            user_comment="当前不进入研究",
        )
        assert event.resulting_revision_id == user_revision.revision_id

    with SQLiteThesisRepository(database) as reopened:
        modified_card = reopened.get_card(modified.thesis_id)
        rejected_card = reopened.get_card(rejected.thesis_id)
        accepted = reopened.get_current_accepted_revision(modified.thesis_id)
        assert modified_card.lifecycle_status is ThesisLifecycleStatus.ACTIVE
        assert accepted.market_expectation == "用户修改后的研究预期"
        assert rejected_card.lifecycle_status is ThesisLifecycleStatus.REJECTED
        assert reopened.get_current_accepted_revision(rejected.thesis_id) is None
        assert len(reopened.list_decisions(modified.thesis_id)) == 1
        assert len(reopened.list_decisions(rejected.thesis_id)) == 1


def test_m5_pages_are_registered_and_failure_is_user_visible():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    candidate_source = (ROOT / "pages" / "6_candidates.py").read_text(encoding="utf-8")
    thesis_source = (ROOT / "pages" / "7_thesis.py").read_text(encoding="utf-8")

    assert 'st.Page("pages/7_thesis.py", title="深度研究"' in app_source
    assert "CandidatePromotionService" in candidate_source
    assert "研究失败，可重试。候选决定未改变。" in candidate_source
    assert 'st.switch_page("pages/7_thesis.py")' in candidate_source
    assert "Accept" not in thesis_source
    assert "接受研究" in thesis_source
    assert "修改后接受" in thesis_source
    assert "拒绝研究" in thesis_source
    assert "Recorded/Fake · 离线示例，非真实研究" in thesis_source
