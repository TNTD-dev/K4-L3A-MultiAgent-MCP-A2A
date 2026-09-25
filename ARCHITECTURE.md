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
were advertised, and passes their validated evidence envelopes to the verifier.
The CLI emits `case_received` and `case_finalized`; `solve_case` emits
assignments, per-result consumption, handoffs, and verification events between
them. The input's customer message supplies only lookup and claim identifiers;
entity facts and claim verdicts are projected from MCP envelopes.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case manifest | Validate `case_id`, select the explicit `order_id`, discover tools, and assign only available scoped specialists | `task_assigned` → available specialist |
| Order/item | explicit `order_id` and coordinator assignment | Call only discovered `get_order`, `get_order_items`, and `get_sellers`; preserve envelopes and project unique item/seller IDs | `tool_result_consumed` → `handoff` to verifier |
| Payment | not in the first path | Reserved for a later specialist; no payment facts are inferred here | none |
| Shipment | explicit `order_id` and coordinator assignment | Call only discovered `get_shipment_summary`; use explicit late events and shipment IDs only | `tool_result_consumed` → `handoff` to verifier |
| Policy | not in the first path | Reserved for a later specialist; no policy facts are inferred here | none |
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
schema. The workflow validates again when a test stub is used, retains each
server-issued `evidence_ref` unchanged, filters scoped rows/events, maps only
fields present in `data` to output, and emits one `tool_result_consumed` per
result. Missing or malformed specialist data produces an
insufficient-evidence result; no evidence reference is generated locally or
reused between cases.

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
Unsupported or missing decision domains remain insufficient; the workflow never
manufactures a shipment ID, seller, cause, action, or refund from a customer
message. Claim topics are hypotheses, not verdicts.

## 7. Reproducibility

The workflow is deterministic apart from trace event IDs and timestamps: one
call per discovered order/item/seller/shipment tool per case, no concurrency, no
random seed, and no model call.
`pyproject.toml` declares bounded dependency version ranges (there is no checked-in
lockfile); run `pytest -q` and `ruff check .`. API keys are never written to
source, output, or trace.
