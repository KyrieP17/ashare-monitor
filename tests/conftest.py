from __future__ import annotations

import pytest

from thesis.adapters import MockDemoAdapter
from thesis.proposal_builders import MockProposalBuilder
from thesis.repository import SQLiteThesisRepository
from thesis.workflow import ThesisWorkflow


@pytest.fixture
def repository():
    repo = SQLiteThesisRepository()
    yield repo
    repo.close()


@pytest.fixture
def workflow(repository):
    return ThesisWorkflow(repository, MockDemoAdapter(), MockProposalBuilder())
