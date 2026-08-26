from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from thesis.candidate_repository import SQLiteCandidateRepository
from thesis.candidate_rules import (
    DEFAULT_MARKET_CANDIDATE_LIMIT,
    RULE_CONSECUTIVE,
    RULE_LIMIT_UP,
    RULE_RESEARCH,
    RULE_WATCHLIST,
    build_candidate_cards,
)
from thesis.candidates import CandidateDecision, CandidateObservation, ScanRunStatus
from thesis.freshness import artifact_freshness
from thesis.models import DataStatus
from thesis.realtime_scan import RealtimeScanner


DAY = date(2026, 8, 26)
NOW = datetime(2026, 8, 26, 2, 0, tzinfo=UTC)


def observation(
    instrument_id: str,
    *,
    source: str,
    metrics: dict,
    reason: str = "原始原因",
    ref: str | None = None,
) -> CandidateObservation:
    return CandidateObservation(
        instrument_id=instrument_id,
        instrument_name={"CN.SH.600001": "甲", "CN.SZ.000002": "乙"}.get(instrument_id, "丙"),
        source=source,
        data_as_of=NOW,
        retrieved_at=NOW,
        status=DataStatus.AVAILABLE,
        coverage="test_coverage",
        observation_ref_id=ref or f"obs:{source}:{instrument_id}",
        raw_reference=f"raw[{instrument_id}]",
        source_snapshot_id=f"snapshot:{source}",
        reason=reason,
        metrics=metrics,
    )


def test_candidate_rules_prioritize_watchlist_then_consecutive_then_limit_up():
    observations = [
        observation("CN.SH.600001", source="public.ths.limit_up_pool", metrics={"boards": 2, "chg_pct": 10}),
        observation("CN.SZ.000002", source="public.tencent.watchlist", metrics={"chg_pct": 4, "turnover_pct": 1}),
        observation("CN.SZ.000003", source="public.ths.limit_up_pool", metrics={"boards": 1, "chg_pct": 10}),
    ]

    cards = build_candidate_cards(observations, trade_date=DAY, seen_at=NOW)

    assert [card.instrument_id for card in cards] == ["CN.SZ.000002", "CN.SH.600001", "CN.SZ.000003"]
    assert cards[0].trigger_rules == [RULE_WATCHLIST]
    assert cards[1].trigger_rules == [RULE_CONSECUTIVE]
    assert cards[2].trigger_rules == [RULE_LIMIT_UP]
    assert cards[0].reason_text.startswith("确定性规则 + 数据源原始 reason 字段拼接：")


def test_research_pool_is_unbounded_and_does_not_consume_market_slots():
    research_ids = [f"CN.SZ.00{index:04d}" for index in range(1, 9)]
    observations = [
        observation(
            instrument_id,
            source="public.tencent.research_focus",
            metrics={"user_selected": True, "chg_pct": index, "turnover_pct": 1},
        )
        for index, instrument_id in enumerate(research_ids, start=1)
    ]
    observations.extend(
        observation(
            f"CN.SH.600{index:03d}",
            source="public.ths.limit_up_pool",
            metrics={"boards": 5, "chg_pct": 10},
        )
        for index in range(1, 13)
    )

    cards = build_candidate_cards(observations, trade_date=DAY, seen_at=NOW)
    research_cards = [card for card in cards if RULE_RESEARCH in card.trigger_rules]
    market_cards = [card for card in cards if RULE_RESEARCH not in card.trigger_rules]

    assert len(research_cards) == 8
    assert {card.instrument_id for card in research_cards} == set(research_ids)
    assert all(card.trigger_rules == [RULE_RESEARCH] for card in research_cards)
    assert len(market_cards) == DEFAULT_MARKET_CANDIDATE_LIMIT
    assert all(RULE_CONSECUTIVE in card.trigger_rules for card in market_cards)

    limited = build_candidate_cards(observations, trade_date=DAY, seen_at=NOW, limit=3)
    assert sum(RULE_RESEARCH in card.trigger_rules for card in limited) == 8
    assert sum(RULE_RESEARCH not in card.trigger_rules for card in limited) == 3


def test_market_limit_keeps_first_board_candidates_when_capacity_remains():
    observations = [
        observation(
            f"CN.SH.601{index:03d}",
            source="public.ths.limit_up_pool",
            metrics={"boards": 3, "chg_pct": 10},
        )
        for index in range(1, 8)
    ]
    observations.extend(
        observation(
            f"CN.SZ.002{index:03d}",
            source="public.ths.limit_up_pool",
            metrics={"boards": 1, "chg_pct": 10},
        )
        for index in range(1, 5)
    )

    cards = build_candidate_cards(observations, trade_date=DAY, seen_at=NOW)

    assert len(cards) == DEFAULT_MARKET_CANDIDATE_LIMIT
    assert sum(RULE_CONSECUTIVE in card.trigger_rules for card in cards) == 7
    assert sum(RULE_LIMIT_UP in card.trigger_rules for card in cards) == 3


def test_market_limit_guarantees_one_first_board_when_priority_would_exclude_all():
    observations = [
        observation(
            "CN.SH.688981",
            source="public.tencent.watchlist",
            metrics={"chg_pct": 4, "turnover_pct": 1},
        )
    ]
    observations.extend(
        observation(
            f"CN.SH.601{index:03d}",
            source="public.ths.limit_up_pool",
            metrics={"boards": 3, "chg_pct": 10},
        )
        for index in range(1, 10)
    )
    observations.append(
        observation(
            "CN.SZ.002999",
            source="public.ths.limit_up_pool",
            metrics={"boards": 1, "chg_pct": 10},
        )
    )

    cards = build_candidate_cards(observations, trade_date=DAY, seen_at=NOW)

    assert len(cards) == DEFAULT_MARKET_CANDIDATE_LIMIT
    assert sum(RULE_WATCHLIST in card.trigger_rules for card in cards) == 1
    assert sum(RULE_CONSECUTIVE in card.trigger_rules for card in cards) == 8
    assert sum(RULE_LIMIT_UP in card.trigger_rules for card in cards) == 1


def test_market_limit_without_first_board_keeps_original_priority_result():
    observations = [
        observation(
            f"CN.SH.601{index:03d}",
            source="public.ths.limit_up_pool",
            metrics={"boards": 2 + index, "chg_pct": 10},
        )
        for index in range(1, 13)
    ]

    cards = build_candidate_cards(observations, trade_date=DAY, seen_at=NOW)

    assert len(cards) == DEFAULT_MARKET_CANDIDATE_LIMIT
    assert all(RULE_CONSECUTIVE in card.trigger_rules for card in cards)
    assert [card.instrument_id for card in cards] == [
        f"CN.SH.601{index:03d}" for index in range(12, 2, -1)
    ]


def test_multiple_first_boards_use_existing_strength_then_code_order():
    observations = [
        observation(
            f"CN.SH.601{index:03d}",
            source="public.ths.limit_up_pool",
            metrics={"boards": 4, "chg_pct": 10},
        )
        for index in range(1, 11)
    ]
    observations.extend(
        [
            observation(
                "CN.SZ.002003",
                source="public.ths.limit_up_pool",
                metrics={"boards": 1, "chg_pct": 9},
            ),
            observation(
                "CN.SZ.002002",
                source="public.ths.limit_up_pool",
                metrics={"boards": 1, "chg_pct": 10},
            ),
            observation(
                "CN.SZ.002001",
                source="public.ths.limit_up_pool",
                metrics={"boards": 1, "chg_pct": 10},
            ),
        ]
    )

    cards = build_candidate_cards(observations, trade_date=DAY, seen_at=NOW)
    guaranteed = [card for card in cards if RULE_LIMIT_UP in card.trigger_rules]

    assert len(guaranteed) == 1
    assert guaranteed[0].instrument_id == "CN.SZ.002001"


def test_first_board_floor_does_not_change_research_pool_membership():
    research_ids = [f"CN.SZ.000{index:03d}" for index in range(1, 9)]
    observations = [
        observation(
            instrument_id,
            source="public.tencent.research_focus",
            metrics={"user_selected": True},
        )
        for instrument_id in research_ids
    ]
    observations.extend(
        observation(
            f"CN.SH.603{index:03d}",
            source="public.ths.limit_up_pool",
            metrics={"boards": 3, "chg_pct": 10},
        )
        for index in range(1, 11)
    )
    observations.append(
        observation(
            "CN.SH.605999",
            source="public.ths.limit_up_pool",
            metrics={"boards": 1, "chg_pct": 10},
        )
    )

    cards = build_candidate_cards(observations, trade_date=DAY, seen_at=NOW)
    research_cards = [card for card in cards if RULE_RESEARCH in card.trigger_rules]
    market_cards = [card for card in cards if RULE_RESEARCH not in card.trigger_rules]

    assert {card.instrument_id for card in research_cards} == set(research_ids)
    assert len(market_cards) == DEFAULT_MARKET_CANDIDATE_LIMIT
    assert sum(RULE_LIMIT_UP in card.trigger_rules for card in market_cards) == 1


def test_research_market_overlap_is_merged_without_consuming_market_capacity():
    overlap = "CN.SH.600001"
    observations = [
        observation(
            overlap,
            source="public.tencent.research_focus",
            metrics={"user_selected": True},
        ),
        observation(
            overlap,
            source="public.ths.limit_up_pool",
            metrics={"boards": 2, "chg_pct": 10},
        ),
    ]
    observations.extend(
        observation(
            f"CN.SZ.003{index:03d}",
            source="public.ths.limit_up_pool",
            metrics={"boards": 1, "chg_pct": 10},
        )
        for index in range(1, DEFAULT_MARKET_CANDIDATE_LIMIT + 1)
    )

    cards = build_candidate_cards(observations, trade_date=DAY, seen_at=NOW)
    research_cards = [card for card in cards if RULE_RESEARCH in card.trigger_rules]
    market_cards = [card for card in cards if RULE_RESEARCH not in card.trigger_rules]

    assert len(research_cards) == 1
    assert research_cards[0].trigger_rules == [RULE_RESEARCH, RULE_CONSECUTIVE]
    assert len(market_cards) == DEFAULT_MARKET_CANDIDATE_LIMIT


def test_conflicting_sources_are_preserved_and_marked_conflicted():
    cards = build_candidate_cards(
        [
            observation("CN.SH.600001", source="public.ths.limit_up_pool", metrics={"boards": 2, "price": 10.0}),
            observation("CN.SH.600001", source="public.tencent.watchlist", metrics={"chg_pct": 4, "price": 10.2}),
        ],
        trade_date=DAY,
        seen_at=NOW,
    )

    assert len(cards) == 1
    assert cards[0].freshness_status is DataStatus.CONFLICTED
    assert len(cards[0].observations) == 2
    assert set(cards[0].source_names) == {"public.ths.limit_up_pool", "public.tencent.watchlist"}


def test_same_day_upsert_increments_hits_and_preserves_decision(tmp_path):
    database = tmp_path / "candidate.sqlite"
    first = build_candidate_cards(
        [observation("CN.SH.600001", source="public.ths.limit_up_pool", metrics={"boards": 2})],
        trade_date=DAY,
        seen_at=NOW,
    )[0]
    later_observation = observation(
        "CN.SH.600001",
        source="public.tencent.watchlist",
        metrics={"chg_pct": 4},
        ref="obs:later",
    )
    second = build_candidate_cards(
        [later_observation], trade_date=DAY, seen_at=NOW + timedelta(minutes=4)
    )[0]

    with SQLiteCandidateRepository(database) as repository:
        repository.upsert([first])
        repository.set_decision(first.candidate_id, CandidateDecision.KEEP)
        merged = repository.upsert([second])[0]

    assert merged.hit_count == 2
    assert merged.first_seen_at == NOW
    assert merged.last_seen_at == NOW + timedelta(minutes=4)
    assert merged.user_decision is CandidateDecision.KEEP
    assert set(merged.trigger_rules) == {RULE_CONSECUTIVE, RULE_WATCHLIST}

    with SQLiteCandidateRepository(database) as reopened:
        loaded = reopened.list(trade_date=DAY)
    assert len(loaded) == 1
    assert loaded[0].user_decision is CandidateDecision.KEEP


def test_loop_continues_after_one_round_failure(tmp_path):
    good = observation("CN.SH.600001", source="public.ths.limit_up_pool", metrics={"boards": 2})

    class FlakyAdapter:
        calls = 0

        def collect(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary")
            return [good]

    errors: list[str] = []
    results: list[int] = []
    beijing = timezone(timedelta(hours=8))
    with SQLiteCandidateRepository(tmp_path / "loop.sqlite") as repository:
        scanner = RealtimeScanner(
            FlakyAdapter(),
            repository,
            now=lambda: datetime(2026, 8, 26, 10, 0, tzinfo=beijing),
            sleeper=lambda _: None,
        )
        scanner.run_loop(
            interval_seconds=180,
            on_error=errors.append,
            on_result=lambda result: results.append(result.scan_run.status),
            max_cycles=2,
        )
        stored = repository.list(trade_date=DAY)

    assert len(errors) == 1
    assert results == [ScanRunStatus.FAILED, ScanRunStatus.SUCCEEDED]
    assert len(stored) == 1


def test_old_artifact_is_marked_stale():
    payload = {"meta": {"trade_date": "20260821", "generated_at": "2026-08-23 19:51:26"}}
    now = datetime(2026, 8, 26, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    result = artifact_freshness(payload, now=now)

    assert result.stale is True
    assert result.trade_date == date(2026, 8, 21)
    assert result.expected_trade_date == DAY


def test_candidate_page_registered_and_cloud_boundary_visible():
    root = Path(__file__).resolve().parents[1]
    app_source = (root / "app.py").read_text(encoding="utf-8")
    page_source = (root / "pages" / "6_candidates.py").read_text(encoding="utf-8")
    assert 'st.Page("pages/6_candidates.py"' in app_source
    assert "实时候选仅本地模式可用" in page_source
    assert "run_realtime_scan.py" in page_source
