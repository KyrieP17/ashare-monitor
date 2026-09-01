from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.candidate_research_adapter import CandidateResearchAdapter
from thesis.hot_money_lens import build_hot_money_lens
from thesis.proposal_builders import DeterministicReplayProposalBuilder


@dataclass(frozen=True)
class PreviewRow:
    trade_date: str
    instrument_id: str
    instrument_name: str
    decision: str
    role: str
    confidence: str
    opponent_view: list[str]
    price_in_view: list[str]
    counter_signals: list[str]
    limitations: list[str]
    observation_ref_ids: list[str]
    direct_trading_allowed: bool


def build_preview_rows(database: Path, trade_date: date | None = None) -> list[PreviewRow]:
    with SQLiteCandidateRepository(database) as repository:
        candidates = repository.list()
    if not candidates:
        return []
    selected_date = trade_date or max(candidate.trade_date for candidate in candidates)
    selected = [candidate for candidate in candidates if candidate.trade_date == selected_date]
    rows: list[PreviewRow] = []
    for candidate in selected:
        adapter = CandidateResearchAdapter(candidate)
        snapshot = adapter.get_market_snapshot(candidate.trade_date, [adapter.instrument])
        proposal = DeterministicReplayProposalBuilder().build_proposal(
            thesis_id=uuid4(),
            snapshot=snapshot,
            instrument=adapter.instrument,
            version=1,
            derived_from_revision_id=None,
            previous_revision=None,
        )
        report = build_hot_money_lens(
            snapshot,
            proposal,
            adapter.instrument.instrument_id,
        )
        rows.append(
            PreviewRow(
                trade_date=candidate.trade_date.isoformat(),
                instrument_id=candidate.instrument_id,
                instrument_name=candidate.instrument_name,
                decision=candidate.user_decision.value,
                role=report.role,
                confidence=report.confidence.value,
                opponent_view=report.opponent_view,
                price_in_view=report.price_in_view,
                counter_signals=report.counter_signals,
                limitations=report.limitations,
                observation_ref_ids=report.observation_ref_ids,
                direct_trading_allowed=report.direct_trading_allowed,
            )
        )
    return rows


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("trade date must use YYYY-MM-DD") from exc


def _print_markdown(rows: list[PreviewRow]) -> None:
    if not rows:
        print("候选箱没有可预览的数据。")
        return
    print(f"# 游资情绪与对手盘 Lens · {rows[0].trade_date}")
    print()
    print("| 标的 | 决策 | 角色 | 置信度 | 对手盘首要观察 |")
    print("|---|---|---|---|---|")
    for row in rows:
        opponent = row.opponent_view[0] if row.opponent_view else "证据不足"
        print(
            f"| {row.instrument_name} `{row.instrument_id}` | {row.decision} | "
            f"{row.role} | {row.confidence} | {opponent} |"
        )
    print()
    print("边界：只读研究预览；不读取账户，不输出仓位、买点或下单指令。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview the bounded hot-money lens on CandidateCard data.")
    parser.add_argument(
        "--database",
        type=Path,
        default=ROOT / "data" / "thesis.db",
        help="Path to the shared Candidate/Thesis SQLite database.",
    )
    parser.add_argument("--trade-date", type=_parse_date, help="Optional date in YYYY-MM-DD format.")
    parser.add_argument("--json", action="store_true", help="Emit complete structured JSON.")
    args = parser.parse_args()
    rows = build_preview_rows(args.database, args.trade_date)
    if args.json:
        print(json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2))
    else:
        _print_markdown(rows)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
