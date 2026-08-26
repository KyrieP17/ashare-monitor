import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from mcp.server.fastmcp import FastMCP

from .candidate_repository import SQLiteCandidateRepository
from .candidate_research_adapter import CandidateResearchAdapter
from .candidates import CandidateCard
from .gate3_models import ProposalReviewRecord
from .gate3_tools import ReadOnlyMarketTools
from .gate3_validation import HardProposalValidator
from .models import DiscoverySource, ThesisCard, ThesisLifecycleStatus, ThesisRevision
from .price_volume import PublicPriceVolumeTool
from .public_market import PublicMarketClient
from .repository import SQLiteThesisRepository
from .semantic_reviewer import RecordedSemanticReviewer
from .symbols import normalize_symbol


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "thesis.db"
SERVER_NAME = "ashare-thesis-workbench"


class MCPResearchService:
    """Interactive Claude Desktop boundary over the existing typed-tool contracts."""

    def __init__(
        self,
        database: str | Path = DEFAULT_DATABASE,
        *,
        price_client: PublicMarketClient | None = None,
    ) -> None:
        self.database = Path(database)
        self.price_client = price_client

    def get_market_snapshot(self, trade_date_value: str, symbols: list[str]) -> dict[str, Any]:
        candidate = self._candidate_for_single_symbol(trade_date_value, symbols)
        with SQLiteThesisRepository(self.database) as repository:
            tool = self._tools(candidate, repository)
            result = tool.get_market_snapshot(
                candidate.trade_date,
                [candidate.instrument_id],
                llm_tool_call_id=self._call_id(),
            )
        return result.model_dump(mode="json")

    def get_stock_observation(
        self,
        instrument_id: str,
        trade_date_value: str,
        lookback_days: int = 1,
    ) -> dict[str, Any]:
        candidate = self._candidate_for_symbol(trade_date_value, instrument_id)
        with SQLiteThesisRepository(self.database) as repository:
            result = self._tools(candidate, repository).get_stock_observation(
                candidate.instrument_id,
                candidate.trade_date,
                lookback_days,
                llm_tool_call_id=self._call_id(),
            )
        return result.model_dump(mode="json")

    def get_sector_observations(
        self,
        instrument_id: str,
        trade_date_value: str,
    ) -> list[dict[str, Any]]:
        candidate = self._candidate_for_symbol(trade_date_value, instrument_id)
        with SQLiteThesisRepository(self.database) as repository:
            result = self._tools(candidate, repository).get_sector_observations(
                candidate.instrument_id,
                candidate.trade_date,
                llm_tool_call_id=self._call_id(),
            )
        return [item.model_dump(mode="json") for item in result]

    def get_fund_flow_observations(
        self,
        trade_date_value: str,
        instrument_id: str,
        sector_name: str | None = None,
    ) -> list[dict[str, Any]]:
        candidate = self._candidate_for_symbol(trade_date_value, instrument_id)
        with SQLiteThesisRepository(self.database) as repository:
            result = self._tools(candidate, repository).get_fund_flow_observations(
                candidate.trade_date,
                instrument_id=candidate.instrument_id,
                sector_name=sector_name,
                llm_tool_call_id=self._call_id(),
            )
        return [item.model_dump(mode="json") for item in result]

    def get_price_volume_context(
        self,
        instrument_id: str,
        end_trade_date: str,
        lookback_days: int = 10,
    ) -> dict[str, Any]:
        parsed_date = self._date(end_trade_date)
        normalized = normalize_symbol(instrument_id).instrument_id
        with SQLiteCandidateRepository(self.database) as repository:
            result = PublicPriceVolumeTool(
                self.price_client or PublicMarketClient(timeout=15),
                repository,
            ).get_price_volume_context(normalized, parsed_date, lookback_days)
        return result.model_dump(mode="json")

    def submit_thesis_proposal(
        self,
        instrument_id: str,
        trade_date_value: str,
        thesis_id: str,
        proposal: dict[str, Any],
        claude_model: str,
    ) -> dict[str, Any]:
        """Validate a Claude-authored initial proposal, then persist it unaccepted."""

        generator_kind = self._generator_kind(claude_model)
        if generator_kind is None:
            return self._error(
                "invalid_model_info",
                "claude_model must identify the Claude model and cannot be empty.",
            )
        try:
            expected_thesis_id = UUID(thesis_id)
        except (TypeError, ValueError):
            return self._error("invalid_thesis_id", "thesis_id must be a valid UUID.")
        try:
            snapshot_id = UUID(str(proposal.get("based_on_snapshot_id")))
        except (TypeError, ValueError):
            return self._error(
                "invalid_snapshot_id",
                "proposal.based_on_snapshot_id must be a valid persisted snapshot UUID.",
            )

        try:
            candidate = self._candidate_for_symbol(trade_date_value, instrument_id)
        except (LookupError, ValueError):
            return self._error(
                "candidate_not_found",
                "No matching CandidateCard was found for instrument_id and trade_date.",
            )

        with SQLiteThesisRepository(self.database) as repository:
            existing = repository.find_active_card_by_instrument(candidate.instrument_id)
            if existing is not None:
                return {
                    **self._error(
                        "active_thesis_exists",
                        "An active or draft ThesisCard already exists; open the existing card instead.",
                    ),
                    "existing_thesis_id": str(existing.thesis_id),
                }
            try:
                snapshot = repository.get_snapshot(snapshot_id)
            except Exception:
                return self._error(
                    "snapshot_not_found",
                    "The referenced snapshot is not persisted. Call get_market_snapshot first.",
                )

            stocks = snapshot.stock_observations
            if (
                snapshot.trade_date != candidate.trade_date
                or len(stocks) != 1
                or stocks[0].instrument.instrument_id != candidate.instrument_id
            ):
                return self._error(
                    "snapshot_scope_mismatch",
                    "The snapshot must cover exactly the submitted candidate and trade date.",
                )

            validated, hard_result = HardProposalValidator().validate(
                proposal,
                snapshot=snapshot,
                thesis_id=expected_thesis_id,
                version=1,
                derived_from_revision_id=None,
            )
            if not hard_result.is_valid or validated is None:
                return {
                    "ok": False,
                    "status": "validation_failed",
                    "thesis_id": str(expected_thesis_id),
                    "accepted": False,
                    "issues": [item.model_dump(mode="json") for item in hard_result.issues],
                }

            semantic_review = RecordedSemanticReviewer().review(validated, snapshot)
            review = ProposalReviewRecord(
                proposal_revision_id=validated.revision_id,
                semantic_review=semantic_review,
                graph_trace=[
                    "claude_desktop",
                    "hard_validation",
                    "semantic_reviewer",
                    "ready_for_human_review",
                ],
                generator_kind=generator_kind,
                repair_count=0,
            )
            instrument = stocks[0].instrument.model_copy(
                update={"name": candidate.instrument_name}
            )
            card = ThesisCard(
                thesis_id=expected_thesis_id,
                instrument=instrument,
                lifecycle_status=ThesisLifecycleStatus.DRAFT,
                discovery_source=DiscoverySource.EXTERNAL_PLATFORM,
                discovery_note=(
                    "Claude Desktop + MCP interactive research; manually initiated by the user."
                ),
                created_from_snapshot_id=snapshot.snapshot_id,
                created_at=datetime.now(UTC),
            )
            try:
                repository.create_thesis_bundle(snapshot, card, validated, review)
            except Exception as exc:
                return self._error(
                    "persistence_failed",
                    f"The validated proposal was not persisted: {type(exc).__name__}.",
                )

        return {
            "ok": True,
            "status": "ready_for_human_review",
            "thesis_id": str(expected_thesis_id),
            "proposal_revision_id": str(validated.revision_id),
            "accepted": False,
            "generator_kind": generator_kind,
            "next_step": "Open the local 深度研究 page and choose Accept, Modify, or Reject.",
        }

    def _candidate_for_single_symbol(
        self,
        trade_date_value: str,
        symbols: list[str],
    ) -> CandidateCard:
        if len(symbols) != 1:
            raise ValueError("the current Candidate MCP adapter requires exactly one symbol")
        return self._candidate_for_symbol(trade_date_value, symbols[0])

    def _candidate_for_symbol(self, trade_date_value: str, symbol: str) -> CandidateCard:
        parsed_date = self._date(trade_date_value)
        instrument_id = normalize_symbol(symbol).instrument_id
        with SQLiteCandidateRepository(self.database) as repository:
            matching = [
                item
                for item in repository.list(trade_date=parsed_date)
                if item.instrument_id == instrument_id
            ]
        if not matching:
            raise LookupError(
                f"no CandidateCard for {instrument_id} on {parsed_date.isoformat()}"
            )
        return matching[0]

    @staticmethod
    def _tools(
        candidate: CandidateCard,
        repository: SQLiteThesisRepository,
    ) -> ReadOnlyMarketTools:
        return ReadOnlyMarketTools(
            CandidateResearchAdapter(candidate),
            repository,
            default_instruments=[candidate.instrument_id],
        )

    @staticmethod
    def _date(value: str) -> date:
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("date must use YYYY-MM-DD") from exc

    @staticmethod
    def _call_id() -> str:
        return f"claude-mcp:{uuid4()}"

    @staticmethod
    def _generator_kind(model_info: str) -> str | None:
        clean = " ".join(str(model_info).split()).strip()[:80]
        return f"claude-mcp:{clean}" if clean else None

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "rejected",
            "accepted": False,
            "error": {"code": code, "message": message},
        }


def create_mcp_server(service: MCPResearchService | None = None) -> FastMCP:
    service = service or MCPResearchService(
        os.environ.get("THESIS_DB_PATH", str(DEFAULT_DATABASE))
    )
    server = FastMCP(
        SERVER_NAME,
        instructions=(
            "Interactive, local A-share research tools for Claude Desktop. "
            "This server never starts research automatically. Read evidence first, then call "
            "submit_thesis_proposal with a complete unaccepted ThesisRevision."
        ),
    )

    @server.tool()
    def get_market_snapshot(trade_date: str, symbols: list[str]) -> dict[str, Any]:
        """Return one persisted CandidateCard as a dated typed MarketSnapshot."""
        return service.get_market_snapshot(trade_date, symbols)

    @server.tool()
    def get_stock_observation(
        instrument_id: str,
        trade_date: str,
        lookback_days: int = 1,
    ) -> dict[str, Any]:
        """Return the existing typed stock observation for one candidate and date."""
        return service.get_stock_observation(instrument_id, trade_date, lookback_days)

    @server.tool()
    def get_sector_observations(
        instrument_id: str,
        trade_date: str,
    ) -> list[dict[str, Any]]:
        """Return the candidate's sourced sector observations."""
        return service.get_sector_observations(instrument_id, trade_date)

    @server.tool()
    def get_fund_flow_observations(
        trade_date: str,
        instrument_id: str,
        sector_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return sourced fund-flow observations for the candidate or named sector."""
        return service.get_fund_flow_observations(trade_date, instrument_id, sector_name)

    @server.tool()
    def get_price_volume_context(
        instrument_id: str,
        end_trade_date: str,
        lookback_days: int = 10,
    ) -> dict[str, Any]:
        """Fetch the existing M3a deterministic 5/10-day price-volume context on demand."""
        return service.get_price_volume_context(instrument_id, end_trade_date, lookback_days)

    @server.tool()
    def submit_thesis_proposal(
        instrument_id: str,
        trade_date: str,
        thesis_id: str,
        proposal: dict[str, Any],
        claude_model: str,
    ) -> dict[str, Any]:
        """Hard-validate and persist one Claude proposal as pending; never auto-accept."""
        return service.submit_thesis_proposal(
            instrument_id,
            trade_date,
            thesis_id,
            proposal,
            claude_model,
        )

    # FastMCP derives an ordinary MCP JSON Schema from the Python signature.
    # Keep the runtime argument as a raw dict so HardProposalValidator can
    # return structured schema issues, while advertising the complete domain
    # contract to Claude. This is intentionally not OpenAI's strict wrapper.
    submit_tool = server._tool_manager.get_tool("submit_thesis_proposal")
    assert submit_tool is not None
    proposal_schema = ThesisRevision.model_json_schema()
    definitions = proposal_schema.pop("$defs", {})
    submit_tool.parameters["properties"]["proposal"] = proposal_schema
    if definitions:
        submit_tool.parameters["$defs"] = definitions

    return server


mcp = create_mcp_server()


if __name__ == "__main__":
    mcp.run(transport="stdio")
