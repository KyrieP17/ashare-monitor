# -*- coding: utf-8 -*-
"""公共模块：数据加载、样式、渲染函数"""
import json
import os

import plotly.graph_objects as go
import streamlit as st

from thesis.freshness import ArtifactFreshness, artifact_freshness

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

CSS = """
<style>
.badge{display:inline-block;padding:2px 12px;border-radius:12px;font-size:14px;font-weight:600}
.b-attack{background:#fdecec;color:#d43c3c}.b-split{background:#fff7e6;color:#b57a1e}
.b-retreat{background:#e8f7ef;color:#1a9e5c}.b-gray{background:#eceff3;color:#8a8f99}
.b-pre{background:#e6f1fb;color:#185FA5}
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
.legacy-note{display:inline-block;padding:3px 9px;border-radius:8px;background:#eceff3;color:#61666f;font-size:12px;font-weight:600}
.candidate-tags{margin:4px 0 8px}.candidate-tag{display:inline-block;padding:2px 7px;margin:0 5px 4px 0;border-radius:8px;background:#eef2f6;color:#4e5969;font-size:11px}
.candidate-tag.focus{background:#fdecec;color:#b53838}.candidate-code{color:#8a8f99;font-size:12px;font-weight:500}
.candidate-decision{display:inline-block;padding:2px 7px;border-radius:8px;background:#f1f3f5;color:#646a73;font-size:11px}
</style>
"""

DISCLAIMER = (
    '<div class="disc"><b>免责声明</b>：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。'
    '市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，'
    '必要时咨询持牌专业机构。过往表现不预示未来收益。数据来自同花顺/新浪财经/腾讯财经公开接口，'
    '由 GitHub Actions 每日自动更新。</div>')

ROLE_CLS = {"龙头": "r-leader", "中军": "r-mid", "补涨": "r-fill", "跟风": "r-follow"}


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


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


def traj_html(hist, cur):
    if not hist:
        return ""
    h = '<div class="traj">'
    for t in hist:
        if not t.get("in_pool"):
            color, hgt, tip, label = "#e4e7eb", 6, f"{t['date']} 未涨停", "·"
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


def stock_card(s, show_traj=True, *, trade_date=None, stale=False):
    date_line = f'<div class="rsn">对应交易日：{esc(trade_date)}</div>' if trade_date else ""
    if stale:
        return (f'<div class="stk"><div class="nm">{esc(s["name"])}<span class="code">{s["code"]}</span></div>'
                f'{date_line}<div class="rsn">{esc(s["reason"])}</div>'
                f'<div class="rsn">{s["boards"]}板 · 封单{s["seal_yi"]}亿 · 换手{s["turnover_pct"]}% · {esc(s["lut_type"])}</div>'
                '<div class="rsn" style="color:#8a8f99">旧版风险结论已停用；以下仅为对应历史交易日记录。</div></div>')
    risks = (f'<div class="rsn" style="color:#6b7078">旧规则注意项（仅针对该交易日）：'
             f'{esc("；".join(s["risks"]))}</div>') if s["risks"] else ""
    promoted = ' · <span class="up">晋级</span>' if s.get("promoted") else ""
    traj = traj_html(s.get("score_history"), s["score"]) if show_traj else ""
    return (f'<div class="stk"><div class="nm">{esc(s["name"])}<span class="code">{s["code"]}</span>'
            f'<span class="role {ROLE_CLS.get(s["role"], "r-follow")}">{s["role"]}</span>'
            f'<span class="sc {sc_cls(s["score"])}">{s["score"]}分</span></div>'
            f'{date_line}<div class="rsn">{esc(s["reason"])}</div>'
            f'<div class="rsn">封单{s["seal_yi"]}亿 · 换手{s["turnover_pct"]}% · {esc(s["lut_type"])}'
            f'{" · " + s["first_seal"] if s["first_seal"] != "15:00" else ""}{promoted}</div>'
            f'{risks}{traj}</div>')


def render_legacy_freshness(payload, source: str) -> ArtifactFreshness:
    freshness = artifact_freshness(payload)
    trade_date = freshness.trade_date.isoformat() if freshness.trade_date else "未知"
    generated_at = (
        freshness.generated_at.strftime("%Y-%m-%d %H:%M:%S")
        if freshness.generated_at else "未知"
    )
    st.markdown('<span class="legacy-note">旧版规则看板</span>', unsafe_allow_html=True)
    st.caption(
        f"数据交易日：{trade_date} · 数据生成时间：{generated_at} · "
        f"是否过期：{'是' if freshness.stale else '否'} · 数据来源：{source}"
    )
    if freshness.stale:
        st.error("当前展示的是历史数据，不代表今日市场状态。")
    else:
        st.info("旧版规则内容仅对应所示交易日，不代表模型研究结论。")
    return freshness


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


def us_fig(rows, title):
    fig = go.Figure(go.Bar(
        x=[r["chg_pct"] for r in rows], y=[r["name"] for r in rows], orientation="h",
        marker_color=["#d43c3c" if r["chg_pct"] >= 0 else "#1a9e5c" for r in rows],
        text=[f'{r["chg_pct"]:+.2f}%' for r in rows], textposition="outside",
        hovertemplate="%{y}<br>涨跌 %{x}%<br>量比 %{customdata}",
        customdata=[r["vol_ratio"] for r in rows],
    ))
    fig.update_layout(title=title, height=30 * len(rows) + 80, margin=dict(l=10, r=60, t=40, b=10),
                      xaxis_title="%", plot_bgcolor="white", paper_bgcolor="white")
    return fig
