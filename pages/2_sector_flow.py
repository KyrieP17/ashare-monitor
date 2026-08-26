# -*- coding: utf-8 -*-
"""板块资金流页：行业/概念资金流图 + B池主升启动 + C池炸板修复 + 板块评测"""
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import DISCLAIMER, esc, flow_fig, inject_css, load, render_legacy_freshness
from thesis.freshness import artifact_freshness

inject_css()
st.page_link("home.py", label="← 返回市场总览")

M = load("latest.json")
L = load("limit_up.json")
if not M:
    st.error("未找到数据文件 data/latest.json")
    st.stop()

st.markdown("## 板块资金流")
freshness = render_legacy_freshness(M, "data/latest.json；附加历史池 data/limit_up.json")
st.caption(f'数据时间 {M["meta"]["generated_at"]} · 来源：新浪财经 MoneyFlow · 红流入/绿流出')

tab1, tab2 = st.tabs(["行业板块", "概念板块"])
with tab1:
    st.plotly_chart(flow_fig(M["boards_industry"], 15, "行业板块主力净流入（亿元）"),
                    use_container_width=True)
with tab2:
    st.plotly_chart(flow_fig(M["boards_concept"], 15, "概念板块主力净流入（亿元）"),
                    use_container_width=True)

if L:
    limit_freshness = artifact_freshness(L)
    st.divider()
    if limit_freshness.stale:
        st.warning("旧版风险结论与评分已停用；以下仅保留对应历史交易日的池内事实。")
    with st.expander("旧版 B/C 池历史记录（默认收起）"):
        st.caption(f'对应交易日：{L["meta"]["trade_date"]}')
        st.markdown("#### B 池历史记录")
        if L["pool_b_starters"]:
            rows = [{"名称": s["name"], "代码": s["code"], "板数": s["boards"],
                     "涨停原因": s["reason"], "封单(亿)": s["seal_yi"],
                     "换手%": s["turnover_pct"], "首封": s["first_seal"]}
                    for s in L["pool_b_starters"]]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("该历史交易日无 B 池记录")

        st.markdown("#### C 池历史记录")
        if L["pool_c_repair"]:
            rows = [{"名称": s["name"], "代码": s["code"], "板数": s["boards"],
                     "涨停原因": s["reason"], "开板次数": s["open_num"],
                     "封单(亿)": s["seal_yi"], "换手%": s["turnover_pct"]}
                    for s in L["pool_c_repair"][:15]]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.caption("该历史交易日无 C 池记录")

    if not limit_freshness.stale and L["pool_b_starters"]:
        with st.expander("旧版评分与注意项（仅针对该交易日）"):
            st.caption("旧规则注意项（仅针对该交易日），不代表模型研究结论。")
            rows = []
            for s in L["pool_b_starters"]:
                rows.append({"历史评分": s["score"], "名称": s["name"], "代码": s["code"],
                             "旧角色": s["role"], "旧规则注意项（仅针对该交易日）": "；".join(s["risks"]) or "—"})
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.divider()
with st.expander("旧版板块评分（历史记录，默认收起）"):
    st.caption("仅对应上方数据交易日，不代表今日结论。")
    th1 = M["config"]["board_score"]["active_threshold"]
    th2 = M["config"]["board_score"]["neutral_threshold"]
    rows = []
    for i, bs in enumerate(M["boards_industry"][:20]):
        rating = "积极关注" if bs["score"] >= th1 else ("中性观察" if bs["score"] >= th2 else "谨慎回避")
        rows.append({"排名": i + 1, "板块": bs["name"], "历史评分": bs["score"], "旧规则评级": rating,
                     "主力净流入(亿)": bs["net_yi"], "涨跌幅%": bs["chg_pct"],
                     "领涨股": f'{bs["lead_stock"]} {bs["lead_chg_pct"]}%', "旧评分依据": bs["reason"]})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.markdown(DISCLAIMER, unsafe_allow_html=True)
