# -*- coding: utf-8 -*-
"""
P1 持仓检查器 —— 按体系第五层卖出规则逐日检查持仓
规则：断板不修复 / 跌破MA5 / 高位放量长阴 / 成本止损止盈 / 仓位阶段提醒
输入：positions.json（用户维护，cost>0 视为真实持仓）
输出：data/positions_report.json
"""
import os, json
from datetime import datetime, timezone, timedelta

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests

BJ = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

STOP_LOSS = -5.0     # 体系分档止损（高潮-5/分歧-4，取中性）
TAKE_PROFIT_1 = 15.0
TAKE_PROFIT_2 = 25.0


def http_get(url, encoding=None):
    s = requests.Session(); s.trust_env = False
    r = s.get(url, headers=UA, timeout=15)
    r.encoding = encoding or r.apparent_encoding
    return r


def quote(code):
    r = http_get(f"https://qt.gtimg.cn/q={code}", encoding="gbk")
    f = r.text.split("=", 1)[1].strip().strip('";').split("~")
    def fl(i):
        try:
            return float(f[i])
        except (ValueError, IndexError):
            return 0.0
    return {"name": f[1], "price": fl(3), "prev_close": fl(4),
            "chg_pct": fl(32), "volume_hand": fl(6)}


def kline(code, n=25):
    r = http_get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq",
                 encoding="utf-8")
    d = r.json()
    node = d["data"].get(code, {})
    rows = node.get("qfqday") or node.get("day") or []
    return [[x[0], float(x[2]), float(x[5])] for x in rows]  # [date, close, vol]


def was_limit_up_yesterday(code, limit_up_codes):
    return code.replace("sh", "").replace("sz", "") in limit_up_codes


def main():
    with open(os.path.join(BASE, "positions.json"), encoding="utf-8") as f:
        positions = [p for p in json.load(f)["positions"] if p.get("cost", 0) > 0]

    # 当日涨停名单（断板检测用）
    lu_codes = set()
    lu_path = os.path.join(DATA_DIR, "limit_up.json")
    if os.path.exists(lu_path):
        with open(lu_path, encoding="utf-8") as f:
            lu = json.load(f)
        for lst in lu.get("ladder", {}).values():
            for s in lst:
                lu_codes.add(s["code"])

    report = []
    for p in positions:
        code = p["code"]
        try:
            q = quote(code)
            ks = kline(code)
        except Exception as e:
            report.append({"code": code, "name": p["name"], "error": str(e)})
            continue

        pnl = round((q["price"] - p["cost"]) / p["cost"] * 100, 2)
        closes = [k[1] for k in ks]
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
        vols = [k[2] for k in ks]
        avg5_vol = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else 0
        vol_ratio = round(q["volume_hand"] / avg5_vol, 2) if avg5_vol else None

        signals = []
        level = "hold"  # hold / watch / reduce / exit

        # 1 断板不修复（连板持仓今天没涨停且跌）
        code6 = code.replace("sh", "").replace("sz", "")
        if p.get("boards_at_buy", 0) >= 2 and code6 not in lu_codes and q["chg_pct"] < -3:
            signals.append(f"断板且跌 {q['chg_pct']}%，未见修复——按体系减/退")
            level = "exit"
        # 2 跌破 MA5
        if ma5 and q["price"] < ma5 * 0.99:
            signals.append(f"跌破 MA5（{ma5:.2f}）——趋势警戒")
            level = max(level, "reduce", key=["hold","watch","reduce","exit"].index)
        # 3 高位放量长阴
        if q["chg_pct"] <= -5 and vol_ratio and vol_ratio >= 2:
            signals.append(f"放量长阴（{q['chg_pct']}%，量比{vol_ratio}）——出货信号")
            level = "exit"
        # 4 止损止盈
        if pnl <= STOP_LOSS:
            signals.append(f"触及止损线（{pnl}% ≤ {STOP_LOSS}%）")
            level = "exit"
        elif pnl >= TAKE_PROFIT_2:
            signals.append(f"浮盈 {pnl}% ≥ {TAKE_PROFIT_2}%——可分批兑现")
            level = max(level, "watch", key=["hold","watch","reduce","exit"].index)
        elif pnl >= TAKE_PROFIT_1:
            signals.append(f"浮盈 {pnl}% ≥ {TAKE_PROFIT_1}%——进入止盈观察")

        stage_txt = {1: "试错仓(30%)", 2: "确认仓(70%)", 3: "主升仓(100%)"}.get(p.get("stage", 1), "?")
        report.append({
            "code": code, "name": p["name"], "cost": p["cost"], "price": q["price"],
            "pnl_pct": pnl, "stage": stage_txt, "level": level,
            "signals": signals, "chg_pct": q["chg_pct"],
            "ma5": round(ma5, 2) if ma5 else None, "vol_ratio": vol_ratio,
        })

    payload = {
        "meta": {"generated_at": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
                 "positions_count": len(report),
                 "rule_note": "卖出规则：断板不修复 / 跌破MA5 / 放量长阴 / 止损-5% / 止盈15%+"},
        "positions": report,
    }
    with open(os.path.join(DATA_DIR, "positions_report.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    print(f"OK 持仓检查 {len(report)} 只")
    for r in report:
        if r.get("signals"):
            print(f"  [{r['level'].upper()}] {r['name']}({r['code']}) 盈亏{r['pnl_pct']}% | {'；'.join(r['signals'])}")
    if not any(r.get("signals") for r in report):
        print("  无持仓信号（或未配置真实持仓 positions.json）")


if __name__ == "__main__":
    main()
