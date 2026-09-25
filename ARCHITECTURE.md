# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → MCP specialists → Decision specialist → Verifier → Output
                         │                      │              │
                         └──────── MCP ─────────┴──────────────┴── Trace
```

The coordinator discovers the server tools, assigns only specialists whose tools
were advertised and whose domain is relevant to the case, and passes their
validated evidence envelopes to the decision specialist. Order/item/seller/shipment cases
use the entity slice; payment/refund cases use the financial slice, which
requires payment and refund evidence before it can finalize. When a policy tool
is advertised, the policy specialist consumes the verified slice and its
validated `policy` envelope to produce the final issue/status/action decision.
Cases carrying `policy_version` require this step; an unavailable tool or
evidence yields a visible insufficient-evidence result and `policy_decided`
trace event rather than retaining a pre-policy decision. When `OPENAI_API_KEY`
is configured, the hybrid path asks GPT-5.6 Luna for a schema-constrained
decision over that bounded evidence slice. Deterministic normalization then
deduplicates identifiers, caps confidence, enforces no-action split-payment
semantics, restores every consumed evidence reference, and validates the public
output contract before finalization. Without that environment variable, the
fully deterministic specialist workflow remains available.
The CLI emits `case_received` and `case_finalized`; `solve_case` emits
assignments, per-result consumption, handoffs, and verification events between
them. The input's customer message supplies only lookup and claim identifiers;
entity facts and claim verdicts are projected from MCP envelopes.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case manifest | Validate `case_id`, select the explicit `order_id`, discover tools, and assign only available scoped specialists | `task_assigned` → available specialist |
| Order/item | explicit `order_id` and coordinator assignment | Call only discovered `get_order`, `get_order_items`, and `get_sellers`; preserve envelopes and project unique item/seller IDs | `tool_result_consumed` → `handoff` to verifier |
| Payment | explicit order lookup and financial claim scope | Call only discovered `get_order_payments` and `get_payment_timeline`; derive payment issue and references from their scoped envelopes | `tool_result_consumed` → `handoff` to decision specialist |
| Refund | explicit order lookup and financial claim scope | Call only discovered `get_refund_timeline`; derive refund state, BRL totals, and refund lines from its scoped envelope | `tool_result_consumed` → `handoff` to decision specialist |
| Shipment | explicit `order_id` and coordinator assignment | Call only discovered `get_shipment_summary`; use explicit late events and shipment IDs only | `tool_result_consumed` → `handoff` to verifier |
| Policy | validated operational/financial handoffs and discovered policy tool | Call only the discovered policy tool, apply explicit policy fields, and fail closed when policy evidence is missing or inconclusive | `policy_decided` → verifier |
| Decision specialist | claim-scoped validated evidence | Produce a strict-schema proposal without inventing facts or identifiers; never write prompts or hidden reasoning to trace | `policy_decided` → verifier |
| Verifier | validated MCP envelope and proposed output | Validate evidence and L3A output contracts, then mark verification complete | `verification_completed` → coordinator |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

The observable message envelope is represented by trace fields: `case_id` is the
correlation key, `target` identifies the next actor, and `evidence_refs` identify
immutable MCP results. The coordinator emits one assignment per specialist;
each specialist hands off its consumed refs exactly once after successful calls;
the decision specialist and verifier then return exactly once. There is no retry
loop and no prompt or model reasoning is written to trace.

## 4. Evidence lifecycle

`EvidenceGateway.call` validates each MCP response against the public evidence
schema. The gateway request carries the active `case_id`; the public evidence
envelope has no response `case_id` field, so the workflow scopes returned data
by the requested `order_id`. The workflow validates again when a test stub is
used, retains each server-issued `evidence_ref` unchanged, filters scoped
rows/events, maps only fields present in `data` to output, and emits one
`tool_result_consumed` per relevant result. Missing or malformed specialist
data produces an insufficient-evidence result; no evidence reference is
generated locally or reused between cases. Hybrid routing selects three to five
tools from the customer's named claim topics. Every successfully consumed
reference is copied into the output, its claim assessments, and the final
`policy_decided` and `verification_completed` events.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | No (one bounded call) | Downgrade the affected specialist slice; do not invent output facts | `verification_completed:mcp_timeout` |
| Not found | No | Downgrade the affected specialist slice; do not convert absence into a reference | `verification_completed:mcp_not_found` |
| Source conflict | No | Record the conflict and fail closed with insufficient evidence | `verification_completed` only after validation |
| Invalid evidence envelope | No | Fail contract validation | no completion event |
| Malformed specialist data | No | Emit a contract-shaped insufficient result with consumed refs | `verification_completed` after validation |
| Model/API failure | No | Fail the run visibly; never replace authoritative evidence with a guessed result | no completion event |

The workflow uses a five-second single-attempt bound for every gateway call. A
timeout or not-found response is never retried, so an audited request cannot be
duplicated or race a late response. Optional specialist failures retain only the
already-consumed order envelope, emit a bounded public decision code, and fail
closed. Missing evidence is never converted into a guessed entity, claim,
amount, responsible party, or action.

## 6. Verification invariants

Before finalize the verifier checks the evidence and output schemas, requires the
`order` domain and an evidence `order_id` matching the requested ID, requires
all output refs to be consumed, filters and deduplicates in-scope
order/item/seller/shipment identifiers, and preserves claim refs. Item,
financial, policy, and shipment rows must carry the requested order scope;
seller rows are retained only when linked to an in-scope item. Contradictory
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

The hybrid boundary additionally requires the returned `case_id` to match the
input, publishes exactly the consumed claim-scoped evidence set, removes
duplicate actions and identifiers, drops invalid one-source conflicts, caps
model confidence at `0.80`, and keeps the primary issue aligned with a supported
or partially supported primary claim. A valid split payment always resolves to
`no_action`, no refund, and no resolution action.

## 7. Reproducibility

The deterministic path is reproducible apart from trace event IDs and
timestamps. The hybrid path records its model name through configuration,
defaults to `gpt-5.6-luna` with medium reasoning effort, makes one call per
selected tool and one model call per case, and performs no retries. Remote model
generation is not bit-for-bit deterministic. `DAY09_CONCURRENCY` controls the
number of cases in flight and defaults to one. Artifact validation requires every case
to have `case_received → task_assigned → handoff → verification_completed →
case_finalized` in order, and checks that every submitted evidence ref has a
`tool_result_consumed` event. Trace payloads contain no prompts or chain of
thought.
`pyproject.toml` declares bounded dependency version ranges (there is no checked-in
lockfile); run `pytest -q` and `ruff check .`. API keys are never written to
source, output, or trace.
