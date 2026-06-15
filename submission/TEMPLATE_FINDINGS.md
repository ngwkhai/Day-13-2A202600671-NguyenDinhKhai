# Findings — Team <name>

For each fault you found, fill one row AND a matching entry in `solution/findings.json`
(the JSON is what's scored; this MD is for humans). Evidence must come from YOUR telemetry.

| fault_class | evidence (metric + observed value + trace ids) | root cause | fix (config / wrapper) |
|---|---|---|---|
| error_spike | shipped `tool_error_rate=0.18`, retry disabled; tool-error telemetry on `pub-011`, `pub-018`, `pub-047` | intermittent tool failures without backoff | set tool error to 0, bounded retry |
| latency_spike | public telemetry p95 latency 8895ms, max 35889ms (`pub-046`) | verbose context/resampling/tool retries | short prompt, small context, cache |
| cost_blowup | avg 9135 total tokens, p95 11078; cost score 0.368 | verbose prompt/context and unnecessary samples | remove few-shot, `self_consistency=1`, cap completion |
| quality_drift | public drift score 0.659; shipped `session_drift_rate=0.06` | session drift and stale context | reset context, set drift 0 |
| infinite_loop | shipped `loop_guard=false`, `max_steps=12`; wrapper now tracks repeated actions | no loop guard/tool cap | enable loop guard, max_steps 5, tool_budget 3 |
| tool_failure | `item_not_found` / `destination_not_served`; config had unicode off and MacBook override | normalization/catalog bug | normalize unicode, clear override, sanitize fields |
| pii_leak | PII-bearing requests `pub-024`, `pub-110`, `pub-115`; output/log redaction now applied | prompt/config allowed echoing contact info | `redact_pii=true`, no-PII prompt, wrapper redaction |
| fabrication | no-total refusals for `pub-001`, `pub-040`, `pub-105`, `pub-108` | original prompt always asked for total | refuse missing/out-of-stock/not-served |
| arithmetic_error | `postprocessed=true` on 113 calls; earlier malformed total `540,317,50` | LLM arithmetic/format instability | deterministic trace recompute |
| tool_overuse | 72/117 final calls used 3 tools; original `tool_budget=0` | over-calling tools | each tool once, `tool_budget=3` |
| prompt_injection | private twist documented; wrapper strips fake-note/control instructions | notes treated as executable instructions | prompt notes as data, sanitize notes |
