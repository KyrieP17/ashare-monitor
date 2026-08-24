from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.models import DiscoverySource, MarketSnapshot, ReviewDecision
from thesis.proposal_builders import DeterministicReplayProposalBuilder
from thesis.repository import RepositoryError, SQLiteThesisRepository
from thesis.snapshot_identity import snapshot_semantic_fingerprint
from thesis.symbols import normalize_symbol
from thesis.workflow import ThesisWorkflow


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DAY_1 = date(2026, 8, 20)
DAY_2 = date(2026, 8, 21)


def test_independent_real_json_parses_are_idempotent_across_repository_restart(tmp_path):
    adapter = ExistingJsonAdapter(DATA_DIR)
    first = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")])
    second = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")])

    assert first.snapshot_id == second.snapshot_id
    assert first.raw_data_hash == second.raw_data_hash
    assert first.created_at != second.created_at
    assert first.model_dump_json() != second.model_dump_json()

    database = tmp_path / "gate23.sqlite"
    repository = SQLiteThesisRepository(database)
    repository.ensure_snapshot(first)
    repository.close()

    reopened = SQLiteThesisRepository(database)
    reused = reopened.ensure_snapshot(second)
    stored = reopened.get_snapshot(first.snapshot_id)
    assert reused == stored == first
    reopened.close()


def test_semantic_fingerprint_ignores_only_parse_event_times():
    adapter = ExistingJsonAdapter(DATA_DIR)
    first = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")])
    second = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")])

    assert first.created_at != second.created_at
    assert snapshot_semantic_fingerprint(first) == snapshot_semantic_fingerprint(second)

    changed = first.model_dump(mode="json")
    changed["stock_observations"][0]["membership_metrics"][0]["value"] = False
    assert snapshot_semantic_fingerprint(first) != snapshot_semantic_fingerprint(
        MarketSnapshot.model_validate(changed)
    )


def test_first_written_payload_is_not_overwritten_by_semantic_retry(tmp_path):
    adapter = ExistingJsonAdapter(DATA_DIR)
    first = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")])
    second = adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")])
    repository = SQLiteThesisRepository(tmp_path / "first-write.sqlite")

    repository.ensure_snapshot(first)
    repository.ensure_snapshot(second)

    stored = repository.get_snapshot(first.snapshot_id)
    assert stored.model_dump_json() == first.model_dump_json()
    assert stored.created_at != second.created_at
    repository.close()


def test_initial_and_update_workflows_reuse_independently_parsed_snapshots(tmp_path):
    adapter = ExistingJsonAdapter(DATA_DIR)
    repository = SQLiteThesisRepository(tmp_path / "workflow-reuse.sqlite")
    workflow = ThesisWorkflow(repository, adapter, DeterministicReplayProposalBuilder())

    repository.ensure_snapshot(adapter.get_market_snapshot(DAY_1, [normalize_symbol("002437")]))
    card, initial = workflow.start_thesis(
        "002437", DAY_1, DiscoverySource.MANUAL_SEARCH
    )
    workflow.decide(initial.revision_id, ReviewDecision.ACCEPT)

    repository.ensure_snapshot(adapter.get_market_snapshot(DAY_2, [card.instrument]))
    update = workflow.propose_update(card.thesis_id, DAY_2)

    assert update.version == 2
    assert repository.get_card(card.thesis_id).current_accepted_revision_id == initial.revision_id
    repository.close()


def _semantic_conflicts(snapshot: MarketSnapshot) -> list[MarketSnapshot]:
    variants: list[MarketSnapshot] = []

    def mutate(*path: str, value) -> None:
        payload = snapshot.model_dump(mode="json")
        target = payload
        for key in path[:-1]:
            target = target[int(key)] if isinstance(target, list) else target[key]
        last = path[-1]
        if isinstance(target, list):
            target[int(last)] = value
        else:
            target[last] = value
        variants.append(MarketSnapshot.model_validate(payload))

    metric = ("stock_observations", "0", "membership_metrics", "0")
    mutate(*metric, "value", value=False)
    mutate(*metric, "unit", value="flag")
    mutate(*metric, "status", value="conflicted")
    mutate(*metric, "source", "data_as_of", value="2026-08-19")
    mutate(*metric, "source", "provider", value="different-provider")
    mutate(*metric, "raw_reference", value="different/raw/reference")

    limitation_payload = snapshot.model_dump(mode="json")
    limitation_payload["known_limitations"].append("new semantic limitation")
    variants.append(MarketSnapshot.model_validate(limitation_payload))
    return variants


def test_business_semantic_changes_remain_strict_conflicts(tmp_path):
    snapshot = ExistingJsonAdapter(DATA_DIR).get_market_snapshot(
        DAY_1, [normalize_symbol("002437")]
    )
    repository = SQLiteThesisRepository(tmp_path / "semantic-conflicts.sqlite")
    repository.ensure_snapshot(snapshot)

    for conflict in _semantic_conflicts(snapshot):
        with pytest.raises(RepositoryError, match="identity conflict"):
            repository.ensure_snapshot(conflict)

    changed_hash = snapshot.model_copy(update={"raw_data_hash": "changed-raw-hash"})
    with pytest.raises(RepositoryError, match="identity conflict"):
        repository.ensure_snapshot(changed_hash)

    assert repository.get_snapshot(snapshot.snapshot_id) == snapshot
    repository.close()
