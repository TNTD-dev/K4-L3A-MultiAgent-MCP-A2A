from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from contextlib import asynccontextmanager
from decimal import Decimal
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


class PaymentRefundGateway(StubGateway):
    def __init__(self, order: dict[str, Any], payment: dict[str, Any], refund: dict[str, Any]):
        super().__init__(order)
        self.responses = {"get_order": order, "get_payment": payment, "get_refund": refund}

    async def list_tools(self) -> list[str]:
        return list(self.responses)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        return self.responses[tool_name]


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


def _specialist_evidence(
    domain: str, ref_suffix: str, issue: str | None = None, *, status: str = "no_action"
) -> dict[str, Any]:
    data = {
        "order_id": "order-1",
        "item_ids": [],
        "seller_ids": [],
        "payment_references": [f"payment-{ref_suffix}"] if domain == "payment" else [],
        "shipment_ids": [],
        "recommended_refund_brl": 0,
        "refund_lines": [],
        "resolution_actions": [] if status != "action_required" else ["review_refund"],
        "case_status": status,
        "confidence": 0.95,
    }
    if issue is not None:
        data["primary_issue"] = issue
    return {
        "schema_version": "day09-mcp-evidence-v1",
        "evidence_ref": f"ev_{ref_suffix * 20}",
        "result_hash": f"sha256:{hashlib.sha256(ref_suffix.encode()).hexdigest()}",
        "domain": domain,
        "data": data,
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


def test_order_decision_cannot_supply_payment_classification(tmp_path: Path) -> None:
    evidence = _evidence()
    evidence["data"]["primary_issue"] = "payment_mismatch"
    gateway = StubGateway(evidence)
    trace = TraceWriter(tmp_path / "trace.jsonl", _contracts())

    output = asyncio.run(
        solve_case({"case_id": "CASE_001", "order_id": "order-1"}, gateway, trace)
    )

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"


@pytest.mark.parametrize("issue", ["valid_split_payment", "payment_mismatch", "duplicate_charge"])
def test_payment_issue_comes_from_payment_evidence(tmp_path: Path, issue: str) -> None:
    contracts = _contracts()
    order = _evidence()
    payment = _specialist_evidence("payment", "p", issue)
    refund = _specialist_evidence("refund", "r")
    gateway = PaymentRefundGateway(order, payment, refund)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_001",
                "order_id": "order-1",
                "customer_request": {
                    "claims": [{"topic": issue}, {"topic": "requested_full_refund"}]
                },
            },
            gateway,
            trace,
        )
    )

    assert output["assessment"]["primary_issue"] == issue
    assert output["affected_entities"]["payment_references"] == ["payment-p"]
    assert gateway.calls == [
        ("get_order", "CASE_001", {"order_id": "order-1"}),
        ("get_payment", "CASE_001", {"order_id": "order-1"}),
        ("get_refund", "CASE_001", {"order_id": "order-1"}),
    ]


@pytest.mark.parametrize("issue", ["refund_pending", "refund_failed"])
def test_refund_issue_and_lines_come_from_refund_evidence(tmp_path: Path, issue: str) -> None:
    contracts = _contracts()
    order = _evidence()
    payment = _specialist_evidence("payment", "p")
    refund = _specialist_evidence("refund", "r", issue, status="action_required")
    refund["data"]["recommended_refund_brl"] = 10.3
    refund["data"]["refund_lines"] = [
        {"reason_code": "refund", "amount_brl": 10.1, "entity_id": "payment-p"},
        {"reason_code": "fee", "amount_brl": 0.2, "entity_id": "payment-p"},
    ]
    gateway = PaymentRefundGateway(order, payment, refund)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_001",
                "order_id": "order-1",
                "customer_request": {
                    "claims": [{"topic": issue}, {"topic": "requested_full_refund"}]
                },
            },
            gateway,
            trace,
        )
    )

    assert output["assessment"]["primary_issue"] == issue
    assert output["financial_resolution"]["recommended_refund_brl"] == 10.3
    assert {line["entity_id"] for line in output["financial_resolution"]["refund_lines"]} == {
        "payment-p"
    }


def test_payment_refund_slice_is_insufficient_without_specialist_decision(tmp_path: Path) -> None:
    contracts = _contracts()
    order = _evidence()
    payment = _specialist_evidence("payment", "p")
    refund = _specialist_evidence("refund", "r")
    gateway = PaymentRefundGateway(order, payment, refund)
    trace = TraceWriter(tmp_path / "trace.jsonl", contracts)

    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_001",
                "order_id": "order-1",
                "customer_request": {
                    "claims": [
                        {"topic": "payment_mismatch"},
                        {"topic": "requested_full_refund"},
                    ]
                },
            },
            gateway,
            trace,
        )
    )

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["evidence_refs"] == [
        order["evidence_ref"],
        payment["evidence_ref"],
        refund["evidence_ref"],
    ]


def test_missing_payment_tool_cannot_fall_back_to_order_decision(tmp_path: Path) -> None:
    class MissingPaymentGateway(PaymentRefundGateway):
        def __init__(self, order: dict[str, Any], refund: dict[str, Any]) -> None:
            super().__init__(order, refund, refund)
            self.responses = {"get_order": order, "get_refund": refund}

    order = _evidence()
    refund = _specialist_evidence("refund", "r", "refund_pending", status="action_required")
    refund["data"]["resolution_actions"] = ["review_refund"]
    gateway = MissingPaymentGateway(order, refund)
    trace = TraceWriter(tmp_path / "trace.jsonl", _contracts())

    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_001",
                "order_id": "order-1",
                "customer_request": {"claims": [{"topic": "payment_mismatch"}]},
            },
            gateway,
            trace,
        )
    )

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert gateway.calls == [
        ("get_order", "CASE_001", {"order_id": "order-1"}),
        ("get_refund", "CASE_001", {"order_id": "order-1"}),
    ]


def test_wrong_domain_specialist_result_is_rejected(tmp_path: Path) -> None:
    order = _evidence()
    payment = _specialist_evidence("payment", "p")
    payment["domain"] = "refund"
    refund = _specialist_evidence("refund", "r")
    gateway = PaymentRefundGateway(order, payment, refund)
    trace = TraceWriter(tmp_path / "trace.jsonl", _contracts())

    with pytest.raises(ValueError, match="did not return payment evidence"):
        asyncio.run(
            solve_case(
                {
                    "case_id": "CASE_001",
                    "order_id": "order-1",
                    "customer_request": {"claims": [{"topic": "payment_mismatch"}]},
                },
                gateway,
                trace,
            )
        )


def test_refund_line_entity_must_match_authoritative_identifier(tmp_path: Path) -> None:
    order = _evidence()
    payment = _specialist_evidence("payment", "p")
    refund = _specialist_evidence("refund", "r", "refund_pending", status="action_required")
    refund["data"]["resolution_actions"] = ["review_refund"]
    refund["data"]["recommended_refund_brl"] = 1.2
    refund["data"]["refund_lines"] = [
        {"reason_code": "refund", "amount_brl": 1.2, "entity_id": "not-from-evidence"}
    ]
    gateway = PaymentRefundGateway(order, payment, refund)
    trace = TraceWriter(tmp_path / "trace.jsonl", _contracts())

    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_001",
                "order_id": "order-1",
                "customer_request": {"claims": [{"topic": "refund_pending"}]},
            },
            gateway,
            trace,
        )
    )

    assert output["assessment"]["primary_issue"] == "insufficient_evidence"
    assert output["financial_resolution"]["refund_lines"] == []


def test_irrelevant_payment_tools_are_not_called_or_cited(tmp_path: Path) -> None:
    order = _evidence()
    payment = _specialist_evidence("payment", "p", "payment_mismatch")
    refund = _specialist_evidence("refund", "r", "refund_pending", status="action_required")
    gateway = PaymentRefundGateway(order, payment, refund)
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, _contracts())

    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_001",
                "order_id": "order-1",
                "customer_request": {"claims": [{"topic": "canceled_order_paid"}]},
            },
            gateway,
            trace,
        )
    )

    assert output["assessment"]["primary_issue"] == "canceled_order_paid"
    assert gateway.calls == [("get_order", "CASE_001", {"order_id": "order-1"})]
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert not any(event.get("tool_name") in {"get_payment", "get_refund"} for event in events)


def test_refund_money_reconciliation_uses_decimal_values(tmp_path: Path) -> None:
    order = _evidence()
    payment = _specialist_evidence("payment", "p")
    refund = _specialist_evidence("refund", "r", "refund_pending", status="action_required")
    refund["data"]["resolution_actions"] = ["review_refund"]
    refund["data"]["recommended_refund_brl"] = 0.3
    refund["data"]["refund_lines"] = [
        {"reason_code": "a", "amount_brl": 0.1, "entity_id": "payment-p"},
        {"reason_code": "b", "amount_brl": 0.2, "entity_id": "payment-p"},
    ]
    gateway = PaymentRefundGateway(order, payment, refund)
    trace = TraceWriter(tmp_path / "trace.jsonl", _contracts())

    output = asyncio.run(
        solve_case(
            {
                "case_id": "CASE_001",
                "order_id": "order-1",
                "customer_request": {"claims": [{"topic": "refund_pending"}]},
            },
            gateway,
            trace,
        )
    )

    assert Decimal(str(output["financial_resolution"]["recommended_refund_brl"])) == Decimal("0.3")
    assert sum(
        (
            Decimal(str(line["amount_brl"]))
            for line in output["financial_resolution"]["refund_lines"]
        ),
        Decimal("0"),
    ) == Decimal("0.3")


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
