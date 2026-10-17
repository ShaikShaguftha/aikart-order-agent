
# Luintix Latency Analysis

## Summary

- Total requests analysed: 135
- Average total response time: 13299.47 ms
- p95 total response time: 71189.94 ms
- Slowest response: 199349.78 ms
- LLM generation share of average latency: 63.68%

## Latency by stage

| Stage | Average (ms) | p95 (ms) | Maximum (ms) |
|---|---:|---:|---:|
| Data retrieval / tool execution | 129.13 | 740.58 | 2858.26 |
| Context preparation | 0.28 | 0.38 | 13.73 |
| LLM generation | 8468.87 | 57030.53 | 163427.59 |
| Total request | 13299.47 | 71189.94 | 199349.78 |

## Finding

Data retrieval and context preparation complete in milliseconds, while LLM generation accounts for the majority of end-to-end latency. The primary bottleneck is therefore LLM response generation, not database or tool execution.

## Proposed fixes

1. Reduce the system prompt and conversation-history size.
2. Enable response streaming so users receive feedback earlier.
3. Cache repeated order/status lookups within a short TTL.
4. Use a faster model for simple deterministic lookup queries.
5. Avoid a second LLM call when the first response is already supported by tool results.
6. Consider parallelising independent read-only tool calls.
