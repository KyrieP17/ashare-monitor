# -*- coding: utf-8 -*-
"""入口：st.navigation 多页导航（multipage v2，显式注册，规避 page_link 注册表 bug）"""
import streamlit as st

st.set_page_config(page_title="A股盯盘 · 高弹性体系", layout="wide", page_icon="📈",
                   initial_sidebar_state="expanded")

pg = st.navigation([
    st.Page("home.py", title="市场总览", icon="📈", default=True),
    st.Page("pages/1_ladder.py", title="连板梯队", icon="🔥"),
    st.Page("pages/2_sector_flow.py", title="板块资金流", icon="💰"),
    st.Page("pages/3_us_market.py", title="美股板块", icon="🌎"),
    st.Page("pages/4_watchlist.py", title="自选股监控", icon="⭐"),
    st.Page("pages/5_tracker.py", title="评分有效性", icon="🎯"),
])
pg.run()
