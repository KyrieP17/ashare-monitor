# -*- coding: utf-8 -*-
"""美股板块页：盘前/盘中涨跌 + 量比（资金流代理）"""
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import DISCLAIMER, inject_css, load, us_fig

st.set_page_config(page_title="美股板块", layout="wide", page_icon="🌎")
inject_css()

U = load("us_market.json")
if not U:
    st.error("未找到数据文件 data/us_market.json（等待 GitHub Actions 下次更新）")
    st.stop()

m = U["meta"]
state_zh = {"premarket": "盘前", "regular": "盘中", "afterhours": "盘后",
            "closed": "休市", "closed_weekend": "周末休市"}.get(m["us_market_state"], m["us_market_state"])
state_cls = {"盘前": "b-pre", "盘中": "b-attack"}.get(state_zh, "b-gray")
st.markdown(f'## 美股板块 <span class="badge {state_cls}">{state_zh}</span>', unsafe_allow_html=True)
st.caption(f'{m["et_time"]} · 生成于 {m["generated_at"]}（北京）· {m["flow_note"]}')

cols = st.columns(3)
for i, idx in enumerate(U["indices"]):
    cols[i].metric(idx["name"], f'{idx["price"]:.2f}', f'{idx["chg_pct"]:+.2f}%')

st.divider()
tab1, tab2 = st.tabs(["11大行业板块（SPDR ETF）", "热门主题（ETF）"])
with tab1:
    st.plotly_chart(us_fig(U["sectors"], "行业板块涨跌幅 %（红涨绿跌 · 悬浮看量比）"),
                    use_container_width=True)
with tab2:
    st.plotly_chart(us_fig(U["themes"], "主题板块涨跌幅 %（红涨绿跌 · 悬浮看量比）"),
                    use_container_width=True)

with st.expander("明细数据"):
    rows = [{"代码": s["symbol"], "名称": s["name"], "现价": s["price"],
             "涨跌幅%": s["chg_pct"], "量比(5日)": s["vol_ratio"],
             "昨收": s["prev_close"]} for s in U["sectors"] + U["themes"]]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.markdown(DISCLAIMER, unsafe_allow_html=True)
