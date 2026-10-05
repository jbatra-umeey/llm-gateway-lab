# Five-minute walkthrough

1. Run `demo` and follow the failure, fallback, cache-hit and cross-tenant requests.
2. Explain why the failed provider consumes its reservation.
3. Open the concurrent reservation test and explain the write transaction.
4. Explain how the cache key changes when the model configuration changes.
5. Distinguish demo quota credits from a real billing guarantee.

Discussion prompts: How would you reconcile a request after a crash? How would
you prevent duplicate in-flight calls? What metrics and held-out tasks would
justify choosing a cheaper model for a given request class?

Describe this as an AI-assisted reference implementation. Use real deployments
and business results only when you can substantiate them from your own work.
