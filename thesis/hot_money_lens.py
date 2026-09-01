from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field

from .models import (
    DataStatus,
    DomainModel,
    MarketRegime,
    MarketSnapshot,
    MetricObservation,
    StockObservation,
    ThesisRevision,
)


class LensConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ScenarioEffect(StrEnum):
    STRENGTHEN = "strengthen"
    OBSERVE = "observe"
    WEAKEN = "weaken"


class HotMoneyScenario(DomainModel):
    name: str = Field(min_length=1)
    trigger: str = Field(min_length=1)
    effect: ScenarioEffect
    research_response: str = Field(min_length=1)


class HotMoneyLensReport(DomainModel):
    instrument_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    role_basis: list[str] = Field(default_factory=list)
    market_fit: str = Field(min_length=1)
    opponent_view: list[str] = Field(default_factory=list)
    price_in_view: list[str] = Field(default_factory=list)
    counter_signals: list[str] = Field(default_factory=list)
    scenarios: list[HotMoneyScenario] = Field(default_factory=list)
    observation_ref_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    confidence: LensConfidence
    direct_trading_allowed: Literal[False] = False


def _find_stock(snapshot: MarketSnapshot, instrument_id: str) -> StockObservation | None:
    return next(
        (
            stock
            for stock in snapshot.stock_observations
            if stock.instrument.instrument_id == instrument_id
        ),
        None,
    )


def _metric_map(stock: StockObservation) -> dict[str, MetricObservation]:
    metrics = [
        *stock.membership_metrics,
        *stock.price_metrics,
        *stock.fund_flow_metrics,
    ]
    available = [metric for metric in metrics if metric.status is DataStatus.AVAILABLE]
    return {metric.metric_key: metric for metric in sorted(available, key=lambda item: item.observed_at)}


def _number(metric: MetricObservation | None) -> float | None:
    if metric is None or isinstance(metric.value, bool):
        return None
    if isinstance(metric.value, (Decimal, int)):
        return float(metric.value)
    return None


def _boolean(metric: MetricObservation | None) -> bool | None:
    if metric is None or not isinstance(metric.value, bool):
        return None
    return metric.value


def build_hot_money_lens(
    snapshot: MarketSnapshot,
    proposal: ThesisRevision,
    instrument_id: str,
) -> HotMoneyLensReport:
    """Build a deterministic, source-bounded short-horizon research lens.

    The report deliberately describes observable structure and falsifiable
    scenarios. It never infers an actor's hidden intent or emits an order.
    """

    stock = _find_stock(snapshot, instrument_id)
    base_limitations = [
        "该 Lens 只用于研究复核，不读取账户，也不生成仓位、买点或下单指令。",
        "公开行情只能观察行为结果，不能据此确认所谓主力、游资席位或其真实意图。",
    ]
    if stock is None:
        return HotMoneyLensReport(
            instrument_id=instrument_id,
            role="角色未证实",
            market_fit="缺少该标的的结构化 StockObservation，无法判断环境适配。",
            counter_signals=["当前 Snapshot 未包含该标的，先补齐同一历史时点的数据。"],
            scenarios=_default_scenarios(),
            limitations=[*base_limitations, *snapshot.known_limitations],
            confidence=LensConfidence.LOW,
        )

    metrics = _metric_map(stock)
    used_refs: list[str] = []

    def use(key: str) -> MetricObservation | None:
        metric = metrics.get(key)
        if metric is not None and metric.observation_ref_id not in used_refs:
            used_refs.append(metric.observation_ref_id)
        return metric

    boards = _number(use("boards"))
    in_limit_up_pool = _boolean(use("in_limit_up_pool"))
    open_num = _number(use("open_num"))
    turnover_pct = _number(use("turnover_pct"))
    chg_pct = _number(use("chg_pct"))
    board_chg_pct = _number(use("board_chg_pct"))
    board_net_yi = _number(use("board_net_yi"))
    lead_chg_pct = _number(use("lead_chg_pct"))

    role_basis: list[str] = []
    if boards is not None and boards >= 2:
        role = f"{int(boards)}板身位候选"
        role_basis.append(f"快照记录连续板数为 {int(boards)}。")
    elif in_limit_up_pool is True:
        role = "首板情绪候选"
        role_basis.append("快照确认标的进入当日涨停池。")
    elif stock.sectors:
        role = "板块共振观察标的"
        role_basis.append("快照存在标的与板块的结构化关联。")
    else:
        role = "角色未证实"
        role_basis.append("当前证据不足以给标的分配短线市场角色。")
    if stock.sectors:
        sector_names = "、".join(dict.fromkeys(sector.sector_name for sector in stock.sectors))
        role_basis.append(f"关联板块：{sector_names}；是否为全市场核心仍需横向样本验证。")

    market_fit = {
        MarketRegime.ATTACK: "进攻环境：短线结构与市场状态方向一致，但仍需验证板块宽度与次日承接。",
        MarketRegime.DIVERGENCE: "分歧环境：辨识度可能上升，同时失败率与筹码交换风险也更高。",
        MarketRegime.RETREAT: "退潮环境：高位和弱证据题材的容错率下降，应提高证伪权重。",
        MarketRegime.UNKNOWN: "市场环境尚未结构化判定，不能声称该角色与大盘情绪匹配。",
    }[snapshot.market_regime]

    opponent_view: list[str] = []
    if in_limit_up_pool is True:
        opponent_view.append("标的进入当日涨停池，说明当日短线关注显著；这不代表后续仍有承接。")
    if open_num is None:
        opponent_view.append("缺少开板次数，无法评估封板过程中的供需分歧。")
    elif open_num == 0:
        opponent_view.append("快照记录开板次数为 0；只能描述当日封板过程，不能外推次日强弱。")
    elif open_num == 1:
        opponent_view.append("出现 1 次开板后回封，说明当日供需已经出现分歧。")
    else:
        opponent_view.append(f"出现 {int(open_num)} 次开板，筹码交换与分歧程度相对更高。")
    if turnover_pct is not None:
        if turnover_pct >= 20:
            opponent_view.append(
                f"换手率 {turnover_pct:.2f}%，筹码交换活跃；仅凭高换手不能判定吸筹或出货。"
            )
        elif turnover_pct >= 10:
            opponent_view.append(f"换手率 {turnover_pct:.2f}%，存在一定筹码交换。")
    if board_chg_pct is not None:
        sector_text = f"未归属具体板块的板块涨跌幅 {board_chg_pct:.2f}%"
        if board_net_yi is not None:
            sector_text += f"、净流指标 {board_net_yi:.2f} 亿元"
        if lead_chg_pct is not None:
            sector_text += f"、领涨指标 {lead_chg_pct:.2f}%"
        opponent_view.append(sector_text + "；这些是板块背景，不等同于个股资金意图。")

    sector_metric_keys: set[str] = set()
    for sector in stock.sectors:
        sector_metrics = {
            metric.metric_key: metric
            for metric in sector.metrics
            if metric.status is DataStatus.AVAILABLE
        }
        sector_metric_keys.update(sector_metrics)
        for metric in sector_metrics.values():
            if metric.observation_ref_id not in used_refs:
                used_refs.append(metric.observation_ref_id)
        sector_chg = _number(sector_metrics.get("board_chg_pct"))
        sector_net = _number(sector_metrics.get("board_net_yi"))
        sector_lead = _number(sector_metrics.get("lead_chg_pct"))
        parts: list[str] = []
        if sector_chg is not None:
            parts.append(f"涨跌幅 {sector_chg:.2f}%")
        if sector_net is not None:
            parts.append(f"净流指标 {sector_net:.2f} 亿元")
        if sector_lead is not None:
            parts.append(f"领涨指标 {sector_lead:.2f}%")
        if parts:
            opponent_view.append(
                f"关联板块“{sector.sector_name}”：{'、'.join(parts)}；"
                "这些是板块背景，不等同于个股资金意图。"
            )

    price_in_view = list(dict.fromkeys(proposal.price_in_risks))
    if boards is not None and boards >= 3:
        price_in_view.append(
            f"已处于 {int(boards)} 板身位，市场预期可能高度显性化；后续需要新增证据而非仅靠标签延续。"
        )
    elif in_limit_up_pool is True and chg_pct is not None and chg_pct >= 9.5:
        price_in_view.append("当日接近涨停的价格表现已反映一部分乐观预期，需区分信息增量与价格重复。")
    if not price_in_view:
        price_in_view.append("现有证据不足以量化 Price In 程度，暂不作低估或高估判断。")

    counter_signals = [item.claim for item in proposal.counter_evidence]
    if snapshot.market_regime is MarketRegime.UNKNOWN:
        counter_signals.append("市场情绪状态为 UNKNOWN，环境适配结论暂不可用。")
    if not stock.sectors:
        counter_signals.append("缺少结构化板块关联，无法验证梯队、宽度或板块共振。")
    counter_signals = list(dict.fromkeys(counter_signals))

    evidence_dimensions = sum(
        value is not None
        for value in (
            boards,
            in_limit_up_pool,
            open_num,
            turnover_pct,
            board_chg_pct,
            board_net_yi,
            lead_chg_pct,
        )
    ) + int(bool(stock.sectors)) + sum(
        key in sector_metric_keys
        for key in ("board_chg_pct", "board_net_yi", "lead_chg_pct")
    )
    confidence = (
        LensConfidence.HIGH
        if evidence_dimensions >= 6 and snapshot.market_regime is not MarketRegime.UNKNOWN
        else LensConfidence.MEDIUM
        if evidence_dimensions >= 3
        else LensConfidence.LOW
    )

    limitations = list(
        dict.fromkeys(
            [
                *base_limitations,
                *stock.known_limitations,
                *snapshot.known_limitations,
            ]
        )
    )
    return HotMoneyLensReport(
        instrument_id=instrument_id,
        role=role,
        role_basis=role_basis,
        market_fit=market_fit,
        opponent_view=opponent_view,
        price_in_view=list(dict.fromkeys(price_in_view)),
        counter_signals=counter_signals,
        scenarios=_default_scenarios(),
        observation_ref_ids=used_refs,
        limitations=limitations,
        confidence=confidence,
    )


def _default_scenarios() -> list[HotMoneyScenario]:
    return [
        HotMoneyScenario(
            name="证据强化",
            trigger="催化经独立来源确认，且板块宽度、梯队或承接出现可核验改善。",
            effect=ScenarioEffect.STRENGTHEN,
            research_response="新增带时间戳的证据，再复核原 Thesis；不因单一价格上涨自动强化。",
        ),
        HotMoneyScenario(
            name="仅价格继续",
            trigger="价格保持强势，但没有新的可核验催化或板块证据。",
            effect=ScenarioEffect.OBSERVE,
            research_response="维持观察并提高 Price In 权重，不把价格本身循环论证为基本面证据。",
        ),
        HotMoneyScenario(
            name="证伪或退潮",
            trigger="催化被证伪、板块梯队瓦解、反复开板恶化或关键反证出现。",
            effect=ScenarioEffect.WEAKEN,
            research_response="把证据写入 counter evidence，提交减弱或失效 proposal 供人工审核。",
        ),
    ]
