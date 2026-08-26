from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

from .candidates import CandidateCard, CandidateDecision, ScanMode, ScanRun, ScanRunStatus
from .models import DataStatus
from .price_volume import PriceVolumeContext


class SQLiteCandidateRepository:
    """Candidate persistence that can share the same SQLite file as ThesisCard."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.database = str(database)
        self._connection = sqlite3.connect(self.database)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS candidate_cards (
                candidate_id TEXT PRIMARY KEY,
                trade_date TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                user_decision TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE (trade_date, instrument_id)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scan_runs (
                scan_run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                trade_date TEXT,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_scan_runs_started_at ON scan_runs(started_at DESC)"
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_scan_runs_mode_started ON scan_runs(mode, started_at DESC)"
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS price_volume_contexts (
                observation_ref_id TEXT PRIMARY KEY,
                instrument_id TEXT NOT NULL,
                end_trade_date TEXT NOT NULL,
                lookback_days INTEGER NOT NULL,
                adjustment_method TEXT NOT NULL,
                source_payload_hash TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE (
                    instrument_id,
                    end_trade_date,
                    lookback_days,
                    adjustment_method,
                    source_payload_hash
                )
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_price_volume_identity
            ON price_volume_contexts(
                instrument_id, end_trade_date, lookback_days, adjustment_method, retrieved_at DESC
            )
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteCandidateRepository:
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

    def upsert(self, cards: list[CandidateCard]) -> list[CandidateCard]:
        saved: list[CandidateCard] = []
        with self._transaction() as connection:
            for incoming in cards:
                row = connection.execute(
                    "SELECT payload FROM candidate_cards WHERE trade_date = ? AND instrument_id = ?",
                    (incoming.trade_date.isoformat(), incoming.instrument_id),
                ).fetchone()
                card = incoming if row is None else _merge(CandidateCard.model_validate_json(row["payload"]), incoming)
                connection.execute(
                    """
                    INSERT INTO candidate_cards(candidate_id, trade_date, instrument_id, user_decision, last_seen_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_date, instrument_id) DO UPDATE SET
                        candidate_id = excluded.candidate_id,
                        user_decision = excluded.user_decision,
                        last_seen_at = excluded.last_seen_at,
                        payload = excluded.payload
                    """,
                    (
                        card.candidate_id,
                        card.trade_date.isoformat(),
                        card.instrument_id,
                        card.user_decision.value,
                        card.last_seen_at.isoformat(),
                        card.model_dump_json(),
                    ),
                )
                saved.append(card)
        return saved

    def list(self, *, trade_date: date | None = None) -> list[CandidateCard]:
        if trade_date is None:
            rows = self._connection.execute(
                "SELECT payload FROM candidate_cards ORDER BY trade_date DESC, last_seen_at DESC"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT payload FROM candidate_cards WHERE trade_date = ? ORDER BY last_seen_at DESC",
                (trade_date.isoformat(),),
            ).fetchall()
        return [CandidateCard.model_validate_json(row["payload"]) for row in rows]

    def latest_trade_date(self) -> date | None:
        row = self._connection.execute("SELECT MAX(trade_date) AS value FROM candidate_cards").fetchone()
        return date.fromisoformat(row["value"]) if row and row["value"] else None

    def get(self, candidate_id: str) -> CandidateCard:
        row = self._connection.execute(
            "SELECT payload FROM candidate_cards WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return CandidateCard.model_validate_json(row["payload"])

    def set_decision(self, candidate_id: str, decision: CandidateDecision) -> CandidateCard:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM candidate_cards WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if row is None:
                raise KeyError(candidate_id)
            card = CandidateCard.model_validate_json(row["payload"]).model_copy(
                update={"user_decision": decision}
            )
            connection.execute(
                "UPDATE candidate_cards SET user_decision = ?, payload = ? WHERE candidate_id = ?",
                (decision.value, card.model_dump_json(), candidate_id),
            )
        return card

    def create_scan_run(self, scan_run: ScanRun) -> ScanRun:
        if scan_run.status is not ScanRunStatus.RUNNING:
            raise ValueError("new ScanRun must start as RUNNING")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO scan_runs(scan_run_id, started_at, completed_at, trade_date, status, mode, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_run.scan_run_id,
                    scan_run.started_at.isoformat(),
                    None,
                    None,
                    scan_run.status.value,
                    scan_run.mode.value,
                    scan_run.model_dump_json(),
                ),
            )
        return scan_run

    def update_scan_run(self, scan_run: ScanRun) -> ScanRun:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE scan_runs
                SET completed_at = ?, trade_date = ?, status = ?, mode = ?, payload = ?
                WHERE scan_run_id = ?
                """,
                (
                    scan_run.completed_at.isoformat() if scan_run.completed_at else None,
                    scan_run.trade_date.isoformat() if scan_run.trade_date else None,
                    scan_run.status.value,
                    scan_run.mode.value,
                    scan_run.model_dump_json(),
                    scan_run.scan_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(scan_run.scan_run_id)
        return scan_run

    def latest_scan_run(self, *, mode: ScanMode | None = None) -> ScanRun | None:
        if mode is None:
            row = self._connection.execute(
                "SELECT payload FROM scan_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._connection.execute(
                "SELECT payload FROM scan_runs WHERE mode = ? ORDER BY started_at DESC LIMIT 1",
                (mode.value,),
            ).fetchone()
        return ScanRun.model_validate_json(row["payload"]) if row else None

    def latest_once_scan_run(self) -> ScanRun | None:
        return self.latest_scan_run(mode=ScanMode.ONCE)

    def latest_loop_scan_run(self) -> ScanRun | None:
        return self.latest_scan_run(mode=ScanMode.LOOP)

    def latest_usable_scan_run(self) -> ScanRun | None:
        row = self._connection.execute(
            """
            SELECT payload FROM scan_runs
            WHERE status IN (?, ?)
            ORDER BY started_at DESC LIMIT 1
            """,
            (ScanRunStatus.SUCCEEDED.value, ScanRunStatus.PARTIAL.value),
        ).fetchone()
        return ScanRun.model_validate_json(row["payload"]) if row else None

    def save_price_volume_context(self, context: PriceVolumeContext) -> PriceVolumeContext:
        identity = (
            context.instrument_id,
            context.end_trade_date.isoformat(),
            context.lookback_days,
            context.adjustment_method,
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT payload FROM price_volume_contexts WHERE observation_ref_id = ?",
                (context.observation_ref_id,),
            ).fetchone()
            if existing is not None:
                return PriceVolumeContext.model_validate_json(existing["payload"])
            conflict = connection.execute(
                """
                SELECT 1 FROM price_volume_contexts
                WHERE instrument_id = ? AND end_trade_date = ? AND lookback_days = ?
                  AND adjustment_method = ? AND source_payload_hash <> ?
                LIMIT 1
                """,
                (*identity, context.source_payload_hash),
            ).fetchone()
            stored = context.model_copy(
                update={"status": DataStatus.CONFLICTED}
            ) if conflict is not None else context
            connection.execute(
                """
                INSERT INTO price_volume_contexts(
                    observation_ref_id, instrument_id, end_trade_date, lookback_days,
                    adjustment_method, source_payload_hash, retrieved_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.observation_ref_id,
                    stored.instrument_id,
                    stored.end_trade_date.isoformat(),
                    stored.lookback_days,
                    stored.adjustment_method,
                    stored.source_payload_hash,
                    stored.retrieved_at.isoformat(),
                    stored.model_dump_json(),
                ),
            )
        return stored

    def get_price_volume_context(self, observation_ref_id: str) -> PriceVolumeContext | None:
        row = self._connection.execute(
            "SELECT payload FROM price_volume_contexts WHERE observation_ref_id = ?",
            (observation_ref_id,),
        ).fetchone()
        return PriceVolumeContext.model_validate_json(row["payload"]) if row else None

    def list_price_volume_contexts(
        self,
        *,
        instrument_id: str | None = None,
        end_trade_date: date | None = None,
        lookback_days: int | None = None,
    ) -> list[PriceVolumeContext]:
        clauses: list[str] = []
        values: list[object] = []
        if instrument_id is not None:
            clauses.append("instrument_id = ?")
            values.append(instrument_id)
        if end_trade_date is not None:
            clauses.append("end_trade_date = ?")
            values.append(end_trade_date.isoformat())
        if lookback_days is not None:
            clauses.append("lookback_days = ?")
            values.append(lookback_days)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._connection.execute(
            "SELECT payload FROM price_volume_contexts" + where + " ORDER BY retrieved_at DESC",
            values,
        ).fetchall()
        contexts = [PriceVolumeContext.model_validate_json(row["payload"]) for row in rows]
        hashes_by_identity: dict[tuple[str, date, int, str], set[str]] = {}
        for item in contexts:
            identity = (item.instrument_id, item.end_trade_date, item.lookback_days, item.adjustment_method)
            hashes_by_identity.setdefault(identity, set()).add(item.source_payload_hash)
        return [
            item.model_copy(update={"status": DataStatus.CONFLICTED})
            if len(hashes_by_identity[(item.instrument_id, item.end_trade_date, item.lookback_days, item.adjustment_method)]) > 1
            else item
            for item in contexts
        ]


def _merge(existing: CandidateCard, incoming: CandidateCard) -> CandidateCard:
    observation_map = {item.observation_ref_id: item for item in existing.observations}
    observation_map.update({item.observation_ref_id: item for item in incoming.observations})
    return incoming.model_copy(
        update={
            "candidate_id": existing.candidate_id,
            "first_seen_at": existing.first_seen_at,
            "last_seen_at": max(existing.last_seen_at, incoming.last_seen_at),
            "hit_count": existing.hit_count + 1,
            "trigger_rules": list(dict.fromkeys(existing.trigger_rules + incoming.trigger_rules)),
            "source_snapshot_ids": list(dict.fromkeys(existing.source_snapshot_ids + incoming.source_snapshot_ids)),
            "source_names": list(dict.fromkeys(existing.source_names + incoming.source_names)),
            "user_decision": existing.user_decision,
            "observations": list(observation_map.values()),
        }
    )
