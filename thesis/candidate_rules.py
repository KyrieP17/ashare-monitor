from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from .candidates import CandidateCard, CandidateObservation, candidate_id_for
from .models import DataStatus


RULE_WATCHLIST = "WATCHLIST_ACTIVITY"
RULE_RESEARCH = "RESEARCH_FOCUS"
RULE_CONSECUTIVE = "CONSECUTIVE_LIMIT_UP"
RULE_LIMIT_UP = "LIMIT_UP_POOL"
RULE_SECTOR = "SECTOR_RESONANCE"
DEFAULT_MARKET_CANDIDATE_LIMIT = 10


def build_candidate_cards(
    observations: list[CandidateObservation],
    *,
    trade_date: date,
    seen_at: datetime,
    limit: int = DEFAULT_MARKET_CANDIDATE_LIMIT,
) -> list[CandidateCard]:
    grouped: dict[str, list[tuple[int, str, CandidateObservation]]] = defaultdict(list)
    for observation in observations:
        rule = _rule_for(observation)
        if rule is None:
            continue
        priority, trigger = rule
        grouped[observation.instrument_id].append((priority, trigger, observation))

    research_cards: list[CandidateCard] = []
    market_ranked: list[tuple[tuple[int, float, str], CandidateCard]] = []
    for instrument_id, matches in grouped.items():
        matches.sort(key=lambda item: (item[0], item[1], item[2].source))
        selected_observations = _deduplicate_observations([item[2] for item in matches])
        triggers = _ordered_unique(item[1] for item in matches)
        sources = _ordered_unique(item.source for item in selected_observations)
        snapshot_ids = _ordered_unique(item.source_snapshot_id for item in selected_observations)
        conflicted = observations_conflict(selected_observations)
        status = DataStatus.CONFLICTED if conflicted else _freshness(selected_observations)
        reasons = _ordered_unique(item.reason for item in selected_observations if item.reason)
        reason_text = "确定性规则 + 数据源原始 reason 字段拼接：" + "；".join(reasons or triggers)
        data_as_of = max(item.data_as_of for item in selected_observations)
        name = next(item.instrument_name for item in selected_observations if item.instrument_name)
        card = CandidateCard(
            candidate_id=candidate_id_for(trade_date, instrument_id),
            trade_date=trade_date,
            instrument_id=instrument_id,
            instrument_name=name,
            first_seen_at=seen_at,
            last_seen_at=seen_at,
            hit_count=1,
            trigger_rules=triggers,
            reason_text=reason_text,
            source_snapshot_ids=snapshot_ids,
            source_names=sources,
            data_as_of=data_as_of,
            freshness_status=status,
            observations=selected_observations,
        )
        if RULE_RESEARCH in triggers:
            # Research-pool membership is explicit user intent. Preserve every
            # such card in source order and keep any simultaneous market
            # observations merged into it without consuming a market slot.
            research_cards.append(card)
            continue
        best_priority = min(item[0] for item in matches)
        strength = max(_strength(item[2], item[1]) for item in matches)
        market_ranked.append(((best_priority, -strength, instrument_id), card))
    market_ranked.sort(key=lambda item: item[0])
    market_cards = _select_market_cards(market_ranked, limit)
    return research_cards + market_cards


def _select_market_cards(
    ranked: list[tuple[tuple[int, float, str], CandidateCard]],
    limit: int,
) -> list[CandidateCard]:
    capacity = max(0, limit)
    selected = ranked[:capacity]
    if capacity == 0 or any(RULE_LIMIT_UP in card.trigger_rules for _, card in selected):
        return [card for _, card in selected]

    first_board = next(
        ((rank, card) for rank, card in ranked if RULE_LIMIT_UP in card.trigger_rules),
        None,
    )
    if first_board is None:
        return [card for _, card in selected]

    # Only intervene when strict priority would exclude every first-board card.
    # The guaranteed card is already the highest-ranked first board under the
    # existing strength/code ordering; all other slots keep their prior order.
    selected_ids = {card.candidate_id for _, card in ranked[: capacity - 1]}
    selected_ids.add(first_board[1].candidate_id)
    return [card for _, card in ranked if card.candidate_id in selected_ids]


def observations_conflict(observations: list[CandidateObservation]) -> bool:
    if any(item.status is DataStatus.CONFLICTED for item in observations):
        return True
    by_metric: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for observation in observations:
        for key in ("price", "chg_pct"):
            value = observation.metrics.get(key)
            if isinstance(value, (int, float)):
                by_metric[key].append((observation.source, float(value)))
    for values in by_metric.values():
        if len({source for source, _ in values}) < 2:
            continue
        numbers = [value for _, value in values]
        scale = max(abs(value) for value in numbers) or 1.0
        if (max(numbers) - min(numbers)) / scale > 0.005:
            return True
    return False


def _rule_for(observation: CandidateObservation) -> tuple[int, str] | None:
    metrics = observation.metrics
    if observation.source == "public.tencent.research_focus" and metrics.get("user_selected") is True:
        return 0, RULE_RESEARCH
    if observation.source == "public.tencent.watchlist":
        chg = abs(_number(metrics.get("chg_pct")))
        turnover = _number(metrics.get("turnover_pct"))
        if chg >= 3 or turnover >= 5:
            return 1, RULE_WATCHLIST
    if observation.source == "public.ths.limit_up_pool":
        boards = int(_number(metrics.get("boards")))
        return (2, RULE_CONSECUTIVE) if boards >= 2 else (3, RULE_LIMIT_UP)
    if observation.source == "public.sina.board_flow":
        if (
            _number(metrics.get("board_net_yi")) > 0
            and _number(metrics.get("board_chg_pct")) >= 1
            and _number(metrics.get("lead_chg_pct")) >= 5
        ):
            return 4, RULE_SECTOR
    if (
        observation.source == "legacy.market.theme_counts"
        and metrics.get("sector_resonance") is True
    ):
        return 4, RULE_SECTOR
    return None


def _strength(observation: CandidateObservation, trigger: str) -> float:
    metrics = observation.metrics
    if trigger == RULE_RESEARCH:
        return abs(_number(metrics.get("chg_pct"))) + _number(metrics.get("turnover_pct")) / 10
    if trigger == RULE_WATCHLIST:
        return abs(_number(metrics.get("chg_pct"))) + _number(metrics.get("turnover_pct")) / 10
    if trigger == RULE_CONSECUTIVE:
        return _number(metrics.get("boards")) * 100 + _number(metrics.get("chg_pct"))
    if trigger == RULE_LIMIT_UP:
        return _number(metrics.get("chg_pct"))
    return _number(metrics.get("board_net_yi")) + _number(metrics.get("lead_chg_pct"))


def _freshness(observations: list[CandidateObservation]) -> DataStatus:
    statuses = {item.status for item in observations}
    if DataStatus.ERROR in statuses:
        return DataStatus.ERROR
    if DataStatus.STALE in statuses:
        return DataStatus.STALE
    if DataStatus.MISSING in statuses:
        return DataStatus.MISSING
    return DataStatus.AVAILABLE


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ordered_unique(values) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values))


def _deduplicate_observations(observations: list[CandidateObservation]) -> list[CandidateObservation]:
    return list({item.observation_ref_id: item for item in observations}.values())
