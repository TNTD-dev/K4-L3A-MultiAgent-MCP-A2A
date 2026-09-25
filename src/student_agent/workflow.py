from __future__ import annotations

import asyncio
import re
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
_STATUSES = {"action_required", "no_action", "needs_investigation"}
_PAYMENT_ISSUES = {"valid_split_payment", "payment_mismatch", "duplicate_charge"}
_REFUND_ISSUES = {"refund_pending", "refund_failed"}
_POLICY_TOOL_PREFERENCE = (
    "get_policy",
    "get_policy_rules",
    "get_policy_evidence",
)
_POLICY_REQUIRED_CASE_FIELDS = ("policy_version",)
_REFUND_ACTION_MARKERS = ("refund", "reimburse", "repay")
# Calls are deliberately single-attempt.  A retry could duplicate an audited
# MCP call and, more importantly, make a late result race the verifier.  The
# short bound keeps a broken gateway from holding a case open indefinitely.
_MCP_CALL_TIMEOUT_SECONDS = 5.0


async def _call_gateway(
    gateway: EvidenceGateway,
    tool_name: str,
    *,
    case_id: str,
    order_id: str,
) -> dict[str, Any]:
    """Make one bounded, idempotent MCP request.

    ``wait_for`` also works with the small async gateway stubs used by the
    workflow tests.  No retry is performed: every server call is audited and
    repeating a non-idempotent or timed-out request would make provenance
    ambiguous.
    """

    try:
        result = await asyncio.wait_for(
            gateway.call(tool_name, case_id=case_id, order_id=order_id),
            timeout=_MCP_CALL_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise RuntimeError(f"MCP tool {tool_name} timed out") from exc
    if not isinstance(result, dict):
        raise ValueError(f"MCP tool {tool_name} returned a non-object result")
    return result


def _failure_decision_code(error: BaseException) -> str:
    """Map gateway failures to compact public decision codes."""

    message = str(error).lower()
    if "timed out" in message or "timeout" in message:
        return "mcp_timeout"
    if "not found" in message or "not_found" in message:
        return "mcp_not_found"
    return "mcp_failure"


def _empty_financial() -> dict[str, Any]:
    return {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []}


def _policy_tool(tools: list[str]) -> str | None:
    """Select one advertised policy tool without guessing an unavailable name."""

    available = set(tools)
    for candidate in _POLICY_TOOL_PREFERENCE:
        if candidate in available:
            return candidate
    policy_tools = sorted(
        tool
        for tool in available
        if isinstance(tool, str) and "policy" in tool.lower() and tool.startswith("get_")
    )
    return policy_tools[0] if policy_tools else None


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
        # Item rows must carry their owning order.  Seller registries in the
        # public gateway may omit it, but an explicitly supplied scope must
        # always match; seller linkage is checked against item rows below.
        if expected_domain == "item" and row_order_id != requested_order_id:
            valid = False
            continue
        if (
            expected_domain != "item"
            and row_order_id is not None
            and row_order_id != requested_order_id
        ):
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
    if data_order_id is None and values:
        valid = False
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
                if event_order_id is None and event.get("shipment_id") is not None:
                    valid = False
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
        event_order_id = event.get("order_id")
        if event_order_id not in (None, requested_order_id):
            continue
        if event_order_id is None:
            valid = False
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


def _has_invalid_claim(case: dict[str, Any]) -> bool:
    """Detect malformed or unsupported customer hypotheses before finalization."""

    request = case.get("customer_request")
    if not isinstance(request, dict) or request.get("claims") is None:
        return False
    claims = request.get("claims")
    if not isinstance(claims, list):
        return True
    supported_topics = _ISSUES | {"requested_full_refund"}
    for claim in claims:
        if not isinstance(claim, dict):
            return True
        claim_id, topic = claim.get("claim_id"), claim.get("topic")
        if claim_id is not None and (not isinstance(claim_id, str) or not claim_id):
            return True
        if not isinstance(topic, str) or not topic or topic not in supported_topics:
            return True
    return False


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
    # A seller registry row has no order identity in some gateway versions;
    # item linkage is therefore the authority for the seller IDs we retain.
    # Never let an unrelated seller row become an affected entity.
    if item_evidence is None:
        seller_ids = []
        sellers_valid = False
    else:
        linked_sellers = set(item_seller_ids)
        if any(seller_id not in linked_sellers for seller_id in seller_ids):
            sellers_valid = False
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

    if not _evidence_in_scope(order_evidence, "order", requested_order_id):
        raise ValueError("verifier: order evidence is outside the requested scope")
    for evidence in auxiliary:
        if (
            not _evidence_in_scope(evidence, evidence.get("domain"), requested_order_id)
            and result["assessment"]["primary_issue"] != "insufficient_evidence"
        ):
            raise ValueError("verifier: specialist evidence is outside the requested scope")
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


def _evidence_in_scope(
    evidence: dict[str, Any], expected_domain: str | None, requested_order_id: str
) -> bool:
    """Reject envelopes whose entity rows cannot be tied to this order.

    The evidence contract intentionally leaves ``data`` open because each MCP
    domain has a different shape.  This verifier-level check is therefore the
    boundary that prevents a valid envelope from smuggling in cross-case IDs.
    """

    if evidence.get("domain") != expected_domain:
        return False
    data = evidence.get("data")
    if expected_domain == "order":
        return isinstance(data, dict) and data.get("order_id") == requested_order_id
    if expected_domain in {"payment", "refund", "policy"}:
        return isinstance(data, dict) and data.get("order_id") == requested_order_id
    if expected_domain == "item":
        if not isinstance(data, list):
            return False
        return all(
            isinstance(row, dict)
            and row.get("order_id") == requested_order_id
            and isinstance(row.get("order_item_id"), str)
            and bool(row["order_item_id"])
            for row in data
        )
    if expected_domain == "seller":
        if not isinstance(data, list):
            return False
        return all(
            isinstance(row, dict)
            and isinstance(row.get("seller_id"), str)
            and bool(row["seller_id"])
            and (row.get("order_id") is None or row.get("order_id") == requested_order_id)
            for row in data
        )
    if expected_domain == "shipment":
        if not isinstance(data, dict):
            return False
        if data.get("order_id") not in (None, requested_order_id):
            return False
        events = data.get("events", [])
        if not isinstance(events, list):
            return False
        # An unscoped summary is acceptable only when it contains no entity or
        # event facts.  Any returned shipment/event must carry the order scope.
        if data.get("order_id") is None and any(
            data.get(key) is not None for key in ("shipment_id", "shipment_ids")
        ):
            return False
        return all(
            isinstance(event, dict)
            and event.get("order_id") == requested_order_id
            for event in events
        )
    return False


def _entity_supporting_refs(
    evidence_items: dict[str, dict[str, Any]], result: dict[str, Any]
) -> list[str]:
    """Select entity refs that support fields retained in a policy output."""

    entities = result["affected_entities"]
    refs = [evidence_items["get_order"]["evidence_ref"]]
    if entities.get("item_ids") and "get_order_items" in evidence_items:
        refs.append(evidence_items["get_order_items"]["evidence_ref"])
    if entities.get("seller_ids"):
        seller_source = "get_sellers" if "get_sellers" in evidence_items else "get_order_items"
        refs.append(evidence_items[seller_source]["evidence_ref"])
    if entities.get("shipment_ids") and "get_shipment_summary" in evidence_items:
        refs.append(evidence_items["get_shipment_summary"]["evidence_ref"])
    for claim in result.get("claim_assessments", []):
        claim_refs = claim.get("evidence_refs", [])
        if not isinstance(claim_refs, list) or len(claim_refs) != 1:
            continue
        for ref in claim_refs:
            for evidence in evidence_items.values():
                if evidence["evidence_ref"] == ref:
                    refs.append(ref)
    return list(dict.fromkeys(refs))


def _money(value: Any, label: str) -> Decimal:
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
    return int(value) if value == value.to_integral_value() else float(value)


def _policy_decision_data(data: dict[str, Any]) -> dict[str, Any] | None:
    """Read an explicit policy decision while tolerating a documented wrapper."""

    nested = data.get("policy_decision")
    if nested is None:
        nested = data.get("decision")
    if nested is not None and not isinstance(nested, dict):
        return None
    decision = dict(data)
    if isinstance(nested, dict):
        # A nested decision is authoritative, while envelope metadata remains
        # available for fields that are not part of the decision itself.
        decision.update(nested)
    for key in ("policy_status", "decision_status", "evidence_status"):
        status = decision.get(key)
        if isinstance(status, str) and status.lower() in {
            "inconclusive",
            "insufficient_evidence",
            "needs_investigation",
            "unavailable",
        }:
            return None
    if decision.get("applicable") is False or decision.get("policy_applicable") is False:
        return None
    return decision


def _policy_financial(data: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    nested = data.get("financial_resolution")
    if nested is not None:
        if not isinstance(nested, dict):
            return None, False
        return nested, True
    if "recommended_refund_brl" in data or "refund_lines" in data:
        if "recommended_refund_brl" not in data or "refund_lines" not in data:
            return None, False
        return {
            "currency": "BRL",
            "recommended_refund_brl": data["recommended_refund_brl"],
            "refund_lines": data["refund_lines"],
        }, True
    return None, False


def _validated_financial(
    financial: dict[str, Any], authoritative_ids: set[str]
) -> tuple[dict[str, Any], bool]:
    if financial.get("currency") != "BRL":
        return _empty_financial(), False
    raw_lines = financial.get("refund_lines")
    if not isinstance(raw_lines, list) or len(raw_lines) > 10:
        return _empty_financial(), False
    try:
        total = _money(financial.get("recommended_refund_brl"), "policy refund total")
        lines: list[dict[str, Any]] = []
        line_total = Decimal("0")
        for index, raw_line in enumerate(raw_lines):
            if not isinstance(raw_line, dict):
                return _empty_financial(), False
            reason = raw_line.get("reason_code")
            entity_id = raw_line.get("entity_id")
            if not isinstance(reason, str) or not reason or len(reason) > 80:
                return _empty_financial(), False
            if entity_id is not None and (
                not isinstance(entity_id, str)
                or len(entity_id) > 128
                or entity_id not in authoritative_ids
            ):
                return _empty_financial(), False
            amount = _money(raw_line.get("amount_brl"), f"policy refund line {index}")
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


def _policy_insufficient(
    case_id: str,
    base: dict[str, Any],
    supporting_refs: list[str],
    policy_ref: str | None = None,
) -> dict[str, Any]:
    """Preserve verified entities/claim assessments but expose policy uncertainty."""

    result = {
        **base,
        "case_id": case_id,
        "assessment": {
            "primary_issue": "insufficient_evidence",
            "case_status": "needs_investigation",
            "confidence": 0.0,
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": list(
            dict.fromkeys([*supporting_refs, *([policy_ref] if policy_ref else [])])
        ),
        "financial_resolution": _empty_financial(),
        "resolution_actions": [],
        "data_conflicts": base.get("data_conflicts", []),
    }
    return result


def _policy_output(
    case_id: str,
    base: dict[str, Any],
    policy_evidence: dict[str, Any],
    consumed_refs: list[str],
    supporting_refs: list[str],
    authoritative_ids: set[str],
) -> dict[str, Any]:
    """Apply a validated policy decision to verified facts, failing closed."""

    policy_ref = policy_evidence["evidence_ref"]
    if policy_ref not in consumed_refs or len(set(consumed_refs)) != len(consumed_refs):
        raise ValueError("verifier: policy evidence reference is not uniquely consumed")
    if not set(supporting_refs).issubset(consumed_refs):
        raise ValueError("verifier: policy supporting reference was not consumed")
    data = policy_evidence.get("data")
    if not isinstance(data, dict):
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
    scoped_order = data.get("order_id")
    requested_order = base["affected_entities"]["order_ids"]
    if scoped_order is not None and scoped_order not in requested_order:
        raise ValueError("MCP policy evidence returned a different order")
    decision = _policy_decision_data(data)
    if decision is None:
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
    issue = decision.get("primary_issue")
    status = decision.get("case_status")
    confidence = decision.get("confidence")
    actions = decision.get("resolution_actions")
    base_issue = base["assessment"]["primary_issue"]
    if base_issue == "insufficient_evidence" or (
        base_issue != issue
    ):
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
    if (
        not isinstance(issue, str)
        or issue not in _ISSUES
        or issue == "insufficient_evidence"
        or not isinstance(status, str)
        or status not in _STATUSES
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
        or not isinstance(actions, list)
        or len(actions) > 8
        or not all(
            isinstance(action, str) and 0 < len(action) <= 80 for action in actions
        )
        or len(actions) != len(set(actions))
        or (status == "action_required") != bool(actions)
        or (status != "action_required" and actions)
    ):
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
    allowed_actions = decision.get("allowed_actions")
    if allowed_actions is not None and (
        not isinstance(allowed_actions, list)
        or not all(isinstance(action, str) for action in allowed_actions)
        or not set(actions).issubset(allowed_actions)
    ):
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)

    financial = base.get("financial_resolution", _empty_financial())
    policy_financial, has_policy_financial = _policy_financial(decision)
    if has_policy_financial:
        if policy_financial is None:
            return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
        financial, valid = _validated_financial(policy_financial, authoritative_ids)
        if not valid:
            return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
    if status != "action_required" and (
        financial.get("recommended_refund_brl") != 0 or financial.get("refund_lines")
    ):
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
    refund_action = any(
        any(marker in action.lower() for marker in _REFUND_ACTION_MARKERS)
        for action in actions
    )
    if refund_action and financial.get("recommended_refund_brl") == 0 and not financial.get(
        "refund_lines"
    ):
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)

    parties = decision.get(
        "responsible_parties",
        base["root_cause_analysis"].get("responsible_parties", []),
    )
    if not isinstance(parties, list) or len(parties) > 5:
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
    seen_parties: set[tuple[str, str | None]] = set()
    seller_ids = set(base["affected_entities"].get("seller_ids", []))
    payment_refs = set(base["affected_entities"].get("payment_references", []))
    shipment_ids = set(base["affected_entities"].get("shipment_ids", []))
    for party in parties:
        party_type = party.get("party_type") if isinstance(party, dict) else None
        party_id = party.get("party_id") if isinstance(party, dict) else None
        if (
            not isinstance(party, dict)
            or set(party) != {"party_type", "party_id"}
            or party_type not in {
                "seller",
                "platform",
                "logistics_provider",
                "payment_provider",
                "customer",
                "unknown",
            }
            or not (isinstance(party_id, str) or party_id is None)
            or isinstance(party_id, str) and not 1 <= len(party_id) <= 128
        ):
            return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
        identity = (party_type, party_id)
        if identity in seen_parties:
            return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
        seen_parties.add(identity)
        in_scope = {
            "seller": party_id in seller_ids,
            "payment_provider": party_id in payment_refs,
            "logistics_provider": party_id in shipment_ids,
        }
        if party_type in in_scope and not in_scope[party_type]:
            return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
        if party_type in {"platform", "customer", "unknown"} and party_id is not None:
            return _policy_insufficient(case_id, base, supporting_refs, policy_ref)

    ranked_causes = decision.get(
        "ranked_causes", base["root_cause_analysis"].get("ranked_causes", [])
    )
    if not isinstance(ranked_causes, list) or len(ranked_causes) > 5 or any(
        not isinstance(cause, dict)
        or set(cause) != {"cause_code", "rank"}
        or not isinstance(cause.get("cause_code"), str)
        or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,79}", cause["cause_code"])
        or not isinstance(cause.get("rank"), int)
        or isinstance(cause.get("rank"), bool)
        or not 1 <= cause["rank"] <= 5
        for cause in ranked_causes
    ):
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
    causes = [cause["cause_code"] for cause in ranked_causes]
    ranks = [cause["rank"] for cause in ranked_causes]
    if len(causes) != len(set(causes)) or len(ranks) != len(set(ranks)):
        return _policy_insufficient(case_id, base, supporting_refs, policy_ref)
    result = {
        **base,
        "case_id": case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": status,
            "confidence": confidence,
        },
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": parties,
        },
        "evidence_refs": list(dict.fromkeys([*supporting_refs, policy_ref])),
        "financial_resolution": financial,
        "resolution_actions": actions,
    }
    return result


def _verify_policy_result(
    result: dict[str, Any], consumed_refs: list[str], policy_ref: str | None
) -> None:
    refs = result.get("evidence_refs", [])
    if (
        not isinstance(refs, list)
        or len(refs) != len(set(refs))
        or not set(refs).issubset(consumed_refs)
        or (policy_ref is not None and policy_ref not in refs)
    ):
        raise ValueError("verifier: policy output evidence_refs are unsupported")
    status = result["assessment"]["case_status"]
    actions = result["resolution_actions"]
    if (status == "action_required") != bool(actions):
        raise ValueError("verifier: policy status is inconsistent with actions")


def _verify_result_invariants(result: dict[str, Any], consumed_refs: list[str]) -> None:
    """Validate semantic invariants shared by every workflow slice.

    The JSON schema validates shape, while this function validates the
    relationships that determine whether a result is safe to submit.
    """

    refs = result.get("evidence_refs")
    if not isinstance(refs, list) or len(refs) != len(set(refs)):
        raise ValueError("verifier: evidence references are not unique")
    if not set(refs).issubset(consumed_refs):
        raise ValueError("verifier: output evidence references were not consumed")

    assessment = result["assessment"]
    issue = assessment["primary_issue"]
    status = assessment["case_status"]
    confidence = assessment["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("verifier: confidence is not numeric")
    if not isfinite(float(confidence)) or not 0 <= confidence <= 1:
        raise ValueError("verifier: confidence is outside [0, 1]")
    actions = result["resolution_actions"]
    if (status == "action_required") != bool(actions):
        raise ValueError("verifier: status is inconsistent with actions")
    if issue == "insufficient_evidence" and status != "needs_investigation":
        raise ValueError("verifier: insufficient evidence must need investigation")
    if issue == "insufficient_evidence" and confidence != 0:
        raise ValueError("verifier: insufficient evidence cannot have confidence")

    entities = result["affected_entities"]
    entity_sets: dict[str, set[str]] = {}
    for key in (
        "order_ids",
        "item_ids",
        "seller_ids",
        "payment_references",
        "shipment_ids",
    ):
        values = entities[key]
        if len(values) != len(set(values)):
            raise ValueError(f"verifier: duplicate {key}")
        entity_sets[key] = set(values)

    financial = result["financial_resolution"]
    total = _money(financial["recommended_refund_brl"], "output refund total")
    line_total = Decimal("0")
    for line in financial["refund_lines"]:
        entity_id = line["entity_id"]
        if entity_id is not None and not any(
            entity_id in values
            for values in entity_sets.values()
        ):
            raise ValueError("verifier: refund line entity is outside affected entities")
        line_total += _money(line["amount_brl"], "output refund line")
    if total != line_total:
        raise ValueError("verifier: financial total does not equal refund-line sum")
    refund_action = any(
        any(marker in action.lower() for marker in _REFUND_ACTION_MARKERS)
        for action in actions
    )
    if refund_action and total == 0 and not financial["refund_lines"]:
        raise ValueError("verifier: refund action has no refundable amount")
    if status != "action_required" and (total != 0 or financial["refund_lines"]):
        raise ValueError("verifier: non-action result contains a refund")

    causes = result["root_cause_analysis"]["ranked_causes"]
    cause_codes = [cause["cause_code"] for cause in causes]
    ranks = [cause["rank"] for cause in causes]
    if len(cause_codes) != len(set(cause_codes)) or len(ranks) != len(set(ranks)):
        raise ValueError("verifier: duplicate root-cause code or rank")
    parties = result["root_cause_analysis"]["responsible_parties"]
    seen_parties: set[tuple[str, str | None]] = set()
    for party in parties:
        identity = (party["party_type"], party["party_id"])
        if identity in seen_parties:
            raise ValueError("verifier: duplicate responsible party")
        seen_parties.add(identity)
        party_type, party_id = identity
        if party_id is not None:
            allowed = {
                "seller": entity_sets["seller_ids"],
                "payment_provider": entity_sets["payment_references"],
                "logistics_provider": entity_sets["shipment_ids"],
            }.get(party_type)
            if allowed is not None and party_id not in allowed:
                raise ValueError("verifier: responsible party is outside affected entities")

    claims = result.get("claim_assessments", [])
    claim_ids: set[str] = set()
    for claim in claims:
        claim_id = claim["claim_id"]
        if claim_id in claim_ids:
            raise ValueError("verifier: duplicate claim assessment")
        claim_ids.add(claim_id)
        claim_confidence = claim["confidence"]
        if not isfinite(float(claim_confidence)) or not 0 <= claim_confidence <= 1:
            raise ValueError("verifier: claim confidence is outside [0, 1]")
        claim_refs = claim["evidence_refs"]
        if not set(claim_refs).issubset(consumed_refs):
            raise ValueError("verifier: claim cites unconsumed evidence")

    for conflict in result["data_conflicts"]:
        sources = conflict["sources"]
        selected = conflict["selected_source"]
        if len(sources) < 2 or len(sources) != len(set(sources)):
            raise ValueError("verifier: invalid data conflict sources")
        if selected is not None and selected not in sources:
            raise ValueError("verifier: selected conflict source is not listed")


def _policy_required(case: dict[str, Any]) -> bool:
    return any(case.get(field) for field in _POLICY_REQUIRED_CASE_FIELDS)


async def _finalize_policy(
    *,
    case_id: str,
    order_id: str,
    base: dict[str, Any],
    gateway: EvidenceGateway,
    trace: TraceWriter,
    policy_tool: str | None,
    required: bool,
    consumed_refs: list[str],
    supporting_refs: list[str],
    authoritative_ids: set[str],
) -> dict[str, Any]:
    """Apply policy once for every workflow path and emit one decision event."""

    if policy_tool is None and not required:
        return base
    policy_evidence: dict[str, Any] | None = None
    failure_code = "policy_tool_unavailable"
    if policy_tool is not None:
        try:
            policy_evidence = await _investigate_policy(
                case_id, order_id, policy_tool, gateway, trace
            )
        except RuntimeError as exc:
            failure_code = _failure_decision_code(exc)
        except ValueError:
            failure_code = "policy_evidence_unavailable"
    if policy_evidence is None:
        result = _policy_insufficient(case_id, base, supporting_refs)
        trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor="policy-agent",
            target="verifier",
            decision_code=failure_code,
            evidence_refs=result["evidence_refs"],
        )
        return result

    policy_ref = policy_evidence["evidence_ref"]
    if policy_ref in consumed_refs:
        result = _policy_insufficient(case_id, base, supporting_refs)
        decision_code = "policy_evidence_unavailable"
    else:
        policy_consumed_refs = [*consumed_refs, policy_ref]
        result = _policy_output(
            case_id,
            base,
            policy_evidence,
            policy_consumed_refs,
            supporting_refs,
            authoritative_ids,
        )
        decision_code = (
            "policy_applied"
            if result["assessment"]["primary_issue"] != "insufficient_evidence"
            else "insufficient_policy_evidence"
        )
    _verify_policy_result(
        result,
        [*consumed_refs, policy_ref],
        policy_ref if policy_ref not in consumed_refs else None,
    )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier",
        decision_code=decision_code,
        evidence_refs=result["evidence_refs"],
    )
    return result


async def _investigate_policy(
    case_id: str,
    order_id: str,
    policy_tool: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    """Call only the policy tool selected from MCP discovery."""

    contracts = trace.contracts
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy-agent",
        decision_code="policy_lookup",
    )
    evidence = await _call_gateway(
        gateway, policy_tool, case_id=case_id, order_id=order_id
    )
    contracts.validate_evidence(evidence, f"MCP tool {policy_tool}")
    if evidence.get("domain") != "policy":
        raise ValueError(f"MCP {policy_tool} did not return policy evidence")
    if isinstance(evidence.get("data"), dict):
        scoped_order = evidence["data"].get("order_id")
        if scoped_order is not None and scoped_order != order_id:
            raise ValueError(f"MCP {policy_tool} returned evidence for a different order")
    ref = evidence["evidence_ref"]
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor="policy-agent",
        tool_name=policy_tool,
        evidence_refs=[ref],
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy-agent",
        target="verifier",
        decision_code="policy_evidence_ready",
        evidence_refs=[ref],
    )
    return evidence


def _financial_insufficient(
    case_id: str,
    order_id: str,
    evidence_refs: list[str],
    data_conflicts: list[dict[str, Any]] | None = None,
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
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "data_conflicts": data_conflicts or [],
        "financial_resolution": _empty_financial(),
        "resolution_actions": [],
    }


def _financial_decision(data: dict[str, Any], domain: str) -> str | None:
    issue = data.get("primary_issue")
    allowed = _PAYMENT_ISSUES if domain == "payment" else _REFUND_ISSUES
    if isinstance(issue, str) and issue in allowed:
        return issue
    if domain == "refund":
        state = data.get("refund_status")
        if state in {"pending", "refund_pending"}:
            return "refund_pending"
        if state in {"failed", "refund_failed"}:
            return "refund_failed"
    return None


def _authoritative_entity_ids(records: list[tuple[str, dict[str, Any]]]) -> set[str] | None:
    ids: set[str] = set()
    for _, data in records:
        order_id = data.get("order_id")
        if not isinstance(order_id, str) or not order_id:
            return None
        ids.add(order_id)
        for key in ("item_ids", "payment_references"):
            values = data.get(key)
            if values is None:
                continue
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                return None
            ids.update(values)
    return ids


def _financial_resolution(
    records: list[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, Any], bool]:
    """Read financial fields only from the scoped refund envelope."""

    source = next((data for domain, data in records if domain == "refund"), None)
    if source is None or "recommended_refund_brl" not in source or "refund_lines" not in source:
        return _empty_financial(), False
    raw_lines = source["refund_lines"]
    if not isinstance(raw_lines, list):
        return _empty_financial(), False
    authoritative_ids = _authoritative_entity_ids(records)
    if authoritative_ids is None:
        return _empty_financial(), False
    try:
        total = _money(source["recommended_refund_brl"], "recommended_refund_brl")
        lines: list[dict[str, Any]] = []
        line_total = Decimal("0")
        for index, raw_line in enumerate(raw_lines):
            if not isinstance(raw_line, dict):
                return _empty_financial(), False
            reason = raw_line.get("reason_code")
            entity_id = raw_line.get("entity_id")
            if not isinstance(reason, str) or not reason:
                return _empty_financial(), False
            if entity_id is not None and (
                not isinstance(entity_id, str) or entity_id not in authoritative_ids
            ):
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


def _financial_output(
    case_id: str,
    records: list[tuple[str, dict[str, Any]]],
    evidence_refs: list[str],
    requested_order_id: str,
) -> dict[str, Any]:
    payment_records = [data for domain, data in records if domain == "payment"]
    refund_records = [data for domain, data in records if domain == "refund"]
    if len(payment_records) != 1 or len(refund_records) != 1:
        return _financial_insufficient(case_id, requested_order_id, evidence_refs)
    decisions = []
    for domain, data in (("payment", payment_records[0]), ("refund", refund_records[0])):
        if "primary_issue" in data and _financial_decision(data, domain) is None:
            return _financial_insufficient(case_id, requested_order_id, evidence_refs)
        issue = _financial_decision(data, domain)
        if issue is not None:
            decisions.append(issue)
    if len(decisions) != 1:
        conflicts = None
        if len(decisions) > 1:
            conflicts = [
                {
                    "field": "financial_primary_issue",
                    "sources": ["payment:primary_issue", "refund:primary_issue"],
                    "selected_source": None,
                    "resolution_code": "conflicting_financial_decisions",
                }
            ]
        return _financial_insufficient(
            case_id, requested_order_id, evidence_refs, conflicts
        )
    issue = decisions[0]
    decision_data = payment_records[0] if issue in _PAYMENT_ISSUES else refund_records[0]
    status = decision_data.get("case_status")
    confidence = decision_data.get("confidence")
    actions = decision_data.get("resolution_actions")
    if (
        not isinstance(status, str)
        or status not in _STATUSES
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
        or not isinstance(actions, list)
        or not all(isinstance(action, str) for action in actions)
        or (status == "action_required") != bool(actions)
    ):
        return _financial_insufficient(case_id, requested_order_id, evidence_refs)
    financial, valid_financial = _financial_resolution(records)
    if not valid_financial:
        return _financial_insufficient(case_id, requested_order_id, evidence_refs)
    payment_refs: list[str] = []
    for data in (*payment_records, *refund_records):
        refs = data.get("payment_references")
        if refs is None:
            continue
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            return _financial_insufficient(case_id, requested_order_id, evidence_refs)
        payment_refs.extend(refs)
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
            "item_ids": [],
            "seller_ids": [],
            "payment_references": list(dict.fromkeys(payment_refs)),
            "shipment_ids": [],
        },
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": list(dict.fromkeys(evidence_refs)),
        "data_conflicts": [],
        "financial_resolution": financial,
        "resolution_actions": actions,
    }


def _verify_financial(
    result: dict[str, Any],
    records: list[tuple[str, dict[str, Any]]],
    evidence_refs: list[str],
    requested_order_id: str,
) -> None:
    if result["evidence_refs"] != list(dict.fromkeys(evidence_refs)):
        raise ValueError("verifier: output evidence_refs do not link consumed evidence")
    if any(data.get("order_id") != requested_order_id for _, data in records):
        raise ValueError("verifier: financial evidence is outside the requested order")
    financial = result["financial_resolution"]
    total = _money(financial["recommended_refund_brl"], "output refund total")
    line_total = sum(
        (_money(line["amount_brl"], "output refund line") for line in financial["refund_lines"]),
        Decimal("0"),
    )
    if total != line_total:
        raise ValueError("verifier: financial total does not equal refund-line sum")


def _requested_financial(case: dict[str, Any]) -> bool:
    request = case.get("customer_request")
    claims = request.get("claims") if isinstance(request, dict) else None
    topics = {
        claim.get("topic")
        for claim in claims or []
        if isinstance(claim, dict) and isinstance(claim.get("topic"), str)
    }
    return bool(topics & (_PAYMENT_ISSUES | _REFUND_ISSUES))


def _financial_tool(tools: list[str], domain: str) -> str | None:
    name = {"payment": "get_payment", "refund": "get_refund"}[domain]
    return name if name in tools else None


async def _solve_financial(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    tools: list[str],
    policy_tool: str | None = None,
    policy_required: bool = False,
    invalid_claim: bool = False,
) -> dict[str, Any]:
    contracts = trace.contracts
    records: list[tuple[str, dict[str, Any]]] = []
    evidence_refs: list[str] = []
    failure_codes: list[str] = []

    async def investigate(domain: str, tool_name: str, actor: str) -> None:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=f"{domain}_lookup",
        )
        evidence = await _call_gateway(
            gateway, tool_name, case_id=case_id, order_id=order_id
        )
        contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        data = evidence.get("data")
        if evidence.get("domain") != domain or not isinstance(data, dict):
            raise ValueError(f"MCP {tool_name} did not return {domain} evidence")
        if data.get("order_id") != order_id:
            raise ValueError(f"MCP {tool_name} returned evidence for a different order")
        ref = evidence["evidence_ref"]
        if ref in evidence_refs:
            raise ValueError("verifier: evidence reference is non-unique")
        evidence_refs.append(ref)
        records.append((domain, data))
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[ref],
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="verifier",
            decision_code="evidence_ready",
            evidence_refs=[ref],
        )

    await investigate("order", "get_order", "order-agent")
    for domain, actor in (("payment", "payment-agent"), ("refund", "refund-agent")):
        tool = _financial_tool(tools, domain)
        if tool is not None:
            # A specialist failure is a bounded, fail-closed outcome.  The
            # order envelope remains usable for correlation, but no missing
            # payment/refund fact is inferred from it.
            try:
                await investigate(domain, tool, actor)
            except RuntimeError as exc:
                failure_codes.append(_failure_decision_code(exc))
                continue
    result = _financial_output(case_id, records, evidence_refs, order_id)
    if invalid_claim:
        result = _financial_insufficient(case_id, order_id, evidence_refs)
    _verify_financial(result, records, evidence_refs, order_id)
    result = await _finalize_policy(
        case_id=case_id,
        order_id=order_id,
        base=result,
        gateway=gateway,
        trace=trace,
        policy_tool=policy_tool,
        required=policy_required,
        consumed_refs=evidence_refs,
        supporting_refs=evidence_refs,
        authoritative_ids=_authoritative_entity_ids(records) or set(),
    )
    _verify_result_invariants(result, evidence_refs + [
        ref for ref in result["evidence_refs"] if ref not in evidence_refs
    ])
    contracts.validate_output(result, f"output/{case_id}")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=failure_codes[0] if failure_codes else "output_contract_valid",
        evidence_refs=result["evidence_refs"],
    )
    return result


async def _solve_order_only(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    policy_tool: str | None = None,
    policy_required: bool = False,
    invalid_claim: bool = False,
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
    evidence = await _call_gateway(
        gateway, "get_order", case_id=case_id, order_id=order_id
    )
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
    if evidence["data"].get("primary_issue") in _PAYMENT_ISSUES | _REFUND_ISSUES or invalid_claim:
        result = _slice_insufficient_output(case_id, evidence, [evidence_ref])
    else:
        result = _output(case_id, evidence)
    _verify(result, evidence, order_id)
    consumed_refs = [evidence_ref]
    result = await _finalize_policy(
        case_id=case_id,
        order_id=order_id,
        base=result,
        gateway=gateway,
        trace=trace,
        policy_tool=policy_tool,
        required=policy_required,
        consumed_refs=consumed_refs,
        supporting_refs=[evidence_ref],
        authoritative_ids={order_id, *result["affected_entities"].get("item_ids", [])},
    )
    _verify_result_invariants(result, [*consumed_refs, *[
        ref for ref in result["evidence_refs"] if ref not in consumed_refs
    ]])
    contracts.validate_output(result, f"output/{case_id}")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code="output_contract_valid",
        evidence_refs=result["evidence_refs"],
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

    policy_tool = _policy_tool(tools)
    if _requested_financial(case):
        return await _solve_financial(
            case_id,
            order_id,
            gateway,
            trace,
            tools,
            policy_tool,
            _policy_required(case),
            _has_invalid_claim(case),
        )

    # Keep the issue-1 path contract-compatible for a gateway that only exposes
    # get_order. Once any issue-2 specialist is advertised, use every matching
    # discovered tool and fail closed for an unavailable specialist result.
    specialist_tools = {
        name: name
        for name in ("get_order_items", "get_sellers", "get_shipment_summary")
        if name in tools
    }
    if not specialist_tools:
        return await _solve_order_only(
            case_id,
            order_id,
            gateway,
            trace,
            policy_tool,
            _policy_required(case),
            _has_invalid_claim(case),
        )

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
    evidence = await _call_gateway(
        gateway, "get_order", case_id=case_id, order_id=order_id
    )
    contracts.validate_evidence(evidence, "MCP tool get_order")
    if evidence["domain"] != "order" or not isinstance(evidence["data"], dict):
        raise ValueError("MCP get_order did not return order evidence")
    if evidence["data"].get("order_id") != order_id:
        raise ValueError("MCP get_order returned evidence for a different order")
    evidence_items: dict[str, dict[str, Any]] = {"get_order": evidence}
    failure_codes: list[str] = []
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
        try:
            specialist = await _call_gateway(
                gateway, tool_name, case_id=case_id, order_id=order_id
            )
        except RuntimeError as exc:
            # Missing/timeout specialist evidence is not a reason to invent
            # entities.  The verifier will downgrade the slice because the
            # corresponding envelope is absent.
            failure_codes.append(_failure_decision_code(exc))
            continue
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
    elif not order_item_available:
        # The advertised shipment specialist may have timed out or returned
        # not-found.  Still make the coordinator's order envelope observable
        # as a handoff so failure cases retain an auditable A2A lifecycle.
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor="shipment-agent",
            target="verifier",
            decision_code="order_evidence_ready",
            evidence_refs=[evidence_items["get_order"]["evidence_ref"]],
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
    if _has_invalid_claim(case):
        result = _slice_insufficient_output(
            case_id,
            evidence_items["get_order"],
            consumed_refs,
            item_ids=result["affected_entities"]["item_ids"],
            seller_ids=result["affected_entities"]["seller_ids"],
            shipment_ids=result["affected_entities"]["shipment_ids"],
            claim_assessments=result.get("claim_assessments"),
            data_conflicts=result.get("data_conflicts"),
        )
    _verify_slice(
        result,
        evidence_items["get_order"],
        consumed[1:],
        order_id,
        consumed_refs,
    )
    authoritative_ids = {order_id}
    for key in ("item_ids", "payment_references", "seller_ids", "shipment_ids"):
        authoritative_ids.update(result["affected_entities"].get(key, []))
    result = await _finalize_policy(
        case_id=case_id,
        order_id=order_id,
        base=result,
        gateway=gateway,
        trace=trace,
        policy_tool=policy_tool,
        required=_policy_required(case),
        consumed_refs=consumed_refs,
        supporting_refs=_entity_supporting_refs(evidence_items, result),
        authoritative_ids=authoritative_ids,
    )
    _verify_result_invariants(result, [*consumed_refs, *[
        ref for ref in result["evidence_refs"] if ref not in consumed_refs
    ]])
    contracts.validate_output(result, f"output/{case_id}")
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=failure_codes[0] if failure_codes else "output_contract_valid",
        evidence_refs=result["evidence_refs"],
    )
    return result
