# -*- coding: utf-8 -*-
"""
A股盯盘 · 小资金高弹性体系 —— Streamlit 公开版
数据：GitHub Actions 每交易日收盘后自动更新 data/*.json；也可点按钮实时刷新
"""
import json
import os
import subprocess
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

st.set_page_config(page_title="A股盯盘 · 高弹性体系", layout="wide", page_icon="📈")

CSS = """
<style>
.badge{display:inline-block;padding:2px 12px;border-radius:12px;font-size:14px;font-weight:600}
.b-attack{background:#fdecec;color:#d43c3c}.b-split{background:#fff7e6;color:#b57a1e}
.b-retreat{background:#e8f7ef;color:#1a9e5c}.b-gray{background:#eceff3;color:#8a8f99}
.stk{background:#fafbfc;border:1px solid #eef0f3;border-radius:8px;padding:8px 12px;margin-bottom:8px}
.stk .nm{font-weight:600;font-size:14px}.stk .code{color:#8a8f99;font-weight:400;font-size:12px;margin-left:4px}
.stk .sc{float:right;font-weight:700}.sc80{color:#b22222}.sc65{color:#b57a1e}.sc50{color:#8a8f99}
.stk .rsn{font-size:12px;color:#6b7078;margin-top:2px;line-height:1.5}
.role{display:inline-block;font-size:11px;border-radius:6px;padding:0 6px;margin-left:6px}
.r-leader{background:#fdecec;color:#b22222}.r-mid{background:#e6f1fb;color:#185FA5}
.r-fill{background:#fff7e6;color:#b57a1e}.r-follow{background:#eceff3;color:#8a8f99}
.traj{display:flex;align-items:flex-end;gap:3px;margin-top:14px}
.traj .bar{width:14px;border-radius:2px 2px 0 0;position:relative}
.traj .bar span{position:absolute;top:-14px;left:50%;transform:translateX(-50%);font-size:10px;color:#8a8f99}
.tier-h{font-size:13px;font-weight:600;color:#8a8f99;margin:10px 0 6px}
.disc{font-size:12px;color:#a0a4ab;line-height:1.7;background:#fafbfc;border-radius:8px;padding:12px 16px;margin-top:20px}
.up{color:#d43c3c}.down{color:#1a9e5c}
</style>
"""


@st.cache_data(ttl=300)
def load(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def esc(s):
    return str(s).replace("<", "&lt;")


def sc_cls(s):
    return "sc80" if s >= 80 else ("sc65" if s >= 65 else "sc50")


ROLE_CLS = {"龙头": "r-leader", "中军": "r-mid", "补涨": "r-fill", "跟风": "r-follow"}


def traj_html(hist, cur):
    if not hist:
        return ""
    h = '<div class="traj">'
    for t in hist:
        if not t.get("in_pool"):
            color, hgt, tip = "#e4e7eb", 6, f"{t['date']} 未涨停"
            label = "·"
        else:
            color = ("#d43c3c" if t["score"] >= 80 else "#EF9F27" if t["score"] >= 65
                     else "#B4B2A9" if t["score"] >= 50 else "#e4e7eb")
            hgt = max(6, round(t["score"] / 100 * 32))
            tip = f"{t['date']} {t['score']}分 {t['boards']}板"
            label = str(t["score"])
        h += f'<div class="bar" style="height:{hgt}px;background:{color}" title="{tip}"><span>{label}</span></div>'
    c = "#d43c3c" if cur >= 80 else "#EF9F27" if cur >= 65 else "#B4B2A9"
    h += (f'<div class="bar" style="height:{max(8, round(cur / 100 * 32))}px;background:{c};'
          f'outline:1.5px solid #1f2329" title="今日 {cur}分"><span style="color:#1f2329">{cur}</span></div></div>')
    return h


def stock_card(s, show_traj=True):
    risks = f'<div class="rsn" style="color:#1a7a46">风险：{esc("；".join(s["risks"]))}</div>' if s["risks"] else ""
    promoted = ' · <span class="up">晋级</span>' if s.get("promoted") else ""
    traj = traj_html(s.get("score_history"), s["score"]) if show_traj else ""
    return (f'<div class="stk"><div class="nm">{esc(s["name"])}<span class="code">{s["code"]}</span>'
            f'<span class="role {ROLE_CLS.get(s["role"], "r-follow")}">{s["role"]}</span>'
            f'<span class="sc {sc_cls(s["score"])}">{s["score"]}分</span></div>'
            f'<div class="rsn">{esc(s["reason"])}</div>'
            f'<div class="rsn">封单{s["seal_yi"]}亿 · 换手{s["turnover_pct"]}% · {esc(s["lut_type"])}'
            f'{" · " + s["first_seal"] if s["first_seal"] != "15:00" else ""}{promoted}</div>'
            f'{risks}{traj}</div>')


def flow_fig(boards, top_n, title):
    inflow = sorted([b for b in boards if b["net_yi"] > 0], key=lambda x: -x["net_yi"])[:top_n]
    outflow = sorted([b for b in boards if b["net_yi"] <= 0], key=lambda x: x["net_yi"])[:top_n]
    rows = outflow + inflow[::-1]
    fig = go.Figure(go.Bar(
        x=[b["net_yi"] for b in rows], y=[b["name"] for b in rows], orientation="h",
        marker_color=["#d43c3c" if b["net_yi"] >= 0 else "#1a9e5c" for b in rows],
        text=[f'{b["net_yi"]}亿' for b in rows], textposition="outside",
        hovertemplate="%{y}<br>净流入 %{x} 亿<br>涨跌幅 %{customdata[0]}%<br>领涨 %{customdata[1]} %{customdata[2]}%",
        customdata=[[b["chg_pct"], b["lead_stock"], b["lead_chg_pct"]] for b in rows],
    ))
    fig.update_layout(title=title, height=30 * len(rows) + 80, margin=dict(l=10, r=60, t=40, b=10),
                      xaxis_title="亿元", plot_bgcolor="white", paper_bgcolor="white")
    return fig


def main():
    st.markdown(CSS, unsafe_allow_html=True)
    L = load("limit_up.json")
    M = load("latest.json")

    senti = L["meta"]["sentiment"] if L else "未知"
    senti_cls = {"进攻期": "b-attack", "分歧期": "b-split", "退潮期": "b-retreat"}.get(senti, "b-gray")
    st.markdown(f'## A股盯盘 · 小资金高弹性体系 <span class="badge {senti_cls}">{senti}</span>',
                unsafe_allow_html=True)
    if L:
        st.caption(f'交易日 {L["meta"]["trade_date"]} · {L["meta"]["sentiment_note"]} · 生成于 {L["meta"]["generated_at"]}')

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
            "卖出：断板不修复 / 板块集体炸板 / 高位放量长阴 / 跌破关键位反抽失败。接受多次小亏，让少数主升贡献主要收益。")

    if st.button("立即刷新数据（实时拉取，约1-2分钟）"):
        with st.spinner("正在拉取最新数据…"):
            for script, args in [("fetch_data.py", ["--force"]), ("limit_up_scan.py", [])]:
                subprocess.run([sys.executable, os.path.join(BASE, script)] + args,
                               capture_output=True, timeout=600)
        st.cache_data.clear()
        st.rerun()

    # ---- 统计卡 ----
    cols = st.columns(7)
    if L:
        m = L["meta"]
        cols[0].metric("涨停家数", m["total_limit_up"], f'昨日 {m["prev_total"] or "—"}')
        cols[1].metric("晋级率", f'{m["promo_rate_pct"]}%' if m["promo_rate_pct"] is not None else "—")
        cols[2].metric("开板率", f'{m["open_ratio_pct"]}%')
        cols[3].metric("空间板", f'{m["max_board"]} 板', f'昨日 {m["prev_max_board"] or "—"} 板')
    if M:
        for i, c in enumerate(["sh000001", "sz399001", "sz399006"]):
            q = M["indices"].get(c)
            if q:
                cols[4 + i].metric(q["name"], f'{q["price"]:.2f}', f'{q["chg_pct"]:+.2f}%')

    # ---- 双栏：梯队 | 资金流 ----
    left, right = st.columns(2)
    with left:
        st.markdown("#### 连板梯队（A池）")
        if L:
            themes = "　".join(f'{t[0]}×{t[1]}' for t in L["meta"]["themes_top"][:6])
            st.caption(f"主线题材：{themes}")
            for b in range(L["meta"]["max_board"], 0, -1):
                lst = L["ladder"].get(str(b))
                if not lst:
                    st.markdown(f'<div class="tier-h">{b}板 —— 断层</div>', unsafe_allow_html=True)
                    continue
                visible = [s for s in lst if not (s["score"] == 0 and s["boards"] == 1)]
                if b == 1:
                    st.markdown(f'<div class="tier-h">首板 {len(visible)} 只（评分≥50 见下方 B 池）</div>',
                                unsafe_allow_html=True)
                    continue
                st.markdown(f'<div class="tier-h">{b} 板 · {len(visible)} 只</div>', unsafe_allow_html=True)
                for s in visible:
                    st.markdown(stock_card(s), unsafe_allow_html=True)
    with right:
        st.markdown("#### 板块资金流")
        if M:
            st.plotly_chart(flow_fig(M["boards_industry"], 12, "行业板块（亿元 · 红流入/绿流出）"),
                            use_container_width=True)
            st.plotly_chart(flow_fig(M["boards_concept"], 12, "概念板块（亿元）"),
                            use_container_width=True)

    # ---- B 池 ----
    st.markdown("#### B 池 · 主升启动（首板/突破结构，评分≥50）")
    if L and L["pool_b_starters"]:
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

    # ---- C 池 ----
    st.markdown("#### C 池 · 炸板修复（开板后回封，高风险高弹性）")
    if L and L["pool_c_repair"]:
        rows = [{"评分": s["score"], "名称": s["name"], "代码": s["code"], "板数": s["boards"],
                 "涨停原因": s["reason"], "开板次数": s["open_num"], "封单(亿)": s["seal_yi"],
                 "换手%": s["turnover_pct"], "风险": "；".join(s["risks"]) or "—"}
                for s in L["pool_c_repair"][:15]]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.caption("本日无回封个股")

    # ---- 自选股 ----
    st.markdown("#### 自选股异动")
    if M:
        if M["alerts"]:
            for a in M["alerts"]:
                st.warning(f'**{a["name"]}**（{a["code"]}）{a["type"]}　现价 {a["price"]}')
        else:
            st.caption("本轮无异动触发")
        rows = []
        for w in M["watchlist"]:
            flow = w.get("fund_flow")
            rows.append({"名称": w["name"], "代码": w["code"], "现价": w["price"],
                         "涨跌幅%": w["chg_pct"], "成交额(亿)": round(w["amount_wan"] / 10000, 2),
                         "量比(5日)": w["vol_ratio"],
                         "主力净流入(亿)": round(flow["main_net_wan"] / 100, 2) if flow else None,
                         "异动": "；".join(w["alerts"]) or "—"})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown(
        '<div class="disc"><b>免责声明</b>：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。'
        '市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，'
        '必要时咨询持牌专业机构。过往表现不预示未来收益。数据来自同花顺/新浪财经/腾讯财经公开接口，'
        '由 GitHub Actions 每交易日自动更新。</div>', unsafe_allow_html=True)


main()
