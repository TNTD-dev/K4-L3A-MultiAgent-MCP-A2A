from __future__ import annotations

from decimal import Decimal, InvalidOperation
from math import isfinite
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
_PAYMENT_ISSUES = {"valid_split_payment", "payment_mismatch", "duplicate_charge"}
_REFUND_ISSUES = {"refund_pending", "refund_failed"}
_STATUSES = {"action_required", "no_action", "needs_investigation"}


def _empty_financial() -> dict[str, Any]:
    return {
        "currency": "BRL",
        "recommended_refund_brl": 0,
        "refund_lines": [],
    }


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
        raise ValueError(f"MCP evidence field {key} is not a string array")
    return list(dict.fromkeys(value))


def _money(value: Any, label: str) -> Decimal:
    """Parse a non-negative JSON number without doing binary-float arithmetic."""

    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{label} is not a number")
    if isinstance(value, float) and not isfinite(value):
        raise ValueError(f"{label} is not finite")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} is not a decimal number") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    return amount


def _json_money(value: Decimal) -> int | float:
    """Return a JSON-serialisable number after exact Decimal reconciliation."""

    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _insufficient_output(
    case_id: str, evidence_refs: list[str], order_id: str
) -> dict[str, Any]:
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [order_id],
            "item_ids": [],
            "seller_ids": [],
            "payment_references": [],
            "shipment_ids": [],
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": _empty_financial(),
        "resolution_actions": [],
    }


def _valid_decision(data: dict[str, Any], domain: str) -> str | None:
    """Read an explicit MCP decision; derive refund state only from refund evidence."""

    issue = data.get("primary_issue")
    allowed = (
        _PAYMENT_ISSUES
        if domain == "payment"
        else _REFUND_ISSUES
        if domain == "refund"
        else _ISSUES
    )
    if isinstance(issue, str) and issue in allowed:
        return issue
    if domain == "refund":
        state = data.get("refund_status")
        if state in {"pending", "refund_pending"}:
            return "refund_pending"
        if state in {"failed", "refund_failed"}:
            return "refund_failed"
    return None


def _decision(
    records: list[tuple[str, dict[str, Any]]], *, allow_order_decision: bool
) -> str | None:
    """Resolve decisions from authoritative records and reject conflicting facts."""

    specialist_records = [
        (domain, data) for domain, data in records if domain in {"payment", "refund"}
    ]
    if any(
        "primary_issue" in data
        and (not isinstance(data["primary_issue"], str) or data["primary_issue"] not in _ISSUES)
        for _, data in specialist_records
    ):
        return None
    specific = [
        (domain, data, issue)
        for domain, data in records
        if (issue := _valid_decision(data, domain)) is not None
        and (domain in {"payment", "refund"})
    ]
    if specific:
        issues = {issue for _, _, issue in specific}
        if len(issues) != 1:
            return None
        issue = specific[0][2]
        if issue not in _PAYMENT_ISSUES | _REFUND_ISSUES:
            return None
        return issue
    if specialist_records or not allow_order_decision:
        return None
    order_decisions = {
        issue
        for domain, data in records
        if domain == "order"
        and (issue := _valid_decision(data, domain)) is not None
    }
    if len(order_decisions) != 1:
        return None
    issue = order_decisions.pop()
    return issue if issue not in _PAYMENT_ISSUES | _REFUND_ISSUES else None


def _decision_data(
    records: list[tuple[str, dict[str, Any]]], issue: str, *, allow_order_decision: bool
) -> dict[str, Any] | None:
    for domain, data in records:
        if domain in {"payment", "refund"} and _valid_decision(data, domain) == issue:
            return data
    if allow_order_decision:
        for domain, data in records:
            if (
                domain == "order"
                and issue not in _PAYMENT_ISSUES | _REFUND_ISSUES
                and _valid_decision(data, domain) == issue
            ):
                return data
    return None


def _authoritative_entity_ids(records: list[tuple[str, dict[str, Any]]]) -> set[str] | None:
    """Collect only canonical identifiers that MCP evidence makes authoritative."""

    identifiers: set[str] = set()
    for _, data in records:
        order_id = data.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            return None
        identifiers.add(order_id)
        for key in ("item_ids", "payment_references"):
            value = data.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                return None
            identifiers.update(value)
    return identifiers


def _financial_resolution(
    records: list[tuple[str, dict[str, Any]]],
    issue: str,
    *,
    allow_order_decision: bool,
) -> tuple[dict[str, Any], bool]:
    """Project and reconcile MCP refund fields; return (financial, valid)."""

    source_domains = {"refund"} if not allow_order_decision else {"order"}
    source = next(
        (
            data
            for domain, data in records
            if domain in source_domains
            and ("recommended_refund_brl" in data or "refund_lines" in data)
        ),
        None,
    )
    if source is None:
        return _empty_financial(), False
    total_raw = source.get("recommended_refund_brl")
    raw_lines = source.get("refund_lines")
    if total_raw is None or not isinstance(raw_lines, list):
        return _empty_financial(), False
    authoritative_ids = _authoritative_entity_ids(records)
    if authoritative_ids is None:
        return _empty_financial(), False
    try:
        total = _money(total_raw, "recommended_refund_brl")
        lines: list[dict[str, Any]] = []
        line_total = Decimal("0")
        for index, raw_line in enumerate(raw_lines):
            if not isinstance(raw_line, dict):
                return _empty_financial(), False
            reason = raw_line.get("reason_code")
            entity_id = raw_line.get("entity_id")
            if not isinstance(reason, str) or not reason:
                return _empty_financial(), False
            if entity_id is not None and not isinstance(entity_id, str):
                return _empty_financial(), False
            if entity_id is not None and entity_id not in authoritative_ids:
                return _empty_financial(), False
            amount = _money(raw_line.get("amount_brl"), f"refund_lines[{index}].amount_brl")
            line_total += amount
            lines.append(
                {
                    "reason_code": reason,
                    "amount_brl": _json_money(amount),
                    "entity_id": entity_id,
                }
            )
    except (InvalidOperation, TypeError, ValueError):
        return _empty_financial(), False
    if total != line_total:
        return _empty_financial(), False
    return {
        "currency": "BRL",
        "recommended_refund_brl": _json_money(total),
        "refund_lines": lines,
    }, True


def _output(
    case_id: str,
    records: list[tuple[str, dict[str, Any]]],
    evidence_refs: list[str],
    requested_order_id: str,
    *,
    allow_order_decision: bool,
) -> dict[str, Any]:
    """Project only scoped, explicit MCP fields into the public output."""

    issue = _decision(records, allow_order_decision=allow_order_decision)
    if issue is None:
        return _insufficient_output(case_id, evidence_refs, requested_order_id)
    decision_data = _decision_data(
        records, issue, allow_order_decision=allow_order_decision
    )
    if decision_data is None:
        return _insufficient_output(case_id, evidence_refs, requested_order_id)
    status = decision_data.get("case_status")
    if not isinstance(status, str) or status not in _STATUSES:
        return _insufficient_output(case_id, evidence_refs, requested_order_id)
    confidence = decision_data.get("confidence", 0.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return _insufficient_output(case_id, evidence_refs, requested_order_id)
    if not 0 <= confidence <= 1:
        return _insufficient_output(case_id, evidence_refs, requested_order_id)
    financial, valid_financial = _financial_resolution(
        records, issue, allow_order_decision=allow_order_decision
    )
    if not valid_financial:
        return _insufficient_output(case_id, evidence_refs, requested_order_id)
    actions = decision_data.get("resolution_actions")
    if actions is None:
        return _insufficient_output(case_id, evidence_refs, requested_order_id)
    if not isinstance(actions, list) or not all(isinstance(action, str) for action in actions):
        return _insufficient_output(case_id, evidence_refs, requested_order_id)
    if (status == "action_required") != bool(actions):
        return _insufficient_output(case_id, evidence_refs, requested_order_id)

    def collect_entities(key: str) -> list[str] | None:
        values: list[str] = []
        for _, data in records:
            value = data.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return None
            values.extend(value)
        return list(dict.fromkeys(values))

    item_ids = collect_entities("item_ids")
    seller_ids = collect_entities("seller_ids")
    payment_references = collect_entities("payment_references")
    shipment_ids = collect_entities("shipment_ids")
    if any(value is None for value in (item_ids, seller_ids, payment_references, shipment_ids)):
        return _insufficient_output(case_id, evidence_refs, requested_order_id)

    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [requested_order_id],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_references,
            "shipment_ids": shipment_ids,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": financial,
        "resolution_actions": actions,
    }


def _verify(
    result: dict[str, Any],
    records: list[tuple[str, dict[str, Any]]],
    evidence_refs: list[str],
    requested_order_id: str,
) -> None:
    """Check cross-field and scope invariants not expressible in JSON Schema."""

    if result["evidence_refs"] != evidence_refs:
        raise ValueError("verifier: output evidence_refs do not link consumed evidence")
    for domain, data in records:
        if not isinstance(data, dict) or data.get("order_id") != requested_order_id:
            raise ValueError(f"verifier: {domain} evidence is outside the requested order")
    financial = result["financial_resolution"]
    try:
        total = _money(financial["recommended_refund_brl"], "output refund total")
        line_total = sum(
            (
                _money(line["amount_brl"], "output refund line")
                for line in financial["refund_lines"]
            ),
            Decimal("0"),
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("verifier: invalid financial total") from exc
    if total != line_total:
        raise ValueError("verifier: financial total does not equal refund-line sum")
    status = result["assessment"]["case_status"]
    actions = result["resolution_actions"]
    if (status == "action_required") != bool(actions):
        raise ValueError("verifier: action_required status is inconsistent with actions")


def _requested_financial_domains(case: dict[str, Any]) -> set[str]:
    """Use customer topics only to select investigation scope, never to classify."""

    request = case.get("customer_request")
    claims = request.get("claims") if isinstance(request, dict) else None
    topics = {
        claim.get("topic")
        for claim in claims or []
        if isinstance(claim, dict) and isinstance(claim.get("topic"), str)
    }
    if topics & (_PAYMENT_ISSUES | _REFUND_ISSUES):
        # Payment cases still need refund evidence for a supported zero/non-zero
        # financial result; refund cases need payment references for line-ID checks.
        return {"payment", "refund"}
    if "requested_full_refund" in topics:
        return {"refund"}
    return set()


def _tool_for_domain(tools: list[str], domain: str) -> str | None:
    """Select only canonical tool names that were returned by MCP discovery."""

    canonical = {"payment": "get_payment", "refund": "get_refund"}
    name = canonical[domain]
    return name if name in tools else None


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run coordinator → order/payment/refund specialists → verifier."""

    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case must contain a non-empty case_id")
    order_id = _order_id(case)
    tools = await gateway.list_tools()
    if "get_order" not in tools:
        raise RuntimeError("MCP Gateway does not expose the required get_order tool")
    required_domains = _requested_financial_domains(case)

    records: list[tuple[str, dict[str, Any]]] = []
    evidence_refs: list[str] = []

    async def investigate(domain: str, tool_name: str, actor: str) -> None:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=f"{domain}_lookup",
        )
        evidence = await gateway.call(tool_name, case_id=case_id, order_id=order_id)
        trace.contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        data = evidence.get("data")
        if evidence.get("domain") != domain or not isinstance(data, dict):
            raise ValueError(f"MCP {tool_name} did not return {domain} evidence")
        if data.get("order_id") != order_id:
            raise ValueError(f"MCP {tool_name} returned evidence for a different order")
        evidence_ref = evidence["evidence_ref"]
        if evidence_ref not in evidence_refs:
            evidence_refs.append(evidence_ref)
        records.append((domain, data))
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[evidence_ref],
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="verifier",
            decision_code="evidence_ready",
            evidence_refs=[evidence_ref],
        )

    await investigate("order", "get_order", "order-agent")
    for domain, actor in (("payment", "payment-agent"), ("refund", "refund-agent")):
        if domain not in required_domains:
            continue
        tool_name = _tool_for_domain(tools, domain)
        if tool_name is not None:
            await investigate(domain, tool_name, actor)

    if required_domains and not required_domains.issubset({domain for domain, _ in records}):
        result = _insufficient_output(case_id, evidence_refs, order_id)
    else:
        result = _output(
            case_id,
            records,
            evidence_refs,
            order_id,
            allow_order_decision=not required_domains,
        )
    trace.contracts.validate_output(result, f"output/{case_id}")
    _verify(result, records, evidence_refs, order_id)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="output_contract_valid",
        evidence_refs=evidence_refs,
    )
    return result
