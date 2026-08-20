# -*- coding: utf-8 -*-
"""
P3 次日确认检查器 —— 体系第四层"次日确认才进"的自动化
对昨日推荐（65+）股票做两阶段检查：
  --phase auction  9:26 集合竞价后：竞价幅度 vs 昨收（不明显低于预期 = >-2%）
  --phase morning  10:05 开盘后：现价 vs 今开（分歧收回）、现价 vs 昨收（转强）
输出：data/confirm.json（合并两阶段结果）
"""
import os, sys, json, glob, argparse
from datetime import datetime, timezone, timedelta

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests

BJ = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def quote_batch(codes):
    s = requests.Session(); s.trust_env = False
    r = s.get("https://qt.gtimg.cn/q=" + ",".join(codes), headers=UA, timeout=15)
    r.encoding = "gbk"
    out = {}
    for line in r.text.strip().split(";"):
        if "=" not in line:
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
        out[code] = {"name": f[1], "price": fl(3), "prev_close": fl(4),
                     "open": fl(5), "chg_pct": fl(32)}
    return out


def latest_rec_pack():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "rec_history", "*.json")))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["auction", "morning"], required=True)
    args = ap.parse_args()

    pack = latest_rec_pack()
    if not pack or not pack.get("recs"):
        print("SKIP: 无历史推荐记录")
        return

    codes = []
    for r in pack["recs"]:
        c = r["code"]
        codes.append(("sh" if c.startswith(("6", "9")) else "sz") + c)
    quotes = quote_batch(codes)

    result_path = os.path.join(DATA_DIR, "confirm.json")
    result = {"date": pack["date"], "rec_date_source": pack["date"], "checks": {}}
    if os.path.exists(result_path):
        try:
            with open(result_path, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("rec_date_source") == pack["date"]:
                result = old
        except Exception:
            pass

    for r in pack["recs"]:
        code = ("sh" if r["code"].startswith(("6", "9")) else "sz") + r["code"]
        q = quotes.get(code)
        if not q or not q["price"]:
            continue
        entry = result["checks"].setdefault(r["code"], {
            "name": r["name"], "score": r["score"], "grade": r["grade"],
            "boards": r["boards"], "rec_close": r["close"]})

        if args.phase == "auction":
            auc_pct = round((q["price"] - r["close"]) / r["close"] * 100, 2) if r["close"] else 0
            entry["auction_pct"] = auc_pct
            entry["auction_ok"] = auc_pct > -2.0   # 竞价不明显低于预期
        else:  # morning
            entry["open_pct"] = round((q["open"] - r["close"]) / r["close"] * 100, 2) if r["close"] else 0
            entry["now_pct"] = q["chg_pct"]
            entry["reclaim"] = q["price"] >= q["open"]           # 分歧后收回开盘
            entry["strong"] = q["chg_pct"] > 0                    # 转强
            # 确认判定：竞价不弱 +（收回 或 转强）
            auc_ok = entry.get("auction_ok", True)
            passed = auc_ok and (entry["reclaim"] or entry["strong"])
            entry["confirm"] = "通过" if passed else ("观察" if auc_ok else "淘汰")

    result["updated_at"] = datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")
    result["phase"] = args.phase
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)

    n_pass = sum(1 for v in result["checks"].values() if v.get("confirm") == "通过")
    print(f"OK {args.phase} 检查 {len(result['checks'])} 只（推荐日 {pack['date']}）"
          + (f" 确认通过 {n_pass} 只" if args.phase == "morning" else ""))
    for code, v in result["checks"].items():
        if args.phase == "auction":
            mark = "√" if v.get("auction_ok") else "×"
            print(f"  {mark} {v['name']}({code}) 竞价 {v.get('auction_pct')}%")
        else:
            print(f"  [{v.get('confirm','?')}] {v['name']}({code}) 开{v.get('open_pct')}% 现{v.get('now_pct')}%")


if __name__ == "__main__":
    main()
