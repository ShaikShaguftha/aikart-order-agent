# Performance Audit

## Scope
Review of agent execution, session handling, database access, and webhook processing for latency, reliability, and resource usage. No code changes were made in this phase; findings and recommendations are documented for implementation planning.

## Findings

1. LLM tool loop
The chat agent allows up to 5 LLM invocations per user query (max_iterations = 5 in backend/agent.py). Simple queries that need one tool call still reserve the possibility of 5 Groq API calls, increasing latency and token cost.
Recommendation: Measure average LLM calls per query. If most queries complete in 1-2 calls, consider lowering the cap for simple query types while keeping the higher limit for multi-step cancellation and return flows.

2. Session history storage
sessions_memory in backend/server.py stores conversation history in an in-memory dict with no limit on the number of sessions. On a long-running server, this grows without bound.
Recommendation: Add a maximum session count or TTL-based cleanup, or persist sessions to the database.

3. Database indexes
Queries on orders, audit_logs, and demo_requests filter by company_id. Without indexes on these columns, lookups slow down as data grows.
Recommendation: Check the current table schemas; if company_id is not indexed, add indexes and measure query time before and after.

4. Webhook processing
Shippo events are processed in background tasks, which keeps webhook responses fast. WooCommerce webhook processing is synchronous.
Recommendation: Measure WooCommerce webhook processing time. If provider calls are slow, move processing to a background task, verifying that delivery deduplication still works.

5. HTTP client handling
Connectors create HTTP clients for provider calls. Connection reuse, timeouts, and retry limits should be consistent across providers to avoid socket exhaustion under load.
Recommendation: Review each connector for shared client usage, explicit timeouts, and bounded retries.

## Measurement plan
No optimization should be implemented without before/after evidence. Measure:
- Average /api/chat response time
- Average LLM calls per query
- Database query time with a realistic data volume
Compare results before and after any change. Only merge optimizations that show a measurable improvement without regressing tests.