from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

from .gate3_models import ProposalReviewRecord, ToolInvocation
from .models import (
    DecisionEvent,
    MarketSnapshot,
    ReviewDecision,
    RevisionChanges,
    RevisionType,
    ThesisCard,
    ThesisLifecycleStatus,
    ThesisRevision,
)
from .snapshot_identity import snapshots_semantically_equal


class RepositoryError(RuntimeError):
    pass


class NotFoundError(RepositoryError):
    pass


class DuplicateActiveThesisError(RepositoryError):
    pass


class InvalidStateTransitionError(RepositoryError):
    pass


class SQLiteThesisRepository:
    """Transactional local repository for immutable snapshots and thesis history."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.database = str(database)
        self._connection = sqlite3.connect(self.database)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteThesisRepository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                raw_data_hash TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE (trade_date, raw_data_hash)
            );

            CREATE TABLE IF NOT EXISTS thesis_cards (
                thesis_id TEXT PRIMARY KEY,
                instrument_id TEXT NOT NULL,
                lifecycle_status TEXT NOT NULL,
                created_from_snapshot_id TEXT NOT NULL REFERENCES market_snapshots(snapshot_id),
                current_accepted_revision_id TEXT,
                payload TEXT NOT NULL,
                FOREIGN KEY (current_accepted_revision_id) REFERENCES thesis_revisions(revision_id)
                    DEFERRABLE INITIALLY DEFERRED
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_instrument
            ON thesis_cards(instrument_id)
            WHERE lifecycle_status IN ('draft', 'active');

            CREATE TABLE IF NOT EXISTS thesis_revisions (
                revision_id TEXT PRIMARY KEY,
                thesis_id TEXT NOT NULL REFERENCES thesis_cards(thesis_id),
                based_on_snapshot_id TEXT NOT NULL REFERENCES market_snapshots(snapshot_id),
                derived_from_revision_id TEXT REFERENCES thesis_revisions(revision_id),
                revision_type TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version >= 1),
                accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
                payload TEXT NOT NULL,
                UNIQUE (thesis_id, version)
            );

            CREATE TABLE IF NOT EXISTS decision_events (
                decision_id TEXT PRIMARY KEY,
                thesis_id TEXT NOT NULL REFERENCES thesis_cards(thesis_id),
                proposal_revision_id TEXT NOT NULL UNIQUE REFERENCES thesis_revisions(revision_id),
                resulting_revision_id TEXT REFERENCES thesis_revisions(revision_id),
                decision TEXT NOT NULL,
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposal_reviews (
                review_id TEXT PRIMARY KEY,
                proposal_revision_id TEXT NOT NULL UNIQUE REFERENCES thesis_revisions(revision_id),
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tool_invocations (
                invocation_id TEXT PRIMARY KEY,
                llm_tool_call_id TEXT UNIQUE,
                tool_name TEXT NOT NULL,
                status TEXT NOT NULL,
                snapshot_id TEXT REFERENCES market_snapshots(snapshot_id),
                payload TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    @staticmethod
    def _json(model: object) -> str:
        return model.model_dump_json()  # type: ignore[attr-defined]

    def save_snapshot(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO market_snapshots(snapshot_id, trade_date, raw_data_hash, payload) VALUES (?, ?, ?, ?)",
                    (str(snapshot.snapshot_id), snapshot.trade_date.isoformat(), snapshot.raw_data_hash, self._json(snapshot)),
                )
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"snapshot is immutable or duplicate: {snapshot.snapshot_id}") from exc
        return snapshot

    def _ensure_snapshot(self, connection: sqlite3.Connection, snapshot: MarketSnapshot) -> None:
        payload = self._json(snapshot)
        row = connection.execute(
            "SELECT trade_date, raw_data_hash, payload FROM market_snapshots WHERE snapshot_id = ?",
            (str(snapshot.snapshot_id),),
        ).fetchone()
        if row is not None:
            existing = MarketSnapshot.model_validate_json(row["payload"])
            if (
                row["trade_date"] == snapshot.trade_date.isoformat()
                and row["raw_data_hash"] == snapshot.raw_data_hash
                and snapshots_semantically_equal(existing, snapshot)
            ):
                return
            raise RepositoryError(
                f"snapshot identity conflict; existing payload cannot be overwritten: {snapshot.snapshot_id}"
            )

        duplicate = connection.execute(
            "SELECT snapshot_id, payload FROM market_snapshots WHERE trade_date = ? AND raw_data_hash = ?",
            (snapshot.trade_date.isoformat(), snapshot.raw_data_hash),
        ).fetchone()
        if duplicate is not None:
            raise RepositoryError(
                "snapshot hash collision or inconsistent identity for the same trade date and raw_data_hash"
            )
        connection.execute(
            "INSERT INTO market_snapshots(snapshot_id, trade_date, raw_data_hash, payload) VALUES (?, ?, ?, ?)",
            (str(snapshot.snapshot_id), snapshot.trade_date.isoformat(), snapshot.raw_data_hash, payload),
        )

    def ensure_snapshot(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        """Persist or reuse a semantically identical snapshot without overwriting first-write metadata."""
        with self._transaction() as connection:
            self._ensure_snapshot(connection, snapshot)
        return self.get_snapshot(snapshot.snapshot_id)

    @staticmethod
    def _validate_initial_bundle(
        card: ThesisCard,
        proposal: ThesisRevision,
        review: ProposalReviewRecord | None,
    ) -> None:
        if card.lifecycle_status is not ThesisLifecycleStatus.DRAFT:
            raise InvalidStateTransitionError("new thesis card must be DRAFT")
        if card.current_accepted_revision_id is not None:
            raise InvalidStateTransitionError("new thesis card cannot already have an accepted revision")
        if proposal.thesis_id != card.thesis_id or proposal.version != 1:
            raise InvalidStateTransitionError("initial proposal must be version 1 for the new thesis")
        if proposal.revision_type is not RevisionType.AGENT_PROPOSAL or proposal.accepted:
            raise InvalidStateTransitionError("initial revision must be an unaccepted agent proposal")
        if proposal.based_on_snapshot_id != card.created_from_snapshot_id:
            raise InvalidStateTransitionError("card and proposal must reference the same initial snapshot")
        if review is not None and review.proposal_revision_id != proposal.revision_id:
            raise InvalidStateTransitionError("review must belong to the pending proposal")

    def _insert_review(
        self,
        connection: sqlite3.Connection,
        review: ProposalReviewRecord,
    ) -> None:
        connection.execute(
            "INSERT INTO proposal_reviews(review_id, proposal_revision_id, payload) VALUES (?, ?, ?)",
            (str(review.review_id), str(review.proposal_revision_id), self._json(review)),
        )

    def create_thesis_bundle(
        self,
        snapshot: MarketSnapshot,
        card: ThesisCard,
        proposal: ThesisRevision,
        review: ProposalReviewRecord | None = None,
    ) -> tuple[ThesisCard, ThesisRevision, ProposalReviewRecord | None]:
        """Persist snapshot, initial card, proposal and optional review in one transaction."""
        self._validate_initial_bundle(card, proposal, review)
        if proposal.based_on_snapshot_id != snapshot.snapshot_id:
            raise InvalidStateTransitionError("proposal must reference the bundled snapshot")
        try:
            with self._transaction() as connection:
                self._ensure_snapshot(connection, snapshot)
                connection.execute(
                    """INSERT INTO thesis_cards(
                           thesis_id, instrument_id, lifecycle_status, created_from_snapshot_id,
                           current_accepted_revision_id, payload
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(card.thesis_id), card.instrument.instrument_id, card.lifecycle_status.value,
                        str(card.created_from_snapshot_id), None, self._json(card),
                    ),
                )
                self._insert_revision(connection, proposal)
                if review is not None:
                    self._insert_review(connection, review)
        except sqlite3.IntegrityError as exc:
            if "uq_active_instrument" in str(exc) or "thesis_cards.instrument_id" in str(exc):
                raise DuplicateActiveThesisError(
                    f"an active or draft thesis already exists for {card.instrument.instrument_id}"
                ) from exc
            raise RepositoryError(f"could not create thesis bundle: {exc}") from exc
        return card, proposal, review

    def add_update_bundle(
        self,
        snapshot: MarketSnapshot,
        proposal: ThesisRevision,
        review: ProposalReviewRecord | None = None,
    ) -> tuple[ThesisRevision, ProposalReviewRecord | None]:
        """Persist a dated snapshot and its validated update proposal atomically."""
        if proposal.revision_type is not RevisionType.AGENT_PROPOSAL or proposal.accepted:
            raise InvalidStateTransitionError("only an unaccepted agent proposal may be added")
        if proposal.based_on_snapshot_id != snapshot.snapshot_id:
            raise InvalidStateTransitionError("proposal must reference the bundled snapshot")
        if review is not None and review.proposal_revision_id != proposal.revision_id:
            raise InvalidStateTransitionError("review must belong to the update proposal")
        try:
            with self._transaction() as connection:
                card_row = connection.execute(
                    "SELECT payload FROM thesis_cards WHERE thesis_id = ?",
                    (str(proposal.thesis_id),),
                ).fetchone()
                if card_row is None:
                    raise NotFoundError(f"thesis not found: {proposal.thesis_id}")
                card = ThesisCard.model_validate_json(card_row["payload"])
                if card.lifecycle_status not in (ThesisLifecycleStatus.DRAFT, ThesisLifecycleStatus.ACTIVE):
                    raise InvalidStateTransitionError("only DRAFT or ACTIVE theses can receive an update proposal")
                if card.current_accepted_revision_id != proposal.derived_from_revision_id:
                    raise InvalidStateTransitionError("proposal must derive from the current accepted revision")
                if proposal.version != self._next_version_in(connection, proposal.thesis_id):
                    raise InvalidStateTransitionError("proposal version must be the next version in the thesis chain")
                self._ensure_snapshot(connection, snapshot)
                self._insert_revision(connection, proposal)
                if review is not None:
                    self._insert_review(connection, review)
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"could not add update bundle: {exc}") from exc
        return proposal, review

    def get_snapshot(self, snapshot_id: UUID) -> MarketSnapshot:
        row = self._connection.execute(
            "SELECT payload FROM market_snapshots WHERE snapshot_id = ?", (str(snapshot_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"snapshot not found: {snapshot_id}")
        return MarketSnapshot.model_validate_json(row["payload"])

    def _insert_tool_invocation(
        self,
        connection: sqlite3.Connection,
        invocation: ToolInvocation,
    ) -> None:
        connection.execute(
            """INSERT INTO tool_invocations(
                   invocation_id, llm_tool_call_id, tool_name, status, snapshot_id, payload
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(invocation.invocation_id),
                invocation.llm_tool_call_id,
                invocation.tool_name.value,
                invocation.status.value,
                str(invocation.snapshot_id) if invocation.snapshot_id else None,
                self._json(invocation),
            ),
        )

    def save_tool_invocation(self, invocation: ToolInvocation) -> ToolInvocation:
        """Append a terminal invocation; successful events require an existing snapshot."""
        try:
            with self._transaction() as connection:
                if invocation.snapshot_id is not None:
                    row = connection.execute(
                        "SELECT 1 FROM market_snapshots WHERE snapshot_id = ?",
                        (str(invocation.snapshot_id),),
                    ).fetchone()
                    if row is None:
                        raise RepositoryError(
                            f"successful tool invocation snapshot is not persisted: {invocation.snapshot_id}"
                        )
                self._insert_tool_invocation(connection, invocation)
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"could not save tool invocation: {exc}") from exc
        return invocation

    def save_snapshot_tool_invocation(
        self,
        snapshot: MarketSnapshot,
        invocation: ToolInvocation,
    ) -> ToolInvocation:
        """Atomically persist/reuse a snapshot and its successful tool invocation."""
        if invocation.status.value != "succeeded" or invocation.snapshot_id != snapshot.snapshot_id:
            raise InvalidStateTransitionError(
                "snapshot bundle requires a successful invocation referencing that snapshot"
            )
        try:
            with self._transaction() as connection:
                self._ensure_snapshot(connection, snapshot)
                self._insert_tool_invocation(connection, invocation)
        except sqlite3.IntegrityError as exc:
            raise RepositoryError(f"could not save snapshot tool invocation: {exc}") from exc
        return invocation

    def get_tool_invocation(self, invocation_id: UUID) -> ToolInvocation:
        row = self._connection.execute(
            "SELECT payload FROM tool_invocations WHERE invocation_id = ?",
            (str(invocation_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"tool invocation not found: {invocation_id}")
        return ToolInvocation.model_validate_json(row["payload"])

    def list_tool_invocations(self) -> list[ToolInvocation]:
        rows = self._connection.execute(
            "SELECT payload FROM tool_invocations ORDER BY rowid"
        ).fetchall()
        return [ToolInvocation.model_validate_json(row["payload"]) for row in rows]

    def _insert_revision(self, connection: sqlite3.Connection, revision: ThesisRevision) -> None:
        connection.execute(
            """INSERT INTO thesis_revisions(
                   revision_id, thesis_id, based_on_snapshot_id, derived_from_revision_id,
                   revision_type, version, accepted, payload
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(revision.revision_id), str(revision.thesis_id), str(revision.based_on_snapshot_id),
                str(revision.derived_from_revision_id) if revision.derived_from_revision_id else None,
                revision.revision_type.value, revision.version, int(revision.accepted), self._json(revision),
            ),
        )

    def get_card(self, thesis_id: UUID) -> ThesisCard:
        row = self._connection.execute(
            "SELECT payload FROM thesis_cards WHERE thesis_id = ?", (str(thesis_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"thesis not found: {thesis_id}")
        return ThesisCard.model_validate_json(row["payload"])

    def get_revision(self, revision_id: UUID) -> ThesisRevision:
        row = self._connection.execute(
            "SELECT payload FROM thesis_revisions WHERE revision_id = ?", (str(revision_id),)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"revision not found: {revision_id}")
        return ThesisRevision.model_validate_json(row["payload"])

    def list_revisions(self, thesis_id: UUID) -> list[ThesisRevision]:
        rows = self._connection.execute(
            "SELECT payload FROM thesis_revisions WHERE thesis_id = ? ORDER BY version", (str(thesis_id),)
        ).fetchall()
        return [ThesisRevision.model_validate_json(row["payload"]) for row in rows]

    def list_decisions(self, thesis_id: UUID) -> list[DecisionEvent]:
        rows = self._connection.execute(
            "SELECT payload FROM decision_events WHERE thesis_id = ? ORDER BY rowid", (str(thesis_id),)
        ).fetchall()
        return [DecisionEvent.model_validate_json(row["payload"]) for row in rows]

    def get_proposal_review(self, proposal_revision_id: UUID) -> ProposalReviewRecord:
        row = self._connection.execute(
            "SELECT payload FROM proposal_reviews WHERE proposal_revision_id = ?",
            (str(proposal_revision_id),),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"proposal review not found: {proposal_revision_id}")
        return ProposalReviewRecord.model_validate_json(row["payload"])

    def list_pending_proposals(self, thesis_id: UUID | None = None) -> list[ThesisRevision]:
        parameters: tuple[str, ...] = ()
        thesis_filter = ""
        if thesis_id is not None:
            thesis_filter = "AND r.thesis_id = ?"
            parameters = (str(thesis_id),)
        rows = self._connection.execute(
            f"""SELECT r.payload
                FROM thesis_revisions AS r
                LEFT JOIN decision_events AS d ON d.proposal_revision_id = r.revision_id
                WHERE r.revision_type = 'agent_proposal'
                  AND r.accepted = 0
                  AND d.decision_id IS NULL
                  {thesis_filter}
                ORDER BY r.rowid""",
            parameters,
        ).fetchall()
        return [ThesisRevision.model_validate_json(row["payload"]) for row in rows]

    def list_active_cards(self) -> list[ThesisCard]:
        rows = self._connection.execute(
            "SELECT payload FROM thesis_cards WHERE lifecycle_status IN ('draft', 'active') ORDER BY rowid"
        ).fetchall()
        return [ThesisCard.model_validate_json(row["payload"]) for row in rows]

    def find_active_card_by_instrument(self, instrument_id: str) -> ThesisCard | None:
        row = self._connection.execute(
            """SELECT payload FROM thesis_cards
               WHERE instrument_id = ? AND lifecycle_status IN ('draft', 'active')""",
            (instrument_id,),
        ).fetchone()
        return ThesisCard.model_validate_json(row["payload"]) if row else None

    def next_version(self, thesis_id: UUID) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM thesis_revisions WHERE thesis_id = ?",
            (str(thesis_id),),
        ).fetchone()
        return int(row["next_version"])

    def get_current_accepted_revision(self, thesis_id: UUID) -> ThesisRevision | None:
        card = self.get_card(thesis_id)
        if card.current_accepted_revision_id is None:
            return None
        return self.get_revision(card.current_accepted_revision_id)

    def review_proposal(
        self,
        proposal_revision_id: UUID,
        decision: ReviewDecision,
        *,
        changes: RevisionChanges | None = None,
        user_comment: str | None = None,
    ) -> tuple[DecisionEvent, ThesisRevision | None]:
        proposal = self.get_revision(proposal_revision_id)
        if proposal.revision_type is not RevisionType.AGENT_PROPOSAL or proposal.accepted:
            raise InvalidStateTransitionError("only an unaccepted agent proposal can be reviewed")
        if decision is ReviewDecision.MODIFY and changes is None:
            raise InvalidStateTransitionError("MODIFY requires structured revision changes")
        if decision is not ReviewDecision.MODIFY and changes is not None:
            raise InvalidStateTransitionError("structured changes are only valid for MODIFY")
        card = self.get_card(proposal.thesis_id)
        resulting_revision: ThesisRevision | None = None

        with self._transaction() as connection:
            already_reviewed = connection.execute(
                "SELECT 1 FROM decision_events WHERE proposal_revision_id = ?", (str(proposal_revision_id),)
            ).fetchone()
            if already_reviewed:
                raise InvalidStateTransitionError("proposal has already been reviewed")

            if decision is ReviewDecision.ACCEPT:
                accepted_data = proposal.model_dump()
                accepted_data["accepted"] = True
                accepted_proposal = ThesisRevision.model_validate(accepted_data)
                self._update_revision(connection, accepted_proposal)
                lifecycle = self._accepted_lifecycle(card, proposal)
                updated_card = card.model_copy(
                    update={
                        "current_accepted_revision_id": proposal.revision_id,
                        "lifecycle_status": lifecycle,
                    }
                )
                self._update_card(connection, updated_card)
            elif decision is ReviewDecision.MODIFY:
                assert changes is not None
                change_data = changes.model_dump(exclude_none=True)
                user_revision_data = proposal.model_dump()
                user_revision_data.update(
                    {
                        **change_data,
                        "revision_id": uuid4(),
                        "derived_from_revision_id": proposal.revision_id,
                        "revision_type": RevisionType.USER_REVISION,
                        "version": self._next_version_in(connection, proposal.thesis_id),
                        "accepted": True,
                    }
                )
                resulting_revision = ThesisRevision.model_validate(user_revision_data)
                self._insert_revision(connection, resulting_revision)
                lifecycle = self._accepted_lifecycle(card, resulting_revision)
                updated_card = card.model_copy(
                    update={
                        "current_accepted_revision_id": resulting_revision.revision_id,
                        "lifecycle_status": lifecycle,
                    }
                )
                self._update_card(connection, updated_card)
            else:
                lifecycle = (
                    ThesisLifecycleStatus.REJECTED
                    if card.current_accepted_revision_id is None
                    else card.lifecycle_status
                )
                updated_card = card.model_copy(update={"lifecycle_status": lifecycle})
                self._update_card(connection, updated_card)

            event = DecisionEvent(
                decision_id=uuid4(),
                thesis_id=proposal.thesis_id,
                proposal_revision_id=proposal.revision_id,
                decision=decision,
                resulting_revision_id=resulting_revision.revision_id if resulting_revision else None,
                user_comment=user_comment,
                created_at=datetime.now(UTC),
            )
            connection.execute(
                """INSERT INTO decision_events(
                       decision_id, thesis_id, proposal_revision_id, resulting_revision_id, decision, payload
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(event.decision_id), str(event.thesis_id), str(event.proposal_revision_id),
                    str(event.resulting_revision_id) if event.resulting_revision_id else None,
                    event.decision.value, self._json(event),
                ),
            )
        return event, resulting_revision

    @staticmethod
    def _accepted_lifecycle(card: ThesisCard, revision: ThesisRevision) -> ThesisLifecycleStatus:
        if revision.proposed_lifecycle_status is not None:
            return revision.proposed_lifecycle_status
        if card.lifecycle_status is ThesisLifecycleStatus.DRAFT:
            return ThesisLifecycleStatus.ACTIVE
        return card.lifecycle_status

    @staticmethod
    def _next_version_in(connection: sqlite3.Connection, thesis_id: UUID) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM thesis_revisions WHERE thesis_id = ?",
            (str(thesis_id),),
        ).fetchone()
        return int(row["next_version"])

    def _update_revision(self, connection: sqlite3.Connection, revision: ThesisRevision) -> None:
        connection.execute(
            "UPDATE thesis_revisions SET accepted = ?, payload = ? WHERE revision_id = ?",
            (int(revision.accepted), self._json(revision), str(revision.revision_id)),
        )

    def _update_card(self, connection: sqlite3.Connection, card: ThesisCard) -> None:
        connection.execute(
            """UPDATE thesis_cards
               SET lifecycle_status = ?, current_accepted_revision_id = ?, payload = ?
               WHERE thesis_id = ?""",
            (
                card.lifecycle_status.value,
                str(card.current_accepted_revision_id) if card.current_accepted_revision_id else None,
                self._json(card),
                str(card.thesis_id),
            ),
        )
