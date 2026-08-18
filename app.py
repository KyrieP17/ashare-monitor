# -*- coding: utf-8 -*-
"""主页：市场总览（情绪判定 + 双市场指数 + 今日要点 + 体系逻辑）"""
import os
import subprocess
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DISCLAIMER, inject_css, load

st.set_page_config(page_title="A股盯盘 · 市场总览", layout="wide", page_icon="📈")
inject_css()

L = load("limit_up.json")
M = load("latest.json")
U = load("us_market.json")

senti = L["meta"]["sentiment"] if L else "未知"
senti_cls = {"进攻期": "b-attack", "分歧期": "b-split", "退潮期": "b-retreat"}.get(senti, "b-gray")
st.markdown(f'## A股盯盘 · 小资金高弹性体系 <span class="badge {senti_cls}">{senti}</span>',
            unsafe_allow_html=True)
if L:
    st.caption(f'A股交易日 {L["meta"]["trade_date"]} · {L["meta"]["sentiment_note"]} · 生成于 {L["meta"]["generated_at"]}')
if U:
    st.caption(f'美股时段：{U["meta"]["et_time"]}（{U["meta"]["us_market_state"]}）· {U["meta"]["flow_note"]}')

with st.expander("体系逻辑（五层框架）", expanded=False):
    st.markdown(
        "**核心目标**：不追求每天赚钱，只找两种机会——**连板龙头**（骑乘情绪主升）与**主升启动股**（加速前进入）。\n"
        "路径：市场情绪 → 主线板块 → 三池候选 → 角色识别 → 次日确认 → 持有主升。\n\n"
        "**① 市场环境**：涨停家数趋势 / 炸板率 / 连板高度 / 晋级率 → 进攻期可接力，分歧期降仓，退潮期不重仓接力。\n\n"
        "**② 三个池**：A 连板龙头（2板+、主线、辨识度）；B 主升启动（首板/突破/60日新高、MA多头，分龙头/中军/补涨）；C 炸板修复（回封、弱转强）。\n\n"
        "**③ 五维评分**：板块环境25 + 辨识度角色25 + 趋势突破20 + 封板结构20 + 风险可交易10。"
        "80+ 核心观察 / 65-79 等待确认 / 50-64 后排跟踪 / <50 忽略。**高分≠买入信号，只表示值得重点盯。**\n\n"
        "**④ 次日确认才进**：连板五选二（竞价不弱 / 分歧快速收回 / 回封带动板块 / 中军同步 / 放量无抛压）；"
        "主升（回踩突破位不破 / 回踩MA5-10转强 / 缩量调整放量突破 / 强于板块）。买「分歧后的再次转强」。\n\n"
        "**⑤ 持仓与卖出**：30% 试错 → 40% 确认 → 30% 主升，三层确认后才单票重仓。"
        "卖出：断板不修复 / 板块集体炸板 / 高位放量长阴 / 跌破关键位反抽失败。")

if st.button("立即刷新数据（实时拉取，约1-2分钟）"):
    with st.spinner("正在拉取最新数据…"):
        for script, args in [("fetch_data.py", ["--force"]), ("limit_up_scan.py", []), ("us_market.py", [])]:
            subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), script)] + args,
                           capture_output=True, timeout=600)
    st.cache_data.clear()
    st.rerun()

# ---- 双市场指数 ----
st.markdown("#### 市场脉搏")
cols = st.columns(7)
if L:
    m = L["meta"]
    cols[0].metric("涨停家数", m["total_limit_up"], f'昨日 {m["prev_total"] or "—"}')
    cols[1].metric("晋级率", f'{m["promo_rate_pct"]}%' if m["promo_rate_pct"] is not None else "—")
    cols[2].metric("开板率", f'{m["open_ratio_pct"]}%')
    cols[3].metric("空间板", f'{m["max_board"]} 板')
if M:
    idx_cols = st.columns(3)
    for i, c in enumerate(["sh000001", "sz399001", "sz399006"]):
        q = M["indices"].get(c)
        if q:
            idx_cols[i].metric(q["name"] + "（A股）", f'{q["price"]:.2f}', f'{q["chg_pct"]:+.2f}%')
if U:
    us_cols = st.columns(3)
    for i, idx in enumerate(U["indices"]):
        us_cols[i].metric(idx["name"] + "（美股）", f'{idx["price"]:.2f}', f'{idx["chg_pct"]:+.2f}%')

# ---- 今日要点 ----
st.markdown("#### 今日要点")
c1, c2 = st.columns(2)
with c1:
    if L:
        core = [s for s in L["pool_a_leaders"] if s["grade"] == "核心观察"]
        if core:
            st.error("**核心观察（80+）**：" + "；".join(
                f'{s["name"]}({s["code"]}) {s["boards"]}板 {s["score"]}分' for s in core))
        else:
            st.info("本日无 80+ 核心观察标的（分歧期/退潮期宁缺毋滥）")
        st.caption("连板梯队详情见左侧导航「连板梯队」页")
with c2:
    if M:
        b_in = M["boards_industry"][0] if M["boards_industry"] else None
        b_out = M["boards_industry"][-1] if M["boards_industry"] else None
        if b_in and b_out:
            st.success(f'**资金主攻**：{b_in["name"]} +{b_in["net_yi"]}亿　|　'
                       f'**资金撤退**：{b_out["name"]} {b_out["net_yi"]}亿')
        st.caption("板块资金流详情见左侧导航「板块资金流」页")

st.markdown(DISCLAIMER, unsafe_allow_html=True)
