# -*- coding: utf-8 -*-
"""
美股板块/主题扫描 —— 盘前盘中涨跌 + 量能（资金流代理口径）
数据源：新浪美股（gb_ 盘前实时价）、腾讯美股日K（5日均量）
口径说明：美股无公开的板块资金流接口，用「量比（当日量/5日均量）」作资金活跃度代理。
输出：data/us_market.json + data/us_market.js
"""
import os, json, re
from datetime import datetime, timezone, timedelta

for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests

BJ = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      "Referer": "https://finance.sina.com.cn"}

SECTORS = [
    ("xlk", "科技 XLK"), ("xlf", "金融 XLF"), ("xle", "能源 XLE"),
    ("xlv", "医疗 XLV"), ("xly", "可选消费 XLY"), ("xlp", "必需消费 XLP"),
    ("xli", "工业 XLI"), ("xlb", "材料 XLB"), ("xlu", "公用事业 XLU"),
    ("xlre", "房地产 XLRE"), ("xlc", "通信 XLC"),
]
THEMES = [
    ("soxx", "半导体 SOXX"), ("kweb", "中概互联 KWEB"), ("xbi", "生物科技 XBI"),
    ("arkk", "创新科技 ARKK"), ("ita", "军工 ITA"), ("xme", "金属矿业 XME"),
    ("jets", "航空 JETS"), ("tan", "太阳能 TAN"), ("xhb", "住宅建筑 XHB"),
    ("icln", "清洁能源 ICLN"),
]
INDICES = [("int_dji", "道琼斯"), ("int_nasdaq", "纳斯达克"), ("int_sp500", "标普500")]


def http_get(url, encoding=None):
    s = requests.Session()
    s.trust_env = False
    r = s.get(url, headers=UA, timeout=15)
    r.encoding = encoding or "gbk"
    return r


def us_market_state():
    """按当前 UTC 时间推美东交易时段（含夏令时粗判）"""
    utc = datetime.now(timezone.utc)
    y = utc.year
    # DST：3月第2个周日 ~ 11月第1个周日（美东 UTC-4），其余 UTC-5
    mar = datetime(y, 3, 8, tzinfo=timezone.utc)
    dst_start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1, tzinfo=timezone.utc)
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    offset = -4 if dst_start <= utc.replace(hour=0) < dst_end else -5
    et = utc + timedelta(hours=offset)
    hm = et.hour * 60 + et.minute
    wd = et.weekday()
    if wd >= 5:
        state = "closed_weekend"
    elif 240 <= hm < 570:      # 4:00-9:30 ET
        state = "premarket"
    elif 570 <= hm < 960:      # 9:30-16:00 ET
        state = "regular"
    elif 960 <= hm < 1200:     # 16:00-20:00 ET
        state = "afterhours"
    else:
        state = "closed"
    return state, et.strftime("%Y-%m-%d %H:%M ET")


def fetch_sina(symbols):
    """新浪美股批量行情。返回 {symbol: {...}}"""
    url = "https://hq.sinajs.cn/list=" + ",".join(f"gb_{s}" for s in symbols)
    text = http_get(url).text
    out = {}
    for line in text.strip().split(";"):
        m = re.match(r'var hq_str_gb_(\w+)="(.*)"', line.strip())
        if not m or not m.group(2):
            continue
        sym, f = m.group(1), m.group(2).split(",")
        if len(f) < 27:
            continue
        def fl(i):
            try:
                return float(f[i])
            except (ValueError, IndexError):
                return 0.0
        out[sym] = {
            "symbol": sym.upper(), "name_raw": f[0],
            "price": fl(1), "chg_pct": fl(2), "quote_time": f[3],
            "open": fl(5), "high": fl(6), "low": fl(7),
            "volume": fl(10), "prev_close": fl(26),
        }
    return out


def fetch_indices():
    url = "https://hq.sinajs.cn/list=" + ",".join(s for s, _ in INDICES)
    text = http_get(url).text
    out = []
    names = dict(INDICES)
    for line in text.strip().split(";"):
        m = re.match(r'var hq_str_(\w+)="(.*)"', line.strip())
        if not m or not m.group(2):
            continue
        f = m.group(2).split(",")
        out.append({"name": names.get(m.group(1), m.group(1)),
                    "price": float(f[1]), "chg": float(f[2]), "chg_pct": float(f[3])})
    return out


def avg_volume_5d(symbol):
    """新浪美股日K取5日均量（接口返回全历史，取尾部）"""
    url = ("https://stock.finance.sina.com.cn/usstock/api/jsonp_v2.php/a/"
           f"US_MinKService.getDailyK?symbol={symbol}&___qn=3&scale=240&ma=no&datalen=8")
    try:
        text = http_get(url, encoding="utf-8").text
        m = re.search(r"\((\[.*\])\)", text, re.S)
        if not m:
            return 0.0
        arr = json.loads(m.group(1))
        vols = [float(r["v"]) for r in arr][-6:-1]  # 剔除当日（可能不完整）
        return sum(vols) / len(vols) if vols else 0.0
    except Exception:
        return 0.0


def main():
    state, et_time = us_market_state()
    indices = fetch_indices()

    all_syms = [s for s, _ in SECTORS] + [s for s, _ in THEMES]
    quotes = fetch_sina(all_syms)
    names = dict(SECTORS + THEMES)

    def build(sym_list):
        out = []
        for sym, _ in sym_list:
            q = quotes.get(sym)
            if not q:
                continue
            avg5 = avg_volume_5d(sym)
            vol_ratio = round(q["volume"] / avg5, 2) if avg5 else None
            out.append({
                "symbol": q["symbol"], "name": names[sym],
                "price": q["price"], "chg_pct": q["chg_pct"],
                "volume": q["volume"], "vol_ratio": vol_ratio,
                "prev_close": q["prev_close"],
            })
        out.sort(key=lambda x: -x["chg_pct"])
        return out

    payload = {
        "meta": {
            "generated_at": datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
            "et_time": et_time,
            "us_market_state": state,
            "flow_note": "美股无公开板块资金流接口，以量比（当日量/5日均量）作资金活跃度代理",
        },
        "indices": indices,
        "sectors": build(SECTORS),
        "themes": build(THEMES),
    }
    with open(os.path.join(DATA_DIR, "us_market.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    with open(os.path.join(DATA_DIR, "us_market.js"), "w", encoding="utf-8") as f:
        f.write("window.US_MARKET_DATA = ")
        json.dump(payload, f, ensure_ascii=False)
        f.write(";")

    state_zh = {"premarket": "盘前", "regular": "盘中", "afterhours": "盘后",
                "closed": "休市", "closed_weekend": "周末休市"}[state]
    print(f"OK 美股{state_zh}（{et_time}） 板块{len(payload['sectors'])} 主题{len(payload['themes'])}")
    for s in payload["sectors"][:3]:
        print(f"  {s['name']} {s['chg_pct']:+.2f}% 量比{s['vol_ratio']}")


if __name__ == "__main__":
    main()
