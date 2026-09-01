# -*- coding: utf-8 -*-
"""主入口：CandidateCard 工作台 + 只读旧版市场看板。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import streamlit as st


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from common import DISCLAIMER, inject_css, load
from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.candidates import CandidateDecision, ScanRunStatus
from thesis.freshness import artifact_freshness
from thesis.market_environment_report import MarketEnvironmentReport, build_market_environment_report
from thesis.scan_status_ui import render_scan_status


inject_css()
candidate_db = ROOT / "data" / "thesis.db"
scan_script = ROOT / "scripts" / "run_realtime_scan.py"
is_cloud = bool(
    os.environ.get("STREAMLIT_SHARING_MODE")
    or os.environ.get("STREAMLIT_RUNTIME_ENV") == "cloud"
    or os.environ.get("IS_STREAMLIT_CLOUD")
)

REGIME_LABELS = {
    "attack": "进攻",
    "divergence": "分歧",
    "retreat": "退潮",
    "unknown": "未知",
}


def _render_market_environment(report: MarketEnvironmentReport, *, stale: bool) -> None:
    st.markdown("### 柚子视角 · 市场环境报告")
    st.caption(
        f"交易日 {report.trade_date.isoformat()} · 环境 {REGIME_LABELS[report.regime.value]} · "
        f"证据置信度 {report.confidence.value.upper()}"
    )
    if stale:
        st.error("这是一份历史市场环境快照，不代表当前交易日状态。")
        st.warning(report.headline)
    elif report.regime.value == "divergence":
        st.warning(report.headline)
    elif report.regime.value == "attack":
        st.success(report.headline)
    else:
        st.info(report.headline)

    first, second, third, fourth = st.columns(4)
    breadth_delta = (
        f"{report.stats.total_limit_up_change_pct:+.1f}%"
        if report.stats.total_limit_up_change_pct is not None
        else None
    )
    first.metric(
        "涨停池覆盖",
        report.stats.total_limit_up if report.stats.total_limit_up is not None else "数据不足",
        breadth_delta,
    )
    second.metric(
        "候选空间高度",
        f"{report.stats.selected_max_board} 板"
        if report.stats.selected_max_board is not None
        else "数据不足",
    )
    third.metric(
        "已知样本平均开板",
        f"{report.stats.average_open_num:.1f} 次"
        if report.stats.average_open_num is not None
        else "数据不足",
    )
    fourth.metric("板块共振候选", report.stats.sector_resonance_count)

    left, right = st.columns(2)
    with left:
        st.markdown("**结构判断**")
        for item in report.structure_read:
            st.write(f"- {item}")
    with right:
        st.markdown("**风险与反证**")
        if report.risk_signals:
            for item in report.risk_signals:
                st.write(f"- {item}")
        else:
            st.caption("当前没有达到规则阈值的结构性风险信号。")

    with st.expander("证据、三情景与数据边界"):
        st.markdown("**证据**")
        for item in report.evidence:
            st.write(f"- {item}")
        st.markdown("**三情景**")
        for scenario in report.scenarios:
            st.write(f"- {scenario.name}：{scenario.confirmation}")
            st.caption(scenario.interpretation)
        st.markdown("**限制**")
        for item in report.limitations:
            st.caption(f"- {item}")

st.markdown("## A股 Monitor · CandidateCard 工作台")
st.caption("CandidateCard 由确定性规则生成，不是买入建议；当前广度数据来自公开数据源。")
st.caption(
    "tonghuasun-agent 尚未参与候选计算；本地 LOOP 进程停止后不会继续刷新。"
    "Streamlit Cloud 不具备本地持续扫描能力，页面存在也不代表后台扫描器正在运行。"
)

action_refresh, action_open, action_status = st.columns(3)
refresh_clicked = action_refresh.button(
    "刷新一次候选",
    type="primary",
    use_container_width=True,
    disabled=is_cloud,
)
action_open.page_link("pages/6_candidates.py", label="打开候选箱", use_container_width=True)
if action_status.button("查看扫描状态", use_container_width=True):
    st.session_state["show_scan_details"] = not st.session_state.get("show_scan_details", False)
if is_cloud:
    st.info("Streamlit Cloud 只展示部署时已有数据；本地扫描入口和本地 SQLite 持久化不可用。")

if refresh_clicked:
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(scan_script),
                "--once",
                "--database",
                str(candidate_db),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        st.error("候选刷新超时（90 秒）；请查看扫描状态中的 RUNNING 记录是否疑似停滞。")
    except Exception as exc:
        st.error(f"候选刷新未启动：{type(exc).__name__}: {str(exc)[:240]}")
    else:
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        with SQLiteCandidateRepository(candidate_db) as verification_repository:
            persisted = verification_repository.latest_once_scan_run()
        persisted_ok = bool(
            persisted
            and f"scan_run_id={persisted.scan_run_id}" in stdout
            and persisted.completed_at is not None
        )
        if completed.returncode == 0 and persisted_ok and persisted.status in {
            ScanRunStatus.SUCCEEDED,
            ScanRunStatus.PARTIAL,
        }:
            st.success(
                f"候选刷新完成：{persisted.status.value.upper()}，"
                f"Observation {persisted.observation_count}，Candidate {persisted.candidate_count}。"
            )
        else:
            details = " | ".join(part for part in (stdout[-600:], stderr[-600:]) if part)
            persistence_note = "" if persisted_ok else " | 未验证到对应 ScanRun 持久化记录"
            st.error(
                f"候选刷新失败（返回码 {completed.returncode}）{persistence_note}："
                f"{details or '无 stdout/stderr 摘要'}"
            )

with SQLiteCandidateRepository(candidate_db) as candidate_repository:
    render_scan_status(
        candidate_repository,
        expanded=st.session_state.get("show_scan_details", False),
    )
    candidate_date = candidate_repository.latest_trade_date()
    all_candidate_cards = candidate_repository.list()
    candidate_cards = (
        [card for card in all_candidate_cards if card.trade_date == candidate_date]
        if candidate_date
        else []
    )
    market_environment = build_market_environment_report(all_candidate_cards)

if market_environment is None:
    market_environment_payload = load("market_environment.json")
    if market_environment_payload:
        market_environment = MarketEnvironmentReport.model_validate(market_environment_payload)

visible_candidates = [card for card in candidate_cards if card.user_decision is not CandidateDecision.IGNORE]
st.markdown("#### 候选摘要")
if candidate_date:
    kept = sum(card.user_decision is CandidateDecision.KEEP for card in visible_candidates)
    promoted = sum(card.user_decision is CandidateDecision.PROMOTE for card in visible_candidates)
    st.write(
        f"{candidate_date.isoformat()} 候选 {len(visible_candidates)} 只 · "
        f"KEEP {kept} · PROMOTE {promoted}"
    )
else:
    st.info("尚无本地候选。请运行一次候选刷新；云端页面不代表持续扫描。")

if market_environment is not None:
    market_environment_freshness = artifact_freshness(
        market_environment.model_dump(mode="json")
    )
    _render_market_environment(
        market_environment,
        stale=market_environment_freshness.stale,
    )

st.divider()
st.warning(
    "以下为旧版规则看板，仅供历史参考，不代表当前 CandidateCard 候选逻辑。"
    "旧脚本仍保留，但已退出首页主操作入口。"
)

L = load("limit_up.json")
M = load("latest.json")
U = load("us_market.json")
L_FRESH = artifact_freshness(L)
M_FRESH = artifact_freshness(M)

senti = L["meta"]["sentiment"] if L else "未知"
senti_cls = {"进攻期": "b-attack", "分歧期": "b-split", "退潮期": "b-retreat"}.get(senti, "b-gray")
badge_text = f"数据已过期 · {senti}" if L_FRESH.stale else senti
st.markdown(
    f'### 旧版市场总览 <span class="badge {senti_cls}">{badge_text}</span>',
    unsafe_allow_html=True,
)
if L:
    st.caption(
        f'旧版 A 股交易日 {L["meta"]["trade_date"]} · {L["meta"]["sentiment_note"]} · '
        f'生成于 {L["meta"]["generated_at"]}'
    )
if L_FRESH.stale:
    shown_date = L_FRESH.trade_date.isoformat() if L_FRESH.trade_date else "未知"
    st.warning(
        f"旧版数据已过期：展示交易日 {shown_date}，最近预期有效数据日 "
        f"{L_FRESH.expected_trade_date.isoformat()}。不得视为当前实时状态。"
    )
if U:
    st.caption(f'旧版美股时段：{U["meta"]["et_time"]}（{U["meta"]["us_market_state"]}）')

nav1, nav2, nav3, nav4, nav5 = st.columns(5)
nav1.page_link("pages/1_ladder.py", label="🔥 旧连板梯队", use_container_width=True)
nav2.page_link("pages/2_sector_flow.py", label="💰 旧板块资金流", use_container_width=True)
nav3.page_link("pages/3_us_market.py", label="🌎 美股板块", use_container_width=True)
nav4.page_link("pages/4_watchlist.py", label="⭐ 自选股监控", use_container_width=True)
nav5.page_link("pages/6_candidates.py", label="📥 实时候选箱", use_container_width=True)

with st.expander("旧版五维评分体系（仅历史参考）", expanded=False):
    st.markdown(
        "此处记录旧版市场情绪、三池和五维评分逻辑，不是当前 CandidateCard 规则。"
        "当前候选只使用自选异动、连板、普通涨停池和板块共振四类确定性规则。"
    )

st.markdown("#### 旧版市场脉搏")
cols = st.columns(7)
if L:
    market = L["meta"]
    cols[0].metric("涨停家数", market["total_limit_up"], f'前值 {market["prev_total"] or "—"}')
    cols[1].metric("晋级率", f'{market["promo_rate_pct"]}%' if market["promo_rate_pct"] is not None else "—")
    cols[2].metric("开板率", f'{market["open_ratio_pct"]}%')
    cols[3].metric("空间板", f'{market["max_board"]} 板')
if M:
    index_columns = st.columns(3)
    for index, code in enumerate(["sh000001", "sz399001", "sz399006"]):
        quote = M["indices"].get(code)
        if quote:
            index_columns[index].metric(quote["name"] + "（旧版数据）", f'{quote["price"]:.2f}', f'{quote["chg_pct"]:+.2f}%')
if U:
    us_columns = st.columns(3)
    for index, item in enumerate(U["indices"]):
        us_columns[index].metric(item["name"] + "（美股）", f'{item["price"]:.2f}', f'{item["chg_pct"]:+.2f}%')

st.markdown("#### 旧版数据要点")
left, right = st.columns(2)
with left:
    if L:
        legacy_core = [item for item in L["pool_a_leaders"] if item["grade"] == "核心观察"]
        if legacy_core:
            st.info(
                "**旧版核心观察（历史评分输出）**："
                + "；".join(
                    f'{item["name"]}({item["code"]}) {item["boards"]}板 {item["score"]}分'
                    for item in legacy_core
                )
            )
        else:
            st.caption("旧版数据中无 80+ 评分记录。")
with right:
    if M:
        inflow = M["boards_industry"][0] if M["boards_industry"] else None
        outflow = M["boards_industry"][-1] if M["boards_industry"] else None
        if inflow and outflow:
            st.info(
                f'旧版资金记录：{inflow["name"]} {inflow["net_yi"]:+} 亿；'
                f'{outflow["name"]} {outflow["net_yi"]:+} 亿'
            )

if M_FRESH.stale:
    st.caption("旧版板块与自选数据同样已过期；页面不会自动把它解释为当前候选信号。")

st.markdown(DISCLAIMER, unsafe_allow_html=True)
