# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import DISCLAIMER, esc, inject_css
from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.candidates import CandidateCard, CandidateDecision
from thesis.price_volume import PriceVolumeContext, PublicPriceVolumeTool
from thesis.promotion import CandidatePromotionService, PromotionResearchError
from thesis.public_market import PublicMarketClient
from thesis.repository import SQLiteThesisRepository
from thesis.scan_health import format_beijing
from thesis.scan_status_ui import render_scan_status


RULE_LABELS = {
    "RESEARCH_FOCUS": "用户研究池",
    "WATCHLIST_ACTIVITY": "自选股异动",
    "CONSECUTIVE_LIMIT_UP": "连板候选",
    "LIMIT_UP_POOL": "普通涨停候选",
    "SECTOR_RESONANCE": "板块共振",
}
SOURCE_LABELS = {
    "public.tencent.research_focus": "腾讯行情 · 用户研究池",
    "public.tencent.watchlist": "腾讯行情 · 自选股",
    "public.ths.limit_up_pool": "同花顺公开涨停池",
    "public.sina.board_flow": "新浪板块资金流",
}
DECISION_LABELS = {
    CandidateDecision.PENDING: "待决定",
    CandidateDecision.KEEP: "已保留",
    CandidateDecision.IGNORE: "已忽略",
    CandidateDecision.PROMOTE: "已转入研究",
}
FRESHNESS_LABELS = {
    "available": "可用",
    "missing": "数据不足",
    "stale": "已过期",
    "conflicted": "来源冲突",
    "error": "异常",
}


inject_css()
st.title("CandidateCard 候选工作台")
st.caption("用户研究池与公开市场发现汇总在同一工作台；候选不等于买入建议。")

is_cloud = bool(
    os.environ.get("STREAMLIT_SHARING_MODE")
    or os.environ.get("STREAMLIT_RUNTIME_ENV") == "cloud"
    or os.environ.get("IS_STREAMLIT_CLOUD")
)
if is_cloud:
    st.info("实时候选仅本地模式可用；云端内容不代表持续扫描。")
else:
    st.info("本地页面不会自动启动扫描器；ONCE 与 LOOP 状态分别记录。")

with st.expander("候选生成与数据边界"):
    st.write("候选由确定性规则和用户研究池产生，不调用 LLM，不输出综合推荐分数。")
    st.write("当前广度数据来自公开数据源；tonghuasun-agent 尚未参与候选计算。")
    st.write("价格行为仅在点击后按需读取，不会随页面加载或分钟扫描批量请求。")
    st.code("python scripts/run_realtime_scan.py --once  # 或 --loop", language="bash")


def _short_code(instrument_id: str) -> str:
    _, exchange, code = instrument_id.split(".")
    return f"{code}.{exchange}"


def _clean_reason(text: str) -> str:
    prefix = "确定性规则 + 数据源原始 reason 字段拼接："
    return text.removeprefix(prefix).strip() or "暂无补充原因"


def _pct(value: float | None) -> str:
    return "数据不足" if value is None else f"{value:.2f}%"


def _number(value: float | None, suffix: str = "") -> str:
    return "数据不足" if value is None else f"{value:.2f}{suffix}"


def _render_price_context(context: PriceVolumeContext) -> None:
    st.markdown("##### 价格与成交行为")
    first, second, third = st.columns(3)
    first.metric("5 日涨幅", _pct(context.return_5d_pct))
    second.metric("10 日涨幅", _pct(context.return_10d_pct))
    third.metric(
        "完整交易日成交量 / 前5日均量",
        _number(context.latest_completed_volume_vs_prev5_avg, " 倍"),
    )
    fourth, fifth, sixth = st.columns(3)
    fourth.metric("距 10 日最高价", _pct(context.distance_to_10d_high_pct))
    fifth.metric("最大收盘回撤", _pct(context.max_close_drawdown_10d_pct))
    sixth.metric("换手率", _pct(context.latest_turnover_pct))

    if context.deterministic_price_in_flags:
        st.markdown("**Price In 确定性提示**")
        for flag in context.deterministic_price_in_flags:
            st.write(f"- {flag.message}")
    else:
        st.caption("当前没有达到模板提示阈值的指标。")

    with st.expander("数据来源与计算口径"):
        st.write(f"完整来源：{context.source}")
        st.write(f"数据截至（北京时间）：{format_beijing(context.data_as_of)}")
        st.write(f"复权方式：{context.adjustment_method}；当前 Bar 完整：{'是' if context.current_bar_complete else '否'}")
        st.write(f"覆盖：{context.coverage}")
        st.caption("最大收盘回撤基于每日收盘价序列，不代表日内最大振幅。")
        st.caption("完整交易日成交量 / 前5日均量与旧数据源盘中量比/供应商口径不是同一口径。")
        for limitation in context.limitations:
            st.write(f"- {limitation}")
        st.caption(f"Observation：{context.observation_ref_id}")


def _rule_tags(card: CandidateCard) -> str:
    tags = []
    for rule in card.trigger_rules:
        focus = " focus" if rule == "RESEARCH_FOCUS" else ""
        tags.append(f'<span class="candidate-tag{focus}">{esc(RULE_LABELS.get(rule, rule))}</span>')
    return '<div class="candidate-tags">' + "".join(tags) + "</div>"


def _render_card(
    card: CandidateCard,
    *,
    repository: SQLiteCandidateRepository,
    cloud: bool,
) -> None:
    with st.container(border=True):
        st.markdown(
            f'#### {esc(card.instrument_name)} '
            f'<span class="candidate-code">{_short_code(card.instrument_id)}</span> '
            f'<span class="candidate-decision">{DECISION_LABELS[card.user_decision]}</span>',
            unsafe_allow_html=True,
        )
        st.markdown(_rule_tags(card), unsafe_allow_html=True)
        st.write(_clean_reason(card.reason_text))

        sources = "、".join(SOURCE_LABELS.get(item, item) for item in card.source_names)
        st.caption(
            f"首次 {format_beijing(card.first_seen_at)} · 最近 {format_beijing(card.last_seen_at)} · "
            f"命中 {card.hit_count} 次"
        )
        st.caption(
            f"数据截至 {format_beijing(card.data_as_of)} · "
            f"状态 {FRESHNESS_LABELS.get(card.freshness_status.value, card.freshness_status.value)} · "
            f"来源 {sources}"
        )

        action_columns = st.columns(3)
        for column, decision, label in (
            (action_columns[0], CandidateDecision.KEEP, "保留关注"),
            (action_columns[1], CandidateDecision.IGNORE, "忽略"),
            (action_columns[2], CandidateDecision.PROMOTE, "转入研究"),
        ):
            if column.button(
                label,
                key=f"{decision.value}-{card.candidate_id}",
                type="primary" if card.user_decision is decision else "secondary",
                disabled=cloud,
            ):
                if decision is not CandidateDecision.PROMOTE:
                    repository.set_decision(card.candidate_id, decision)
                    st.rerun()
                else:
                    try:
                        with SQLiteThesisRepository(database) as thesis_repository:
                            outcome = CandidatePromotionService(
                                repository,
                                thesis_repository,
                            ).promote(card.candidate_id)
                    except PromotionResearchError:
                        st.error("研究失败，可重试。候选决定未改变。")
                    else:
                        st.session_state["opened_thesis_id"] = str(outcome.thesis_id)
                        st.switch_page("pages/7_thesis.py")

        state_key = f"price-volume-context-{card.candidate_id}"
        if st.button(
            "查看价格行为",
            key=f"price-volume-{card.candidate_id}",
            disabled=cloud,
        ):
            try:
                with st.spinner("读取公开历史行情并计算确定性指标…"):
                    context = PublicPriceVolumeTool(
                        PublicMarketClient(timeout=15),
                        repository,
                    ).get_price_volume_context(card.instrument_id, card.trade_date, 10)
                st.session_state[state_key] = context.model_dump_json()
            except Exception as exc:
                st.error(f"价格行为读取失败：{type(exc).__name__}。请稍后重试。")
        if state_key in st.session_state:
            _render_price_context(
                PriceVolumeContext.model_validate_json(st.session_state[state_key])
            )


def _render_section(
    title: str,
    description: str,
    cards: list[CandidateCard],
    *,
    repository: SQLiteCandidateRepository,
    cloud: bool,
) -> None:
    st.markdown(f"## {title}")
    st.caption(description)
    if not cards:
        st.info("当前没有该分区的候选。")
        return
    for start in range(0, len(cards), 2):
        columns = st.columns(2, gap="medium")
        for column, card in zip(columns, cards[start:start + 2], strict=False):
            with column:
                _render_card(card, repository=repository, cloud=cloud)


database = ROOT / "data" / "thesis.db"
with SQLiteCandidateRepository(database) as repository:
    render_scan_status(repository)
    latest_date = repository.latest_trade_date()
    cards = repository.list(trade_date=latest_date) if latest_date else []

    if not cards:
        st.warning("尚无候选数据。请先在本地运行公开市场扫描。")
    else:
        st.caption(f"候选交易日：{latest_date.isoformat()} · 共 {len(cards)} 只")
        focus_cards = [card for card in cards if "RESEARCH_FOCUS" in card.trigger_rules]
        market_cards = [card for card in cards if "RESEARCH_FOCUS" not in card.trigger_rules]
        _render_section(
            "我的关注",
            "用户研究池；未来可并列真实持仓与同花顺自选，但本轮尚未接入。",
            focus_cards,
            repository=repository,
            cloud=is_cloud,
        )
        _render_section(
            "市场发现",
            "自选异动、连板、普通涨停与板块共振产生的公开市场候选。",
            market_cards,
            repository=repository,
            cloud=is_cloud,
        )

st.markdown(DISCLAIMER, unsafe_allow_html=True)
