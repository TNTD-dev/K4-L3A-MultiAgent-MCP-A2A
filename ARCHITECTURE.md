# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
Input → Coordinator → Specialists → Verifier → Output
                         │              │
                         └── MCP ───────┴── Trace
```

The first path implements one specialist: the coordinator discovers and assigns
`get_order` to `order-agent`, then hands its validated evidence envelope to the
verifier. The CLI emits `case_received` and `case_finalized`; `solve_case` emits
the specialist and verifier events between them.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case manifest | Validate `case_id`, select the explicit `order_id`, discover `get_order`, and assign the lookup | `task_assigned` → order-agent |
| Order/item | explicit `order_id` and coordinator assignment | Call only discovered `get_order`; preserve the returned evidence envelope unchanged | `tool_result_consumed` → `handoff` to verifier |
| Payment | not in the first path | Reserved for a later specialist; no payment facts are inferred here | none |
| Shipment | not in the first path | Reserved for a later specialist; no shipment facts are inferred here | none |
| Policy | not in the first path | Reserved for a later specialist; no policy facts are inferred here | none |
| Verifier | validated MCP envelope and proposed output | Validate evidence and L3A output contracts, then mark verification complete | `verification_completed` → coordinator |

Nêu rõ actor nào được quyền gọi tool nào. Tránh cho mọi agent quyền truy vấn tất cả tool nếu không cần thiết.

## 3. A2A protocol

The observable message envelope is represented by trace fields: `case_id` is the
correlation key, `target` identifies the next actor, and `evidence_refs` identify
the immutable MCP result. The order specialist hands off exactly once after a
successful call; the verifier then returns exactly once. There is no retry loop
in this narrow path and no model reasoning is written to trace.

## 4. Evidence lifecycle

`EvidenceGateway.call` validates the MCP response against the public evidence
schema. The workflow validates again when a test stub is used, retains the
server-issued `evidence_ref`, maps only fields present in `data` to output, and
emits `tool_result_consumed`. Missing tools, identifiers, or envelopes fail the
case; no evidence reference is generated locally or reused between cases.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout | No | Fail the case; do not invent output facts | gateway error |
| Not found | No | Fail the case; do not convert absence into a reference | gateway error |
| Source conflict | No | This slice has one order source; invalid or incomplete decision fields become insufficient evidence | `verification_completed` only after validation |
| Invalid specialist result | No | Fail contract validation | no completion event |

Retry phải có giới hạn và idempotent. Không chuyển missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Before finalize the verifier checks the evidence and output schemas, requires the
`order` domain and an evidence `order_id` matching the requested ID, requires the
output evidence reference to equal the consumed reference, reconciles the refund
total with refund lines, and checks action/status consistency. The first path
leaves unsupported entities, causes, claims, and actions empty rather than
manufacturing them.

## 7. Reproducibility

The workflow is deterministic apart from trace event IDs and timestamps: one
`get_order` call per case, no concurrency, no random seed, and no model call.
`pyproject.toml` declares bounded dependency version ranges (there is no checked-in
lockfile); run `pytest -q` and `ruff check .`. API keys are never written to
source, output, or trace.
