# -*- coding: utf-8 -*-
"""
涨停池扫描 v3 —— 小资金高弹性框架（含连板股前5日分数轨迹）
路径：市场情绪 → 主线板块 → 三池分类 → 角色识别 → 五维评分
新增：拉最近7个交易日涨停池（本地缓存），A池连板股按当时数据回溯前5日评分
输出：data/limit_up.json + data/limit_up.js
"""
import os, json, re, argparse
from datetime import datetime, timezone, timedelta

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests

BJ = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
CACHE_DIR = os.path.join(DATA_DIR, "pool_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://data.10jqka.com.cn/",
}
ZT_URL = ("https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
          "?page=1&limit=200&field=199112,10,9001,330323,330324,330325,9002,"
          "330329,133971,133970,1968584,3475914,9003,9004"
          "&filter=HS,GEM2STAR&date={date}&order_field=330324&order_type=0")

HIST_DAYS = 5  # 连板股回溯天数


def http_get(url, headers=None, timeout=15):
    s = requests.Session()
    s.trust_env = False
    return s.get(url, headers=headers or THS_HEADERS, timeout=timeout)


def recent_trade_dates(n=8):
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           "param=sh000001,day,,,15,qfq")
    d = http_get(url).json()
    node = d["data"]["sh000001"]
    rows = node.get("qfqday") or node.get("day")
    return [r[0].replace("-", "") for r in rows[-n:][::-1]]


def parse_boards(high_days):
    if high_days == "首板":
        return 1, 1
    m = re.match(r"(\d+)天(\d+)板", high_days or "")
    return (int(m.group(1)), int(m.group(2))) if m else (1, 1)


def fetch_pool(date):
    """带本地缓存的涨停池拉取"""
    cache = os.path.join(CACHE_DIR, f"{date}.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            d = json.load(f)
    else:
        d = http_get(ZT_URL.format(date=date)).json()
        if d.get("status_code") != 0:
            raise RuntimeError(f"涨停池接口异常({date}): {d.get('status_msg')}")
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    return d["data"]["info"], d["data"]["page"]["total"]


def kline_features(code, end_date=None):
    """日K特征。end_date=YYYYMMDD 时取截至该日的数据（历史评分用）"""
    prefix = ("sh" if code.startswith(("6", "9")) else "sz") + code
    if end_date:
        end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
        param = f"{prefix},day,,{end},65,qfq"
    else:
        param = f"{prefix},day,,,65,qfq"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
    try:
        d = http_get(url).json()
        node = d["data"][prefix]
        rows = node.get("qfqday") or node.get("day") or []
        if len(rows) < 25:
            return None
        closes = [float(r[2]) for r in rows]
        highs = [float(r[3]) for r in rows]
        last = closes[-1]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
        ma20_prev = sum(closes[-21:-1]) / 20
        hi60 = max(highs[-60:]) if len(highs) >= 60 else max(highs)
        prev20 = round((last - closes[-21]) / closes[-21] * 100, 2)
        return {
            "ma20_up": ma20 > ma20_prev,
            "ma_bull": ma60 is not None and ma20 > ma60,
            "new_high_60d": last >= hi60 * 0.999,
            "dist_high_pct": round((hi60 - last) / hi60 * 100, 2),
            "prev20d_chg": prev20,
        }
    except Exception:
        return None


def build_day_context(date, prev_ctx):
    """构建某交易日的完整上下文：解析池、题材统计、情绪、环境分"""
    pool, total = fetch_pool(date)
    theme_counter = {}
    parsed = []
    for x in pool:
        days, bn = parse_boards(x.get("high_days"))
        themes = [t.strip() for t in re.split(r"[+＋]", x.get("reason_type", "")) if t.strip()]
        for t in themes:
            theme_counter[t] = theme_counter.get(t, 0) + 1
        parsed.append((x, days, bn, themes))
    theme_max_cnt = max(theme_counter.values()) if theme_counter else 1
    hot_themes = {t for t, c in theme_counter.items() if c >= 3}
    theme_max_boards = {}
    for x, days, bn, themes in parsed:
        for t in themes:
            theme_max_boards[t] = max(theme_max_boards.get(t, 0), bn)

    cur_boards = {x["code"]: bn for x, _, bn, _ in parsed}
    promo_rate = None
    if prev_ctx:
        ok = tot2 = 0
        for code, pb in prev_ctx["boards_map"].items():
            if pb >= 2:
                tot2 += 1
                if cur_boards.get(code, 0) > pb:
                    ok += 1
        promo_rate = round(ok / tot2 * 100, 1) if tot2 else None

    opened = sum(1 for x, _, _, _ in parsed if (x.get("open_num") or 0) > 0)
    open_ratio = round(opened / total * 100, 1) if total else 0
    max_board = max((bn for _, _, bn, _ in parsed), default=0)
    prev_total = prev_ctx["total"] if prev_ctx else None
    prev_max = prev_ctx["max_board"] if prev_ctx else 0

    # 情绪三态
    if prev_total is None:
        sentiment, note = "未知", "前一交易日数据缺失"
    elif (total < prev_total * 0.8) or open_ratio > 45 or (prev_max >= 3 and max_board < prev_max - 1):
        sentiment = "退潮期"
        note = f"涨停{prev_total}→{total}，开板率{open_ratio}%，空间板{prev_max}→{max_board}"
    elif total >= prev_total and (promo_rate is None or promo_rate >= 40) and open_ratio <= 30:
        sentiment = "进攻期"
        note = f"涨停{prev_total}→{total}，晋级率{promo_rate}%，开板率{open_ratio}%"
    else:
        sentiment = "分歧期"
        note = f"涨停{prev_total}→{total}，晋级率{promo_rate}%，开板率{open_ratio}%"
    env_score = {"进攻期": 10, "分歧期": 6, "退潮期": 3}.get(sentiment, 5)

    return {
        "date": date, "parsed": parsed, "total": total,
        "theme_counter": theme_counter, "theme_max_cnt": theme_max_cnt,
        "hot_themes": hot_themes, "theme_max_boards": theme_max_boards,
        "boards_map": cur_boards, "promo_rate": promo_rate,
        "open_ratio": open_ratio, "max_board": max_board,
        "prev_total": prev_total, "prev_max_board": prev_max,
        "sentiment": sentiment, "sentiment_note": note, "env_score": env_score,
    }


def identify_role(rec, theme_max_boards):
    if rec["boards"] >= 2 and rec["_themes"] and \
            rec["boards"] >= theme_max_boards.get(rec["_themes"][0], 0):
        return "龙头"
    if rec["float_cap_yi"] >= 100 and rec["boards"] == 1:
        return "中军"
    if rec["boards"] == 1 and rec["theme_hot"]:
        return "补涨"
    return "跟风"


def score_stock(x, days, bn, themes, ctx, with_kline=True):
    """对单条涨停记录按当时上下文打五维分。返回完整 rec。"""
    rec = {
        "code": x["code"], "name": x["name"], "days": days, "boards": bn,
        "reason": x.get("reason_type", ""), "_themes": themes,
        "chg_pct": round(x.get("change_rate") or 0, 2),
        "turnover_pct": round(x.get("turnover_rate") or 0, 2),
        "seal_yi": round((x.get("order_amount") or 0) / 1e8, 2),
        "float_cap_yi": round((x.get("currency_value") or 0) / 1e8, 1),
        "open_num": x.get("open_num") or 0,
        "lut_type": x.get("limit_up_type", ""),
        "close": round(float(x.get("latest") or 0), 2),
        "first_seal": datetime.fromtimestamp(int(x["first_limit_up_time"]), BJ).strftime("%H:%M")
                      if x.get("first_limit_up_time") else "15:00",
        "theme_cnt": max((ctx["theme_counter"].get(t, 0) for t in themes), default=0),
        "theme_hot": bool(set(themes) & ctx["hot_themes"]),
    }
    rec["kline"] = kline_features(x["code"], ctx["date"]) if with_kline else None
    rec["role"] = identify_role(rec, ctx["theme_max_boards"])

    if "一字" in rec["lut_type"]:
        rec.update({"score": 0, "grade": "忽略",
                    "breakdown": {"note": "一字板"}, "risks": ["一字板，无买点"]})
        return rec

    d, risks = {}, []
    # 1 板块环境 /25
    theme_score = round(rec["theme_cnt"] / ctx["theme_max_cnt"] * 15) if ctx["theme_max_cnt"] else 0
    d["板块环境"] = min(theme_score + ctx["env_score"], 25)
    if not rec["theme_hot"]:
        risks.append("题材非当日主线")
    # 2 辨识度角色 /25
    base = {1: 8, 2: 13, 3: 17, 4: 20, 5: 22}.get(bn, 25)
    if rec["role"] == "龙头":
        base = min(base + 3, 25)
    elif rec["role"] == "中军":
        base = min(base + 2, 25)
    d["辨识度角色"] = base
    # 3 趋势突破 /20
    k = rec.get("kline") or {}
    t = 0
    if k.get("new_high_60d"):
        t += 8
    elif k.get("dist_high_pct") is not None and k["dist_high_pct"] <= 5:
        t += 5
    if k.get("ma_bull"):
        t += 6
    elif k.get("ma20_up"):
        t += 3
    if k.get("prev20d_chg") is not None and k["prev20d_chg"] <= -10 and bn == 1:
        t += 6
    d["趋势突破"] = min(t, 20)
    # 4 封板结构 /20
    s = 0
    sr = rec["seal_yi"] / rec["float_cap_yi"] * 100 if rec["float_cap_yi"] else 0
    if sr >= 3:
        s += 8
    elif sr >= 1:
        s += 5
    else:
        s += 2
        if bn >= 2:
            risks.append("封单偏弱")
    if rec["open_num"] == 0:
        s += 6
    elif rec["open_num"] <= 2:
        s += 4
        risks.append(f"开板{rec['open_num']}次后回封")
    else:
        s += 1
        risks.append(f"开板{rec['open_num']}次，分歧大")
    if rec["first_seal"] <= "10:00":
        s += 4
    elif rec["first_seal"] <= "11:30":
        s += 2
    elif rec["first_seal"] >= "14:30":
        risks.append("尾盘封板")
    if 5 <= rec["turnover_pct"] <= 20:
        s += 2
    d["封板结构"] = min(s, 20)
    # 5 风险可交易 /10
    r = 5
    cv = rec["float_cap_yi"]
    r += 5 if cv < 15 else 3 if cv < 50 else 2 if cv < 100 else 1
    if k.get("prev20d_chg") is not None and k["prev20d_chg"] >= 20 and bn == 1:
        r -= 5
        risks.append(f"高位首板(20日+{k['prev20d_chg']}%)")
    d["风险可交易"] = max(r, 0)

    total = sum(v for v in d.values())
    grade = ("核心观察" if total >= 80 else "等待确认" if total >= 65
             else "后排跟踪" if total >= 50 else "忽略")
    rec.update({"score": total, "grade": grade, "breakdown": d, "risks": risks})
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    args = ap.parse_args()

    dates = recent_trade_dates(HIST_DAYS + 3)   # 当日 + 前5日 + 最早那日的前一日
    date = args.date or dates[0]

    # 逐日构建上下文（新→旧，需前一日算晋级率，所以从旧到新处理）
    use_dates = dates[:HIST_DAYS + 2][::-1]     # 旧→新
    ctxs = {}
    prev_ctx = None
    for d in use_dates:
        try:
            ctx = build_day_context(d, prev_ctx)
            ctxs[d] = ctx
            prev_ctx = ctx
        except Exception as e:
            print(f"WARN: {d} 涨停池获取失败: {e}")

    if date not in ctxs:
        ctx = build_day_context(date, prev_ctx)
        ctxs[date] = ctx
    ctx = ctxs[date]

    # 当日全量评分
    stocks = []
    for x, days, bn, themes in ctx["parsed"]:
        rec = score_stock(x, days, bn, themes, ctx)
        rec["promoted"] = (bn > ctxs.get(ctxs_keys_prev(ctxs, date), {}).get("boards_map", {}).get(x["code"], 0)) if False else None
        stocks.append(rec)
    # 晋级标记
    prev_date = ctx.get("prev_date")

    # 三池
    pool_a = sorted([s for s in stocks if s["boards"] >= 2 and s["score"] > 0],
                    key=lambda s: (s["boards"], s["score"]), reverse=True)
    pool_b = sorted([s for s in stocks if s["boards"] == 1 and s["score"] >= 50],
                    key=lambda s: s["score"], reverse=True)
    pool_c = sorted([s for s in stocks if s["open_num"] > 0 and s["score"] > 0],
                    key=lambda s: s["score"], reverse=True)

    # A 池前5日分数轨迹（按当时数据重算）
    hist_dates = [d for d in use_dates if d < date][-HIST_DAYS:]
    for s in pool_a:
        traj = []
        for hd in hist_dates:
            hctx = ctxs.get(hd)
            if not hctx:
                continue
            hit = None
            for x, days2, bn2, themes2 in hctx["parsed"]:
                if x["code"] == s["code"]:
                    hit = (x, days2, bn2, themes2)
                    break
            if hit:
                hr = score_stock(hit[0], hit[1], hit[2], hit[3], hctx)
                traj.append({"date": hd, "score": hr["score"], "grade": hr["grade"],
                             "boards": hr["boards"], "in_pool": True})
            else:
                traj.append({"date": hd, "in_pool": False})
        s["score_history"] = traj
    # 晋级标记（今日板数 > 昨日板数）
    older = [d for d in ctxs if d < date]
    if older:
        yctx = ctxs[max(older)]
        for s in stocks:
            pb = yctx["boards_map"].get(s["code"], 0)
            s["promoted"] = s["boards"] > pb if pb else None

    ladder = {}
    for s in stocks:
        ladder.setdefault(str(s["boards"]), []).append(s)
    for k in ladder:
        ladder[k].sort(key=lambda s: s["score"], reverse=True)

    payload = {
        "meta": {
            "trade_date": date,
            "prev_date": max([d for d in ctxs if d < date], default=None),
            "generated_at": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
            "total_limit_up": ctx["total"], "prev_total": ctx["prev_total"],
            "open_ratio_pct": ctx["open_ratio"], "promo_rate_pct": ctx["promo_rate"],
            "max_board": ctx["max_board"], "prev_max_board": ctx["prev_max_board"],
            "sentiment": ctx["sentiment"], "sentiment_note": ctx["sentiment_note"],
            "themes_top": sorted(ctx["theme_counter"].items(), key=lambda kv: kv[1], reverse=True)[:12],
            "hist_days": HIST_DAYS,
        },
        "pool_a_leaders": pool_a, "pool_b_starters": pool_b, "pool_c_repair": pool_c,
        "ladder": {k: v for k, v in sorted(ladder.items(), key=lambda kv: int(kv[0]), reverse=True)},
    }
    for s in stocks:
        s.pop("_themes", None)

    with open(os.path.join(DATA_DIR, "limit_up.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    with open(os.path.join(DATA_DIR, "limit_up.js"), "w", encoding="utf-8") as f:
        f.write("window.LIMIT_UP_DATA = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";")

    # ---- P0: 推荐落盘（效果追踪数据源） ----
    rec_dir = os.path.join(DATA_DIR, "rec_history")
    os.makedirs(rec_dir, exist_ok=True)
    recs = [{"code": s["code"], "name": s["name"], "score": s["score"],
             "grade": s["grade"], "boards": s["boards"], "role": s["role"],
             "close": s["close"], "reason": s["reason"]}
            for s in stocks if s["grade"] in ("核心观察", "等待确认")]
    with open(os.path.join(rec_dir, f"{date}.json"), "w", encoding="utf-8") as f:
        json.dump({"date": date, "sentiment": ctx["sentiment"], "recs": recs}, f, ensure_ascii=False)

    # ---- P2: 自动入池清单 ----
    auto = [{"code": s["code"], "name": s["name"], "score": s["score"],
             "grade": s["grade"], "boards": s["boards"], "date": date}
            for s in stocks if s["score"] >= 65]
    with open(os.path.join(DATA_DIR, "auto_watch.json"), "w", encoding="utf-8") as f:
        json.dump({"date": date, "stocks": auto}, f, ensure_ascii=False)

    m = payload["meta"]
    print(f"OK {date} 涨停{m['total_limit_up']} 最高{m['max_board']}板 "
          f"开板率{m['open_ratio_pct']}% 晋级率{m['promo_rate_pct']}% 情绪:{m['sentiment']}")
    print(f"三池: 龙头{len(pool_a)} 启动{len(pool_b)} 修复{len(pool_c)}")
    print("--- A池连板股（含前5日轨迹） ---")
    for s in pool_a[:10]:
        traj = " ".join(f"{t['date'][4:]}:{'未涨停' if not t['in_pool'] else str(t['score'])+'分'}"
                        for t in s.get("score_history", []))
        print(f"  {s['boards']}板 {s['name']}({s['code']}) {s['score']}分 {s['grade']} | {traj}")
    top80 = [x for x in stocks if x["grade"] == "核心观察"]
    if top80:
        print("--- 核心观察(80+) ---")
        for s in top80:
            print(f"  {s['score']}分 {s['name']}({s['code']}) {s['boards']}板 {s['role']}")


def ctxs_keys_prev(ctxs, date):
    older = [d for d in ctxs if d < date]
    return max(older) if older else None


if __name__ == "__main__":
    main()
