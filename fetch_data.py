# -*- coding: utf-8 -*-
"""
A股盯盘助手 - 数据拉取 + 异动检测 + 板块评测
数据源：新浪财经 MoneyFlow（板块/个股资金流）、腾讯财经（行情/板块排名/日K）
输出：data/latest.json, data/data.js（看板用）, data/history/<时间戳>.json
用法：python fetch_data.py [--force]  （非交易时段默认跳过，--force 强制拉取）
"""
import os, sys, json, argparse
from datetime import datetime, timezone, timedelta

# --- 本机系统代理会拦截部分财经接口，全部直连 ---
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests

BJ = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
HIST_DIR = os.path.join(DATA_DIR, "history")
os.makedirs(HIST_DIR, exist_ok=True)

SINA_REFERER = {"Referer": "https://finance.sina.com.cn"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}

META = {"sources": {}, "errors": []}


def http_get(url, headers=None, timeout=12, encoding=None):
    s = requests.Session()
    s.trust_env = False
    h = dict(UA)
    if headers:
        h.update(headers)
    r = s.get(url, headers=h, timeout=timeout)
    if encoding:
        r.encoding = encoding
    elif r.apparent_encoding:
        r.encoding = r.apparent_encoding
    return r


def now_bj():
    return datetime.now(BJ)


def trading_status(now):
    """返回 (状态, 已交易分钟数)。状态: pre / trading / lunch / closed / weekend"""
    if now.weekday() >= 5:
        return "weekend", 240
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 30:
        return "pre", 0
    if hm <= 11 * 60 + 30:
        return "trading", hm - (9 * 60 + 30)
    if hm < 13 * 60:
        return "lunch", 120
    if hm <= 15 * 60:
        return "trading", 120 + hm - 13 * 60
    return "closed", 240


# ---------- 数据源 ----------

def fetch_sina_board_flow(fenlei, top_n=100):
    """新浪板块资金流。fenlei=0 行业, 1 概念。双向各取 top_n 合并去重。"""
    merged = {}
    for asc in (0, 1):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"MoneyFlow.ssl_bkzj_bk?page=1&num={top_n}&sort=netamount&asc={asc}&fenlei={fenlei}")
        arr = http_get(url, headers=SINA_REFERER).json()
        for b in arr:
            merged[b["category"]] = {
                "name": b["name"],
                "chg_pct": round(float(b["avg_changeratio"]) * 100, 2),
                "turnover_wan": round(float(b["turnover"]), 1),
                "inflow_yi": round(float(b["inamount"]) / 1e8, 2),
                "outflow_yi": round(float(b["outamount"]) / 1e8, 2),
                "net_yi": round(float(b["netamount"]) / 1e8, 2),
                "net_ratio": round(float(b["ratioamount"]) * 100, 2),
                "lead_stock": b["ts_name"],
                "lead_code": b["ts_symbol"],
                "lead_chg_pct": round(float(b["ts_changeratio"]) * 100, 2),
            }
    META["sources"][f"sina_board_flow_{fenlei}"] = f"{len(merged)} 板块"
    return list(merged.values())


def fetch_tencent_board_rank():
    """腾讯行业板块涨幅榜（含5日/20日涨跌幅）"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/mktHs/rank?l=60&p=1&t=01/averatio&o=0")
    d = http_get(url).json()
    out = []
    for b in d.get("data", []):
        out.append({
            "name": b["bd_name"],
            "chg_pct": float(b["bd_zdf"]),
            "chg_5d": float(b.get("bd_zdf5", 0) or 0),
            "chg_20d": float(b.get("bd_zdf20", 0) or 0),
            "lead_stock": b.get("nzg_name", ""),
            "lead_chg_pct": float(b.get("nzg_zdf", 0) or 0),
        })
    META["sources"]["tencent_board_rank"] = f"{len(out)} 板块"
    return out


def fetch_quotes(codes):
    """腾讯批量行情。返回 {code: {...}}"""
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    text = http_get(url, encoding="gbk").text
    out = {}
    for line in text.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        var, payload = line.split("=", 1)
        code = var.strip().lstrip("v_")
        f = payload.strip().strip('"').split("~")
        if len(f) < 40:
            continue
        def fl(i):
            try:
                return float(f[i])
            except (ValueError, IndexError):
                return 0.0
        out[code] = {
            "code": code, "name": f[1],
            "price": fl(3), "prev_close": fl(4), "open": fl(5),
            "volume_hand": fl(6), "amount_wan": fl(37),
            "chg": fl(31), "chg_pct": fl(32),
            "high": fl(33), "low": fl(34),
            "turnover_pct": fl(38),
            "time": f[30] if len(f) > 30 else "",
        }
    META["sources"]["tencent_quotes"] = f"{len(out)} 只"
    return out


def fetch_avg_volume_5d(code):
    """腾讯日K，取最近5个完整交易日均量（手）"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,9,qfq")
    d = http_get(url).json()
    node = d.get("data", {}).get(code, {})
    rows = node.get("qfqday") or node.get("day") or []
    today = now_bj().strftime("%Y-%m-%d")
    vols = [float(r[5]) for r in rows if r[0] != today][-5:]
    if not vols:
        vols = [float(r[5]) for r in rows][-5:]
    return sum(vols) / len(vols) if vols else 0.0


def fetch_stock_fund_flow(code):
    """个股资金流（新浪历史序列最新一条 = 当日累计，盘中实时更新）"""
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=1&sort=opendate&asc=0&daima={code}")
    arr = http_get(url, headers=SINA_REFERER).json()
    if not arr:
        return None
    r = arr[0]
    return {
        "date": r.get("opendate", ""),
        "main_net_wan": round(float(r.get("r0_net", 0)) / 1e4, 1),
        "main_ratio_pct": round(float(r.get("r0_ratio", 0)) * 100, 2),
    }


# ---------- 板块评测 ----------

def percentile(sorted_vals, v):
    """v 在 sorted_vals（升序）中的分位 0~1"""
    n = len(sorted_vals)
    if n == 0:
        return 0.5
    cnt = sum(1 for x in sorted_vals if x < v)
    return cnt / n


def score_boards(boards, hist_persist):
    """量化评分 0-100：资金规模30 + 占比20 + 涨幅15 + 领涨股10 + 连续性25"""
    nets = sorted(b["net_yi"] for b in boards)
    ratios = sorted(b["net_ratio"] for b in boards)
    chgs = sorted(b["chg_pct"] for b in boards)
    leads = sorted(b["lead_chg_pct"] for b in boards)
    for b in boards:
        s = (percentile(nets, b["net_yi"]) * 30
             + percentile(ratios, b["net_ratio"]) * 20
             + percentile(chgs, b["chg_pct"]) * 15
             + percentile(leads, b["lead_chg_pct"]) * 10
             + hist_persist.get(b["name"], 0.5) * 25)
        b["score"] = round(s, 1)
        reasons = []
        if b["net_yi"] > 0:
            reasons.append(f"主力净流入 {b['net_yi']} 亿")
        else:
            reasons.append(f"主力净流出 {abs(b['net_yi'])} 亿")
        if abs(b["chg_pct"]) >= 1:
            reasons.append(f"板块{'涨' if b['chg_pct'] > 0 else '跌'} {abs(b['chg_pct'])}%")
        hp = hist_persist.get(b["name"])
        if hp is not None:
            reasons.append(f"近期资金连续性 {round(hp * 100)}%")
        reasons.append(f"领涨 {b['lead_stock']} {b['lead_chg_pct']}%")
        b["reason"] = "；".join(reasons)
    boards.sort(key=lambda x: x["score"], reverse=True)
    return boards


def load_hist_persist():
    """近20次刷新中各板块净流入为正的比例（连续性指标）。无历史时返回空dict。"""
    files = sorted(os.listdir(HIST_DIR))[-20:]
    pos, tot = {}, {}
    for fn in files:
        try:
            with open(os.path.join(HIST_DIR, fn), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        for b in d.get("boards_industry", []) + d.get("boards_concept", []):
            tot[b["name"]] = tot.get(b["name"], 0) + 1
            if b.get("net_yi", 0) > 0:
                pos[b["name"]] = pos.get(b["name"], 0) + 1
    return {k: pos.get(k, 0) / t for k, t in tot.items() if t >= 3}


# ---------- 自选股异动 ----------

def check_watchlist(watch, quotes, cfg, elapsed_min, status):
    stocks, alerts = [], []
    th = cfg["alert"]
    for w in watch:
        code, name = w["code"], w["name"]
        q = quotes.get(code)
        if not q:
            continue
        avg5 = 0.0
        try:
            avg5 = fetch_avg_volume_5d(code)
        except Exception as e:
            META["errors"].append(f"{name} 均量获取失败: {type(e).__name__}")
        flow = None
        try:
            flow = fetch_stock_fund_flow(code)
        except Exception as e:
            META["errors"].append(f"{name} 资金流获取失败: {type(e).__name__}")

        # 放量判断：盘中按时间进度折算预计全日量
        if status == "trading" and elapsed_min > 15:
            proj_vol = q["volume_hand"] / (elapsed_min / 240)
        else:
            proj_vol = q["volume_hand"]
        vol_ratio = round(proj_vol / avg5, 2) if avg5 else None

        rec = {**q, "avg5_vol_hand": round(avg5, 0), "vol_ratio": vol_ratio,
               "fund_flow": flow}
        stock_alerts = []
        if abs(q["chg_pct"]) >= th["chg_pct_abs"]:
            stock_alerts.append(f"涨跌幅 {q['chg_pct']}% 超 ±{th['chg_pct_abs']}%")
        if vol_ratio is not None and vol_ratio >= th["volume_ratio"] and q["volume_hand"] > 0:
            stock_alerts.append(f"放量 {vol_ratio} 倍于5日均量")
        if flow and flow["main_net_wan"] * 1e4 >= th["main_inflow_yuan"]:
            stock_alerts.append(f"主力净流入 {round(flow['main_net_wan'] / 100, 2)} 亿")
        rec["alerts"] = stock_alerts
        for a in stock_alerts:
            alerts.append({"code": code, "name": name, "type": a,
                           "chg_pct": q["chg_pct"], "price": q["price"]})
        stocks.append(rec)
    META["sources"]["watchlist"] = f"{len(stocks)} 只"
    return stocks, alerts


# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="非交易时段也强制拉取")
    args = ap.parse_args()

    now = now_bj()
    status, elapsed = trading_status(now)
    if status != "trading" and not args.force:
        print(f"SKIP: 当前 {now.strftime('%H:%M')} 非盘中时段（{status}），未拉取。--force 可强制。")
        return

    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    with open(os.path.join(BASE, "watchlist.json"), encoding="utf-8") as f:
        watch = json.load(f)["stocks"]

    boards_ind, boards_con, rank = [], [], []
    try:
        boards_ind = fetch_sina_board_flow(0)
    except Exception as e:
        META["errors"].append(f"行业板块资金流失败: {type(e).__name__} {e}")
    try:
        boards_con = fetch_sina_board_flow(1)
    except Exception as e:
        META["errors"].append(f"概念板块资金流失败: {type(e).__name__} {e}")
    try:
        rank = fetch_tencent_board_rank()
    except Exception as e:
        META["errors"].append(f"腾讯板块榜失败: {type(e).__name__} {e}")

    codes = [w["code"] for w in watch] + ["sh000001", "sz399001", "sz399006"]
    try:
        quotes = fetch_quotes(codes)
    except Exception as e:
        META["errors"].append(f"行情失败: {type(e).__name__} {e}")
        quotes = {}

    indices = {c: quotes.get(c) for c in ("sh000001", "sz399001", "sz399006") if quotes.get(c)}
    stocks, alerts = check_watchlist(watch, quotes, cfg, elapsed, status)

    hist_persist = load_hist_persist()
    boards_ind = score_boards(boards_ind, hist_persist)
    boards_con = score_boards(boards_con, hist_persist)

    payload = {
        "meta": {
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "trading_status": status,
            "sources": META["sources"],
            "errors": META["errors"],
            "history_samples": len(sorted(os.listdir(HIST_DIR))),
        },
        "indices": indices,
        "boards_industry": boards_ind,
        "boards_concept": boards_con,
        "board_rank_tencent": rank,
        "watchlist": stocks,
        "alerts": alerts,
        "config": cfg,
    }

    ts = now.strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(HIST_DIR, f"{ts}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    with open(os.path.join(DATA_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    with open(os.path.join(DATA_DIR, "data.js"), "w", encoding="utf-8") as f:
        f.write("window.MONITOR_DATA = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";")

    print(f"OK {payload['meta']['generated_at']} status={status} "
          f"行业榜{len(boards_ind)} 概念榜{len(boards_con)} 自选股{len(stocks)} 告警{len(alerts)}")
    if boards_ind:
        top_in = ", ".join(f"{b['name']}+{b['net_yi']}亿" for b in boards_ind[:3])
        top_out = ", ".join(f"{b['name']}{b['net_yi']}亿" for b in boards_ind[-3:])
        print(f"行业净流入TOP3: {top_in} | 净流出TOP3: {top_out}")
    for a in alerts:
        print(f"[ALERT] {a['name']}({a['code']}) {a['type']} 现价{a['price']}")
    if META["errors"]:
        print("WARN: " + " | ".join(META["errors"]))


if __name__ == "__main__":
    main()
