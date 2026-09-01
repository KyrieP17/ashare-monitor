from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from uuid import UUID

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .gate3_models import ToolInvocation, ToolInvocationStatus, ToolName
from .gate3_tools import ReadOnlyMarketTools
from .models import InstrumentRef, MarketSnapshot, ThesisRevision


DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


class OpenAIProviderError(RuntimeError):
    status_category: str | None = None
    retryable: bool = False
    retry_after: str | None = None
    request_id: str | None = None

    def __init__(
        self,
        message: str,
        *,
        status_category: str | None = None,
        retryable: bool = False,
        retry_after: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_category = status_category
        self.retryable = retryable
        self.retry_after = retry_after
        self.request_id = request_id


class OpenAIAuthenticationError(OpenAIProviderError):
    pass


class OpenAICredentialsError(OpenAIAuthenticationError):
    """Backward-compatible name for missing local credentials."""


class OpenAIRateLimitError(OpenAIProviderError):
    pass


class OpenAIQuotaError(OpenAIRateLimitError):
    pass


class OpenAIProviderTimeoutError(OpenAIProviderError):
    pass


class OpenAIResponseFormatError(OpenAIProviderError):
    pass


class OpenAIToolLimitError(OpenAIProviderError):
    pass


class OpenAIRequiredEvidenceError(OpenAIProviderError):
    pass


class ToolArgumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetMarketSnapshotArguments(ToolArgumentModel):
    trade_date: date
    symbols: list[str] | None


class GetStockObservationArguments(ToolArgumentModel):
    instrument_id: str
    trade_date: date
    lookback_days: int = Field(ge=1)


class GetSectorObservationsArguments(ToolArgumentModel):
    instrument_id: str
    trade_date: date


class GetFundFlowObservationsArguments(ToolArgumentModel):
    trade_date: date
    instrument_id: str | None
    sector_name: str | None

    @model_validator(mode="after")
    def require_scope(self) -> GetFundFlowObservationsArguments:
        if self.instrument_id is None and self.sector_name is None:
            raise ValueError("instrument_id or sector_name is required")
        return self


class GetCatalystContextArguments(ToolArgumentModel):
    instrument_id: str
    trade_date: date


_TOOL_ARGUMENT_MODELS: dict[ToolName, type[ToolArgumentModel]] = {
    ToolName.GET_MARKET_SNAPSHOT: GetMarketSnapshotArguments,
    ToolName.GET_STOCK_OBSERVATION: GetStockObservationArguments,
    ToolName.GET_SECTOR_OBSERVATIONS: GetSectorObservationsArguments,
    ToolName.GET_FUND_FLOW_OBSERVATIONS: GetFundFlowObservationsArguments,
    ToolName.GET_CATALYST_CONTEXT: GetCatalystContextArguments,
}


_TOOL_DESCRIPTIONS: dict[ToolName, str] = {
    ToolName.GET_MARKET_SNAPSHOT: (
        "Return a structured, dated market snapshot for the explicitly bounded symbol coverage."
    ),
    ToolName.GET_STOCK_OBSERVATION: (
        "Return the target stock's structured observation for one trading day. "
        "The current adapter only supports lookback_days=1."
    ),
    ToolName.GET_SECTOR_OBSERVATIONS: (
        "Return sourced sector or theme observations for the target stock."
    ),
    ToolName.GET_FUND_FLOW_OBSERVATIONS: (
        "Return sourced fund-flow observations for the target stock or one named sector."
    ),
    ToolName.GET_CATALYST_CONTEXT: (
        "Return the persisted limit-up-pool reason/theme text as a sourced catalyst hypothesis. "
        "MISSING means no text was collected; never infer or invent a catalyst."
    ),
}


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize Pydantic JSON Schema to OpenAI strict-schema object rules."""

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            key: visit(item)
            for key, item in value.items()
            if key != "default"
        }
        if result.get("type") == "object" or "properties" in result:
            properties = result.get("properties", {})
            result["additionalProperties"] = False
            result["required"] = list(properties)
        return result

    return visit(schema)


def openai_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool_name.value,
            "description": _TOOL_DESCRIPTIONS[tool_name],
            "parameters": _strict_schema(argument_model.model_json_schema()),
            "strict": True,
        }
        for tool_name, argument_model in _TOOL_ARGUMENT_MODELS.items()
    ]


def openai_proposal_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "thesis_revision",
        "description": "A complete, unaccepted ThesisRevision proposal for human review.",
        "schema": _strict_schema(ThesisRevision.model_json_schema()),
        "strict": True,
    }


class OpenAIResponseTransport(Protocol):
    def create_response(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]: ...


class RequestsOpenAITransport:
    """Minimal OpenAI Responses API transport; the API key never enters payloads or logs."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str = OPENAI_RESPONSES_URL,
        session: requests.Session | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_key:
            raise OpenAICredentialsError(
                "OPENAI_API_KEY is not set; live OpenAI requests are unavailable"
            )
        self._api_key = resolved_key
        self.endpoint = endpoint
        self.session = session or requests.Session()

    def create_response(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout_seconds,
            )
        except requests.Timeout as exc:
            raise OpenAIProviderTimeoutError(
                f"OpenAI provider timeout; retryable=yes; timeout={timeout_seconds:g}s",
                status_category="timeout",
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise OpenAIProviderError(f"OpenAI Responses API request failed: {type(exc).__name__}") from exc

        if response.status_code >= 400:
            request_id = response.headers.get("x-request-id")
            retry_after = response.headers.get("Retry-After")
            details = [f"HTTP {response.status_code}"]
            if retry_after:
                details.append(f"Retry-After={retry_after}")
            if request_id:
                details.append(f"request_id={request_id}")
            if response.status_code in (401, 403):
                raise OpenAIAuthenticationError(
                    f"OpenAI authentication failed; {'; '.join(details)}; retryable=no",
                    status_category="authentication",
                    retryable=False,
                    request_id=request_id,
                )
            if response.status_code == 429:
                error_code = None
                try:
                    error_body = response.json()
                    if isinstance(error_body, dict) and isinstance(error_body.get("error"), dict):
                        value = error_body["error"].get("code")
                        error_code = value if isinstance(value, str) else None
                except ValueError:
                    pass
                error_class = (
                    OpenAIQuotaError
                    if error_code in {"insufficient_quota", "billing_hard_limit_reached"}
                    else OpenAIRateLimitError
                )
                category = "quota" if error_class is OpenAIQuotaError else "rate_limit"
                raise error_class(
                    f"OpenAI {category}; {'; '.join(details)}; retryable="
                    f"{'no' if category == 'quota' else 'yes'}",
                    status_category=category,
                    retryable=category != "quota",
                    retry_after=retry_after,
                    request_id=request_id,
                )
            raise OpenAIProviderError(
                f"OpenAI provider error; {'; '.join(details)}; retryable="
                f"{'yes' if response.status_code >= 500 else 'no'}",
                status_category=f"http_{response.status_code // 100}xx",
                retryable=response.status_code >= 500,
                retry_after=retry_after,
                request_id=request_id,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenAIResponseFormatError("OpenAI response was not valid JSON") from exc
        if not isinstance(body, dict):
            raise OpenAIResponseFormatError("OpenAI response must be a JSON object")
        return body


@dataclass(frozen=True)
class OpenAIResearchRequest:
    thesis_id: UUID
    instrument: InstrumentRef
    trade_date: date
    version: int = 1
    derived_from_revision_id: UUID | None = None


@dataclass
class OpenAIGenerationResult:
    raw_proposal: dict[str, Any]
    snapshot: MarketSnapshot
    tool_invocations: list[ToolInvocation]
    response_ids: list[str]
    tool_rounds: int
    usage: dict[str, int] = field(default_factory=dict)
    latency_seconds: float = 0.0


def _function_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response.get("output")
    if not isinstance(output, list):
        raise OpenAIResponseFormatError("OpenAI response output must be a list")
    calls: list[dict[str, Any]] = []
    for item in output:
        if isinstance(item, dict) and item.get("type") == "function_call":
            if not all(isinstance(item.get(key), str) for key in ("call_id", "name", "arguments")):
                raise OpenAIResponseFormatError("OpenAI function call is missing call_id, name, or arguments")
            calls.append(item)
    return calls


def _output_text(response: dict[str, Any]) -> str:
    direct = response.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    output = response.get("output")
    if not isinstance(output, list):
        raise OpenAIResponseFormatError("OpenAI response output must be a list")
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
    if not texts:
        raise OpenAIResponseFormatError("OpenAI response contained neither function calls nor output text")
    return "".join(texts)


def _merge_usage(total: dict[str, int], response: dict[str, Any]) -> None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


class OpenAIProviderAdapter:
    """Bounded OpenAI function-calling loop over the four existing read-only tools."""

    kind = "openai-live"

    def __init__(
        self,
        transport: OpenAIResponseTransport,
        tools: ReadOnlyMarketTools,
        *,
        model: str = DEFAULT_OPENAI_MODEL,
        timeout_seconds: float = 45.0,
        max_tool_rounds: int = 2,
        max_tool_invocations: int = 6,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= max_tool_rounds <= 2:
            raise ValueError("max_tool_rounds must be between 1 and 2")
        if not 1 <= max_tool_invocations <= 6:
            raise ValueError("max_tool_invocations must be between 1 and 6")
        self.transport = transport
        self.tools = tools
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tool_rounds = max_tool_rounds
        self.max_tool_invocations = max_tool_invocations

    def generate(self, request: OpenAIResearchRequest) -> OpenAIGenerationResult:
        started = time.monotonic()
        invocation_start = len(self.tools.repository.list_tool_invocations())
        response_ids: list[str] = []
        usage: dict[str, int] = {}
        tool_rounds = 0
        tool_count = 0
        previous_response_id: str | None = None
        pending_input: str | list[dict[str, Any]] = self._user_prompt(request)

        while True:
            payload: dict[str, Any] = {
                "model": self.model,
                "instructions": self._instructions(request),
                "input": pending_input,
                "tools": openai_tool_schemas(),
                "tool_choice": "auto",
                "parallel_tool_calls": True,
                "text": {"format": openai_proposal_format()},
                "max_output_tokens": 8000,
            }
            if previous_response_id is not None:
                payload["previous_response_id"] = previous_response_id
            try:
                response = self.transport.create_response(
                    payload,
                    timeout_seconds=self.timeout_seconds,
                )
            except OpenAIProviderError:
                raise
            except TimeoutError as exc:
                raise OpenAIProviderTimeoutError(
                    f"OpenAI response transport timed out after {self.timeout_seconds:g}s"
                ) from exc
            except Exception as exc:
                raise OpenAIProviderError(
                    f"OpenAI response transport failed: {type(exc).__name__}"
                ) from exc

            response_id = response.get("id")
            if not isinstance(response_id, str) or not response_id:
                raise OpenAIResponseFormatError("OpenAI response is missing its response id")
            response_ids.append(response_id)
            _merge_usage(usage, response)
            calls = _function_calls(response)
            if not calls:
                raw_proposal = self._parse_proposal(response)
                break
            if tool_rounds >= self.max_tool_rounds:
                raise OpenAIToolLimitError(
                    f"OpenAI requested more than {self.max_tool_rounds} tool-call rounds"
                )
            if tool_count + len(calls) > self.max_tool_invocations:
                raise OpenAIToolLimitError(
                    f"OpenAI requested more than {self.max_tool_invocations} tool invocations"
                )

            tool_rounds += 1
            tool_count += len(calls)
            pending_input = [self._execute_tool_call(call, request) for call in calls]
            previous_response_id = response_id

        invocations = self.tools.repository.list_tool_invocations()[invocation_start:]
        snapshot = self._required_snapshot(invocations)
        return OpenAIGenerationResult(
            raw_proposal=raw_proposal,
            snapshot=snapshot,
            tool_invocations=invocations,
            response_ids=response_ids,
            tool_rounds=tool_rounds,
            usage=usage,
            latency_seconds=time.monotonic() - started,
        )

    def _execute_tool_call(
        self,
        call: dict[str, Any],
        request: OpenAIResearchRequest,
    ) -> dict[str, Any]:
        call_id = call["call_id"]
        name = call["name"]
        try:
            tool_name = ToolName(name)
        except ValueError as exc:
            raise OpenAIResponseFormatError(f"OpenAI requested unknown tool: {name}") from exc
        try:
            raw_arguments = json.loads(call["arguments"])
            if not isinstance(raw_arguments, dict):
                raise ValueError("arguments must be a JSON object")
            arguments = _TOOL_ARGUMENT_MODELS[tool_name].model_validate(raw_arguments)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise OpenAIResponseFormatError(
                f"OpenAI supplied invalid arguments for {name}: {type(exc).__name__}"
            ) from exc

        self._enforce_research_scope(tool_name, arguments, request)
        try:
            result = self._dispatch(tool_name, arguments, call_id)
            body = {"ok": True, "result": result.model_dump(mode="json") if isinstance(result, BaseModel) else [item.model_dump(mode="json") for item in result]}
        except Exception as exc:
            body = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        }

    def _dispatch(
        self,
        tool_name: ToolName,
        arguments: ToolArgumentModel,
        call_id: str,
    ) -> Any:
        if tool_name is ToolName.GET_MARKET_SNAPSHOT:
            assert isinstance(arguments, GetMarketSnapshotArguments)
            return self.tools.get_market_snapshot(
                arguments.trade_date,
                arguments.symbols,
                llm_tool_call_id=call_id,
            )
        if tool_name is ToolName.GET_STOCK_OBSERVATION:
            assert isinstance(arguments, GetStockObservationArguments)
            return self.tools.get_stock_observation(
                arguments.instrument_id,
                arguments.trade_date,
                arguments.lookback_days,
                llm_tool_call_id=call_id,
            )
        if tool_name is ToolName.GET_SECTOR_OBSERVATIONS:
            assert isinstance(arguments, GetSectorObservationsArguments)
            return self.tools.get_sector_observations(
                arguments.instrument_id,
                arguments.trade_date,
                llm_tool_call_id=call_id,
            )
        if tool_name is ToolName.GET_FUND_FLOW_OBSERVATIONS:
            assert isinstance(arguments, GetFundFlowObservationsArguments)
            return self.tools.get_fund_flow_observations(
                arguments.trade_date,
                instrument_id=arguments.instrument_id,
                sector_name=arguments.sector_name,
                llm_tool_call_id=call_id,
            )
        assert isinstance(arguments, GetCatalystContextArguments)
        return self.tools.get_catalyst_context(
            arguments.instrument_id,
            arguments.trade_date,
            llm_tool_call_id=call_id,
        )

    @staticmethod
    def _enforce_research_scope(
        tool_name: ToolName,
        arguments: ToolArgumentModel,
        request: OpenAIResearchRequest,
    ) -> None:
        expected_id = request.instrument.instrument_id
        if getattr(arguments, "trade_date") != request.trade_date:
            raise OpenAIResponseFormatError("tool call trade_date is outside the requested research scope")
        if tool_name is ToolName.GET_MARKET_SNAPSHOT:
            assert isinstance(arguments, GetMarketSnapshotArguments)
            if arguments.symbols is not None and set(arguments.symbols) != {expected_id}:
                raise OpenAIResponseFormatError("market snapshot symbols must contain only the target stock")
            return
        instrument_id = getattr(arguments, "instrument_id", None)
        if instrument_id is not None and instrument_id != expected_id:
            raise OpenAIResponseFormatError("tool call instrument_id is outside the requested research scope")

    def _required_snapshot(self, invocations: list[ToolInvocation]) -> MarketSnapshot:
        successful = [item for item in invocations if item.status is ToolInvocationStatus.SUCCEEDED]
        by_name = {item.tool_name: item for item in successful}
        missing = [
            name.value
            for name in (
                ToolName.GET_MARKET_SNAPSHOT,
                ToolName.GET_STOCK_OBSERVATION,
                ToolName.GET_CATALYST_CONTEXT,
            )
            if name not in by_name
        ]
        if missing:
            raise OpenAIRequiredEvidenceError(
                "required typed tools did not succeed: " + ", ".join(missing)
            )
        market = by_name[ToolName.GET_MARKET_SNAPSHOT]
        stock = by_name[ToolName.GET_STOCK_OBSERVATION]
        catalyst = by_name[ToolName.GET_CATALYST_CONTEXT]
        snapshot_ids = {market.snapshot_id, stock.snapshot_id, catalyst.snapshot_id}
        if len(snapshot_ids) != 1 or market.snapshot_id is None:
            raise OpenAIRequiredEvidenceError(
                "market, stock, and catalyst tools did not resolve to the same persisted snapshot"
            )
        return self.tools.repository.get_snapshot(market.snapshot_id)

    @staticmethod
    def _parse_proposal(response: dict[str, Any]) -> dict[str, Any]:
        text = _output_text(response)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OpenAIResponseFormatError("OpenAI final proposal was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise OpenAIResponseFormatError("OpenAI final proposal must be a JSON object")
        return payload

    def _instructions(self, request: OpenAIResearchRequest) -> str:
        identity = {
            "thesis_id": str(request.thesis_id),
            "instrument_id": request.instrument.instrument_id,
            "trade_date": request.trade_date.isoformat(),
            "version": request.version,
            "derived_from_revision_id": (
                str(request.derived_from_revision_id)
                if request.derived_from_revision_id is not None
                else None
            ),
        }
        return (
            "You are a constrained A-share research generator. Use only the supplied structured tools; "
            "never invent observations or inspect raw fixture JSON. In the first tool round call "
            "get_market_snapshot, get_stock_observation, and get_catalyst_context for the exact target. "
            "A MISSING catalyst must remain missing. Sector and fund-flow "
            "tools are optional when they materially improve evidence. Every numeric factual claim must "
            "cite matching observation_ref_ids and source_refs from tool results. Unsupported content must "
            "be labeled inference, assumption, or insufficient evidence. "
            "Apply a bounded hot-money sentiment and counterparty lens when evidence permits: distinguish "
            "the stock's observable short-horizon role, market-regime fit, chip-exchange implications, and "
            "Price In risk. Never infer hidden actor intent, prescribe position size or entry price, or emit "
            "a buy, sell, hold, or order instruction. "
            "Produce one complete ThesisRevision with revision_type=agent_proposal, accepted=false, "
            "proposed_lifecycle_status=null, and a fresh "
            "revision_id. Use the snapshot_id returned by the required tools as based_on_snapshot_id. "
            "Do not alter lifecycle state or accepted pointers. Exact request identity: "
            + json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        )

    @staticmethod
    def _user_prompt(request: OpenAIResearchRequest) -> str:
        return (
            f"Research {request.instrument.instrument_id} for {request.trade_date.isoformat()} and produce "
            "a balanced, sourced Thesis proposal that is ready for human review."
        )
