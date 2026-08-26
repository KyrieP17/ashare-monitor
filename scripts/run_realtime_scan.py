from __future__ import annotations

import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.candidates import ScanMode, ScanRunStatus
from thesis.public_market import PublicMarketAdapter
from thesis.realtime_scan import RealtimeScanner, ScanExecutionResult


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="公开市场确定性候选扫描")
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="执行一次扫描")
    mode.add_argument("--loop", action="store_true", help="盘中循环扫描")
    result.add_argument("--interval-minutes", type=int, choices=(3, 4, 5), default=4)
    result.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "thesis.db")
    return result


def summarize(result: ScanExecutionResult) -> None:
    run = result.scan_run
    print(f"scan_run_id={run.scan_run_id}")
    print(f"mode={run.mode.value}")
    print(f"started_at={run.started_at.isoformat()}")
    print(f"completed_at={run.completed_at.isoformat() if run.completed_at else ''}")
    print(f"status={run.status.value}")
    print(f"trade_date={run.trade_date.isoformat() if run.trade_date else ''}")
    print(f"observation_count={run.observation_count}")
    print(f"candidate_count={run.candidate_count}")
    if run.expected_next_run_at:
        print(f"expected_next_run_at={run.expected_next_run_at.isoformat()}")
    for source in run.source_statuses:
        print(
            f"source_status={source.source}|{source.status.value}|"
            f"observations={source.observation_count}"
        )
    for error in run.error_messages:
        print(f"scan_warning={error}")
    for card in result.cards[:3]:
        print(
            "candidate_example="
            f"{card.instrument_name}|{card.instrument_id}|{','.join(card.trigger_rules)}|{card.freshness_status.value}"
        )
    print("generation_method=deterministic_rules")
    print("breadth_source=public_market")
    print("tonghuasun_in_candidate_calculation=false")


def main() -> int:
    args = parser().parse_args()
    adapter = PublicMarketAdapter(PROJECT_ROOT)
    with SQLiteCandidateRepository(args.database) as repository:
        scanner = RealtimeScanner(adapter, repository)
        if args.once:
            try:
                result = scanner.run_once(mode=ScanMode.ONCE)
            except Exception as exc:
                print(f"scan_persistence_error={type(exc).__name__}:{str(exc)[:240]}")
                return 1
            summarize(result)
            return 0 if result.scan_run.status in {ScanRunStatus.SUCCEEDED, ScanRunStatus.PARTIAL} else 1

        scanner.run_loop(
            interval_seconds=args.interval_minutes * 60,
            on_result=summarize,
            on_error=lambda error: print(f"scan_error={error};next_cycle=continue"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
