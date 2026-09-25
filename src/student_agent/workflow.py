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


def _slice_insufficient_output(
    case_id: str,
    order_evidence: dict[str, Any],
    evidence_refs: list[str],
    *,
    item_ids: list[str] | None = None,
    seller_ids: list[str] | None = None,
    shipment_ids: list[str] | None = None,
    claim_assessments: list[dict[str, Any]] | None = None,
    data_conflicts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a contract-shaped result when the slice cannot decide safely."""

    data = order_evidence["data"]
    result: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "affected_entities": {
            "order_ids": [data["order_id"]],
            "item_ids": item_ids or [],
            "seller_ids": seller_ids or [],
            "payment_references": [],
            "shipment_ids": shipment_ids or [],
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "data_conflicts": data_conflicts or [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }
    if claim_assessments:
        result["claim_assessments"] = claim_assessments
    return result


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


def _unique_ids(values: list[Any]) -> tuple[list[str], bool]:
    """Keep non-empty string identifiers and report whether a row was malformed."""

    result: list[str] = []
    valid = True
    for value in values:
        if not isinstance(value, str) or not value:
            valid = False
            continue
        if value not in result:
            result.append(value)
    return result, valid


def _entity_ids(
    evidence: dict[str, Any], key: str, expected_domain: str, requested_order_id: str
) -> tuple[list[str], bool]:
    """Extract scoped identifiers without inventing IDs absent from an envelope."""

    if evidence.get("domain") != expected_domain:
        return [], False
    data = evidence["data"]
    if not isinstance(data, list):
        return [], False
    values: list[Any] = []
    valid = True
    for row in data:
        if not isinstance(row, dict):
            valid = False
            continue
        row_order_id = row.get("order_id")
        if row_order_id is not None and row_order_id != requested_order_id:
            valid = False
            continue
        value = row.get(key)
        if value is None:
            valid = False
            continue
        values.append(value)
    ids, ids_valid = _unique_ids(values)
    return ids, valid and ids_valid


def _shipment_ids(
    evidence: dict[str, Any], requested_order_id: str
) -> tuple[list[str], bool]:
    if evidence.get("domain") != "shipment":
        return [], False
    data = evidence["data"]
    if not isinstance(data, dict):
        return [], False
    data_order_id = data.get("order_id")
    valid = data_order_id in (None, requested_order_id)
    values: list[Any] = []
    for key in ("shipment_id", "shipment_ids"):
        value = data.get(key)
        if value is None:
            continue
        if key == "shipment_ids":
            if not isinstance(value, list):
                valid = False
            else:
                values.extend(value)
        else:
            values.append(value)
    events = data.get("events", [])
    if events is not None:
        if not isinstance(events, list):
            valid = False
        else:
            for event in events:
                if not isinstance(event, dict):
                    valid = False
                    continue
                event_order_id = event.get("order_id")
                if event_order_id not in (None, requested_order_id):
                    valid = False
                    continue
                if event.get("shipment_id") is not None:
                    values.append(event["shipment_id"])
    ids, ids_valid = _unique_ids(values)
    return ids, valid and ids_valid


def _late_actors(
    shipment_evidence: dict[str, Any], requested_order_id: str
) -> tuple[set[str], bool]:
    """Return authoritative actors for in-scope explicit late events."""

    data = shipment_evidence["data"]
    if (
        shipment_evidence.get("domain") != "shipment"
        or not isinstance(data, dict)
        or not isinstance(data.get("events"), list)
    ):
        return set(), False
    actors: set[str] = set()
    valid = True
    for event in data["events"]:
        if not isinstance(event, dict):
            valid = False
            continue
        if event.get("order_id") not in (None, requested_order_id):
            continue
        event_type = event.get("event_type")
        status = event.get("status")
        if not isinstance(event_type, str) and not isinstance(status, str):
            continue
        late = any(
            isinstance(value, str) and "late" in value.lower()
            for value in (event_type, status)
        )
        actor = event.get("actor")
        if late and isinstance(actor, str):
            actors.add(actor.lower())
    normalized = set()
    if "seller" in actors:
        normalized.add("seller")
    logistics_actors = {"logistics", "logistics_provider", "carrier", "shipping_provider"}
    if actors & logistics_actors:
        normalized.add("logistics")
    return normalized, valid


def _claims(case: dict[str, Any]) -> list[tuple[str, str]]:
    request = case.get("customer_request")
    if not isinstance(request, dict) or not isinstance(request.get("claims"), list):
        return []
    result: list[tuple[str, str]] = []
    for claim in request["claims"]:
        if not isinstance(claim, dict):
            continue
        claim_id, topic = claim.get("claim_id"), claim.get("topic")
        if isinstance(claim_id, str) and claim_id and isinstance(topic, str) and topic:
            result.append((claim_id, topic))
    return result


def _claim_assessments(
    claims: list[tuple[str, str]],
    *,
    late_actors: set[str],
    shipment_data_valid: bool,
    evidence_refs: list[str],
    shipment_ref: str | None,
) -> list[dict[str, Any]]:
    assessments: list[dict[str, Any]] = []
    for claim_id, topic in claims:
        refs = [shipment_ref] if shipment_ref and topic in {
            "late_delivery_seller", "late_delivery_logistics"
        } else list(evidence_refs)
        if topic in {"late_delivery_seller", "late_delivery_logistics"}:
            expected_actor = (
                "seller" if topic == "late_delivery_seller" else "logistics"
            )
            opposing_actor = "logistics" if expected_actor == "seller" else "seller"
            if not shipment_data_valid or len(late_actors) > 1:
                verdict, confidence = "insufficient_evidence", 0.0
            elif expected_actor in late_actors:
                verdict, confidence = "supported", 0.9
            elif opposing_actor in late_actors:
                verdict, confidence = "unsupported", 0.9
            else:
                verdict, confidence = "insufficient_evidence", 0.0
        else:
            # Every customer topic, including an "unsupported" topic, is only
            # a hypothesis. This slice has no explicit fact that can decide the
            # remaining topics, so it must remain insufficient.
            verdict, confidence = "insufficient_evidence", 0.0
        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": list(dict.fromkeys(refs)),
            }
        )
    return assessments


def _slice_output(
    case_id: str,
    order_evidence: dict[str, Any],
    item_evidence: dict[str, Any] | None,
    seller_evidence: dict[str, Any] | None,
    shipment_evidence: dict[str, Any] | None,
    claims: list[tuple[str, str]],
    evidence_refs: list[str],
    requested_order_id: str,
) -> dict[str, Any]:
    order_data = order_evidence["data"]
    item_ids, items_valid = (
        _entity_ids(item_evidence, "order_item_id", "item", requested_order_id)
        if item_evidence
        else ([], False)
    )
    seller_ids, sellers_valid = (
        _entity_ids(seller_evidence, "seller_id", "seller", requested_order_id)
        if seller_evidence
        else ([], False)
    )
    item_seller_ids, item_sellers_valid = (
        _entity_ids(item_evidence, "seller_id", "item", requested_order_id)
        if item_evidence
        else ([], False)
    )
    shipment_ids, shipment_ids_valid = (
        _shipment_ids(shipment_evidence, requested_order_id)
        if shipment_evidence
        else ([], False)
    )
    # Seller records are the authoritative seller source. Item rows can add a
    # seller only when the seller tool is unavailable, but never create one.
    if not seller_ids and item_sellers_valid:
        seller_ids = item_seller_ids
    seller_ids = list(dict.fromkeys([*seller_ids, *item_seller_ids]))
    late_actors, late_events_valid = (
        _late_actors(shipment_evidence, requested_order_id)
        if shipment_evidence
        else (set(), False)
    )
    shipment_ref = shipment_evidence["evidence_ref"] if shipment_evidence else None
    claim_assessments = _claim_assessments(
        claims,
        late_actors=late_actors,
        shipment_data_valid=late_events_valid and shipment_ids_valid,
        evidence_refs=evidence_refs,
        shipment_ref=shipment_ref,
    )
    late_claim_present = any(
        topic in {"late_delivery_seller", "late_delivery_logistics"}
        for _, topic in claims
    )
    data_conflicts: list[dict[str, Any]] = []
    if len(late_actors) > 1:
        data_conflicts = [
            {
                "field": "shipment_late_actor",
                "sources": ["shipment_event:seller", "shipment_event:logistics"],
                "selected_source": None,
                "resolution_code": "conflicting_late_actors",
            }
        ]
    if len(late_actors) == 1 and late_claim_present and late_events_valid:
        late_actor = next(iter(late_actors))
        primary_issue = f"late_delivery_{late_actor}"
        status = "needs_investigation"
        confidence = 0.9
        if late_actor == "seller":
            causes = [{"cause_code": "SELLER_DELAY", "rank": 1}]
            parties = [
                {"party_type": "seller", "party_id": seller_id} for seller_id in seller_ids
            ]
        else:
            causes = [{"cause_code": "LOGISTICS_DELAY", "rank": 1}]
            parties = [{"party_type": "logistics_provider", "party_id": None}]
    else:
        primary_issue = "insufficient_evidence"
        status = "needs_investigation"
        confidence = 0.0
        causes, parties = [], []

    result: dict[str, Any] = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_data["order_id"]],
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": [],
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": claim_assessments,
        "root_cause_analysis": {
            "ranked_causes": causes,
            "responsible_parties": parties,
        },
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "data_conflicts": data_conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": 0.0,
            "refund_lines": [],
        },
        "resolution_actions": [],
    }
    # A malformed or absent specialist result cannot support a positive
    # conclusion. Keep the authoritative entity IDs and refs, but fail closed.
    if not items_valid or not sellers_valid or not shipment_ids_valid:
        result = _slice_insufficient_output(
            case_id,
            order_evidence,
            evidence_refs,
            item_ids=item_ids,
            seller_ids=seller_ids,
            shipment_ids=shipment_ids,
            claim_assessments=claim_assessments,
            data_conflicts=data_conflicts,
        )
    return result


def _verify_slice(
    result: dict[str, Any],
    order_evidence: dict[str, Any],
    auxiliary: list[dict[str, Any]],
    requested_order_id: str,
    consumed_refs: list[str],
) -> None:
    """Verify scope, immutable references, and uniqueness for the four domains."""

    if (
        order_evidence["domain"] != "order"
        or order_evidence["data"].get("order_id") != requested_order_id
    ):
        raise ValueError("verifier: order evidence is outside the requested scope")
    refs = [evidence["evidence_ref"] for evidence in [order_evidence, *auxiliary]]
    if refs != consumed_refs or len(refs) != len(set(refs)):
        raise ValueError("verifier: consumed evidence references changed or repeated")
    if result["evidence_refs"] != list(dict.fromkeys(consumed_refs)):
        raise ValueError("verifier: output evidence_refs do not link consumed evidence")
    entities = result["affected_entities"]
    for key in ("order_ids", "item_ids", "seller_ids", "shipment_ids"):
        values = entities[key]
        if len(values) != len(set(values)):
            raise ValueError(f"verifier: duplicate {key}")
    if entities["order_ids"] != [requested_order_id]:
        raise ValueError("verifier: output order is outside the requested scope")


async def _solve_order_only(
    case_id: str, order_id: str, gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Isolated issue-1 compatibility path for gateways exposing only get_order."""

    contracts = trace.contracts
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order-agent",
        decision_code="order_lookup",
    )
    evidence = await gateway.call("get_order", case_id=case_id, order_id=order_id)
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


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the coordinator and the discovered order/item/shipment specialists."""

    case_id = case.get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("case must contain a non-empty case_id")
    order_id = _order_id(case)
    tools = await gateway.list_tools()
    if "get_order" not in tools:
        raise RuntimeError("MCP Gateway does not expose the required get_order tool")

    # Keep the issue-1 path contract-compatible for a gateway that only exposes
    # get_order. Once any issue-2 specialist is advertised, use every matching
    # discovered tool and fail closed for an unavailable specialist result.
    specialist_tools = {
        name: name
        for name in ("get_order_items", "get_sellers", "get_shipment_summary")
        if name in tools
    }
    if not specialist_tools:
        return await _solve_order_only(case_id, order_id, gateway, trace)

    if any(name in specialist_tools for name in ("get_order_items", "get_sellers")):
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="order-item-agent",
            decision_code="order_item_lookup",
        )
    if "get_shipment_summary" in specialist_tools:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target="shipment-agent",
            decision_code="shipment_lookup",
        )
    contracts = trace.contracts
    evidence = await gateway.call("get_order", case_id=case_id, order_id=order_id)
    contracts.validate_evidence(evidence, "MCP tool get_order")
    if evidence["domain"] != "order" or not isinstance(evidence["data"], dict):
        raise ValueError("MCP get_order did not return order evidence")
    if evidence["data"].get("order_id") != order_id:
        raise ValueError("MCP get_order returned evidence for a different order")
    evidence_items: dict[str, dict[str, Any]] = {"get_order": evidence}
    order_item_available = any(
        name in specialist_tools for name in ("get_order_items", "get_sellers")
    )
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="order-item-agent" if order_item_available else "shipment-agent",
        tool_name="get_order",
        evidence_refs=[evidence["evidence_ref"]],
    )

    for tool_name in ("get_order_items", "get_sellers", "get_shipment_summary"):
        if tool_name not in specialist_tools:
            continue
        specialist = await gateway.call(tool_name, case_id=case_id, order_id=order_id)
        contracts.validate_evidence(specialist, f"MCP tool {tool_name}")
        evidence_items[tool_name] = specialist
        actor = "shipment-agent" if tool_name == "get_shipment_summary" else "order-item-agent"
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[specialist["evidence_ref"]],
        )

    consumed = list(evidence_items.values())
    consumed_refs = [item["evidence_ref"] for item in consumed]
    order_refs = [
        evidence_items[name]["evidence_ref"]
        for name in ("get_order", "get_order_items", "get_sellers")
        if name in evidence_items
    ]
    shipment_refs = (
        [evidence_items["get_shipment_summary"]["evidence_ref"]]
        if "get_shipment_summary" in evidence_items
        else []
    )
    if order_refs and order_item_available:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="order-item-agent",
            target="verifier",
            decision_code="order_item_evidence_ready",
            evidence_refs=order_refs,
        )
    if shipment_refs and order_item_available:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment-agent",
            target="verifier",
            decision_code="shipment_evidence_ready",
            evidence_refs=shipment_refs,
        )
    elif shipment_refs:
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment-agent",
            target="verifier",
            decision_code="shipment_evidence_ready",
            evidence_refs=[*order_refs, *shipment_refs],
        )

    result = _slice_output(
        case_id,
        evidence_items["get_order"],
        evidence_items.get("get_order_items"),
        evidence_items.get("get_sellers"),
        evidence_items.get("get_shipment_summary"),
        _claims(case),
        consumed_refs,
        order_id,
    )
    contracts.validate_output(result, f"output/{case_id}")
    _verify_slice(
        result,
        evidence_items["get_order"],
        consumed[1:],
        order_id,
        consumed_refs,
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="output_contract_valid",
        evidence_refs=consumed_refs,
    )
    return result
