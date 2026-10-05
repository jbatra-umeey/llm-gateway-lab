# Architecture decisions

## Reserve before calling

A single SQLite write transaction checks used plus reserved quota and records a reservation. Separate read/check/write steps would race under concurrent requests.

## Cache within the trust boundary

Cache keys include tenant, task, request, provider model identities and policy settings. The caller must supply an authenticated tenant identity in a service deployment.

## Treat uncertain usage conservatively

Timeouts cannot prove a provider did no work. The lab consumes the full reservation on an unknown result. Crashed requests remain reserved for explicit reconciliation.

## Next engineering step

Add per-provider tokenizers and billing reconciliation, request idempotency, single-flight cache misses, distributed circuits and a quality/latency/cost evaluation dataset.
