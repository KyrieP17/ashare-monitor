# -*- coding: utf-8 -*-
"""自选股页：异动告警 + 持仓检查（体系卖出规则）+ 次日确认 + 行情明细"""
import os
import sys

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import DISCLAIMER, inject_css, load

st.set_page_config(page_title="自选股监控", layout="wide", page_icon="⭐")
inject_css()

M = load("latest.json")
P = load("positions_report.json")
C = load("confirm.json")

st.markdown("## 自选股监控")
if M:
    st.caption(f'数据时间 {M["meta"]["generated_at"]} · 阈值：涨跌幅±4% / 放量2倍 / 主力净流入5000万')

# ---- 持仓区（P1）----
st.markdown("### 持仓检查（体系卖出规则）")
if P and P.get("positions"):
    for r in P["positions"]:
        if r.get("error"):
            st.error(f'{r["name"]}（{r["code"]}）数据获取失败：{r["error"]}')
            continue
        level = r["level"]
        head = f'**{r["name"]}**（{r["code"]}）现价 {r["price"]} · 盈亏 {r["pnl_pct"]:+.2f}% · {r["stage"]}'
        if level == "exit":
            st.error(f'🔴 退出信号｜{head}' + ("" if not r["signals"] else "\n\n- " + "\n- ".join(r["signals"])))
        elif level == "reduce":
            st.warning(f'🟠 减仓信号｜{head}' + ("" if not r["signals"] else "\n\n- " + "\n- ".join(r["signals"])))
        elif level == "watch":
            st.info(f'🟡 观察｜{head}' + ("" if not r["signals"] else "\n\n- " + "\n- ".join(r["signals"])))
        else:
            st.success(f'🟢 持有｜{head}')
    st.caption(P["meta"]["rule_note"])
else:
    st.caption("未配置真实持仓。编辑仓库 positions.json 填入 cost>0 的持仓后，此区按体系第五层卖出规则自动检查。")

# ---- 次日确认区（P3）----
st.markdown("### 次日确认（昨日高分股）")
if C and C.get("checks"):
    st.caption(f'推荐日 {C.get("rec_date_source")} · 检查更新于 {C.get("updated_at")}')
    rows = []
    for code, v in C["checks"].items():
        rows.append({
            "名称": v["name"], "代码": code, "评分": v["score"], "板数": v["boards"],
            "竞价%": v.get("auction_pct"), "开盘%": v.get("open_pct"),
            "现价%": v.get("now_pct"), "分歧收回": "√" if v.get("reclaim") else "—",
            "确认结果": v.get("confirm", "待检查"),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
else:
    st.caption("无待确认推荐。确认检查在交易日 9:26（竞价）与 10:05（开盘30分钟）自动执行。")

# ---- 异动告警 ----
st.markdown("### 异动告警")
if M:
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
                     "主力净流入(亿)": round(flow["main_net_wan"] / 10000, 2) if flow else None,
                     "换手率%": w["turnover_pct"],
                     "异动": "；".join(w["alerts"]) or "—"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.markdown(DISCLAIMER, unsafe_allow_html=True)
