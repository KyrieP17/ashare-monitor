from __future__ import annotations

from datetime import date, datetime

from .models import DataStatus, DomainModel, MarketSnapshot, MetricObservation


CATALYST_METRIC_KEY = "catalyst_reason"


class CatalystContext(DomainModel):
    instrument_id: str
    trade_date: date
    source: str
    source_field: str
    raw_text: str | None = None
    raw_texts: list[str]
    tool_call_id: str | None = None
    observation_ref_id: str
    observation_ref_ids: list[str]
    retrieved_at: datetime
    data_as_of: datetime | date
    limitations: list[str]
    status: DataStatus


def catalyst_context_from_snapshot(
    snapshot: MarketSnapshot,
    instrument_id: str,
    *,
    tool_call_id: str | None,
) -> CatalystContext:
    stock = next(
        (
            item
            for item in snapshot.stock_observations
            if item.instrument.instrument_id == instrument_id
        ),
        None,
    )
    if stock is None:
        raise LookupError(f"snapshot did not return {instrument_id}")

    metrics = [
        item
        for item in stock.membership_metrics
        if item.metric_key == CATALYST_METRIC_KEY
    ]
    if not metrics:
        raise LookupError("snapshot does not implement catalyst_reason observations")

    metrics.sort(key=lambda item: item.observed_at, reverse=True)
    texts = list(
        dict.fromkeys(
            str(item.value).strip()
            for item in metrics
            if item.status in (DataStatus.AVAILABLE, DataStatus.CONFLICTED)
            and isinstance(item.value, str)
            and item.value.strip()
        )
    )
    selected = next(
        (
            item
            for item in metrics
            if item.status in (DataStatus.AVAILABLE, DataStatus.CONFLICTED)
            and isinstance(item.value, str)
            and item.value.strip()
        ),
        metrics[0],
    )
    status = (
        DataStatus.MISSING
        if not texts
        else DataStatus.CONFLICTED
        if len(texts) > 1 or any(item.status is DataStatus.CONFLICTED for item in metrics)
        else DataStatus.AVAILABLE
    )
    source_field = (
        "reason_type"
        if "reason_type" in selected.raw_reference
        else "reason"
        if "reason" in selected.raw_reference
        else CATALYST_METRIC_KEY
    )
    limitations = [
        "涨停池 reason/theme 是数据提供方的事件或题材标签，不是公司公告原文，也不是事实核验结论。",
        "第一层仅复用已采集的涨停池字段；未查询公司公告，不能证明催化剂真实发生或仍然有效。",
    ]
    if not texts:
        limitations.append("该候选没有可用的涨停池 reason/theme 文本；MISSING 不代表不存在催化剂。")
    if status is DataStatus.CONFLICTED:
        limitations.append("同一候选存在多个不同原始 reason 文本，已并列保留，禁止静默覆盖。")

    return CatalystContext(
        instrument_id=instrument_id,
        trade_date=snapshot.trade_date,
        source=selected.source.provider,
        source_field=source_field,
        raw_text=texts[0] if texts else None,
        raw_texts=texts,
        tool_call_id=tool_call_id,
        observation_ref_id=selected.observation_ref_id,
        observation_ref_ids=[item.observation_ref_id for item in metrics],
        retrieved_at=selected.source.retrieved_at,
        data_as_of=selected.source.data_as_of or selected.observed_at,
        limitations=limitations,
        status=status,
    )
