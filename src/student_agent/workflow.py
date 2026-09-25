from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}
_STATUSES = {"action_required", "no_action", "needs_investigation"}


def _order_id(case: dict[str, Any]) -> str:
    """Return an explicit lookup identifier without treating it as evidence."""

    candidates: list[Any] = [case.get("order_id")]
    order_ids = case.get("order_ids")
    if isinstance(order_ids, list):
        candidates.extend(order_ids)
    order = case.get("order")
    if isinstance(order, dict):
        candidates.append(order.get("order_id"))
    customer_request = case.get("customer_request")
    if isinstance(customer_request, dict):
        # A claimed ID is only a lookup input. It is never copied to output.
        candidates.append(customer_request.get("claimed_order_id"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise ValueError("case does not contain an explicit order_id")


def _string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"MCP order evidence field {key} is not a string array")
    return list(dict.fromkeys(value))


def _insufficient_output(case_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    data = evidence["data"]
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [data["order_id"]],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [evidence["evidence_ref"]],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }


def _output(case_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Project only canonical, explicit MCP fields into the public output.

    If evidence does not contain every decision field, the only permitted result
    is the contract-shaped insufficient-evidence outcome.
    """

    data = evidence["data"]
    if not isinstance(data, dict) or not isinstance(data.get("order_id"), str):
        raise ValueError("MCP order evidence must contain an order_id")
    required = {
        "primary_issue",
        "case_status",
        "confidence",
        "recommended_refund_brl",
        "refund_lines",
        "resolution_actions",
    }
    if not required.issubset(data):
        return _insufficient_output(case_id, evidence)
    issue = data["primary_issue"]
    status = data["case_status"]
    confidence = data["confidence"]
    refund_total = data["recommended_refund_brl"]
    raw_lines = data["refund_lines"]
    actions = data["resolution_actions"]
    if (
        not isinstance(issue, str)
        or issue not in _ISSUES
        or not isinstance(status, str)
        or status not in _STATUSES
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
        or not isinstance(refund_total, (int, float))
        or isinstance(refund_total, bool)
        or refund_total < 0
        or not isinstance(raw_lines, list)
        or not isinstance(actions, list)
        or not all(isinstance(action, str) for action in actions)
    ):
        return _insufficient_output(case_id, evidence)
    refund_lines: list[dict[str, Any]] = []
    for line in raw_lines:
        if not isinstance(line, dict):
            return _insufficient_output(case_id, evidence)
        reason = line.get("reason_code")
        amount = line.get("amount_brl")
        entity_id = line.get("entity_id")
        if (
            not isinstance(reason, str)
            or not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or amount < 0
            or entity_id is not None
            and not isinstance(entity_id, str)
        ):
            return _insufficient_output(case_id, evidence)
        refund_lines.append(
            {"reason_code": reason, "amount_brl": amount, "entity_id": entity_id}
        )
    if status == "action_required" and not actions:
        return _insufficient_output(case_id, evidence)
    if status != "action_required" and actions:
        return _insufficient_output(case_id, evidence)
    try:
        if Decimal(str(refund_total)) != sum(
            (Decimal(str(line["amount_brl"])) for line in refund_lines), Decimal("0")
        ):
            return _insufficient_output(case_id, evidence)
    except (InvalidOperation, ValueError):
        return _insufficient_output(case_id, evidence)

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [data["order_id"]],
            "item_ids": _string_list(data, "item_ids"),
            "seller_ids": _string_list(data, "seller_ids"),
            "payment_references": _string_list(data, "payment_references"),
            "shipment_ids": _string_list(data, "shipment_ids"),
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": [evidence["evidence_ref"]],
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_total,
            "refund_lines": refund_lines,
        },
        "resolution_actions": actions,
    }


def _verify(
    result: dict[str, Any], evidence: dict[str, Any], requested_order_id: str
) -> None:
    """Check invariants that are not expressible in the public JSON schemas."""

    data = evidence["data"]
    if evidence["domain"] != "order":
        raise ValueError("verifier: expected order evidence")
    if not isinstance(data, dict) or data.get("order_id") != requested_order_id:
        raise ValueError("verifier: evidence order_id does not match the requested order")
    if result["evidence_refs"] != [evidence["evidence_ref"]]:
        raise ValueError("verifier: output evidence_refs do not link consumed evidence")
    financial = result["financial_resolution"]
    try:
        total = Decimal(str(financial["recommended_refund_brl"]))
        line_total = sum(
            (Decimal(str(line["amount_brl"])) for line in financial["refund_lines"]),
            Decimal("0"),
        )
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("verifier: invalid financial total") from exc
    if total != line_total:
        raise ValueError("verifier: financial total does not equal refund-line sum")
    status = result["assessment"]["case_status"]
    actions = result["resolution_actions"]
    if (status == "action_required") != bool(actions):
        raise ValueError("verifier: action_required status is inconsistent with actions")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the coordinator → order specialist → verifier path without invention."""

    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case must contain a non-empty case_id")
    order_id = _order_id(case)
    tools = await gateway.list_tools()
    if "get_order" not in tools:
        raise RuntimeError("MCP Gateway does not expose the required get_order tool")

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
        decision_code="order_lookup",
    )
    evidence = await gateway.call("get_order", case_id=case_id, order_id=order_id)
    contracts = trace.contracts
    contracts.validate_evidence(evidence, "MCP tool get_order")
    if evidence["domain"] != "order" or not isinstance(evidence["data"], dict):
        raise ValueError("MCP get_order did not return order evidence")
    if evidence["data"].get("order_id") != order_id:
        raise ValueError("MCP get_order returned evidence for a different order")
    evidence_ref = evidence["evidence_ref"]
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="order-agent",
        tool_name="get_order",
        evidence_refs=[evidence_ref],
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="order-agent",
        target="verifier",
        decision_code="evidence_ready",
        evidence_refs=[evidence_ref],
    )

    result = _output(case_id, evidence)
    contracts.validate_output(result, f"output/{case_id}")
    _verify(result, evidence, order_id)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="output_contract_valid",
        evidence_refs=[evidence_ref],
    )
    return result
