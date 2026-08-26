# -*- coding: utf-8 -*-
"""评分有效性页：推荐追踪 + T+N 收益回填 + 分档胜率"""
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import DISCLAIMER, inject_css, load, render_legacy_freshness

inject_css()

T = load("tracker.json")
st.markdown("## 评分有效性追踪")
freshness = render_legacy_freshness(T, "data/tracker.json")
st.caption("旧版评分追踪历史档案；不再作为当前 CandidateCard 体系的有效性结论。")

if not T or not T.get("details"):
    st.info("追踪数据从 2026-08-20 起积累，首批 T+1 收益将在下一交易日回填。跑满一个月后这里有分档胜率、平均收益与明细。")
    st.markdown(DISCLAIMER, unsafe_allow_html=True)
    st.stop()

st.caption(f'{T["meta"]["note"]} · 更新于 {T["meta"]["generated_at"]}')

# ---- 汇总卡片 ----
s_all = T["summary"]["all"]
s_core = T["summary"]["core"]
s_wait = T["summary"]["wait"]

def stat_line(col, label, s):
    with col:
        st.markdown(f"**{label}**")
        for h in ("t1", "t3", "t5"):
            d = s[h]
            if d["n"]:
                st.metric(f"T+{h[1]}（n={d['n']}）", f"{d['avg_ret']:+.2f}%", f"胜率 {d['win_rate']}%")
            else:
                st.metric(f"T+{h[1]}", "未到期")

c1, c2, c3 = st.columns(3)
stat_line(c1, "全部推荐", s_all)
stat_line(c2, "核心观察（80+）", s_core)
stat_line(c3, "等待确认（65-79）", s_wait)

st.divider()
st.markdown("#### 推荐明细（近60条）")
rows = []
for r in T["details"]:
    rows.append({
        "日期": r["date"], "名称": r["name"], "代码": r["code"],
        "评分": r["score"], "档位": r["grade"], "板数": r["boards"],
        "情绪": r.get("sentiment", ""), "推荐日收盘": r["close"],
        "T+1%": r.get("ret_t1"), "T+3%": r.get("ret_t3"), "T+5%": r.get("ret_t5"),
    })
df = pd.DataFrame(rows)
st.dataframe(df, use_container_width=True, hide_index=True)

st.markdown(DISCLAIMER, unsafe_allow_html=True)
