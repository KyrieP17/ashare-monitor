# -*- coding: utf-8 -*-
"""板块资金流页：行业/概念资金流图 + B池主升启动 + C池炸板修复 + 板块评测"""
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import DISCLAIMER, esc, flow_fig, inject_css, load

st.set_page_config(page_title="板块资金流", layout="wide", page_icon="💰")
inject_css()

M = load("latest.json")
L = load("limit_up.json")
if not M:
    st.error("未找到数据文件 data/latest.json")
    st.stop()

st.markdown("## 板块资金流")
st.caption(f'数据时间 {M["meta"]["generated_at"]} · 来源：新浪财经 MoneyFlow · 红流入/绿流出')

tab1, tab2 = st.tabs(["行业板块", "概念板块"])
with tab1:
    st.plotly_chart(flow_fig(M["boards_industry"], 15, "行业板块主力净流入（亿元）"),
                    use_container_width=True)
with tab2:
    st.plotly_chart(flow_fig(M["boards_concept"], 15, "概念板块主力净流入（亿元）"),
                    use_container_width=True)

if L:
    st.divider()
    st.markdown("#### B 池 · 主升启动（首板/突破结构，评分≥50）")
    if L["pool_b_starters"]:
        rows = []
        for s in L["pool_b_starters"]:
            k = s.get("kline") or {}
            trend = []
            if k.get("new_high_60d"):
                trend.append("60日新高")
            if k.get("ma_bull"):
                trend.append("MA20>MA60")
            elif k.get("ma20_up"):
                trend.append("MA20向上")
            rows.append({"评分": s["score"], "名称": s["name"], "代码": s["code"], "角色": s["role"],
                         "涨停原因": s["reason"], "趋势结构": " · ".join(trend) or "—",
                         "封单(亿)": s["seal_yi"], "换手%": s["turnover_pct"],
                         "首封": s["first_seal"], "流通市值(亿)": s["float_cap_yi"],
                         "风险": "；".join(s["risks"]) or "—"})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("本日无评分≥50的首板")

    st.markdown("#### C 池 · 炸板修复（开板后回封，高风险高弹性）")
    if L["pool_c_repair"]:
        rows = [{"评分": s["score"], "名称": s["name"], "代码": s["code"], "板数": s["boards"],
                 "涨停原因": s["reason"], "开板次数": s["open_num"], "封单(亿)": s["seal_yi"],
                 "换手%": s["turnover_pct"], "风险": "；".join(s["risks"]) or "—"}
                for s in L["pool_c_repair"][:15]]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("本日无回封个股")

st.divider()
st.markdown("#### 板块评测（辅助发现工具）")
th1 = M["config"]["board_score"]["active_threshold"]
th2 = M["config"]["board_score"]["neutral_threshold"]
rows = []
for i, bs in enumerate(M["boards_industry"][:20]):
    rating = "积极关注" if bs["score"] >= th1 else ("中性观察" if bs["score"] >= th2 else "谨慎回避")
    rows.append({"排名": i + 1, "板块": bs["name"], "评分": bs["score"], "评级": rating,
                 "主力净流入(亿)": bs["net_yi"], "涨跌幅%": bs["chg_pct"],
                 "领涨股": f'{bs["lead_stock"]} {bs["lead_chg_pct"]}%', "评分依据": bs["reason"]})
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.markdown(DISCLAIMER, unsafe_allow_html=True)
