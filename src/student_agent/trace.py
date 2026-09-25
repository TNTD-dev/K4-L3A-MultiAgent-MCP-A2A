from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import Contracts

_FORBIDDEN_TRACE_KEYS = {
    "prompt",
    "prompts",
    "cot",
    "chain_of_thought",
    "reasoning",
    "thoughts",
}
_TRACE_LEAK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bprompt\b",
        r"\b(?:chain[\s_-]+of[\s_-]+thought)\b",
        r"\bcot\b",
        r"\b(?:hidden|private)\s+(?:reasoning|thoughts?)\b",
        r"\bthought\s+process\b",
    )
)


def _trace_contains_leak(value: Any) -> bool:
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _TRACE_LEAK_PATTERNS)
    if isinstance(value, dict):
        return any(
            _trace_contains_leak(key) or _trace_contains_leak(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_trace_contains_leak(item) for item in value)
    return False


class TraceWriter:
    """Append observable workflow events. Never put prompts or chain-of-thought here."""

    def __init__(self, path: Path, contracts: Contracts) -> None:
        self.path = path
        self.contracts = contracts
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        *,
        case_id: str,
        event_type: str,
        actor: str,
        target: str | None = None,
        decision_code: str | None = None,
        tool_name: str | None = None,
        evidence_refs: list[str] | None = None,
        attributes: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": "day09-trace-event-v1",
            "event_id": f"evt_{secrets.token_urlsafe(18)}",
            "case_id": case_id,
            "event_type": event_type,
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "actor": actor,
        }
        optional = {
            "target": target,
            "decision_code": decision_code,
            "tool_name": tool_name,
            "evidence_refs": evidence_refs,
            "attributes": attributes,
        }
        event.update({key: value for key, value in optional.items() if value is not None})
        if any(isinstance(key, str) and key.lower() in _FORBIDDEN_TRACE_KEYS for key in event):
            raise ValueError("trace cannot contain prompt or chain-of-thought content")
        if isinstance(attributes, dict) and any(
            isinstance(key, str) and key.lower() in _FORBIDDEN_TRACE_KEYS for key in attributes
        ):
            raise ValueError("trace attributes cannot contain prompt or chain-of-thought content")
        if _trace_contains_leak(event):
            raise ValueError("trace cannot contain prompt or chain-of-thought content")
        self.contracts.validate_trace(event, "trace event")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        return event
