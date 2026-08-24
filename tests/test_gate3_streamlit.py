from __future__ import annotations

from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from thesis.existing_json_adapter import ExistingJsonAdapter
from thesis.gate3_generator import RecordedProposalGenerator
from thesis.gate3_graph import Gate3OfflineWorkflow
from thesis.models import DiscoverySource, ThesisLifecycleStatus
from thesis.repository import SQLiteThesisRepository
from thesis.semantic_reviewer import RecordedSemanticReviewer


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_streamlit_reads_pending_review_accepts_and_persists(tmp_path, monkeypatch):
    database = tmp_path / "ui.sqlite"
    repository = SQLiteThesisRepository(database)
    result = Gate3OfflineWorkflow(
        repository,
        ExistingJsonAdapter(PROJECT_ROOT / "data"),
        RecordedProposalGenerator(),
        RecordedSemanticReviewer(),
    ).start_initial_thesis(
        "CN.SZ.002437",
        date(2026, 8, 21),
        DiscoverySource.MANUAL_SEARCH,
    )
    proposal_id = result.proposal_revision_id
    assert proposal_id is not None
    repository.close()

    monkeypatch.setenv("THESIS_DB_PATH", str(database))
    app = AppTest.from_file(str(PROJECT_ROOT / "thesis" / "streamlit_app.py"))
    app.run(timeout=10)
    assert not app.exception
    assert app.title[0].value == "短线预期工作台"
    assert any("pool_absence_requires_human_interpretation" in item.value for item in app.warning)
    assert len(app.button) == 1
    app.button[0].click().run(timeout=10)
    assert not app.exception

    reopened = SQLiteThesisRepository(database)
    card = reopened.get_card(result.thesis_id)
    assert card.current_accepted_revision_id == proposal_id
    assert card.lifecycle_status is ThesisLifecycleStatus.ACTIVE
    assert len(reopened.list_decisions(result.thesis_id)) == 1
    assert reopened.list_pending_proposals(result.thesis_id) == []
    reopened.close()
