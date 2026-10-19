"""Pydantic-validated Anthropic decisions with hard adjustment and audit bounds."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from accalab.config import ROOT, _load_dotenv
from accalab.models import AgentLog

AGENT_NAMES = ("DataQuality", "NewsContext", "Market", "Critic")
ADJUSTMENT_AGENTS = {"NewsContext", "Market"}
SYSTEM_PROMPTS = {
    "DataQuality": (
        "Check only the supplied data-quality evidence for missing, stale, conflicting, or invalid inputs. "
        "Do not infer facts. Veto when the evidence makes this forecast unsafe to use."
    ),
    "NewsContext": (
        "Use only the supplied, attributable news context. Suggest a small probability adjustment only "
        "when it directly changes the named selection; otherwise return zero. Do not invent news or facts."
    ),
    "Market": (
        "Review the supplied market, selection, price and evidence. Suggest a small adjustment only "
        "when the evidence directly supports it. Do not invent prices, probabilities, or outcomes."
    ),
    "Critic": (
        "Look for contradictions, unsupported evidence, or a serious weakness in the supplied forecast. "
        "Veto only when justified; do not change the probability."
    ),
}


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent_name: Literal["DataQuality", "NewsContext", "Market", "Critic"]
    fixture_id: int | None = None
    prediction_id: int | None = None
    market_id: int | None = None
    market: str = Field(min_length=1, max_length=160)
    selection: str = Field(min_length=1, max_length=160)
    line: float | None = None
    probability: float = Field(ge=0, le=1)
    p_low: float = Field(ge=0, le=1)
    decimal_odds: float | None = Field(default=None, gt=1)
    bookmaker_id: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @field_validator("p_low")
    @classmethod
    def lower_bound_not_above_probability(cls, value: float, info):
        probability = info.data.get("probability")
        if probability is not None and value > probability:
            raise ValueError("p_low cannot exceed the model probability")
        return value


class AgentAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    adjustment: float = Field(ge=-0.05, le=0.05)
    veto: bool
    reason: str = Field(min_length=3, max_length=140)


class AgentResponseError(ValueError):
    def __init__(self, message: str, response=None, assessment=None):
        super().__init__(message)
        self.response = response
        self.assessment = assessment


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool
    model: str
    max_tokens: int
    input_cost_per_million_usd: float
    output_cost_per_million_usd: float
    max_context_characters: int
    enabled_agents: dict[str, bool]


@dataclass(frozen=True)
class AgentPipelineResult:
    baseline_probability: float
    probability: float
    p_low: float
    adjustment: float
    vetoed: bool
    veto_reasons: tuple[str, ...]
    why_this_pick: str
    called_agents: tuple[str, ...]
    failed_agents: tuple[str, ...]


def load_agent_settings(path=None) -> AgentSettings:
    source = path or ROOT / "config" / "agents.toml"
    raw = tomllib.loads(source.read_text(encoding="utf-8"))["agents"]
    return AgentSettings(
        enabled=bool(raw["enabled"]),
        model=str(raw["model"]),
        max_tokens=max(64, min(int(raw["max_tokens"]), 2048)),
        input_cost_per_million_usd=float(raw["input_cost_per_million_usd"]),
        output_cost_per_million_usd=float(raw["output_cost_per_million_usd"]),
        max_context_characters=max(1000, min(int(raw["max_context_characters"]), 50_000)),
        enabled_agents={
            name: bool(raw.get(name, {}).get("enabled", False)) for name in AGENT_NAMES
        },
    )


def _aware(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _safe_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_evidence(item)
            for key, item in value.items()
            if not any(
                secret in str(key).casefold()
                for secret in ("secret", "password", "api_key", "token")
            )
        }
    if isinstance(value, (list, tuple)):
        return [_safe_evidence(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _make_client():
    _load_dotenv(ROOT / ".env")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is missing; enabled agents were not called.")
    from anthropic import Anthropic

    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _usage_cost(usage, settings: AgentSettings) -> tuple[int, int, Decimal]:
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cost = (
        input_tokens * settings.input_cost_per_million_usd
        + output_tokens * settings.output_cost_per_million_usd
    ) / 1_000_000
    return input_tokens, output_tokens, Decimal(str(cost)).quantize(Decimal("0.000001"))


def _response_dump(response) -> dict:
    try:
        return response.model_dump(mode="json")
    except (AttributeError, TypeError):
        return {"content": str(getattr(response, "content", ""))}


def _request_payload(data: AgentInput, settings: AgentSettings) -> dict:
    payload = data.model_dump(mode="json")
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(serialized) > settings.max_context_characters:
        payload["evidence"] = {
            "context": "Evidence omitted because it exceeded the configured size limit."
        }
    return payload


def _call_one(client, data: AgentInput, settings: AgentSettings):
    payload = _request_payload(data, settings)
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    response = client.messages.create(
        model=settings.model,
        max_tokens=settings.max_tokens,
        system=SYSTEM_PROMPTS[data.agent_name],
        messages=[{"role": "user", "content": serialized}],
        tools=[
            {
                "name": "record_assessment",
                "description": "Return the bounded assessment for this one forecast.",
                "input_schema": AgentAssessment.model_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": "record_assessment"},
    )
    block = next(
        (item for item in response.content if getattr(item, "type", None) == "tool_use"),
        None,
    )
    if block is None:
        raise AgentResponseError(
            "The provider did not return the required assessment tool call.", response
        )
    try:
        assessment = AgentAssessment.model_validate(block.input, strict=True)
    except ValidationError as exc:
        raise AgentResponseError(f"Invalid provider assessment: {exc}", response) from exc
    if data.agent_name not in ADJUSTMENT_AGENTS and assessment.adjustment != 0:
        raise AgentResponseError(
            f"{data.agent_name} may veto but cannot adjust probabilities.", response, assessment
        )
    return assessment, response, payload


def apply_agents(
    session: Session,
    *,
    fixture_id: int | None,
    prediction_id: int | None,
    market_id: int | None,
    market: str,
    selection: str,
    probability: float,
    p_low: float,
    line: float | None = None,
    decimal_odds: float | None = None,
    bookmaker_id: int | None = None,
    evidence: dict[str, Any] | None = None,
    settings: AgentSettings | None = None,
    client=None,
    as_of: datetime | None = None,
) -> AgentPipelineResult:
    """Run enabled agents, cap the combined change to five points, and append every call log."""
    settings = settings or load_agent_settings()
    baseline = AgentInput(
        agent_name="DataQuality",
        fixture_id=fixture_id,
        prediction_id=prediction_id,
        market_id=market_id,
        market=market,
        selection=selection,
        line=line,
        probability=probability,
        p_low=p_low,
        decimal_odds=decimal_odds,
        bookmaker_id=bookmaker_id,
        evidence=_safe_evidence(evidence or {}),
    )
    enabled = tuple(
        name for name in AGENT_NAMES if settings.enabled and settings.enabled_agents[name]
    )
    if enabled and client is None:
        try:
            client = _make_client()
        except Exception as exc:
            client_error = str(exc)
            client = None
    else:
        client_error = None
    pending_logs = []
    call_results = []
    delta_sum = 0.0
    veto_reasons = []
    failed_agents = []
    for name in enabled:
        agent_input = baseline.model_copy(update={"agent_name": name})
        response = None
        assessment = None
        status = "error"
        error = client_error
        input_tokens = output_tokens = None
        cost_usd = None
        raw_response: dict = {}
        call_payload = _request_payload(agent_input, settings)
        try:
            if client is None:
                raise RuntimeError(client_error or "Anthropic client is unavailable.")
            assessment, response, call_payload = _call_one(client, agent_input, settings)
            status = "ok"
            error = None
            if name in ADJUSTMENT_AGENTS and not assessment.veto:
                delta_sum += assessment.adjustment
            if assessment.veto:
                veto_reasons.append(f"{name}: {assessment.reason}")
            raw_response = _response_dump(response)
        except Exception as exc:
            if isinstance(exc, AgentResponseError):
                response = exc.response
                assessment = exc.assessment
                if assessment and assessment.veto:
                    veto_reasons.append(f"{name}: {assessment.reason}")
            error = str(exc)[:1000]
            status = "error"
            failed_agents.append(name)
            raw_response = _response_dump(response) if response is not None else {}
        if response is not None:
            input_tokens, output_tokens, cost_usd = _usage_cost(response.usage, settings)
        delta_sum = max(-0.05, min(0.05, delta_sum))
        adjusted = max(0.0, min(1.0, probability + delta_sum))
        why = assessment.reason if assessment is not None and not assessment.veto else None
        call_results.append((name, assessment, status))
        pending_logs.append(
            {
                "agent_name": name,
                "input_data": {
                    "request_payload": call_payload,
                    "system_prompt": SYSTEM_PROMPTS[name],
                    "tool_schema": AgentAssessment.model_json_schema(),
                },
                "output_data": {
                    "provider_response": raw_response,
                    "assessment": assessment.model_dump(mode="json") if assessment else {},
                    "cost_rates_usd_per_million_tokens": {
                        "input": settings.input_cost_per_million_usd,
                        "output": settings.output_cost_per_million_usd,
                    },
                },
                "adjustment": assessment.adjustment if assessment else None,
                "veto_reason": assessment.reason if assessment and assessment.veto else None,
                "cost_usd": cost_usd,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model_id": settings.model,
                "status": status,
                "error": error,
                "adjusted_probability": adjusted,
                "why_this_pick": why,
            }
        )
    final_adjustment = max(-0.05, min(0.05, delta_sum))
    final_probability = max(0.0, min(1.0, probability + final_adjustment))
    final_p_low = min(final_probability, max(0.0, p_low + final_adjustment))
    viable_reasons = [
        assessment.reason
        for _, assessment, status in call_results
        if assessment and status == "ok" and not assessment.veto and assessment.reason
    ]
    preferred_reason = next(
        (
            assessment.reason
            for name, assessment, status in call_results
            if name in {"Market", "NewsContext"}
            and assessment
            and status == "ok"
            and not assessment.veto
        ),
        None,
    )
    why_this_pick = (
        preferred_reason
        or (viable_reasons[0] if viable_reasons else "")
        or f"Calibrated model probability: {selection} {final_probability:.0%}."
    )[:140]
    timestamp = _aware(as_of)
    for data in pending_logs:
        session.add(
            AgentLog(
                agent_name=data["agent_name"],
                fixture_id=fixture_id,
                prediction_id=prediction_id,
                market_id=market_id,
                selection=selection,
                line=Decimal(str(line)) if line is not None else None,
                as_of=timestamp,
                input_data=data["input_data"],
                output_data=data["output_data"],
                adjustment=Decimal(str(data["adjustment"]))
                if data["adjustment"] is not None
                else None,
                veto_reason=data["veto_reason"],
                cost_usd=data["cost_usd"],
                baseline_probability=Decimal(str(probability)),
                adjusted_probability=Decimal(str(final_probability)),
                reference_odds=Decimal(str(decimal_odds)) if decimal_odds is not None else None,
                reference_bookmaker_id=bookmaker_id,
                input_tokens=data["input_tokens"],
                output_tokens=data["output_tokens"],
                model_id=data["model_id"],
                status=data["status"],
                error=data["error"],
                why_this_pick=why_this_pick,
            )
        )
    return AgentPipelineResult(
        baseline_probability=probability,
        probability=final_probability,
        p_low=final_p_low,
        adjustment=final_adjustment,
        vetoed=bool(veto_reasons),
        veto_reasons=tuple(veto_reasons),
        why_this_pick=why_this_pick,
        called_agents=enabled,
        failed_agents=tuple(failed_agents),
    )
