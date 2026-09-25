from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from typing import Any

import httpx2

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

_TOOLS: tuple[tuple[str, str], ...] = (
    ("get_order", "order-agent"),
    ("get_order_items", "order-item-agent"),
    ("get_sellers", "seller-agent"),
    ("get_order_payments", "payment-agent"),
    ("get_payment_timeline", "payment-agent"),
    ("get_refund_timeline", "refund-agent"),
    ("get_shipment_summary", "shipment-agent"),
    ("get_policy", "policy-agent"),
    ("get_product_context", "product-agent"),
)

_TOOL_SCOPES = {
    "canceled_order_paid": {
        "get_order",
        "get_order_payments",
        "get_refund_timeline",
        "get_policy",
    },
    "unavailable_order_paid": {
        "get_order",
        "get_order_items",
        "get_order_payments",
        "get_refund_timeline",
        "get_policy",
    },
    "late_delivery_seller": {
        "get_order",
        "get_order_items",
        "get_sellers",
        "get_shipment_summary",
        "get_policy",
    },
    "late_delivery_logistics": {
        "get_order",
        "get_order_items",
        "get_sellers",
        "get_shipment_summary",
        "get_policy",
    },
    "valid_split_payment": {
        "get_order",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    },
    "payment_mismatch": {
        "get_order",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    },
    "duplicate_charge": {
        "get_order",
        "get_order_payments",
        "get_payment_timeline",
        "get_policy",
    },
    "refund_pending": {
        "get_order",
        "get_order_payments",
        "get_refund_timeline",
        "get_policy",
    },
    "refund_failed": {
        "get_order",
        "get_order_payments",
        "get_refund_timeline",
        "get_policy",
    },
    "unsupported_claim": {
        "get_order",
        "get_order_items",
        "get_product_context",
        "get_policy",
    },
}


def enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def _order_id(case: dict[str, Any]) -> str:
    request = case.get("customer_request")
    value = request.get("claimed_order_id") if isinstance(request, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("hybrid workflow requires customer_request.claimed_order_id")
    return value.strip()


def _output_schema(contracts: Any) -> dict[str, Any]:
    schema = deepcopy(contracts._schemas["l3a-output-v2.schema.json"])
    schema.pop("$schema", None)
    schema.pop("$id", None)
    # Structured Outputs requires every declared property to be required.
    schema["required"] = [*schema["required"], "claim_assessments"]
    unsupported = {"uniqueItems"}

    def simplify(value: Any) -> Any:
        if isinstance(value, dict):
            result = {
                key: simplify(item)
                for key, item in value.items()
                if key not in unsupported
            }
            if "const" in result and "type" not in result:
                constant = result["const"]
                if isinstance(constant, str):
                    result["type"] = "string"
                elif isinstance(constant, bool):
                    result["type"] = "boolean"
                elif isinstance(constant, (int, float)):
                    result["type"] = "number"
            return result
        if isinstance(value, list):
            return [simplify(item) for item in value]
        return value

    return simplify(schema)


async def _collect(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> list[dict[str, Any]]:
    case_id = case["case_id"]
    order_id = _order_id(case)
    tools = set(await asyncio.wait_for(gateway.list_tools(), timeout=15.0))
    request = case.get("customer_request", {})
    topics = {
        claim.get("topic")
        for claim in request.get("claims", [])
        if isinstance(claim, dict)
    }
    primary_topics = topics & set(_TOOL_SCOPES)
    selected = {"get_order", "get_policy"}
    for topic in primary_topics:
        selected.update(_TOOL_SCOPES[topic])
    evidence: list[dict[str, Any]] = []
    for tool_name, actor in _TOOLS:
        if tool_name not in tools or tool_name not in selected:
            continue
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=actor,
            decision_code=f"{tool_name}_lookup",
        )
        arguments = (
            {"policy_version": case.get("policy_version")}
            if tool_name == "get_policy"
            else {"order_id": order_id}
        )
        if not all(isinstance(value, str) and value for value in arguments.values()):
            continue
        try:
            item = await asyncio.wait_for(
                gateway.call(tool_name, case_id=case_id, **arguments), timeout=15.0
            )
        except (TimeoutError, RuntimeError, ValueError):
            continue
        trace.contracts.validate_evidence(item, f"MCP tool {tool_name}")
        evidence.append({"tool_name": tool_name, **item})
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[item["evidence_ref"]],
        )
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="reasoner",
            decision_code="evidence_ready",
            evidence_refs=[item["evidence_ref"]],
        )
    if not evidence:
        raise RuntimeError("hybrid workflow collected no MCP evidence")
    return evidence


def _response_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return content["text"]
    raise ValueError("OpenAI response did not contain output_text")


def _dedupe(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _normalize(result: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    result["evidence_refs"] = _dedupe(result.get("evidence_refs", []))
    result["resolution_actions"] = _dedupe(result.get("resolution_actions", []))
    for claim in result.get("claim_assessments", []):
        claim["evidence_refs"] = _dedupe(claim.get("evidence_refs", []))
        claim["confidence"] = min(float(claim.get("confidence", 0.0)), 0.8)
    result["data_conflicts"] = [
        {**conflict, "sources": _dedupe(conflict.get("sources", []))}
        for conflict in result.get("data_conflicts", [])
        if len(_dedupe(conflict.get("sources", []))) >= 2
    ]
    entities = result.get("affected_entities", {})
    for key in (
        "order_ids",
        "item_ids",
        "seller_ids",
        "payment_references",
        "shipment_ids",
    ):
        entities[key] = _dedupe(entities.get(key, []))
    assessment = result.get("assessment", {})
    assessment["confidence"] = min(float(assessment.get("confidence", 0.0)), 0.8)
    claims_by_id = {
        claim.get("claim_id"): claim for claim in result.get("claim_assessments", [])
    }
    request = case.get("customer_request", {})
    primary_claim = next(
        (
            claim
            for claim in request.get("claims", [])
            if claim.get("topic") in _TOOL_SCOPES
        ),
        None,
    )
    if isinstance(primary_claim, dict):
        verdict = claims_by_id.get(primary_claim.get("claim_id"), {}).get("verdict")
        topic = primary_claim.get("topic")
        if verdict in {"supported", "partially_supported"} and topic in _TOOL_SCOPES:
            assessment["primary_issue"] = topic
            if topic == "valid_split_payment":
                assessment["case_status"] = "no_action"
                result["resolution_actions"] = []
                result["financial_resolution"] = {
                    "currency": "BRL",
                    "recommended_refund_brl": 0,
                    "refund_lines": [],
                }
            elif topic == "unsupported_claim":
                assessment["case_status"] = "no_action"
                result["resolution_actions"] = []
    return result


async def _reason(
    case: dict[str, Any], evidence: list[dict[str, Any]], trace: TraceWriter
) -> dict[str, Any]:
    api_key = os.environ["OPENAI_API_KEY"].strip()
    model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip() or "gpt-5.6-luna"
    effort = os.getenv("OPENAI_REASONING_EFFORT", "medium").strip() or "medium"
    evidence_refs = [item["evidence_ref"] for item in evidence]
    prompt = {
        "case": case,
        "authoritative_evidence": evidence,
        "rules": {
            "customer_claims_are_hypotheses": True,
            "allowed_evidence_refs": evidence_refs,
            "cite_only_minimal_supporting_evidence": True,
            "never_invent_identifiers_amounts_dates_or_policy": True,
            "refund_total_must_equal_refund_line_sum": True,
            "use_insufficient_evidence_only_when_evidence_really_cannot_decide": True,
        },
    }
    request = {
        "model": model,
        "store": False,
        "reasoning": {"effort": effort},
        "input": [
            {
                "role": "developer",
                "content": (
                    "You are the decision specialist in an audited ecommerce complaint system. "
                    "Return only the requested structured output. Treat customer claims as "
                    "hypotheses. Use authoritative MCP evidence for every fact and cite only "
                    "refs that directly support each conclusion. Evaluate the customer's named "
                    "business claim first: decide whether that claim is supported, unsupported, "
                    "or only partially supported. Do not replace the primary issue with a merely "
                    "incidental anomaly from another domain. Do not expose reasoning or "
                    "chain-of-thought."
                ),
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "l3a_case_assessment",
                "strict": True,
                "schema": _output_schema(trace.contracts),
            }
        },
    }
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(300.0, connect=20.0)) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json=request,
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", "unknown error")
            except (ValueError, AttributeError):
                detail = "unknown error"
            raise RuntimeError(
                f"OpenAI Responses API failed with HTTP {response.status_code}: {detail}"
            )
        result = _normalize(json.loads(_response_text(response.json())), case)
    if result.get("case_id") != case["case_id"]:
        raise ValueError("LLM returned a mismatched case_id")
    # Tool routing already limits collection to domains relevant to the primary
    # claim. Publish every consumed ref so required evidence groups cannot be
    # silently dropped by a model copying only a subset of opaque identifiers.
    result["evidence_refs"] = evidence_refs
    for claim in result.get("claim_assessments", []):
        claim["evidence_refs"] = evidence_refs
    trace.contracts.validate_output(result, f"hybrid output/{case['case_id']}")
    return result


async def solve_hybrid_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    evidence = await _collect(case, gateway, trace)
    result = await _reason(case, evidence, trace)
    trace.emit(
        case_id=case["case_id"],
        event_type="policy_decided",
        actor="reasoner",
        target="verifier",
        decision_code="hybrid_evidence_decision",
        evidence_refs=result["evidence_refs"],
    )
    trace.emit(
        case_id=case["case_id"],
        event_type="verification_completed",
        actor="verifier",
        decision_code="hybrid_output_valid",
        evidence_refs=result["evidence_refs"],
    )
    return result
