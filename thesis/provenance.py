from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field

from .models import (
    DataStatus,
    EvidenceItem,
    MarketSnapshot,
    MetricObservation,
    SectorObservation,
    ThesisRevision,
)


class ProvenanceIssueCode(StrEnum):
    DUPLICATE_OBSERVATION_REF = "duplicate_observation_ref"
    INVALID_OBSERVATION_REF = "invalid_observation_ref"
    MISSING_OBSERVATION_REF = "missing_observation_ref"
    DATE_MISMATCH = "date_mismatch"
    MISSING_UNIT = "missing_unit"
    MISSING_RAW_REFERENCE = "missing_raw_reference"
    MISSING_DATA_AS_OF = "missing_data_as_of"
    MISSING_VALUE = "missing_value"
    UNEXPECTED_VALUE = "unexpected_value"
    SOURCE_NOT_LINKED = "source_not_linked"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    UNSOURCED_NUMBER = "unsourced_number"
    NUMERIC_VALUE_MISMATCH = "numeric_value_mismatch"
    ERROR_OBSERVATION = "error_observation"
    STALE_OBSERVATION = "stale_observation"
    UNUSABLE_OBSERVATION_STATUS = "unusable_observation_status"


class ProvenanceIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ProvenanceIssueCode
    path: str
    message: str


class ProvenanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[ProvenanceIssue] = Field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.issues


def make_observation_ref_id(
    *,
    dataset: str,
    trade_date: date,
    scope: str,
    metric_key: str,
    raw_reference: str,
) -> str:
    """Create a deterministic observation key independent of LLM invocations."""

    payload = json.dumps(
        {
            "dataset": dataset,
            "trade_date": trade_date.isoformat(),
            "scope": scope,
            "metric_key": metric_key,
            "raw_reference": raw_reference,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"obs_{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def make_sector_observation_ref_id(
    *,
    dataset: str,
    provider: str,
    trade_date: date,
    instrument_id: str,
    taxonomy: str,
    sector_name: str,
    relation: str | None,
    raw_reference: str,
) -> str:
    """Create a stable identity for one sourced sector-membership fact."""
    payload = json.dumps(
        {
            "dataset": dataset,
            "provider": provider,
            "trade_date": trade_date.isoformat(),
            "instrument_id": instrument_id,
            "taxonomy": taxonomy,
            "sector_name": sector_name,
            "relation": relation,
            "raw_reference": raw_reference,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"obs_{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _as_date(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


def _iter_metrics(snapshot: MarketSnapshot) -> Iterator[tuple[str, str, MetricObservation]]:
    for index, metric in enumerate(snapshot.market_metrics):
        yield f"market_metrics[{index}]", "market:CN", metric
    for sector_index, sector in enumerate(snapshot.sector_observations):
        scope = f"sector:{sector.taxonomy}:{sector.sector_name}"
        for metric_index, metric in enumerate(sector.metrics):
            yield f"sector_observations[{sector_index}].metrics[{metric_index}]", scope, metric
    for stock_index, stock in enumerate(snapshot.stock_observations):
        scope = stock.instrument.instrument_id
        for metric_index, metric in enumerate(stock.membership_metrics):
            yield f"stock_observations[{stock_index}].membership_metrics[{metric_index}]", scope, metric
        for metric_index, metric in enumerate(stock.price_metrics):
            yield f"stock_observations[{stock_index}].price_metrics[{metric_index}]", scope, metric
        for metric_index, metric in enumerate(stock.fund_flow_metrics):
            yield f"stock_observations[{stock_index}].fund_flow_metrics[{metric_index}]", scope, metric
        for sector_index, sector in enumerate(stock.sectors):
            sector_scope = f"{scope}:sector:{sector.taxonomy}:{sector.sector_name}"
            for metric_index, metric in enumerate(sector.metrics):
                yield (
                    f"stock_observations[{stock_index}].sectors[{sector_index}].metrics[{metric_index}]",
                    sector_scope,
                    metric,
                )


def _iter_sector_facts(snapshot: MarketSnapshot) -> Iterator[tuple[str, str, SectorObservation]]:
    for index, sector in enumerate(snapshot.sector_observations):
        yield f"sector_observations[{index}]", "market:CN", sector
    for stock_index, stock in enumerate(snapshot.stock_observations):
        for sector_index, sector in enumerate(stock.sectors):
            yield (
                f"stock_observations[{stock_index}].sectors[{sector_index}]",
                stock.instrument.instrument_id,
                sector,
            )


def validate_snapshot_provenance(snapshot: MarketSnapshot) -> ProvenanceReport:
    issues: list[ProvenanceIssue] = []
    seen_refs: dict[str, str] = {}
    for path, scope, metric in _iter_metrics(snapshot):
        previous_path = seen_refs.get(metric.observation_ref_id)
        if previous_path is not None:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.DUPLICATE_OBSERVATION_REF,
                    path=path,
                    message=f"observation_ref_id already used at {previous_path}",
                )
            )
        else:
            seen_refs[metric.observation_ref_id] = path

        if metric.raw_reference:
            expected_ref = make_observation_ref_id(
                dataset=metric.source.endpoint_or_dataset or metric.source.source_id,
                trade_date=snapshot.trade_date,
                scope=scope,
                metric_key=metric.metric_key,
                raw_reference=metric.raw_reference,
            )
            if metric.observation_ref_id != expected_ref:
                issues.append(
                    ProvenanceIssue(
                        code=ProvenanceIssueCode.INVALID_OBSERVATION_REF,
                        path=path,
                        message="observation_ref_id is not the deterministic key for this raw field",
                    )
                )

        data_date = _as_date(metric.source.data_as_of)
        if data_date is None:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.MISSING_DATA_AS_OF,
                    path=path,
                    message="metric source has no data_as_of date",
                )
            )
        elif data_date != snapshot.trade_date:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.DATE_MISMATCH,
                    path=path,
                    message=f"source data_as_of {data_date} does not match snapshot {snapshot.trade_date}",
                )
            )

        if metric.status is DataStatus.AVAILABLE and metric.value is None:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.MISSING_VALUE,
                    path=path,
                    message="AVAILABLE metric has no value",
                )
            )
        if metric.status is DataStatus.MISSING and metric.value is not None:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.UNEXPECTED_VALUE,
                    path=path,
                    message="MISSING metric must not contain a value",
                )
            )
        if metric.status is DataStatus.ERROR:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.ERROR_OBSERVATION,
                    path=path,
                    message="ERROR observations cannot enter a fact-bearing snapshot",
                )
            )
        if metric.status is DataStatus.STALE:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.STALE_OBSERVATION,
                    path=path,
                    message="STALE observations are excluded by the MVP stale-data policy",
                )
            )

        is_number = isinstance(metric.value, (int, Decimal)) and not isinstance(metric.value, bool)
        if is_number and not metric.unit:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.MISSING_UNIT,
                    path=path,
                    message="numeric observation has no unit",
                )
            )
        if metric.status is not DataStatus.MISSING and not metric.raw_reference:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.MISSING_RAW_REFERENCE,
                    path=path,
                    message="observation has no raw field location",
                )
                )
    for path, instrument_id, sector in _iter_sector_facts(snapshot):
        if sector.observation_ref_id is None:
            continue  # Backward-compatible legacy sector observations remain non-addressable.
        previous_path = seen_refs.get(sector.observation_ref_id)
        if previous_path is not None:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.DUPLICATE_OBSERVATION_REF,
                    path=path,
                    message=f"observation_ref_id already used at {previous_path}",
                )
            )
        else:
            seen_refs[sector.observation_ref_id] = path
        if not sector.raw_reference:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.MISSING_RAW_REFERENCE,
                    path=path,
                    message="addressable sector observation has no raw field location",
                )
            )
        else:
            expected_ref = make_sector_observation_ref_id(
                dataset=sector.source.endpoint_or_dataset or sector.source.source_id,
                provider=sector.source.provider,
                trade_date=snapshot.trade_date,
                instrument_id=instrument_id,
                taxonomy=sector.taxonomy,
                sector_name=sector.sector_name,
                relation=sector.relation,
                raw_reference=sector.raw_reference,
            )
            if sector.observation_ref_id != expected_ref:
                issues.append(
                    ProvenanceIssue(
                        code=ProvenanceIssueCode.INVALID_OBSERVATION_REF,
                        path=path,
                        message="sector observation_ref_id is not its deterministic sourced key",
                    )
                )
        data_date = _as_date(sector.source.data_as_of)
        if data_date is None:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.MISSING_DATA_AS_OF,
                    path=path,
                    message="sector source has no data_as_of date",
                )
            )
        elif data_date != snapshot.trade_date:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.DATE_MISMATCH,
                    path=path,
                    message=f"source data_as_of {data_date} does not match snapshot {snapshot.trade_date}",
                )
            )
    return ProvenanceReport(issues=issues)


_NUMBER_PATTERN = re.compile(r"(?<![A-Za-z_0-9])[-+]?\d+(?:\.\d+)?")


def _all_evidence(revision: ThesisRevision) -> Iterator[tuple[str, EvidenceItem]]:
    for index, item in enumerate(revision.support_evidence):
        yield f"support_evidence[{index}]", item
    for index, item in enumerate(revision.counter_evidence):
        yield f"counter_evidence[{index}]", item


def validate_revision_provenance(
    revision: ThesisRevision,
    snapshot: MarketSnapshot,
) -> ProvenanceReport:
    issues = list(validate_snapshot_provenance(snapshot).issues)
    if revision.based_on_snapshot_id != snapshot.snapshot_id:
        issues.append(
            ProvenanceIssue(
                code=ProvenanceIssueCode.SNAPSHOT_MISMATCH,
                path="based_on_snapshot_id",
                message="revision does not reference the supplied snapshot",
            )
        )

    observations: dict[str, MetricObservation | SectorObservation] = {
        metric.observation_ref_id: metric
        for _, _, metric in _iter_metrics(snapshot)
    }
    observations.update(
        {
            sector.observation_ref_id: sector
            for _, _, sector in _iter_sector_facts(snapshot)
            if sector.observation_ref_id is not None
        }
    )
    snapshot_numeric_context = {
        Decimal(snapshot.trade_date.year),
        Decimal(snapshot.trade_date.month),
        Decimal(snapshot.trade_date.day),
        *(Decimal(stock.instrument.code) for stock in snapshot.stock_observations),
    }
    for path, evidence in _all_evidence(revision):
        claim_numbers = [Decimal(match.group(0)) for match in _NUMBER_PATTERN.finditer(evidence.claim)]
        if claim_numbers and not evidence.observation_ref_ids:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.UNSOURCED_NUMBER,
                    path=path,
                    message="evidence claim contains a number without an observation reference",
                )
            )
        referenced_observations: list[MetricObservation | SectorObservation] = []
        for ref in evidence.observation_ref_ids:
            metric = observations.get(ref)
            if metric is None:
                issues.append(
                    ProvenanceIssue(
                        code=ProvenanceIssueCode.MISSING_OBSERVATION_REF,
                        path=path,
                        message=f"observation reference does not exist in snapshot: {ref}",
                    )
                )
            else:
                referenced_observations.append(metric)
                if metric.status is not DataStatus.AVAILABLE:
                    issues.append(
                        ProvenanceIssue(
                            code=ProvenanceIssueCode.UNUSABLE_OBSERVATION_STATUS,
                            path=path,
                            message=(
                                f"evidence cannot cite {metric.status.value} observation as a unique fact: {ref}"
                            ),
                        )
                    )
        metric_source_ids = {metric.source.source_id for metric in referenced_observations}
        evidence_source_ids = {source.source_id for source in evidence.source_refs}
        if metric_source_ids and not metric_source_ids.issubset(evidence_source_ids):
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.SOURCE_NOT_LINKED,
                    path=path,
                    message="evidence source_refs do not cover all referenced observations",
                )
            )
        allowed_numbers = set(snapshot_numeric_context)
        for metric in referenced_observations:
            if isinstance(metric, MetricObservation) and isinstance(metric.value, (int, Decimal)) and not isinstance(metric.value, bool):
                allowed_numbers.add(Decimal(metric.value))
            data_date = _as_date(metric.source.data_as_of)
            if data_date is not None:
                allowed_numbers.update(
                    {Decimal(data_date.year), Decimal(data_date.month), Decimal(data_date.day)}
                )
        unsupported = [number for number in claim_numbers if number not in allowed_numbers]
        if unsupported:
            issues.append(
                ProvenanceIssue(
                    code=ProvenanceIssueCode.NUMERIC_VALUE_MISMATCH,
                    path=path,
                    message=f"claim numbers do not match referenced observations or snapshot context: {unsupported}",
                )
            )
    return ProvenanceReport(issues=issues)
