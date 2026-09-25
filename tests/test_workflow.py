from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from student_agent import VARIANT_ID, cli
from student_agent.cases import CaseSet
from student_agent.config import Settings
from student_agent.contracts import Contracts
from student_agent.submission import validate_artifacts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class StubGateway:
    def __init__(self, evidence: dict[str, Any]) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return ["get_order"]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        return self.evidence


def _contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def _evidence() -> dict[str, Any]:
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": "ev_12345678901234567890",
        "result_hash": f"sha256:{hashlib.sha256(b'order').hexdigest()}",
        "domain": "order",
        "data": {
            "order_id": "order-1",
            "primary_issue": "canceled_order_paid",
            "case_status": "no_action",
            "confidence": 0.9,
            "item_ids": [],
            "seller_ids": ["seller-1"],
            "payment_references": [],
            "shipment_ids": [],
            "recommended_refund_brl": 0,
            "refund_lines": [],
            "resolution_actions": [],
        },
    }


def test_solve_case_runs_contract_validated_workflow(tmp_path: Path) -> None:
    contracts = _contracts()
    evidence = _evidence()
    contracts.validate_evidence(evidence, "stub evidence")
    gateway = StubGateway(evidence)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_001",
                "customer_request": {
                    "claimed_order_id": "order-1",
                    "claims": [{"claim_id": "claim-1", "topic": "canceled_order_paid"}],
                },
            },
            gateway,
            trace,
        )
    )

    contracts.validate_output(output, "workflow output")
    assert output["case_id"] == "CASE_001"
    assert gateway.calls == [("get_order", "CASE_001", {"order_id": "order-1"})]

    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "verification_completed",
    ]
    for event in events:
        contracts.validate_trace(event, "workflow trace")
    assert all(event["case_id"] == "CASE_001" for event in events)
    assert all(event["evidence_refs"] == [evidence["evidence_ref"]] for event in events[1:])


def test_solve_case_fails_closed_when_required_tool_is_missing(tmp_path: Path) -> None:
    class NoOrderGateway(StubGateway):
        async def list_tools(self) -> list[str]:
            return ["get_payment"]

    contracts = _contracts()
    gateway = NoOrderGateway(_evidence())
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    with pytest.raises(RuntimeError, match="required get_order"):
        asyncio.run(solve_case({"case_id": "CASE_001", "order_id": "order-1"}, gateway, trace))


def test_cli_run_writes_and_validates_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[1]
    shutil.copytree(root / "contracts", tmp_path / "contracts")
    case = {
        "case_id": "CASE_001",
        "customer_request": {"claimed_order_id": "order-1", "claims": []},
    }
    case_set = CaseSet("test-v1", VARIANT_ID, ("CASE_001",), {"CASE_001": case})
    evidence = _evidence()
    gateway = StubGateway(evidence)

    @asynccontextmanager
    async def fake_connect_gateway(*args: Any, **kwargs: Any):
        del args, kwargs
        yield gateway

    monkeypatch.setattr(
        cli.Settings,
        "load",
        lambda root=None: Settings("http://test", "test-key", "http://test/mcp", Path(root)),
    )
    monkeypatch.setattr(cli, "load_case_set", lambda root: case_set)
    monkeypatch.setattr(cli, "connect_gateway", fake_connect_gateway)

    asyncio.run(cli._run(tmp_path))

    output_path = tmp_path / "outputs" / "CASE_001.json"
    trace_path = tmp_path / "traces" / "trace.jsonl"
    assert output_path.exists()
    assert trace_path.exists()
    output = json.loads(output_path.read_text())
    contracts = Contracts(tmp_path / "contracts" / "schemas")
    contracts.validate_output(output, "CLI output")
    outputs, trace_lines = validate_artifacts(tmp_path, case_set, contracts)
    assert outputs["CASE_001"] == output
    events = [json.loads(line) for line in trace_lines]
    assert [event["event_type"] for event in events] == [
        "case_received",
        "task_assigned",
        "tool_result_consumed",
        "handoff",
        "verification_completed",
        "case_finalized",
    ]
    assert all(event["case_id"] == "CASE_001" for event in events)
