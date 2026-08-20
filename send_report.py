# -*- coding: utf-8 -*-
"""
每日复盘邮件发送器
凭证从环境变量读取（GitHub Secrets / 本地环境变量），缺失则跳过不报错：
  EMAIL_USER   发件邮箱（Gmail）
  EMAIL_PASS   Gmail 应用专用密码（16位，非登录密码）
  EMAIL_TO     收件邮箱（默认 = EMAIL_USER）
"""
import os, json, smtplib
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.header import Header

BJ = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")


def load(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def build_report():
    L = load("limit_up.json")
    M = load("latest.json")
    P = load("positions_report.json")
    C = load("confirm.json")
    U = load("us_market.json")

    lines = []
    if L:
        m = L["meta"]
        lines.append(f"【市场环境】{m['sentiment']}｜涨停 {m['total_limit_up']} 只（昨日 {m['prev_total']}）"
                     f"｜晋级率 {m['promo_rate_pct']}%｜开板率 {m['open_ratio_pct']}%｜空间板 {m['max_board']} 板")
        lines.append(f"【主线题材】" + "、".join(f"{t[0]}×{t[1]}" for t in m["themes_top"][:5]))
        core = [s for s in L["pool_a_leaders"] if s["grade"] == "核心观察"]
        wait = [s for s in L["pool_a_leaders"] if s["grade"] == "等待确认"][:5]
        if core:
            lines.append("【核心观察 80+】" + "；".join(
                f"{s['name']}({s['code']}){s['boards']}板{s['score']}分" for s in core))
        else:
            lines.append("【核心观察 80+】无（宁缺毋滥）")
        if wait:
            lines.append("【等待确认 65-79】" + "；".join(
                f"{s['name']}{s['boards']}板{s['score']}分" for s in wait))
    if M:
        idx = "｜".join(f"{q['name']}{q['chg_pct']:+.2f}%" for q in M["indices"].values())
        lines.append("【A股指数】" + idx)
        if M["boards_industry"]:
            b_in = M["boards_industry"][0]
            b_out = M["boards_industry"][-1]
            lines.append(f"【资金主攻】{b_in['name']} +{b_in['net_yi']}亿｜【资金撤退】{b_out['name']} {b_out['net_yi']}亿")
        if M["alerts"]:
            lines.append("【自选股异动】" + "；".join(f"{a['name']}{a['type']}" for a in M["alerts"][:5]))
    if P and P.get("positions"):
        sig = [p for p in P["positions"] if p.get("signals")]
        if sig:
            lines.append("【持仓信号】" + "；".join(
                f"{p['name']}盈亏{p['pnl_pct']}% [{'、'.join(p['signals'])}]" for p in sig))
        else:
            lines.append("【持仓信号】无触发，继续持有")
    if C and C.get("checks"):
        passed = [v["name"] for v in C["checks"].values() if v.get("confirm") == "通过"]
        failed = [v["name"] for v in C["checks"].values() if v.get("confirm") == "淘汰"]
        if passed or failed:
            lines.append(f"【次日确认】通过：{'、'.join(passed) or '无'}｜淘汰：{'、'.join(failed) or '无'}")
    if U:
        top = U["sectors"][0] if U["sectors"] else None
        if top:
            lines.append(f"【美股{ {'premarket':'盘前','regular':'盘中','afterhours':'盘后'}.get(U['meta']['us_market_state'],'') }】"
                         f"领涨板块：{top['name']} {top['chg_pct']:+.2f}%（量比{top['vol_ratio']}）")
    lines.append("")
    lines.append("完整看板：https://ashare-monitor-kyriepan17.streamlit.app/")
    lines.append("免责声明：本邮件基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。")
    return "\n".join(lines)


def main():
    user = os.environ.get("EMAIL_USER")
    pwd = os.environ.get("EMAIL_PASS")
    to = os.environ.get("EMAIL_TO") or user
    if not user or not pwd:
        print("SKIP: 未配置 EMAIL_USER/EMAIL_PASS，跳过邮件发送")
        return

    body = build_report()
    L = load("limit_up.json")
    senti = L["meta"]["sentiment"] if L else ""
    date_str = datetime.now(BJ).strftime("%Y-%m-%d")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(f"A股盯盘复盘 {date_str}｜{senti}", "utf-8")
    msg["From"] = user
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())
    print(f"OK 复盘邮件已发送至 {to}")


if __name__ == "__main__":
    main()
