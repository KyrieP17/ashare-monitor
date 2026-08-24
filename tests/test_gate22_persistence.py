from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from thesis.adapters import MockDemoAdapter
from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.gate3_generator import RecordedProposalGenerator
from thesis.gate3_graph import Gate3OfflineWorkflow
from thesis.gate3_models import ProposalReviewRecord, SemanticReviewResult
from thesis.models import (
    DiscoverySource,
    ReviewDecision,
    ThesisCard,
    ThesisLifecycleStatus,
)
from thesis.proposal_builders import (
    DeterministicReplayProposalBuilder,
    IncompatibleProposalBuilderError,
    MockProposalBuilder,
)
from thesis.repository import (
    DuplicateActiveThesisError,
    NotFoundError,
    RepositoryError,
    SQLiteThesisRepository,
)
from thesis.semantic_reviewer import RecordedSemanticReviewer
from thesis.symbols import normalize_symbol
from thesis.workflow import ProposalValidationError, ThesisWorkflow


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DAY_1 = date(2026, 8, 20)
DAY_2 = date(2026, 8, 21)


class RaisingBuilder:
    def build_proposal(self, **kwargs):
        raise RuntimeError("simulated builder failure")


class InvalidProvenanceBuilder:
    def build_proposal(self, **kwargs):
        proposal = MockProposalBuilder().build_proposal(**kwargs)
        evidence = proposal.support_evidence[0].model_copy(
            update={"observation_ref_ids": ["obs_missing_from_snapshot"]}
        )
        return proposal.model_copy(update={"support_evidence": [evidence]})


class RaisingReviewer:
    kind = "recorded-failure"

    def review(self, proposal, snapshot):
        raise RuntimeError("simulated semantic reviewer failure")


def _mock_bundle(symbol: str = "sh600519", trade_date: date = DAY_1):
    instrument = normalize_symbol(symbol)
    snapshot = MockDemoAdapter().get_market_snapshot(trade_date, [instrument])
    card = ThesisCard(
        thesis_id=uuid4(),
        instrument=instrument,
        lifecycle_status=ThesisLifecycleStatus.DRAFT,
        discovery_source=DiscoverySource.MANUAL_SEARCH,
        created_from_snapshot_id=snapshot.snapshot_id,
        created_at=datetime.now(UTC),
    )
    proposal = MockProposalBuilder().build_proposal(
        thesis_id=card.thesis_id,
        snapshot=snapshot,
        instrument=instrument,
        version=1,
        derived_from_revision_id=None,
        previous_revision=None,
    )
    review = ProposalReviewRecord(
        proposal_revision_id=proposal.revision_id,
        semantic_review=SemanticReviewResult(summary="test review"),
        graph_trace=["ready_for_human_review"],
        generator_kind="recorded-test",
        repair_count=0,
    )
    return snapshot, card, proposal, review


def test_proposal_builder_is_required_dependency():
    repository = SQLiteThesisRepository()
    try:
        with pytest.raises(TypeError):
            ThesisWorkflow(repository, MockDemoAdapter())  # type: ignore[call-arg]
    finally:
        repository.close()


def test_mismatched_adapter_and_builder_fails_explicitly_without_snapshot():
    repository = SQLiteThesisRepository()
    adapter = ExistingJsonAdapter(DATA_DIR)
    snapshot_id = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")]).snapshot_id
    workflow = ThesisWorkflow(repository, adapter, MockProposalBuilder())
    with pytest.raises(IncompatibleProposalBuilderError, match="requires close_price"):
        workflow.start_thesis("002437", DAY_1, DiscoverySource.MANUAL_SEARCH)
    with pytest.raises(NotFoundError):
        repository.get_snapshot(snapshot_id)
    assert repository.list_active_cards() == []
    repository.close()


def test_initial_builder_failure_leaves_no_snapshot_and_retry_succeeds():
    repository = SQLiteThesisRepository()
    adapter = MockDemoAdapter()
    snapshot_id = adapter.get_market_snapshot(DAY_1, [normalize_symbol("sh600519")]).snapshot_id
    failing = ThesisWorkflow(repository, adapter, RaisingBuilder())
    with pytest.raises(RuntimeError, match="builder failure"):
        failing.start_thesis("sh600519", DAY_1, DiscoverySource.MANUAL_SEARCH)
    with pytest.raises(NotFoundError):
        repository.get_snapshot(snapshot_id)
    assert repository.list_active_cards() == []

    card, proposal = ThesisWorkflow(repository, adapter, MockProposalBuilder()).start_thesis(
        "sh600519", DAY_1, DiscoverySource.MANUAL_SEARCH
    )
    assert repository.get_snapshot(snapshot_id).snapshot_id == snapshot_id
    assert repository.get_revision(proposal.revision_id).thesis_id == card.thesis_id
    repository.close()


def test_update_builder_failure_leaves_no_snapshot_or_revision_and_same_day_retry_succeeds():
    repository = SQLiteThesisRepository()
    adapter = MockDemoAdapter()
    good = ThesisWorkflow(repository, adapter, MockProposalBuilder())
    card, initial = good.start_thesis("sh600519", DAY_1, DiscoverySource.MANUAL_SEARCH)
    good.decide(initial.revision_id, ReviewDecision.ACCEPT)
    accepted_before = repository.get_card(card.thesis_id).current_accepted_revision_id
    day_two_snapshot = adapter.get_market_snapshot(DAY_2, [card.instrument])

    failing = ThesisWorkflow(repository, adapter, RaisingBuilder())
    with pytest.raises(RuntimeError, match="builder failure"):
        failing.propose_update(card.thesis_id, DAY_2)
    assert len(repository.list_revisions(card.thesis_id)) == 1
    assert repository.get_card(card.thesis_id).current_accepted_revision_id == accepted_before
    with pytest.raises(NotFoundError):
        repository.get_snapshot(day_two_snapshot.snapshot_id)

    update = good.propose_update(card.thesis_id, DAY_2)
    assert update.version == 2
    assert repository.get_snapshot(day_two_snapshot.snapshot_id).snapshot_id == day_two_snapshot.snapshot_id
    assert repository.get_card(card.thesis_id).current_accepted_revision_id == accepted_before
    repository.close()


def test_workflow_hard_validation_failure_does_not_persist_any_bundle_member():
    repository = SQLiteThesisRepository()
    adapter = MockDemoAdapter()
    snapshot_id = adapter.get_market_snapshot(DAY_1, [normalize_symbol("sh600519")]).snapshot_id
    workflow = ThesisWorkflow(repository, adapter, InvalidProvenanceBuilder())
    with pytest.raises(ProposalValidationError, match="provenance validation failed"):
        workflow.start_thesis("sh600519", DAY_1, DiscoverySource.MANUAL_SEARCH)
    with pytest.raises(NotFoundError):
        repository.get_snapshot(snapshot_id)
    assert repository.list_active_cards() == []
    repository.close()


def test_graph_semantic_reviewer_failure_creates_no_ready_or_persistence():
    repository = SQLiteThesisRepository()
    adapter = ExistingJsonAdapter(DATA_DIR)
    snapshot_id = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")]).snapshot_id
    workflow = Gate3OfflineWorkflow(
        repository,
        adapter,
        RecordedProposalGenerator(),
        RaisingReviewer(),
    )
    with pytest.raises(RuntimeError, match="semantic reviewer failure"):
        workflow.start_initial_thesis("002437", DAY_1, DiscoverySource.MANUAL_SEARCH)
    assert repository.list_active_cards() == []
    assert repository.list_pending_proposals() == []
    with pytest.raises(NotFoundError):
        repository.get_snapshot(snapshot_id)
    repository.close()


def test_bundle_rolls_back_snapshot_when_card_insert_fails():
    repository = SQLiteThesisRepository()
    first = _mock_bundle("sh600519", DAY_1)
    repository.create_thesis_bundle(*first)
    second = _mock_bundle("sh600519", DAY_2)
    with pytest.raises(DuplicateActiveThesisError):
        repository.create_thesis_bundle(*second)
    with pytest.raises(NotFoundError):
        repository.get_snapshot(second[0].snapshot_id)
    repository.close()


def test_bundle_rolls_back_snapshot_and_card_when_proposal_insert_fails(monkeypatch):
    repository = SQLiteThesisRepository()
    snapshot, card, proposal, review = _mock_bundle()

    def fail_insert(*args, **kwargs):
        raise RuntimeError("simulated proposal insert failure")

    monkeypatch.setattr(repository, "_insert_revision", fail_insert)
    with pytest.raises(RuntimeError, match="proposal insert failure"):
        repository.create_thesis_bundle(snapshot, card, proposal, review)
    with pytest.raises(NotFoundError):
        repository.get_snapshot(snapshot.snapshot_id)
    with pytest.raises(NotFoundError):
        repository.get_card(card.thesis_id)
    repository.close()


def test_bundle_rolls_back_snapshot_card_and_proposal_when_review_insert_fails(monkeypatch):
    repository = SQLiteThesisRepository()
    snapshot, card, proposal, review = _mock_bundle()

    def fail_review(*args, **kwargs):
        raise RuntimeError("simulated review insert failure")

    monkeypatch.setattr(repository, "_insert_review", fail_review)
    with pytest.raises(RuntimeError, match="review insert failure"):
        repository.create_thesis_bundle(snapshot, card, proposal, review)
    for getter, identity in (
        (repository.get_snapshot, snapshot.snapshot_id),
        (repository.get_card, card.thesis_id),
        (repository.get_revision, proposal.revision_id),
    ):
        with pytest.raises(NotFoundError):
            getter(identity)
    repository.close()


def test_update_bundle_insert_failure_rolls_back_snapshot_and_allows_retry(monkeypatch):
    repository = SQLiteThesisRepository()
    adapter = MockDemoAdapter()
    workflow = ThesisWorkflow(repository, adapter, MockProposalBuilder())
    card, initial = workflow.start_thesis("sh600519", DAY_1, DiscoverySource.MANUAL_SEARCH)
    workflow.decide(initial.revision_id, ReviewDecision.ACCEPT)
    accepted = repository.get_current_accepted_revision(card.thesis_id)
    assert accepted is not None
    snapshot = adapter.get_market_snapshot(DAY_2, [card.instrument])
    proposal = MockProposalBuilder().build_proposal(
        thesis_id=card.thesis_id,
        snapshot=snapshot,
        instrument=card.instrument,
        version=2,
        derived_from_revision_id=accepted.revision_id,
        previous_revision=accepted,
    )

    original_insert = repository._insert_revision
    with monkeypatch.context() as scoped:
        scoped.setattr(
            repository,
            "_insert_revision",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("update insert failure")),
        )
        with pytest.raises(RuntimeError, match="update insert failure"):
            repository.add_update_bundle(snapshot, proposal)
    repository._insert_revision = original_insert

    with pytest.raises(NotFoundError):
        repository.get_snapshot(snapshot.snapshot_id)
    assert len(repository.list_revisions(card.thesis_id)) == 1
    assert repository.get_card(card.thesis_id).current_accepted_revision_id == accepted.revision_id

    retry = workflow.propose_update(card.thesis_id, DAY_2)
    assert retry.version == 2
    assert repository.get_snapshot(snapshot.snapshot_id).snapshot_id == snapshot.snapshot_id
    repository.close()


def test_identical_snapshot_is_idempotent_but_identity_conflict_is_rejected():
    repository = SQLiteThesisRepository()
    snapshot = MockDemoAdapter().get_market_snapshot(DAY_1, [normalize_symbol("sh600519")])
    repository.ensure_snapshot(snapshot)
    repository.ensure_snapshot(snapshot)
    conflicting = snapshot.model_copy(update={"raw_data_hash": "conflicting-hash"})
    with pytest.raises(RepositoryError, match="identity conflict"):
        repository.ensure_snapshot(conflicting)
    assert repository.get_snapshot(snapshot.snapshot_id).raw_data_hash == snapshot.raw_data_hash
    repository.close()


def test_legacy_workflow_and_graph_use_the_same_create_bundle_boundary(tmp_path, monkeypatch):
    workflow_repo = SQLiteThesisRepository(tmp_path / "workflow.sqlite")
    workflow_calls: list[bool] = []
    original_workflow_bundle = workflow_repo.create_thesis_bundle

    def track_workflow_bundle(snapshot, card, proposal, review=None):
        workflow_calls.append(review is not None)
        return original_workflow_bundle(snapshot, card, proposal, review)

    monkeypatch.setattr(workflow_repo, "create_thesis_bundle", track_workflow_bundle)
    ThesisWorkflow(workflow_repo, MockDemoAdapter(), MockProposalBuilder()).start_thesis(
        "sh600519", DAY_1, DiscoverySource.MANUAL_SEARCH
    )
    assert workflow_calls == [False]
    workflow_repo.close()

    graph_repo = SQLiteThesisRepository(tmp_path / "graph.sqlite")
    graph_calls: list[bool] = []
    original_graph_bundle = graph_repo.create_thesis_bundle

    def track_graph_bundle(snapshot, card, proposal, review=None):
        graph_calls.append(review is not None)
        return original_graph_bundle(snapshot, card, proposal, review)

    monkeypatch.setattr(graph_repo, "create_thesis_bundle", track_graph_bundle)
    Gate3OfflineWorkflow(
        graph_repo,
        ExistingJsonAdapter(DATA_DIR),
        RecordedProposalGenerator(),
        RecordedSemanticReviewer(),
    ).start_initial_thesis("002437", DAY_1, DiscoverySource.MANUAL_SEARCH)
    assert graph_calls == [True]
    graph_repo.close()
