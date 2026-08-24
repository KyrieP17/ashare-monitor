from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.gate3_generator import RecordedProposalGenerator
from thesis.gate3_graph import Gate3OfflineWorkflow
from thesis.gate3_models import Gate3RunStatus
from thesis.models import DiscoverySource, ReviewDecision, ThesisLifecycleStatus
from thesis.repository import NotFoundError, SQLiteThesisRepository
from thesis.semantic_reviewer import RecordedSemanticReviewer


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _workflow(database: Path, *, fail_first: bool = False):
    repository = SQLiteThesisRepository(database)
    generator = RecordedProposalGenerator(fail_first=fail_first)
    reviewer = RecordedSemanticReviewer()
    workflow = Gate3OfflineWorkflow(
        repository,
        ExistingJsonAdapter(PROJECT_ROOT / "data"),
        generator,
        reviewer,
    )
    return repository, generator, reviewer, workflow


def test_gate3a_real_fixture_full_graph_accept_and_reopen(tmp_path):
    database = tmp_path / "gate3.sqlite"
    repository, generator, reviewer, workflow = _workflow(database)

    result = workflow.start_initial_thesis(
        "sz002437",
        date(2026, 8, 20),
        DiscoverySource.MANUAL_SEARCH,
    )

    assert result.status is Gate3RunStatus.READY_FOR_HUMAN_REVIEW
    assert result.graph_trace == [
        "generator",
        "hard_validation",
        "semantic_reviewer",
        "ready_for_human_review",
    ]
    assert result.hard_validation.is_valid
    assert result.semantic_review is not None
    assert reviewer.calls
    assert result.proposal_revision_id is not None
    proposal = repository.get_revision(result.proposal_revision_id)
    snapshot = repository.get_snapshot(result.snapshot_id)
    snapshot_refs = {
        metric.observation_ref_id
        for stock in snapshot.stock_observations
        for metric in stock.membership_metrics + stock.price_metrics + stock.fund_flow_metrics
    }
    proposal_refs = {
        ref
        for evidence in proposal.support_evidence + proposal.counter_evidence
        for ref in evidence.observation_ref_ids
    }
    assert proposal_refs
    assert proposal_refs <= snapshot_refs
    assert generator.calls[0].generator_kind == "recorded"

    before = repository.get_card(result.thesis_id)
    assert before.current_accepted_revision_id is None
    assert before.lifecycle_status is ThesisLifecycleStatus.DRAFT
    review = repository.get_proposal_review(proposal.revision_id)
    assert review.graph_trace == result.graph_trace

    event, user_revision = repository.review_proposal(proposal.revision_id, ReviewDecision.ACCEPT)
    assert user_revision is None
    accepted = repository.get_card(result.thesis_id)
    assert accepted.current_accepted_revision_id == proposal.revision_id
    assert accepted.lifecycle_status is ThesisLifecycleStatus.ACTIVE
    assert event.proposal_revision_id == proposal.revision_id
    repository.close()

    reopened = SQLiteThesisRepository(database)
    reloaded_card = reopened.get_card(result.thesis_id)
    reloaded_revision = reopened.get_revision(proposal.revision_id)
    decisions = reopened.list_decisions(result.thesis_id)
    assert reloaded_card.current_accepted_revision_id == proposal.revision_id
    assert reloaded_card.lifecycle_status is ThesisLifecycleStatus.ACTIVE
    assert reloaded_revision.accepted is True
    assert decisions == [event]
    assert reopened.get_proposal_review(proposal.revision_id).proposal_revision_id == proposal.revision_id
    reopened.close()


def test_repair_once_regenerates_complete_proposal_with_new_identity(tmp_path):
    repository, generator, _, workflow = _workflow(tmp_path / "repair.sqlite", fail_first=True)
    result = workflow.start_initial_thesis(
        "002437",
        date(2026, 8, 20),
        DiscoverySource.MANUAL_SEARCH,
    )

    assert result.status is Gate3RunStatus.READY_FOR_HUMAN_REVIEW
    assert result.repair_count == 1
    assert result.graph_trace == [
        "generator",
        "hard_validation",
        "repair_generator",
        "repair_hard_validation",
        "semantic_reviewer",
        "ready_for_human_review",
    ]
    assert len(result.generator_calls) == 2
    first, second = result.generator_calls
    assert first.output_revision_id != second.output_revision_id
    assert second.repair is True
    assert second.validation_issues
    assert any(issue.related_observation_ref_id == "obs_not_in_snapshot" for issue in second.validation_issues)
    assert first.input_snapshot_id == second.input_snapshot_id == result.snapshot_id
    assert first.input_observation_ref_ids == second.input_observation_ref_ids
    assert len(result.failed_outputs) == 1
    revisions = repository.list_revisions(result.thesis_id)
    assert [item.revision_id for item in revisions] == [result.proposal_revision_id]
    with pytest.raises(NotFoundError):
        repository.get_revision(first.output_revision_id)
    repository.close()


class SchemaInvalidFirstGenerator(RecordedProposalGenerator):
    def generate(self, request):
        payload = super().generate(request)
        if request.attempt == 1:
            payload.pop("market_expectation")
            payload["revision_id"] = "not-a-uuid"
        return payload


def test_schema_failure_is_structured_and_repaired_without_graph_crash(tmp_path):
    repository = SQLiteThesisRepository(tmp_path / "schema-repair.sqlite")
    generator = SchemaInvalidFirstGenerator()
    result = Gate3OfflineWorkflow(
        repository,
        ExistingJsonAdapter(PROJECT_ROOT / "data"),
        generator,
        RecordedSemanticReviewer(),
    ).start_initial_thesis(
        "CN.SZ.002437",
        date(2026, 8, 20),
        DiscoverySource.MANUAL_SEARCH,
    )
    assert result.status is Gate3RunStatus.READY_FOR_HUMAN_REVIEW
    assert result.repair_count == 1
    assert any(issue.issue_code.startswith("schema.") for issue in result.generator_calls[1].validation_issues)
    assert len(repository.list_revisions(result.thesis_id)) == 1
    repository.close()


class AlwaysInvalidRecordedGenerator(RecordedProposalGenerator):
    def generate(self, request):
        payload = super().generate(request)
        evidence = payload["support_evidence"] or payload["counter_evidence"]
        if evidence:
            evidence[0]["observation_ref_ids"] = ["obs_always_invalid"]
        return payload


def test_second_validation_failure_stops_without_persistence_or_state_change(tmp_path):
    repository = SQLiteThesisRepository(tmp_path / "failed.sqlite")
    generator = AlwaysInvalidRecordedGenerator()
    workflow = Gate3OfflineWorkflow(
        repository,
        ExistingJsonAdapter(PROJECT_ROOT / "data"),
        generator,
        RecordedSemanticReviewer(),
    )
    result = workflow.start_initial_thesis(
        "002437",
        date(2026, 8, 20),
        DiscoverySource.MANUAL_SEARCH,
    )

    assert result.status is Gate3RunStatus.VALIDATION_FAILED
    assert result.proposal_revision_id is None
    assert result.semantic_review is None
    assert result.repair_count == 1
    assert result.graph_trace[-1] == "validation_failed"
    assert len(result.generator_calls) == 2
    assert len(result.failed_outputs) == 2
    assert repository.list_active_cards() == []
    with pytest.raises(NotFoundError):
        repository.get_snapshot(result.snapshot_id)
    repository.close()


def test_graph_contains_real_gate3_nodes(tmp_path):
    repository, _, _, workflow = _workflow(tmp_path / "nodes.sqlite")
    node_names = set(workflow.graph.get_graph().nodes)
    assert {
        "generator",
        "hard_validation",
        "repair_generator",
        "repair_hard_validation",
        "semantic_reviewer",
        "ready_for_human_review",
        "validation_failed",
    } <= node_names
    repository.close()
