from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from statistics import mean
from typing import Literal

from pydantic import Field

from .candidates import CandidateCard
from .models import DomainModel, MarketRegime


class EnvironmentConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"


class MarketEnvironmentStats(DomainModel):
    total_limit_up: int | None = Field(default=None, ge=0)
    previous_total_limit_up: int | None = Field(default=None, ge=0)
    total_limit_up_change_pct: float | None = None
    selected_limit_up_count: int = Field(ge=0)
    selected_max_board: int | None = Field(default=None, ge=1)
    previous_selected_max_board: int | None = Field(default=None, ge=1)
    known_open_count: int = Field(ge=0)
    average_open_num: float | None = Field(default=None, ge=0)
    open_ratio_pct: float | None = Field(default=None, ge=0)
    promotion_rate_pct: float | None = Field(default=None, ge=0)
    high_divergence_count: int = Field(ge=0)
    sector_resonance_count: int | None = Field(default=None, ge=0)


class MarketEnvironmentScenario(DomainModel):
    name: str = Field(min_length=1)
    confirmation: str = Field(min_length=1)
    interpretation: str = Field(min_length=1)


class MarketEnvironmentReport(DomainModel):
    trade_date: date
    previous_trade_date: date | None = None
    regime: MarketRegime
    confidence: EnvironmentConfidence
    headline: str = Field(min_length=1)
    stats: MarketEnvironmentStats
    evidence: list[str] = Field(default_factory=list)
    structure_read: list[str] = Field(default_factory=list)
    risk_signals: list[str] = Field(default_factory=list)
    scenarios: list[MarketEnvironmentScenario] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    direct_trading_allowed: Literal[False] = False


def build_market_environment_report(
    candidates: list[CandidateCard],
    trade_date: date | None = None,
) -> MarketEnvironmentReport | None:
    if not candidates:
        return None
    dates = sorted({candidate.trade_date for candidate in candidates})
    selected_date = trade_date or dates[-1]
    if selected_date not in dates:
        return None
    previous_dates = [item for item in dates if item < selected_date]
    previous_date = previous_dates[-1] if previous_dates else None
    current = [candidate for candidate in candidates if candidate.trade_date == selected_date]
    previous = (
        [candidate for candidate in candidates if candidate.trade_date == previous_date]
        if previous_date is not None
        else []
    )

    current_stats = _stats(current)
    previous_stats = _stats(previous)
    total_change = _pct_change(current_stats["total_limit_up"], previous_stats["total_limit_up"])
    previous_max = max(previous_stats["boards"], default=None)
    stats = MarketEnvironmentStats(
        total_limit_up=current_stats["total_limit_up"],
        previous_total_limit_up=previous_stats["total_limit_up"],
        total_limit_up_change_pct=total_change,
        selected_limit_up_count=len(current_stats["boards"]),
        selected_max_board=max(current_stats["boards"], default=None),
        previous_selected_max_board=previous_max,
        known_open_count=len(current_stats["opens"]),
        average_open_num=(mean(current_stats["opens"]) if current_stats["opens"] else None),
        high_divergence_count=sum(value >= 3 for value in current_stats["opens"]),
        sector_resonance_count=sum(
            "SECTOR_RESONANCE" in candidate.trigger_rules for candidate in current
        ),
    )
    previous_average_open = mean(previous_stats["opens"]) if previous_stats["opens"] else None
    regime = _classify_regime(stats, previous_max)
    confidence = (
        EnvironmentConfidence.MEDIUM
        if stats.total_limit_up is not None
        and stats.previous_total_limit_up is not None
        and stats.selected_max_board is not None
        and stats.known_open_count >= 3
        else EnvironmentConfidence.LOW
    )

    evidence: list[str] = []
    if stats.total_limit_up is not None:
        breadth = f"涨停池覆盖记录为 {stats.total_limit_up} 只"
        if stats.previous_total_limit_up is not None and total_change is not None:
            breadth += f"，前一交易日 {stats.previous_total_limit_up} 只，变化 {total_change:+.1f}%"
        evidence.append(breadth + "。")
    if stats.selected_max_board is not None:
        height = f"候选样本最高 {stats.selected_max_board} 板"
        if previous_max is not None:
            height += f"，前一交易日样本最高 {previous_max} 板"
        evidence.append(height + "。")
    if stats.average_open_num is not None:
        divergence = (
            f"开板次数有值的 {stats.known_open_count} 只候选，平均开板 "
            f"{stats.average_open_num:.1f} 次"
        )
        if previous_average_open is not None:
            divergence += f"，前一交易日可比样本均值 {previous_average_open:.1f} 次"
        evidence.append(divergence + "。")
    evidence.append(f"当日有 {stats.sector_resonance_count} 只候选同时命中板块共振规则。")

    structure_read = _structure_read(stats, previous_max)
    risk_signals: list[str] = []
    if total_change is not None and total_change <= -15:
        risk_signals.append("涨停宽度较前一交易日明显收缩，市场赚钱效应没有与高度同步扩张。")
    if stats.average_open_num is not None and stats.average_open_num >= 4:
        risk_signals.append("已知开板样本的平均开板次数较高，封板过程中的供需分歧偏强。")
    if stats.known_open_count < stats.selected_limit_up_count:
        risk_signals.append(
            f"{stats.selected_limit_up_count - stats.known_open_count} 只入选涨停候选缺少开板次数，"
            "不能把缺失值当作零开板。"
        )

    return MarketEnvironmentReport(
        trade_date=selected_date,
        previous_trade_date=previous_date,
        regime=regime,
        confidence=confidence,
        headline=_headline(regime),
        stats=stats,
        evidence=evidence,
        structure_read=structure_read,
        risk_signals=risk_signals,
        scenarios=_scenarios(),
        limitations=[
            "涨停总数来自候选 Observation 的 full_limit_up_pool 覆盖元数据；个股梯队只保留确定性规则筛选后的候选子集。",
            "当前缺少全市场跌停家数、完整炸板率、成交额与完整昨日连板晋级集合，因此最高只给中等置信度。",
            "板块净流指标是公开数据源口径，不代表特定席位或账户的真实意图。",
            "该报告只用于研究环境分层，不生成仓位、买点、买卖或下单指令。",
        ],
    )


def build_market_environment_from_legacy(
    payload: dict[str, object] | None,
) -> MarketEnvironmentReport | None:
    """Turn the daily committed limit-up artifact into the same bounded report contract."""

    if not isinstance(payload, dict) or not isinstance(payload.get("meta"), dict):
        return None
    meta = payload["meta"]
    trade_date = _legacy_date(meta.get("trade_date"))
    if trade_date is None:
        return None
    previous_date = _legacy_date(meta.get("prev_date"))
    total = _legacy_int(meta.get("total_limit_up"))
    previous_total = _legacy_int(meta.get("prev_total"))
    max_board = _legacy_int(meta.get("max_board"))
    previous_max = _legacy_int(meta.get("prev_max_board"))
    open_ratio = _legacy_float(meta.get("open_ratio_pct"))
    promotion_rate = _legacy_float(meta.get("promo_rate_pct"))
    change = _pct_change(total, previous_total)
    regime = {
        "进攻期": MarketRegime.ATTACK,
        "分歧期": MarketRegime.DIVERGENCE,
        "退潮期": MarketRegime.RETREAT,
    }.get(str(meta.get("sentiment") or ""), MarketRegime.UNKNOWN)
    stats = MarketEnvironmentStats(
        total_limit_up=total,
        previous_total_limit_up=previous_total,
        total_limit_up_change_pct=change,
        selected_limit_up_count=0,
        selected_max_board=max_board,
        previous_selected_max_board=previous_max,
        known_open_count=0,
        open_ratio_pct=open_ratio,
        promotion_rate_pct=promotion_rate,
        high_divergence_count=0,
        sector_resonance_count=None,
    )
    evidence: list[str] = []
    if total is not None:
        text = f"每日涨停池记录为 {total} 只"
        if previous_total is not None and change is not None:
            text += f"，前一交易日 {previous_total} 只，变化 {change:+.1f}%"
        evidence.append(text + "。")
    if max_board is not None:
        text = f"空间高度 {max_board} 板"
        if previous_max is not None:
            text += f"，前一交易日 {previous_max} 板"
        evidence.append(text + "。")
    if open_ratio is not None:
        evidence.append(f"全量扫描记录的开板率为 {open_ratio:.1f}%。")
    if promotion_rate is not None:
        evidence.append(f"旧版昨日连板集合口径的晋级率为 {promotion_rate:.1f}%。")

    structure: list[str] = []
    if change is not None:
        structure.append(f"涨停宽度环比 {change:+.1f}%，宽度本身{'扩张' if change > 0 else '收缩'}。")
    if max_board is not None and previous_max is not None:
        structure.append(
            f"空间高度由 {previous_max} 板降至 {max_board} 板，最高辨识度标的没有与宽度同步增强。"
            if max_board < previous_max
            else f"空间高度由 {previous_max} 板升至 {max_board} 板。"
        )
    themes = meta.get("themes_top")
    if isinstance(themes, list):
        normalized_themes = [
            f"{item[0]}×{item[1]}"
            for item in themes[:5]
            if isinstance(item, list) and len(item) >= 2
        ]
        if normalized_themes:
            structure.append("题材集中度观察：" + "、".join(normalized_themes) + "。")

    risks: list[str] = []
    if open_ratio is not None and open_ratio >= 40:
        risks.append(f"开板率 {open_ratio:.1f}% 偏高，封板成功率与日内承接存在明显压力。")
    if max_board is not None and previous_max is not None and max_board < previous_max:
        risks.append("空间高度下降，说明高位接力的容错率没有随涨停家数回升。")
    if promotion_rate is not None and promotion_rate < 50:
        risks.append(f"晋级率 {promotion_rate:.1f}% 未过半，连板延续仍不稳定。")

    return MarketEnvironmentReport(
        trade_date=trade_date,
        previous_trade_date=previous_date,
        regime=regime,
        confidence=EnvironmentConfidence.MEDIUM,
        headline=(
            "涨停宽度回升，但空间高度下降且开板压力偏高，赚钱效应仍然脆弱。"
            if regime is MarketRegime.RETREAT
            else _headline(regime)
        ),
        stats=stats,
        evidence=evidence,
        structure_read=structure,
        risk_signals=risks,
        scenarios=_scenarios(),
        limitations=[
            "该报告使用每日提交的 legacy limit_up.json；市场统计较完整，但旧版角色、分数和荐股结论没有被复用。",
            "开板率与晋级率沿用历史扫描器口径，尚未迁移为 Candidate Observation 级审计字段。",
            "未读取账户、席位明细或券商交易接口，不能确认特定游资或主力意图。",
            "该报告只用于研究环境分层，不生成仓位、买点、买卖或下单指令。",
        ],
    )


def _stats(candidates: list[CandidateCard]) -> dict[str, object]:
    total_limit_up: int | None = None
    boards: list[int] = []
    opens: list[float] = []
    for candidate in candidates:
        for observation in candidate.observations:
            if observation.source != "public.ths.limit_up_pool":
                continue
            coverage_match = re.fullmatch(r"full_limit_up_pool:(\d+)", observation.coverage)
            if coverage_match:
                total_limit_up = int(coverage_match.group(1))
            board_value = observation.metrics.get("boards")
            if isinstance(board_value, (int, float)) and not isinstance(board_value, bool):
                boards.append(int(board_value))
            open_value = observation.metrics.get("open_num")
            if isinstance(open_value, (int, float)) and not isinstance(open_value, bool):
                opens.append(float(open_value))
    return {"total_limit_up": total_limit_up, "boards": boards, "opens": opens}


def _pct_change(current: object, previous: object) -> float | None:
    if not isinstance(current, int) or not isinstance(previous, int) or previous == 0:
        return None
    return (current - previous) / previous * 100


def _legacy_date(value: object) -> date | None:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _legacy_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _legacy_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _classify_regime(stats: MarketEnvironmentStats, previous_max: int | None) -> MarketRegime:
    breadth = stats.total_limit_up_change_pct
    height_drop = (
        previous_max - stats.selected_max_board
        if previous_max is not None and stats.selected_max_board is not None
        else None
    )
    if (
        breadth is not None
        and breadth <= -25
        and ((height_drop is not None and height_drop >= 2) or (stats.selected_max_board or 0) <= 2)
    ):
        return MarketRegime.RETREAT
    if (
        breadth is not None
        and breadth >= 10
        and (stats.selected_max_board or 0) >= 4
        and (stats.average_open_num is None or stats.average_open_num <= 2)
    ):
        return MarketRegime.ATTACK
    if (
        (breadth is not None and breadth <= -10)
        or (stats.average_open_num is not None and stats.average_open_num >= 3)
    ):
        return MarketRegime.DIVERGENCE
    return MarketRegime.UNKNOWN


def _headline(regime: MarketRegime) -> str:
    return {
        MarketRegime.ATTACK: "宽度与高度同步改善，但仍需验证次日承接。",
        MarketRegime.DIVERGENCE: "高度尚在、宽度收缩，筹码分歧正在加剧。",
        MarketRegime.RETREAT: "宽度与高度共同走弱，短线容错率显著下降。",
        MarketRegime.UNKNOWN: "证据不足以区分进攻、分歧或退潮。",
    }[regime]


def _structure_read(stats: MarketEnvironmentStats, previous_max: int | None) -> list[str]:
    result: list[str] = []
    if stats.selected_max_board is not None:
        if previous_max == stats.selected_max_board:
            result.append(f"空间高度维持在 {stats.selected_max_board} 板，核心高度尚未直接坍塌。")
        else:
            result.append(f"候选样本空间高度为 {stats.selected_max_board} 板。")
    if stats.total_limit_up_change_pct is not None:
        result.append(
            f"涨停宽度环比 {stats.total_limit_up_change_pct:+.1f}%，"
            "高度信号必须与宽度变化一起解读。"
        )
    if stats.high_divergence_count:
        result.append(f"有 {stats.high_divergence_count} 只已知样本开板至少 3 次，分歧集中在部分高辨识度标的。")
    if stats.sector_resonance_count:
        result.append(f"板块共振候选 {stats.sector_resonance_count} 只，说明结构性机会仍存在，但不是普涨证据。")
    return result


def _scenarios() -> list[MarketEnvironmentScenario]:
    return [
        MarketEnvironmentScenario(
            name="进攻确认",
            confirmation="涨停宽度重新扩张、空间高度维持，同时已知开板压力下降。",
            interpretation="赚钱效应由少数高度标的向更广范围扩散，才可把环境上调为进攻。",
        ),
        MarketEnvironmentScenario(
            name="分歧延续",
            confirmation="空间高度仍在，但涨停宽度不恢复且高开板样本继续增加。",
            interpretation="辨识度与风险同时上升，应把个股反证和 Price In 放在题材标签之前。",
        ),
        MarketEnvironmentScenario(
            name="退潮确认",
            confirmation="涨停宽度继续明显收缩，空间高度下降，板块共振候选同步减少。",
            interpretation="环境从结构性分歧转为系统性弱化，需要重新审查全部短线 Thesis。",
        ),
    ]
