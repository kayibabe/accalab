from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from accalab.agents.evaluation import (
    AgentEvaluationCase,
    evaluate_agent_impact,
    load_agent_evaluation,
)
from accalab.db import Base, make_engine
from accalab.models import AgentLog, Fixture, MarketRegistry, OddsSnapshot, Prediction, Team
from accalab.agents.pipeline import (
    AgentAssessment,
    AgentInput,
    AgentSettings,
    apply_agents,
    load_agent_settings,
)


class CapturingSession:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


class FakeClient:
    def __init__(self, assessments):
        self.assessments = iter(assessments)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        assessment = next(self.assessments)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input=assessment)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
            model_dump=lambda mode="json": {
                "id": "msg_test",
                "content": [{"type": "tool_use", "input": assessment}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )


def settings(*, enabled=True, active=None):
    return AgentSettings(
        enabled=enabled,
        model="claude-sonnet-5-5",
        max_tokens=180,
        input_cost_per_million_usd=2,
        output_cost_per_million_usd=10,
        max_context_characters=12000,
        enabled_agents={
            name: (active is None or name in active)
            for name in ("DataQuality", "NewsContext", "Market", "Critic")
        },
    )


def test_agents_use_validated_outputs_bound_total_adjustment_and_log_calls() -> None:
    client = FakeClient(
        [
            {"adjustment": 0.0, "veto": False, "reason": "Inputs are complete."},
            {"adjustment": 0.04, "veto": False, "reason": "The supplied team update is relevant."},
            {"adjustment": 0.04, "veto": False, "reason": "The quote remains usable."},
            {"adjustment": 0.0, "veto": True, "reason": "The supplied sources conflict."},
        ]
    )
    session = CapturingSession()

    result = apply_agents(
        session,
        fixture_id=123,
        prediction_id=456,
        market_id=1,
        market="1X2",
        selection="Home",
        probability=0.60,
        p_low=0.54,
        decimal_odds=1.9,
        bookmaker_id=8,
        evidence={"news": "Club update", "api_key": "remove-me", "quality": {"fresh": True}},
        settings=settings(),
        client=client,
    )

    assert result.adjustment == pytest.approx(0.05)
    assert result.probability == pytest.approx(0.65)
    assert result.p_low == pytest.approx(0.59)
    assert result.vetoed
    assert len(session.rows) == len(client.calls) == 4
    assert all(row.status == "ok" for row in session.rows)
    assert session.rows[0].cost_usd == Decimal("0.000700")
    assert session.rows[0].input_tokens == 100
    assert len(result.why_this_pick) <= 140
    request_payload = session.rows[0].input_data["request_payload"]
    assert "api_key" not in request_payload["evidence"]
    assert "record_assessment" == client.calls[0]["tool_choice"]["name"]


def test_disabled_agents_do_not_call_provider_or_create_call_logs() -> None:
    session = CapturingSession()
    client = FakeClient([])

    result = apply_agents(
        session,
        fixture_id=1,
        prediction_id=None,
        market_id=1,
        market="1X2",
        selection="Draw",
        probability=0.32,
        p_low=0.27,
        settings=settings(enabled=False),
        client=client,
    )

    assert result.adjustment == 0
    assert result.probability == pytest.approx(0.32)
    assert not result.vetoed
    assert not client.calls
    assert not session.rows


def test_agent_configuration_defaults_to_no_provider_calls() -> None:
    config = load_agent_settings()

    assert not config.enabled
    assert not any(config.enabled_agents.values())


def test_veto_only_agent_cannot_change_probabilities_and_invalid_response_is_logged() -> None:
    client = FakeClient([{"adjustment": 0.03, "veto": True, "reason": "Unsupported change."}])
    session = CapturingSession()

    result = apply_agents(
        session,
        fixture_id=1,
        prediction_id=None,
        market_id=1,
        market="1X2",
        selection="Away",
        probability=0.25,
        p_low=0.20,
        settings=settings(active={"Critic"}),
        client=client,
    )

    assert result.probability == pytest.approx(0.25)
    assert result.vetoed
    assert result.failed_agents == ("Critic",)
    assert session.rows[0].status == "error"
    assert session.rows[0].output_data["provider_response"]["id"] == "msg_test"


def test_agent_input_and_assessment_reject_invalid_probabilities_and_bounds() -> None:
    with pytest.raises(ValidationError):
        AgentInput(
            agent_name="Market",
            market="1X2",
            selection="Home",
            probability=0.5,
            p_low=0.6,
        )
    with pytest.raises(ValidationError):
        AgentAssessment(adjustment=0.051, veto=False, reason="Too large")


def test_with_without_evaluation_reports_sample_brier_log_loss_and_clv() -> None:
    report = evaluate_agent_impact(
        [
            AgentEvaluationCase(0.55, 0.60, 1, 0.04),
            AgentEvaluationCase(0.70, 0.65, 0, -0.02, vetoed=True),
        ]
    )

    assert report["sample_size"] == 2
    assert report["with_agents"]["brier"] < report["without_agents"]["brier"]
    assert report["with_agents"]["log_loss"] < report["without_agents"]["log_loss"]
    assert report["with_agents"]["mean_clv"] == report["without_agents"]["mean_clv"]
    assert report["veto_count"] == 1


def test_database_agent_evaluation_pairs_calls_and_uses_closing_price() -> None:
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    kickoff = datetime(2026, 4, 10, 12, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all([Team(id=1, name="Home"), Team(id=2, name="Away")])
        session.flush()
        session.add(
            Fixture(
                id=100,
                league_id=39,
                league_name="League",
                fixture_date=kickoff,
                kickoff_at=kickoff,
                home_team_id=1,
                away_team_id=2,
                status="FT",
                home_goals=2,
                away_goals=1,
            )
        )
        session.add(
            MarketRegistry(
                id=1,
                canonical_name="1X2",
                display_name="Match Winner",
                supported=True,
                ticket_eligible=True,
            )
        )
        session.flush()
        session.add(
            Prediction(
                id=500,
                fixture_id=100,
                market_id=1,
                selection="Home",
                probability=0.55,
                p_low=0.5,
                as_of=datetime(2026, 4, 10, 9, tzinfo=UTC),
                model_version="baseline-v1",
                why=None,
            )
        )
        session.flush()
        session.add_all(
            [
                OddsSnapshot(
                    fixture_id=100,
                    bookmaker_id=7,
                    bookmaker_name="Book",
                    market_id=1,
                    market_name="Match Winner",
                    selection="Home",
                    odds=2.0,
                    as_of=datetime(2026, 4, 10, 9, tzinfo=UTC),
                ),
                OddsSnapshot(
                    fixture_id=100,
                    bookmaker_id=7,
                    bookmaker_name="Book",
                    market_id=1,
                    market_name="Match Winner",
                    selection="Home",
                    odds=1.8,
                    as_of=datetime(2026, 4, 10, 11, tzinfo=UTC),
                ),
            ]
        )
        session.add_all(
            [
                AgentLog(
                    agent_name="NewsContext",
                    fixture_id=100,
                    prediction_id=500,
                    market_id=1,
                    selection="Home",
                    as_of=datetime(2026, 4, 10, 9, tzinfo=UTC),
                    input_data={},
                    output_data={},
                    baseline_probability=0.55,
                    adjusted_probability=0.60,
                    reference_odds=2.0,
                    reference_bookmaker_id=7,
                    status="ok",
                ),
                AgentLog(
                    agent_name="Critic",
                    fixture_id=100,
                    prediction_id=500,
                    market_id=1,
                    selection="Home",
                    as_of=datetime(2026, 4, 10, 9, tzinfo=UTC),
                    input_data={},
                    output_data={},
                    baseline_probability=0.55,
                    adjusted_probability=0.60,
                    reference_odds=2.0,
                    reference_bookmaker_id=7,
                    status="ok",
                    veto_reason="Evidence conflict",
                ),
            ]
        )
        session.commit()
        report = load_agent_evaluation(session)

    engine.dispose()
    assert report["sample_size"] == 1
    assert report["veto_count"] == 1
    assert report["with_agents"]["brier"] < report["without_agents"]["brier"]
    assert report["with_agents"]["mean_clv"] == pytest.approx(2 / 1.8 - 1)
