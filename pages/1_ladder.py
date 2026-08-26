# -*- coding: utf-8 -*-
"""连板梯队页：A池分层梯队（含5日分数轨迹）+ 主线题材"""
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import DISCLAIMER, inject_css, load, render_legacy_freshness, stock_card

inject_css()
st.page_link("home.py", label="← 返回市场总览")

L = load("limit_up.json")
if not L:
    st.error("未找到数据文件 data/limit_up.json")
    st.stop()

m = L["meta"]
freshness = render_legacy_freshness(L, "data/limit_up.json")
senti_cls = {"进攻期": "b-attack", "分歧期": "b-split", "退潮期": "b-retreat"}.get(m["sentiment"], "b-gray")
sentiment_badge = "" if freshness.stale else f' <span class="badge {senti_cls}">{m["sentiment"]}</span>'
st.markdown(f'## 连板梯队{sentiment_badge}', unsafe_allow_html=True)
st.caption(f'交易日 {m["trade_date"]} · {m["sentiment_note"]} · 生成于 {m["generated_at"]}')

c1, c2, c3, c4 = st.columns(4)
c1.metric("涨停家数", m["total_limit_up"], f'昨日 {m["prev_total"] or "—"}')
c2.metric("连板晋级率", f'{m["promo_rate_pct"]}%' if m["promo_rate_pct"] is not None else "—")
c3.metric("开板率", f'{m["open_ratio_pct"]}%')
c4.metric("空间板", f'{m["max_board"]} 板', f'昨日 {m["prev_max_board"] or "—"} 板')

st.markdown("**主线题材**：" + "　".join(f'{t[0]}×{t[1]}' for t in m["themes_top"][:8]))
if freshness.stale:
    st.warning("旧版评分、角色和轨迹默认停用；下方仅保留历史板数、价格、题材与封板事实。")
else:
    st.caption("旧版分数轨迹仅针对上述交易日；不构成当前研究结论。")
st.divider()

for b in range(m["max_board"], 0, -1):
    lst = L["ladder"].get(str(b))
    if not lst:
        st.markdown(f'<div class="tier-h">{b}板 —— 断层（梯队不完整信号）</div>', unsafe_allow_html=True)
        continue
    visible = [s for s in lst if not (s["score"] == 0 and s["boards"] == 1)]
    if b == 1:
        st.markdown(f'<div class="tier-h">首板 {len(visible)} 只（评分≥50 者见「板块资金流」页下方 B 池）</div>',
                    unsafe_allow_html=True)
        continue
    st.markdown(f'<div class="tier-h">{b} 板 · {len(visible)} 只</div>', unsafe_allow_html=True)
    cols = st.columns(3)
    for i, s in enumerate(visible):
        with cols[i % 3]:
            st.markdown(
                stock_card(s, trade_date=m["trade_date"], stale=freshness.stale),
                unsafe_allow_html=True,
            )

st.markdown(DISCLAIMER, unsafe_allow_html=True)
