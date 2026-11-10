from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from xueban.runtime.contracts import RuntimeRequest, WorkflowDefinition
from xueban.runtime.events import GAP_DETECTED, RunEventBus
from xueban.runtime.service import RuntimeService


def _gap_workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "workflow_id": "gap-detection-test",
            "version": "1.0",
            "name": "Gap Detection Test",
            "entrypoint": "writer",
            "nodes": [
                {
                    "id": "writer",
                    "kind": "agent",
                    "type": "section.generate",
                    "config": {
                        "role": "section-writer",
                        "message_template": "Section drafted.",
                        "gap_detection": {"baseline_key": "baseline"},
                    },
                }
            ],
            "edges": [{"from": "writer", "to": "END", "type": "final"}],
        }
    )


async def _wait_for_gap(service: RuntimeService) -> tuple[str, str]:
    for _ in range(100):
        key = next(iter(service._gap_events.keys()), None)
        if key is not None:
            return key
        await asyncio.sleep(0.01)
    raise AssertionError("gap waiter was not registered in time")


@pytest.mark.asyncio
async def test_gap_detected_event_payload_for_missing_baseline():
    bus = RunEventBus()
    service = RuntimeService(event_bus=bus)
    request = RuntimeRequest(
        workflow=_gap_workflow(),
        input={"goal": "Draft section", "experiment_log": {"baseline": None}},
    )

    run_task = asyncio.create_task(service.run(request))
    run_id, node_id = await _wait_for_gap(service)

    events = bus.get_events(run_id)
    gap_events = [event for _, event in events if getattr(event, "type", None) == GAP_DETECTED]
    assert len(gap_events) == 1
    gap_event = gap_events[0]
    assert gap_event.node_id == node_id
    assert gap_event.gap_type == "incomplete_log"
    assert [choice.id for choice in gap_event.choices] == ["A", "B", "C"]

    service.submit_gap_response(run_id, node_id, "C")
    result = await run_task
    assert result.run.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "user_input", "expected_fragment"),
    [
        ("A", "baseline=0.42", "[supplemented: baseline=0.42]"),
        ("B", None, "[comparison dropped]"),
        ("C", None, "[TODO: will be updated]"),
    ],
)
async def test_gap_response_choices_update_step_output(
    choice: str,
    user_input: str | None,
    expected_fragment: str,
):
    service = RuntimeService(event_bus=RunEventBus())
    request = RuntimeRequest(
        workflow=_gap_workflow(),
        input={"goal": "Draft section", "experiment_log": {"baseline": None}},
    )

    run_task = asyncio.create_task(service.run(request))
    run_id, node_id = await _wait_for_gap(service)
    accepted = service.submit_gap_response(run_id, node_id, choice, user_input)
    assert accepted is True

    result = await run_task
    assert result.run.status == "succeeded"
    assert expected_fragment in result.final_output["message"]
    assert result.steps[0].input["_gap_resolution"]["choice"] == choice


def test_gap_response_endpoint_returns_409_when_node_not_waiting():
    from xueban.server import app

    with TestClient(app=app) as client:
        response = client.post(
            "/workflow/runs/no-run/gap_response",
            json={"node_id": "writer", "gap_choice": "A", "user_input": "x"},
        )

    assert response.status_code == 409


def test_gap_response_endpoint_wakes_waiting_node():
    from xueban.server import app, runtime_service

    key = ("run-endpoint-gap", "writer")
    runtime_service._gap_events[key] = asyncio.Event()

    try:
        with TestClient(app=app) as client:
            response = client.post(
                "/workflow/runs/run-endpoint-gap/gap_response",
                json={"node_id": "writer", "gap_choice": "B"},
            )

        assert response.status_code == 200
        assert runtime_service._gap_responses[key]["gap_choice"] == "B"
        assert runtime_service._gap_events[key].is_set() is True
    finally:
        runtime_service._gap_events.pop(key, None)
        runtime_service._gap_responses.pop(key, None)
