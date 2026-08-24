from datetime import date

import pytest

from thesis.diff import diff_revisions
from thesis.models import (
    DiscoverySource,
    ReviewDecision,
    RevisionChanges,
    RevisionType,
    ThesisAssessment,
    ThesisLifecycleStatus,
)
from thesis.repository import DuplicateActiveThesisError


TRADE_DAY_1 = date(2026, 8, 20)
TRADE_DAY_2 = date(2026, 8, 21)


def start(workflow, symbol="sh600519"):
    return workflow.start_thesis(
        symbol,
        TRADE_DAY_1,
        DiscoverySource.MANUAL_SEARCH,
        "manual Gate 1 test",
    )


def test_first_proposal_accept_activates_card(workflow, repository):
    card, proposal = start(workflow)
    assert card.lifecycle_status is ThesisLifecycleStatus.DRAFT
    assert card.current_accepted_revision_id is None

    event, result = workflow.decide(proposal.revision_id, ReviewDecision.ACCEPT)

    stored_card = repository.get_card(card.thesis_id)
    accepted = repository.get_current_accepted_revision(card.thesis_id)
    assert result is None
    assert event.decision is ReviewDecision.ACCEPT
    assert stored_card.lifecycle_status is ThesisLifecycleStatus.ACTIVE
    assert stored_card.current_accepted_revision_id == proposal.revision_id
    assert accepted is not None and accepted.accepted is True


def test_first_proposal_modify_creates_accepted_user_revision(workflow, repository):
    card, proposal = start(workflow)
    changes = RevisionChanges(
        market_expectation="User-authored expectation after reviewing both sides.",
        assessment=ThesisAssessment.CONFLICTED,
    )

    event, user_revision = workflow.decide(
        proposal.revision_id,
        ReviewDecision.MODIFY,
        changes=changes,
        user_comment="I disagree with the initial framing.",
    )

    assert user_revision is not None
    assert user_revision.revision_type is RevisionType.USER_REVISION
    assert user_revision.version == 2
    assert user_revision.derived_from_revision_id == proposal.revision_id
    assert user_revision.accepted is True
    assert event.resulting_revision_id == user_revision.revision_id
    assert repository.get_card(card.thesis_id).current_accepted_revision_id == user_revision.revision_id
    assert repository.get_revision(proposal.revision_id).accepted is False


def test_first_proposal_reject_marks_card_rejected_and_preserves_audit(workflow, repository):
    card, proposal = start(workflow)

    event, result = workflow.decide(
        proposal.revision_id,
        ReviewDecision.REJECT,
        user_comment="Insufficient basis.",
    )

    stored = repository.get_card(card.thesis_id)
    assert result is None
    assert stored.lifecycle_status is ThesisLifecycleStatus.REJECTED
    assert stored.current_accepted_revision_id is None
    assert repository.list_active_cards() == []
    assert repository.list_decisions(card.thesis_id) == [event]


def test_duplicate_active_card_is_rejected(workflow):
    start(workflow)
    with pytest.raises(DuplicateActiveThesisError):
        start(workflow)


def test_rejected_update_preserves_old_accepted_revision(workflow, repository):
    card, first = start(workflow)
    workflow.decide(first.revision_id, ReviewDecision.ACCEPT)
    update = workflow.propose_update(card.thesis_id, TRADE_DAY_2)

    workflow.decide(update.revision_id, ReviewDecision.REJECT)

    stored = repository.get_card(card.thesis_id)
    assert stored.lifecycle_status is ThesisLifecycleStatus.ACTIVE
    assert stored.current_accepted_revision_id == first.revision_id
    assert repository.get_current_accepted_revision(card.thesis_id).revision_id == first.revision_id
    assert repository.get_revision(update.revision_id).accepted is False


def test_update_modify_has_explicit_proposal_to_user_revision_chain(workflow, repository):
    card, first = start(workflow)
    workflow.decide(first.revision_id, ReviewDecision.ACCEPT)
    update = workflow.propose_update(card.thesis_id, TRADE_DAY_2)

    _, user_revision = workflow.decide(
        update.revision_id,
        ReviewDecision.MODIFY,
        changes=RevisionChanges(
            market_expectation="User keeps the thesis but weakens the expectation.",
            assessment=ThesisAssessment.WEAKENING,
        ),
    )

    assert update.version == 2
    assert update.derived_from_revision_id == first.revision_id
    assert user_revision is not None and user_revision.version == 3
    assert user_revision.derived_from_revision_id == update.revision_id
    assert repository.get_current_accepted_revision(card.thesis_id).revision_id == user_revision.revision_id


def test_invalidation_proposal_never_auto_changes_lifecycle(workflow, repository):
    card, first = start(workflow)
    workflow.decide(first.revision_id, ReviewDecision.ACCEPT)

    invalidation_proposal = workflow.propose_update(
        card.thesis_id,
        TRADE_DAY_2,
        proposed_lifecycle_status=ThesisLifecycleStatus.INVALIDATED,
    )

    before_review = repository.get_card(card.thesis_id)
    assert invalidation_proposal.proposed_lifecycle_status is ThesisLifecycleStatus.INVALIDATED
    assert before_review.lifecycle_status is ThesisLifecycleStatus.ACTIVE
    assert before_review.current_accepted_revision_id == first.revision_id

    workflow.decide(invalidation_proposal.revision_id, ReviewDecision.REJECT)
    after_reject = repository.get_card(card.thesis_id)
    assert after_reject.lifecycle_status is ThesisLifecycleStatus.ACTIVE
    assert after_reject.current_accepted_revision_id == first.revision_id


def test_structured_diff_is_computed_from_revision_fields(workflow):
    _, proposal = start(workflow)
    _, user_revision = workflow.decide(
        proposal.revision_id,
        ReviewDecision.MODIFY,
        changes=RevisionChanges(
            market_expectation="Edited by the user.",
            assessment=ThesisAssessment.STRENGTHENING,
        ),
    )
    revision_diff = diff_revisions(proposal, user_revision)
    assert set(revision_diff.changes) == {"market_expectation", "assessment"}
    assert revision_diff.changes["assessment"].before == ThesisAssessment.PENDING.value
    assert revision_diff.changes["assessment"].after == ThesisAssessment.STRENGTHENING.value
