from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from thesis.adapters import MockDemoAdapter
from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.gate3_models import ToolInvocation, ToolInvocationStatus, ToolName
from thesis.gate3_tools import ReadOnlyMarketTools, ToolCoverageError, ToolInputError
from thesis.models import (
    ClaimType,
    DataStatus,
    EvidenceDirection,
    EvidenceItem,
    EvidenceQuality,
    MarketSnapshot,
    SectorObservation,
)
from thesis.provenance import make_sector_observation_ref_id, validate_revision_provenance
from thesis.proposal_builders import MockProposalBuilder
from thesis.repository import NotFoundError, RepositoryError, SQLiteThesisRepository
from thesis.symbols import normalize_symbol


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DAY_1 = date(2026, 8, 20)


def test_real_typed_tools_return_structured_data_and_audit_stable_refs(tmp_path):
    database = tmp_path / "tools.sqlite"
    repository = SQLiteThesisRepository(database)
    tools = ReadOnlyMarketTools(ExistingJsonAdapter(DATA_DIR), repository)

    snapshot = tools.get_market_snapshot(
        DAY_1,
        ["CN.SZ.002437"],
        llm_tool_call_id="provider-call-1",
    )
    stock = tools.get_stock_observation("CN.SZ.002437", DAY_1)
    sectors = tools.get_sector_observations("CN.SZ.002437", DAY_1)

    assert isinstance(snapshot, MarketSnapshot)
    assert stock.instrument.instrument_id == "CN.SZ.002437"
    assert [
        (item.sector_name, item.taxonomy, item.relation) for item in sectors
    ] == [
        (item.sector_name, item.taxonomy, item.relation) for item in stock.sectors
    ]
    calls = repository.list_tool_invocations()
    assert [item.tool_name for item in calls] == [
        ToolName.GET_MARKET_SNAPSHOT,
        ToolName.GET_STOCK_OBSERVATION,
        ToolName.GET_SECTOR_OBSERVATIONS,
    ]
    assert all(item.status is ToolInvocationStatus.SUCCEEDED for item in calls)
    assert calls[0].llm_tool_call_id == "provider-call-1"
    assert calls[1].llm_tool_call_id is None
    snapshot_refs = {
        metric.observation_ref_id
        for item in snapshot.stock_observations
        for metric in item.membership_metrics + item.price_metrics + item.fund_flow_metrics
    }
    assert snapshot_refs <= set(calls[0].returned_observation_ref_ids)
    assert calls[0].snapshot_id == snapshot.snapshot_id
    assert repository.get_snapshot(calls[0].snapshot_id) == snapshot
    repository.close()

    reopened = SQLiteThesisRepository(database)
    assert reopened.list_tool_invocations() == calls
    assert reopened.get_snapshot(calls[0].snapshot_id) == snapshot
    reopened.close()


def test_sector_tool_audits_every_returned_sector_fact_reference():
    repository = SQLiteThesisRepository()
    tools = ReadOnlyMarketTools(ExistingJsonAdapter(DATA_DIR), repository)

    sectors = tools.get_sector_observations("CN.SZ.002437", DAY_1)
    invocation = repository.list_tool_invocations()[0]

    assert sectors
    assert all(item.observation_ref_id for item in sectors)
    assert set(invocation.returned_observation_ref_ids) == {
        item.observation_ref_id for item in sectors
    }
    repository.close()


def test_sector_fact_references_are_stable_across_independent_parses():
    adapter = ExistingJsonAdapter(DATA_DIR)
    first = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")])
    second = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")])

    first_refs = [item.observation_ref_id for item in first.stock_observations[0].sectors]
    second_refs = [item.observation_ref_id for item in second.stock_observations[0].sectors]

    assert first_refs == second_refs
    assert all(first_refs)
    assert len(first_refs) == len(set(first_refs))


def test_legacy_sector_payload_without_reference_fields_remains_readable():
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        DAY_1, [normalize_symbol("002437")]
    )
    sector_payload = snapshot.stock_observations[0].sectors[0].model_dump(mode="json")
    sector_payload.pop("observation_ref_id")
    sector_payload.pop("raw_reference")

    legacy_sector = SectorObservation.model_validate(sector_payload)

    assert legacy_sector.observation_ref_id is None
    assert legacy_sector.raw_reference is None


def test_multi_day_lookback_is_rejected_instead_of_silently_ignored():
    repository = SQLiteThesisRepository()
    tools = ReadOnlyMarketTools(MockDemoAdapter(), repository)

    with pytest.raises(ToolCoverageError, match="single trading day"):
        tools.get_stock_observation("CN.SH.600519", DAY_1, lookback_days=20)

    invocation = repository.list_tool_invocations()[0]
    assert invocation.status is ToolInvocationStatus.FAILED
    assert invocation.arguments["lookback_days"] == 20
    repository.close()


def test_default_instrument_coverage_is_resolved_into_audit_arguments():
    repository = SQLiteThesisRepository()
    tools = ReadOnlyMarketTools(
        MockDemoAdapter(),
        repository,
        default_instruments=["600519", "300750"],
    )

    tools.get_market_snapshot(DAY_1)
    invocation = repository.list_tool_invocations()[0]

    assert invocation.instrument_coverage == ["CN.SH.600519", "CN.SZ.300750"]
    assert invocation.arguments["resolved_instrument_coverage"] == invocation.instrument_coverage
    repository.close()


def test_successful_invocation_requires_snapshot_and_failed_invocation_forbids_it():
    common = {
        "tool_name": ToolName.GET_MARKET_SNAPSHOT,
        "arguments": {"trade_date": DAY_1.isoformat()},
        "started_at": datetime.now(UTC),
        "completed_at": datetime.now(UTC),
    }
    with pytest.raises(ValidationError, match="snapshot_id"):
        ToolInvocation(
            **common,
            status=ToolInvocationStatus.SUCCEEDED,
        )
    with pytest.raises(ValidationError, match="snapshot_id"):
        ToolInvocation(
            **common,
            status=ToolInvocationStatus.FAILED,
            error="failure after snapshot",
            snapshot_id=uuid4(),
        )


@pytest.mark.parametrize("bad_value", [0, -1])
def test_failed_typed_tool_input_is_audited_without_domain_writes(tmp_path, bad_value):
    repository = SQLiteThesisRepository(tmp_path / f"failed-{bad_value}.sqlite")
    tools = ReadOnlyMarketTools(MockDemoAdapter(), repository)

    with pytest.raises(ToolCoverageError, match="lookback_days"):
        tools.get_stock_observation("CN.SH.600519", DAY_1, lookback_days=bad_value)

    calls = repository.list_tool_invocations()
    assert len(calls) == 1
    assert calls[0].status is ToolInvocationStatus.FAILED
    assert calls[0].snapshot_id is None
    assert calls[0].returned_observation_ref_ids == []
    assert "ToolCoverageError" in (calls[0].error or "")
    assert repository.list_active_cards() == []
    repository.close()


def test_missing_market_coverage_fails_honestly_and_is_audited():
    repository = SQLiteThesisRepository()
    tools = ReadOnlyMarketTools(MockDemoAdapter(), repository)

    with pytest.raises(ToolCoverageError, match="explicit default instrument coverage"):
        tools.get_market_snapshot(DAY_1)

    invocation = repository.list_tool_invocations()[0]
    assert invocation.tool_name is ToolName.GET_MARKET_SNAPSHOT
    assert invocation.status is ToolInvocationStatus.FAILED
    repository.close()


def test_fund_flow_requires_a_scope_and_records_failure():
    repository = SQLiteThesisRepository()
    tools = ReadOnlyMarketTools(MockDemoAdapter(), repository)

    with pytest.raises(ToolInputError, match="instrument_id or sector_name"):
        tools.get_fund_flow_observations(DAY_1)

    assert repository.list_tool_invocations()[0].status is ToolInvocationStatus.FAILED
    repository.close()


class FundFlowFixtureAdapter:
    def get_market_snapshot(self, trade_date, instruments):
        snapshot = MockDemoAdapter().get_market_snapshot(trade_date, instruments)
        stock = snapshot.stock_observations[0]
        close = stock.price_metrics[0]
        flow = close.model_copy(
            update={
                "metric_key": "main_net_flow",
                "value": Decimal("1230000"),
                "unit": "CNY",
                "status": DataStatus.AVAILABLE,
                "observation_ref_id": "obs_fixture_main_net_flow",
                "raw_reference": "fixture.main_net_flow",
            }
        )
        raw_reference = "fixture.sector"
        sector = SectorObservation(
            sector_name="医药",
            taxonomy="fixture",
            relation="fixture_membership",
            observation_ref_id=make_sector_observation_ref_id(
                dataset=close.source.endpoint_or_dataset or close.source.source_id,
                provider=close.source.provider,
                trade_date=trade_date,
                instrument_id=stock.instrument.instrument_id,
                taxonomy="fixture",
                sector_name="医药",
                relation="fixture_membership",
                raw_reference=raw_reference,
            ),
            raw_reference=raw_reference,
            status=DataStatus.AVAILABLE,
            source=close.source,
        )
        updated_stock = stock.model_copy(
            update={"fund_flow_metrics": [flow], "sectors": [sector]}
        )
        return snapshot.model_copy(update={"stock_observations": [updated_stock]})


def test_fund_flow_tool_returns_only_real_observation_refs():
    repository = SQLiteThesisRepository()
    tools = ReadOnlyMarketTools(
        FundFlowFixtureAdapter(),
        repository,
        default_instruments=[normalize_symbol("600519")],
    )

    by_instrument = tools.get_fund_flow_observations(DAY_1, instrument_id="CN.SH.600519")
    by_sector = tools.get_fund_flow_observations(DAY_1, sector_name="医药")

    assert [item.observation_ref_id for item in by_instrument] == ["obs_fixture_main_net_flow"]
    assert by_sector == by_instrument
    instrument_call, sector_call = repository.list_tool_invocations()
    assert instrument_call.returned_observation_ref_ids == ["obs_fixture_main_net_flow"]
    assert sector_call.returned_observation_ref_ids == [
        "obs_fixture_main_net_flow",
        by_sector and FundFlowFixtureAdapter().get_market_snapshot(
            DAY_1, [normalize_symbol("600519")]
        ).stock_observations[0].sectors[0].observation_ref_id,
    ]
    repository.close()


def test_sector_fact_reference_is_accepted_by_provenance_validator():
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        DAY_1, [normalize_symbol("002437")]
    )
    sector = next(item for item in snapshot.stock_observations[0].sectors if item.status is DataStatus.AVAILABLE)
    proposal = MockProposalBuilder().build_proposal(
        thesis_id=uuid4(),
        snapshot=MockDemoAdapter().get_market_snapshot(DAY_1, [normalize_symbol("600519")]),
        instrument=normalize_symbol("600519"),
        version=1,
        derived_from_revision_id=None,
        previous_revision=None,
    )
    evidence = EvidenceItem(
        evidence_id=uuid4(),
        claim=f"该来源把标的标记为{sector.sector_name}",
        claim_type=ClaimType.FACT,
        direction=EvidenceDirection.NEUTRAL,
        evidence_quality=EvidenceQuality.MEDIUM,
        quality_reason="Provider taxonomy is preserved without merging.",
        observation_ref_ids=[sector.observation_ref_id],
        source_refs=[sector.source],
        observed_at=snapshot.created_at,
    )
    proposal = proposal.model_copy(
        update={
            "based_on_snapshot_id": snapshot.snapshot_id,
            "support_evidence": [evidence],
            "counter_evidence": [],
        }
    )

    assert validate_revision_provenance(proposal, snapshot).is_valid


def test_snapshot_and_success_invocation_roll_back_together_on_insert_failure(tmp_path, monkeypatch):
    repository = SQLiteThesisRepository(tmp_path / "atomic-tools.sqlite")
    tools = ReadOnlyMarketTools(MockDemoAdapter(), repository)
    expected_snapshot = MockDemoAdapter().get_market_snapshot(DAY_1, [normalize_symbol("600519")])

    monkeypatch.setattr(
        repository,
        "_insert_tool_invocation",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("simulated invocation insert failure")),
    )
    with pytest.raises(RuntimeError, match="invocation insert failure"):
        tools.get_market_snapshot(DAY_1, ["600519"])

    with pytest.raises(NotFoundError):
        repository.get_snapshot(expected_snapshot.snapshot_id)
    assert repository.list_tool_invocations() == []
    repository.close()


def test_llm_tool_call_id_is_optional_but_unique_when_present():
    repository = SQLiteThesisRepository()
    tools = ReadOnlyMarketTools(MockDemoAdapter(), repository)

    tools.get_market_snapshot(DAY_1, ["600519"], llm_tool_call_id="same-provider-event")
    with pytest.raises(RepositoryError, match="could not save snapshot tool invocation"):
        tools.get_market_snapshot(DAY_1, ["600519"], llm_tool_call_id="same-provider-event")

    calls = repository.list_tool_invocations()
    assert len(calls) == 1
    assert calls[0].llm_tool_call_id == "same-provider-event"
    assert calls[0].returned_observation_ref_ids
    assert "same-provider-event" not in calls[0].returned_observation_ref_ids
    repository.close()
