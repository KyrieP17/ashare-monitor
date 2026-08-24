from datetime import date
from uuid import uuid4

from thesis.adapters import MockDemoAdapter
from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.models import (
    ClaimType,
    EvidenceDirection,
    EvidenceItem,
    EvidenceQuality,
)
from thesis.provenance import (
    ProvenanceIssueCode,
    make_observation_ref_id,
    validate_revision_provenance,
    validate_snapshot_provenance,
)
from thesis.symbols import normalize_symbol
from thesis.workflow import ThesisWorkflow
from thesis.proposal_builders import MockProposalBuilder
from thesis.repository import SQLiteThesisRepository
from thesis.models import DiscoverySource


def test_observation_ref_changes_when_raw_field_changes():
    common = {
        "dataset": "data/latest.json",
        "trade_date": date(2026, 8, 21),
        "scope": "CN.SH.600519",
        "metric_key": "turnover_amount",
    }
    first = make_observation_ref_id(**common, raw_reference="watchlist[code=sh600519].amount_wan")
    second = make_observation_ref_id(**common, raw_reference="watchlist[code=sh600519].price")
    assert first != second


def test_validator_detects_source_date_mismatch():
    snapshot = ExistingJsonAdapter("data").get_market_snapshot(
        date(2026, 8, 21),
        [normalize_symbol("sh600519")],
    )
    stock = snapshot.stock_observations[0]
    metric = stock.price_metrics[0]
    wrong_source = metric.source.model_copy(update={"data_as_of": date(2026, 8, 20)})
    wrong_metric = metric.model_copy(update={"source": wrong_source})
    wrong_stock = stock.model_copy(update={"price_metrics": [wrong_metric, *stock.price_metrics[1:]]})
    wrong_snapshot = snapshot.model_copy(update={"stock_observations": [wrong_stock]})

    report = validate_snapshot_provenance(wrong_snapshot)
    assert ProvenanceIssueCode.DATE_MISMATCH in {issue.code for issue in report.issues}


def test_revision_references_resolve_to_real_snapshot_observations():
    repository = SQLiteThesisRepository()
    workflow = ThesisWorkflow(repository, MockDemoAdapter(), MockProposalBuilder())
    card, proposal = workflow.start_thesis(
        "sh600519",
        date(2026, 8, 20),
        DiscoverySource.MANUAL_SEARCH,
    )
    snapshot = repository.get_snapshot(card.created_from_snapshot_id)

    report = validate_revision_provenance(proposal, snapshot)
    assert report.is_valid
    repository.close()


def test_validator_rejects_evidence_reference_absent_from_snapshot():
    adapter = MockDemoAdapter()
    snapshot = adapter.get_market_snapshot(date(2026, 8, 20), [normalize_symbol("sh600519")])
    repository = SQLiteThesisRepository()
    workflow = ThesisWorkflow(repository, adapter, MockProposalBuilder())
    _, proposal = workflow.start_thesis(
        "sh600519",
        date(2026, 8, 20),
        DiscoverySource.MANUAL_SEARCH,
    )
    original = proposal.support_evidence[0]
    broken = original.model_copy(update={"observation_ref_ids": ["obs_missing"]})
    broken_revision = proposal.model_copy(update={"support_evidence": [broken]})

    report = validate_revision_provenance(broken_revision, snapshot)
    assert ProvenanceIssueCode.MISSING_OBSERVATION_REF in {issue.code for issue in report.issues}
    repository.close()


def test_validator_rejects_numeric_assumption_without_observation_reference():
    adapter = MockDemoAdapter()
    snapshot = adapter.get_market_snapshot(date(2026, 8, 20), [normalize_symbol("sh600519")])
    repository = SQLiteThesisRepository()
    workflow = ThesisWorkflow(repository, adapter, MockProposalBuilder())
    _, proposal = workflow.start_thesis(
        "sh600519",
        date(2026, 8, 20),
        DiscoverySource.MANUAL_SEARCH,
    )
    assumption = EvidenceItem(
        evidence_id=uuid4(),
        claim="Assume the setup lasts 3 trading days.",
        claim_type=ClaimType.ASSUMPTION,
        direction=EvidenceDirection.SUPPORT,
        evidence_quality=EvidenceQuality.UNKNOWN,
        quality_reason="User assumption only.",
        observed_at=proposal.created_at,
    )
    broken_revision = proposal.model_copy(update={"support_evidence": [assumption]})

    report = validate_revision_provenance(broken_revision, snapshot)
    assert ProvenanceIssueCode.UNSOURCED_NUMBER in {issue.code for issue in report.issues}
    repository.close()


def test_real_reference_cannot_cover_a_made_up_number():
    adapter = MockDemoAdapter()
    snapshot = adapter.get_market_snapshot(date(2026, 8, 20), [normalize_symbol("sh600519")])
    repository = SQLiteThesisRepository()
    workflow = ThesisWorkflow(repository, adapter, MockProposalBuilder())
    _, proposal = workflow.start_thesis(
        "sh600519",
        date(2026, 8, 20),
        DiscoverySource.MANUAL_SEARCH,
    )
    original = proposal.support_evidence[0]
    fabricated = original.model_copy(update={"claim": "The sourced close price was 9999 CNY."})
    broken_revision = proposal.model_copy(update={"support_evidence": [fabricated]})

    report = validate_revision_provenance(broken_revision, snapshot)
    assert ProvenanceIssueCode.NUMERIC_VALUE_MISMATCH in {issue.code for issue in report.issues}
    repository.close()
