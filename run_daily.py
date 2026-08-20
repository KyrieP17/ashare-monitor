# -*- coding: utf-8 -*-
"""
每日扫盘入口（Windows 任务计划/守护进程调用）
依次执行：fetch_data → limit_up_scan → us_market → tracker → position_check
日志写入 data/daily_run.log
"""
import os, subprocess
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
PY = r"C:\Users\Kyrie\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
LOG = os.path.join(BASE, "data", "daily_run.log")

os.makedirs(os.path.join(BASE, "data"), exist_ok=True)

PIPELINE = [
    ("fetch_data.py", ["--force"]),
    ("limit_up_scan.py", []),
    ("us_market.py", []),
    ("tracker.py", []),
    ("position_check.py", []),
]


def run(script, *args):
    cmd = [PY, os.path.join(BASE, script)] + list(args)
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="gbk", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    lines = [f"===== {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 每日扫盘开始 ====="]
    for script, args in PIPELINE:
        code, out = run(script, *args)
        lines.append(f"--- {script} exit={code} ---")
        lines.append(out.strip() or "(无输出)")
    lines.append("===== 结束 =====\n")
    text = "\n".join(lines)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    try:
        print(text)   # pythonw 无窗口模式下 stdout 不可用
    except Exception:
        pass


if __name__ == "__main__":
    main()
