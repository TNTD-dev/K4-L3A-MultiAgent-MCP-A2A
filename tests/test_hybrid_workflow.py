from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.hybrid_workflow import _collect, _normalize
from student_agent.trace import TraceWriter


class HybridGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def list_tools(self) -> list[str]:
        return [
            "get_order",
            "get_order_items",
            "get_sellers",
            "get_order_payments",
            "get_payment_timeline",
            "get_refund_timeline",
            "get_shipment_summary",
            "get_policy",
            "get_product_context",
        ]

    async def call(
        self, tool_name: str, *, case_id: str, **arguments: str
    ) -> dict[str, Any]:
        self.calls.append((tool_name, case_id, arguments))
        domains = {
            "get_order": "order",
            "get_order_payments": "payment",
            "get_refund_timeline": "refund",
            "get_policy": "policy",
        }
        return {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name}_12345678901234567890",
            "result_hash": f"sha256:{hashlib.sha256(tool_name.encode()).hexdigest()}",
            "domain": domains[tool_name],
            "data": {"order_id": "order-1"},
        }


def _contracts() -> Contracts:
    root = Path(__file__).resolve().parents[1]
    return Contracts(root / "contracts" / "schemas")


def _case(topic: str = "refund_pending") -> dict[str, Any]:
    return {
        "case_id": "L3A_CASE_001",
        "policy_version": "2026-01",
        "customer_request": {
            "claimed_order_id": "order-1",
            "claims": [{"claim_id": "claim-1", "topic": topic}],
        },
    }


def test_collect_routes_only_claim_scoped_tools_and_traces_refs(tmp_path: Path) -> None:
    gateway = HybridGateway()
    trace_path = tmp_path / "trace.jsonl"
    trace = TraceWriter(trace_path, _contracts())

    evidence = asyncio.run(_collect(_case(), gateway, trace))

    assert [call[0] for call in gateway.calls] == [
        "get_order",
        "get_order_payments",
        "get_refund_timeline",
        "get_policy",
    ]
    assert gateway.calls[-1][2] == {"policy_version": "2026-01"}
    assert len(evidence) == 4
    events = [json.loads(line) for line in trace_path.read_text().splitlines()]
    consumed = [event for event in events if event["event_type"] == "tool_result_consumed"]
    assert [event["evidence_refs"][0] for event in consumed] == [
        item["evidence_ref"] for item in evidence
    ]


def test_normalize_enforces_valid_split_payment_no_action() -> None:
    case = _case("valid_split_payment")
    result = {
        "evidence_refs": ["ev-a", "ev-a"],
        "resolution_actions": ["contact_support", "contact_support"],
        "claim_assessments": [
            {
                "claim_id": "claim-1",
                "verdict": "supported",
                "confidence": 0.99,
                "evidence_refs": ["ev-a", "ev-a"],
            }
        ],
        "data_conflicts": [
            {"field": "status", "sources": ["ev-a", "ev-a"]},
            {"field": "amount", "sources": ["ev-a", "ev-b", "ev-b"]},
        ],
        "affected_entities": {
            "order_ids": ["order-1", "order-1"],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "assessment": {
            "primary_issue": "payment_mismatch",
            "case_status": "action_required",
            "confidence": 0.95,
        },
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 20,
            "refund_lines": [{"amount_brl": 20}],
        },
    }

    normalized = _normalize(result, case)

    assert normalized["assessment"] == {
        "primary_issue": "valid_split_payment",
        "case_status": "no_action",
        "confidence": 0.8,
    }
    assert normalized["financial_resolution"] == {
        "currency": "BRL",
        "recommended_refund_brl": 0,
        "refund_lines": [],
    }
    assert normalized["resolution_actions"] == []
    assert normalized["evidence_refs"] == ["ev-a"]
    assert normalized["claim_assessments"][0]["confidence"] == 0.8
    assert normalized["affected_entities"]["order_ids"] == ["order-1"]
    assert normalized["data_conflicts"] == [
        {"field": "amount", "sources": ["ev-a", "ev-b"]}
    ]
