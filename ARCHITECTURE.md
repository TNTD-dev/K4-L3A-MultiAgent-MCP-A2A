# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

The workflow implements an order specialist plus optional payment and refund
specialists. The coordinator discovers `get_order`, `get_payment`, and
`get_refund` (only invoking names returned by MCP discovery), assigns each
available specialist, and hands validated evidence envelopes to the verifier.
The CLI emits `case_received` and `case_finalized`; `solve_case` emits the
specialist and verifier events between them.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case manifest | Validate `case_id`, select the explicit `order_id`, discover domain tools, and assign lookups | `task_assigned` → specialists |
| Order/item | explicit `order_id` and coordinator assignment | Call only discovered `get_order`; preserve the returned evidence envelope unchanged | `tool_result_consumed` → `handoff` to verifier |
| Payment | explicit order lookup and coordinator assignment | Call discovered `get_payment`; preserve payment references and explicit payment decision fields from the scoped envelope | `tool_result_consumed` → `handoff` to verifier |
| Refund | explicit order lookup and coordinator assignment | Call discovered `get_refund`; preserve refund state, refund lines, and explicit financial fields from the scoped envelope | `tool_result_consumed` → `handoff` to verifier |
| Shipment | not in this slice | Reserved for a later specialist; no shipment facts are inferred here | none |
| Policy | not in the first path | Reserved for a later specialist; no policy facts are inferred here | none |
| Verifier | validated MCP envelope and proposed output | Validate evidence and L3A output contracts, then mark verification complete | `verification_completed` → coordinator |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

The observable message envelope is represented by trace fields: `case_id` is the
correlation key, `target` identifies the next actor, and `evidence_refs` identify
immutable MCP results. Each invoked specialist hands off once after a successful
call; the verifier then returns once. There is no retry loop in this narrow path
and no model reasoning is written to trace.

## 4. Evidence lifecycle

`EvidenceGateway.call` validates the MCP response against the public evidence
schema. The workflow validates again when a test stub is used; the gateway
request carries the active `case_id`, while the public evidence envelope has no
response `case_id` field, so the workflow scopes returned data by the requested
`order_id`. It retains server-issued
`evidence_ref` values, maps only fields present in `data` to output, and emits
`tool_result_consumed`. Missing tools, identifiers, out-of-scope envelopes, or
conflicting payment/refund decisions fail closed as insufficient evidence; no
evidence reference is generated locally or reused between cases.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | No | Fail the case; do not invent output facts | gateway error |
| Not found | No | Fail the case; do not convert absence into a reference | gateway error |
| Source conflict | No | Conflicting payment/refund decisions or invalid/incomplete financial fields become insufficient evidence | `verification_completed` only after validation |
| Invalid specialist result | No | Fail contract validation | no completion event |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Before finalize the verifier checks the evidence and output schemas, requires
each consumed domain envelope and its `order_id` to match the active lookup,
requires output evidence references to equal the consumed server references,
reconciles the BRL refund total with refund lines using `Decimal`, and checks
action/status consistency. Payment/refund issue decisions are accepted only
from payment/refund MCP data; for the financial slice, refund totals and lines
are accepted only from the scoped refund envelope. The order envelope remains
the source only for the original non-financial order-only path. A customer
topic is never used as a decision.
Unsupported entities, causes, claims, and actions remain empty rather than
being manufactured.

## 7. Reproducibility

The workflow is deterministic apart from trace event IDs and timestamps: one
call per discovered domain tool per case, no concurrency, no random seed, and
no model call.
`pyproject.toml` declares bounded dependency version ranges (there is no checked-in
lockfile); run `pytest -q` and `ruff check .`. API keys are never written to
source, output, or trace.
