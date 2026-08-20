# -*- coding: utf-8 -*-
"""
P0 推荐效果追踪器
读取 data/rec_history/*.json（每日65+推荐清单），对到期的推荐回填 T+1/T+3/T+5 收益，
分档汇总胜率与平均收益 → data/tracker.json
收益口径：推荐日收盘 → 推荐日后第 N 个交易日收盘（未复权，短线口径可接受）
"""
import os, json, glob
from datetime import datetime, timezone, timedelta

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests

BJ = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
REC_DIR = os.path.join(DATA_DIR, "rec_history")
HORIZONS = (1, 3, 5)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def trade_days():
    """最近30个交易日（YYYYMMDD，旧→新）"""
    s = requests.Session(); s.trust_env = False
    r = s.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,40,qfq",
              headers=UA, timeout=15)
    d = r.json()
    node = d["data"]["sh000001"]
    rows = node.get("qfqday") or node.get("day")
    return [x[0].replace("-", "") for x in rows[-30:]]


def close_on(code, date):
    """某股某日收盘价（日K截断，无未来函数）"""
    prefix = ("sh" if code.startswith(("6", "9")) else "sz") + code
    end = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    s = requests.Session(); s.trust_env = False
    r = s.get(f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix},day,,{end},10,qfq",
              headers=UA, timeout=15)
    d = r.json()
    node = d["data"].get(prefix, {})
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        return None
    return float(rows[-1][2])


def main():
    days = trade_days()
    day_idx = {d: i for i, d in enumerate(days)}
    today = days[-1]

    details = []
    for fp in sorted(glob.glob(os.path.join(REC_DIR, "*.json"))):
        rec_date = os.path.basename(fp)[:8]
        if rec_date not in day_idx:
            continue
        with open(fp, encoding="utf-8") as f:
            pack = json.load(f)
        for rec in pack.get("recs", []):
            if not rec.get("close"):
                continue
            item = dict(rec)
            item["date"] = rec_date
            item["sentiment"] = pack.get("sentiment", "")
            for n in HORIZONS:
                key = f"ret_t{n}"
                ti = day_idx[rec_date] + n
                if ti < len(days):
                    c = close_on(rec["code"], days[ti])
                    item[key] = round((c - rec["close"]) / rec["close"] * 100, 2) if c else None
                else:
                    item[key] = None  # 未到期
            details.append(item)

    # 分档汇总
    def summarize(rows):
        out = {}
        for n in HORIZONS:
            vals = [r[f"ret_t{n}"] for r in rows if r.get(f"ret_t{n}") is not None]
            if vals:
                out[f"t{n}"] = {"n": len(vals), "win_rate": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
                                "avg_ret": round(sum(vals) / len(vals), 2),
                                "max": max(vals), "min": min(vals)}
            else:
                out[f"t{n}"] = {"n": 0}
        return out

    summary = {
        "all": summarize(details),
        "core": summarize([r for r in details if r["grade"] == "核心观察"]),
        "wait": summarize([r for r in details if r["grade"] == "等待确认"]),
    }

    payload = {
        "meta": {"generated_at": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
                 "latest_trade_day": today,
                 "total_recs": len(details),
                 "note": "收益口径：推荐日收盘→第N交易日收盘，未复权；推荐从 2026-08-20 起积累"},
        "summary": summary,
        "details": sorted(details, key=lambda x: x["date"], reverse=True)[:60],
    }
    with open(os.path.join(DATA_DIR, "tracker.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    print(f"OK 追踪 {len(details)} 条推荐")
    for g in ("core", "wait"):
        s = summary[g]
        if s["t1"]["n"]:
            print(f"  {g}: T+1 胜率{s['t1']['win_rate']}% 均{s['t1']['avg_ret']}% (n={s['t1']['n']})"
                  f" | T+3 胜率{s['t3'].get('win_rate','—')}% (n={s['t3']['n']})")


if __name__ == "__main__":
    main()
