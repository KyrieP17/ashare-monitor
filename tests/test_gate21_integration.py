from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from thesis.existing_json_adapter import ExistingJsonAdapter, ExistingJsonDataError
from thesis.models import (
    DataStatus,
    DiscoverySource,
    ReviewDecision,
    ThesisAssessment,
    ThesisLifecycleStatus,
)
from thesis.proposal_builders import DeterministicReplayProposalBuilder
from thesis.provenance import ProvenanceIssueCode, validate_revision_provenance
from thesis.repository import SQLiteThesisRepository
from thesis.symbols import normalize_symbol
from thesis.workflow import ThesisWorkflow


DATA_DIR = Path(__file__).parents[1] / "data"
DAY_1 = date(2026, 8, 20)
DAY_2 = date(2026, 8, 21)


def membership(snapshot):
    return next(
        metric
        for metric in snapshot.stock_observations[0].membership_metrics
        if metric.metric_key == "in_limit_up_pool"
    )


def deterministic_workflow(repository, data_dir=DATA_DIR):
    return ThesisWorkflow(
        repository,
        ExistingJsonAdapter(data_dir),
        DeterministicReplayProposalBuilder(),
    )


def test_existing_adapter_creates_workflow_proposal_without_close_price_dependency(repository):
    workflow = deterministic_workflow(repository)
    card, proposal = workflow.start_thesis(
        "002437",
        DAY_1,
        DiscoverySource.MANUAL_SEARCH,
    )
    snapshot = repository.get_snapshot(card.created_from_snapshot_id)
    stock = snapshot.stock_observations[0]

    assert all(metric.metric_key != "close_price" for metric in stock.price_metrics)
    assert proposal.support_evidence
    assert proposal.support_evidence[0].observation_ref_ids == [membership(snapshot).observation_ref_id]


def test_no_reliable_price_field_does_not_raise_stop_iteration(repository, tmp_path):
    pool_dir = tmp_path / "pool_cache"
    pool_dir.mkdir()
    shutil.copy(DATA_DIR / "pool_cache" / "20260821.json", pool_dir / "20260821.json")
    workflow = deterministic_workflow(repository, tmp_path)
    card, proposal = workflow.start_thesis(
        "002437",
        DAY_2,
        DiscoverySource.MANUAL_SEARCH,
    )
    snapshot = repository.get_snapshot(card.created_from_snapshot_id)

    assert all(
        metric.metric_key not in {"close_price", "limit_up_close"}
        for metric in snapshot.stock_observations[0].price_metrics
    )
    assert proposal.counter_evidence
    assert proposal.proposed_lifecycle_status is None


def test_complete_pool_emits_available_true_and_false_membership():
    adapter = ExistingJsonAdapter(DATA_DIR)
    present = membership(adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")]))
    absent = membership(adapter.get_market_snapshot(DAY_2, [normalize_symbol("002437")]))

    assert present.status is DataStatus.AVAILABLE and present.value is True
    assert absent.status is DataStatus.AVAILABLE and absent.value is False
    assert "membership_query" in absent.raw_reference
    assert "query_code=002437" in absent.raw_reference
    assert "data.info[code=002437]" not in absent.raw_reference


def test_missing_pool_is_missing_not_false_and_builder_proposes_insufficient_evidence(tmp_path):
    shutil.copy(DATA_DIR / "latest.json", tmp_path / "latest.json")
    adapter = ExistingJsonAdapter(tmp_path)
    snapshot = adapter.get_market_snapshot(DAY_2, [normalize_symbol("sh600519")])
    pool_membership = membership(snapshot)

    assert pool_membership.status is DataStatus.MISSING
    assert pool_membership.value is None

    repository = SQLiteThesisRepository()
    workflow = ThesisWorkflow(repository, adapter, DeterministicReplayProposalBuilder())
    _, proposal = workflow.start_thesis("sh600519", DAY_2, DiscoverySource.MANUAL_SEARCH)
    assert not proposal.support_evidence
    assert not proposal.counter_evidence
    assert "insufficient" in proposal.market_expectation.lower()
    repository.close()


def test_stale_pool_is_excluded_and_disclosed(tmp_path):
    shutil.copy(DATA_DIR / "latest.json", tmp_path / "latest.json")
    pool_dir = tmp_path / "pool_cache"
    pool_dir.mkdir()
    shutil.copy(DATA_DIR / "pool_cache" / "20260820.json", pool_dir / "20260821.json")

    snapshot = ExistingJsonAdapter(tmp_path).get_market_snapshot(
        DAY_2,
        [normalize_symbol("sh600519")],
    )
    all_metrics = (
        snapshot.market_metrics
        + snapshot.stock_observations[0].membership_metrics
        + snapshot.stock_observations[0].price_metrics
        + snapshot.stock_observations[0].fund_flow_metrics
    )
    assert all(metric.status is not DataStatus.STALE for metric in all_metrics)
    assert membership(snapshot).status is DataStatus.MISSING
    assert any("embedded date does not match" in item for item in snapshot.known_limitations)


def test_parse_error_is_explicit_and_cannot_generate_fact(tmp_path):
    (tmp_path / "latest.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(ExistingJsonDataError, match="cannot read or parse"):
        ExistingJsonAdapter(tmp_path).get_market_snapshot(
            DAY_2,
            [normalize_symbol("sh600519")],
        )


@pytest.mark.parametrize(
    ("status", "expected_snapshot_issue"),
    [
        (DataStatus.MISSING, None),
        (DataStatus.CONFLICTED, None),
        (DataStatus.ERROR, ProvenanceIssueCode.ERROR_OBSERVATION),
        (DataStatus.STALE, ProvenanceIssueCode.STALE_OBSERVATION),
    ],
)
def test_unusable_status_cannot_be_wrapped_as_unique_fact(status, expected_snapshot_issue):
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        DAY_1,
        [normalize_symbol("002437")],
    )
    original_metric = membership(snapshot)
    altered_metric = original_metric.model_copy(
        update={"status": status, "value": None if status in {DataStatus.MISSING, DataStatus.ERROR} else True}
    )
    stock = snapshot.stock_observations[0].model_copy(update={"membership_metrics": [altered_metric]})
    altered_snapshot = snapshot.model_copy(update={"stock_observations": [stock]})
    proposal = DeterministicReplayProposalBuilder().build_proposal(
        thesis_id=uuid4(),
        snapshot=snapshot,
        instrument=stock.instrument,
        version=1,
        derived_from_revision_id=None,
        previous_revision=None,
    )

    report = validate_revision_provenance(proposal, altered_snapshot)
    codes = {issue.code for issue in report.issues}
    assert ProvenanceIssueCode.UNUSABLE_OBSERVATION_STATUS in codes
    if expected_snapshot_issue is not None:
        assert expected_snapshot_issue in codes


def test_conflicted_sector_without_metric_ref_is_not_directly_evidence_addressable(tmp_path):
    shutil.copy(DATA_DIR / "limit_up.json", tmp_path / "limit_up.json")
    pool_dir = tmp_path / "pool_cache"
    pool_dir.mkdir()
    shutil.copy(DATA_DIR / "pool_cache" / "20260821.json", pool_dir / "20260821.json")
    payload = (tmp_path / "limit_up.json").read_text(encoding="utf-8")
    payload = payload.replace(
        "苹果产业链+机器人概念+重组+中报预计扭亏",
        "冲突标签",
        1,
    )
    (tmp_path / "limit_up.json").write_text(payload, encoding="utf-8")

    snapshot = ExistingJsonAdapter(tmp_path).get_market_snapshot(
        DAY_2,
        [normalize_symbol("603958")],
    )
    conflicted = [
        sector for sector in snapshot.stock_observations[0].sectors
        if sector.status is DataStatus.CONFLICTED
    ]
    assert conflicted
    assert all(not sector.metrics for sector in conflicted)


def test_002437_two_day_real_replay_preserves_accepted_revision_until_human_decision(repository):
    workflow = deterministic_workflow(repository)
    card, initial = workflow.start_thesis(
        "002437",
        DAY_1,
        DiscoverySource.MANUAL_SEARCH,
        "Gate 2.1 real replay",
    )
    workflow.decide(initial.revision_id, ReviewDecision.ACCEPT)
    accepted_before_update = repository.get_current_accepted_revision(card.thesis_id)

    update = workflow.propose_update(card.thesis_id, DAY_2)
    card_before_decision = repository.get_card(card.thesis_id)

    assert accepted_before_update is not None
    assert update.version == 2
    assert update.derived_from_revision_id == accepted_before_update.revision_id
    assert update.assessment is ThesisAssessment.WEAKENING
    assert update.counter_evidence
    assert "not present" in update.counter_evidence[0].claim
    assert card_before_decision.current_accepted_revision_id == accepted_before_update.revision_id
    assert card_before_decision.lifecycle_status is ThesisLifecycleStatus.ACTIVE
    assert update.proposed_lifecycle_status is None

    workflow.decide(update.revision_id, ReviewDecision.REJECT)
    card_after_reject = repository.get_card(card.thesis_id)
    assert card_after_reject.current_accepted_revision_id == accepted_before_update.revision_id
    assert card_after_reject.lifecycle_status is ThesisLifecycleStatus.ACTIVE
