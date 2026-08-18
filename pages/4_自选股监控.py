# -*- coding: utf-8 -*-
"""自选股页：异动告警 + 行情明细"""
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import DISCLAIMER, inject_css, load

st.set_page_config(page_title="自选股监控", layout="wide", page_icon="⭐")
inject_css()

M = load("latest.json")
if not M:
    st.error("未找到数据文件 data/latest.json")
    st.stop()

st.markdown("## 自选股监控")
st.caption(f'数据时间 {M["meta"]["generated_at"]} · 阈值：涨跌幅±4% / 放量2倍 / 主力净流入5000万')

if M["alerts"]:
    for a in M["alerts"]:
        st.warning(f'**{a["name"]}**（{a["code"]}）{a["type"]}　现价 {a["price"]}')
else:
    st.success("本轮无异动触发")

rows = []
for w in M["watchlist"]:
    flow = w.get("fund_flow")
    rows.append({"名称": w["name"], "代码": w["code"], "现价": w["price"],
                 "涨跌幅%": w["chg_pct"], "成交额(亿)": round(w["amount_wan"] / 10000, 2),
                 "量比(5日)": w["vol_ratio"],
                 "主力净流入(亿)": round(flow["main_net_wan"] / 100, 2) if flow else None,
                 "换手率%": w["turnover_pct"],
                 "异动": "；".join(w["alerts"]) or "—"})
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.markdown(DISCLAIMER, unsafe_allow_html=True)
