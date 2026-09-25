# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

The coordinator discovers the server tools, assigns only specialists whose tools
were advertised and whose domain is relevant to the case, and passes their
validated evidence envelopes to the verifier. Order/item/seller/shipment cases
use the entity slice; payment/refund cases use the financial slice, which
requires payment and refund evidence before it can finalize. When a policy tool
is advertised, the policy specialist consumes the verified slice and its
validated `policy` envelope to produce the final issue/status/action decision.
Cases carrying `policy_version` require this step; an unavailable tool or
evidence yields a visible insufficient-evidence result and `policy_decided`
trace event rather than retaining a pre-policy decision.
The CLI emits `case_received` and `case_finalized`; `solve_case` emits
assignments, per-result consumption, handoffs, and verification events between
them. The input's customer message supplies only lookup and claim identifiers;
entity facts and claim verdicts are projected from MCP envelopes.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case manifest | Validate `case_id`, select the explicit `order_id`, discover tools, and assign only available scoped specialists | `task_assigned` → available specialist |
| Order/item | explicit `order_id` and coordinator assignment | Call only discovered `get_order`, `get_order_items`, and `get_sellers`; preserve envelopes and project unique item/seller IDs | `tool_result_consumed` → `handoff` to verifier |
| Payment | explicit order lookup and financial claim scope | Call only discovered `get_payment`; derive payment issue and payment references from its scoped envelope | `tool_result_consumed` → `handoff` to verifier |
| Refund | explicit order lookup and financial claim scope | Call only discovered `get_refund`; derive refund state, BRL totals, and refund lines from its scoped envelope | `tool_result_consumed` → `handoff` to verifier |
| Shipment | explicit `order_id` and coordinator assignment | Call only discovered `get_shipment_summary`; use explicit late events and shipment IDs only | `tool_result_consumed` → `handoff` to verifier |
| Policy | validated operational/financial handoffs and discovered policy tool | Call only the discovered policy tool, apply explicit policy fields, and fail closed when policy evidence is missing or inconclusive | `policy_decided` → verifier |
| Verifier | validated MCP envelope and proposed output | Validate evidence and L3A output contracts, then mark verification complete | `verification_completed` → coordinator |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

The observable message envelope is represented by trace fields: `case_id` is the
correlation key, `target` identifies the next actor, and `evidence_refs` identify
immutable MCP results. The coordinator emits one assignment per specialist;
each specialist hands off its consumed refs exactly once after successful calls;
the verifier then returns exactly once. There is no retry loop and no model
reasoning is written to trace.

## 4. Evidence lifecycle

`EvidenceGateway.call` validates each MCP response against the public evidence
schema. The gateway request carries the active `case_id`; the public evidence
envelope has no response `case_id` field, so the workflow scopes returned data
by the requested `order_id`. The workflow validates again when a test stub is
used, retains each server-issued `evidence_ref` unchanged, filters scoped
rows/events, maps only fields present in `data` to output, and emits one
`tool_result_consumed` per relevant result. Missing or malformed specialist
data produces an insufficient-evidence result; no evidence reference is
generated locally or reused between cases.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | No | Fail the case; do not invent output facts | gateway error |
| Not found | No | Fail the case; do not convert absence into a reference | gateway error |
| Source conflict | No | Record the conflict and fail closed with insufficient evidence | `verification_completed` only after validation |
| Invalid evidence envelope | No | Fail contract validation | no completion event |
| Malformed specialist data | No | Emit a contract-shaped insufficient result with consumed refs | `verification_completed` after validation |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Before finalize the verifier checks the evidence and output schemas, requires the
`order` domain and an evidence `order_id` matching the requested ID, requires
all output refs to equal the consumed refs, filters and deduplicates in-scope
order/item/seller/shipment identifiers, and preserves claim refs. Contradictory
seller/logistics late events are recorded as a conflict and remain insufficient.
For the financial slice, payment decisions come only from payment evidence;
refund states, recommended totals, and refund lines come only from refund
evidence. Every refund-line `entity_id` must match an authoritative in-scope
order, item, or payment reference. A discovered policy envelope must contain an
explicit schema-supported issue, status, confidence, and unique bounded actions;
action/status and policy refund totals are checked before finalization. Policy
responsible sellers must match an authoritative seller ID. Unsupported or
missing decision domains remain insufficient, and policy output cites only
consumed refs that support the retained facts plus the policy ref. The workflow
never manufactures a shipment ID, seller, cause, action, or refund from a
customer message. Claim topics are hypotheses used only to select relevant
specialists, never verdicts.

## 7. Reproducibility

The workflow is deterministic apart from trace event IDs and timestamps: one
call per discovered relevant tool per case, no concurrency, no random seed, and
no model call.
`pyproject.toml` declares bounded dependency version ranges (there is no checked-in
lockfile); run `pytest -q` and `ruff check .`. API keys are never written to
source, output, or trace.
