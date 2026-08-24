from datetime import date

import pytest

from thesis.adapters import MockDemoAdapter
from thesis.models import DiscoverySource, ReviewDecision
from thesis.repository import RepositoryError, SQLiteThesisRepository
from thesis.proposal_builders import MockProposalBuilder
from thesis.symbols import normalize_symbol
from thesis.workflow import ThesisWorkflow


def test_sqlite_persists_and_reloads_full_version_chain(tmp_path):
    database = tmp_path / "thesis.db"
    first_repo = SQLiteThesisRepository(database)
    workflow = ThesisWorkflow(first_repo, MockDemoAdapter(), MockProposalBuilder())
    card, proposal = workflow.start_thesis(
        "sz300750",
        date(2026, 8, 20),
        DiscoverySource.WATCHLIST,
    )
    workflow.decide(proposal.revision_id, ReviewDecision.ACCEPT)
    snapshot_id = card.created_from_snapshot_id
    first_repo.close()

    second_repo = SQLiteThesisRepository(database)
    reloaded_card = second_repo.get_card(card.thesis_id)
    reloaded_revision = second_repo.get_current_accepted_revision(card.thesis_id)
    reloaded_snapshot = second_repo.get_snapshot(snapshot_id)

    assert reloaded_card.instrument.instrument_id == "CN.SZ.300750"
    assert reloaded_card.current_accepted_revision_id == proposal.revision_id
    assert reloaded_revision is not None and reloaded_revision.accepted is True
    assert reloaded_snapshot.snapshot_id == snapshot_id
    assert reloaded_snapshot.stock_observations[0].instrument.code == "300750"
    assert len(second_repo.list_decisions(card.thesis_id)) == 1
    second_repo.close()


def test_snapshot_rows_are_immutable_and_raw_payload_is_deduplicated(repository):
    snapshot = MockDemoAdapter().get_market_snapshot(
        date(2026, 8, 20),
        [normalize_symbol("sh600519")],
    )
    repository.save_snapshot(snapshot)

    with pytest.raises(RepositoryError, match="immutable or duplicate"):
        repository.save_snapshot(snapshot)
