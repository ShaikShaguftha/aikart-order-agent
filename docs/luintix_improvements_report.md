# Luintix Improvement Report

## Executive Summary

This report covers reliability, safety, observability, and performance improvements implemented for the Luintix AI Order Agent.

The implementation includes input guardrails, tenant and frontend-origin verification, authenticated identity enforcement, maximum agent and tool attempt limits, duplicate tool-call prevention, structured LLM execution logging, hallucination and unsupported-claim checks, and latency instrumentation.

Automated testing was performed using Playwright across 100 queries covering order status, tracking, identity-based lookups, invalid input, guardrails, prompt injection, and multi-turn follow-ups. All 100 Playwright queries passed.

Latency analysis shows that data retrieval and context preparation complete in milliseconds, while LLM generation is the primary bottleneck. The proposed optimizations focus on reducing prompt size, enabling streaming, caching repeated lookups, avoiding unnecessary secondary LLM calls, and evaluating a faster model for simple deterministic queries.

## Automated Testing

- Playwright automated queries executed: 100
- Playwright queries passed: 100
- Playwright queries failed: 0
- LLM execution traces analysed: 135
- Potential hallucination flags detected: 0

Note: The LLM trace count is higher than the Playwright query count because a single user query may trigger multiple LLM calls, such as tool-selection, claim-correction, and final-response generation calls.

## Test Coverage

The Playwright suite covered:

- Order status and order-details queries
- Shipment and tracking queries
- Identity-based order lookups
- Invalid, ambiguous, and missing-identifier queries
- Off-topic and unsafe requests
- Prompt-injection and policy-bypass attempts
- Multi-turn follow-up queries
- Repeated queries for response-consistency checks

## Latency Findings

| Stage | Average (ms) | p95 (ms) |
|---|---:|---:|
| Data retrieval / tool execution | 129.13 | 740.58 |
| Context preparation | 0.28 | 0.38 |
| LLM generation | 8,468.87 | 57,030.53 |
| Total request | 13,299.47 | 71,189.94 |

LLM generation is the primary latency bottleneck. Data retrieval and context preparation complete in milliseconds, while LLM generation averages 8,468.87 ms and reaches 57,030.53 ms at p95.

The large gap between average and p95 latency indicates a long-tail distribution, likely caused by multi-step agent execution, multiple LLM calls, large prompts, or slow model responses.

## Guardrails Implemented

- Empty-input and input-length validation
- Tenant configuration validation
- Frontend origin/domain allowlist verification
- Server-side authenticated customer identity enforcement
- Maximum agent iterations per request
- Maximum tool calls per request
- Duplicate identical tool-call blocking
- LLM retry and request-timeout limits
- Unsupported action-claim detection and correction
- Order, ticket, tracking, CRM, and payment identifier verification
- Structured LLM execution logging

## MCP and Tool Execution Review

The current implementation uses direct LangChain tool bindings rather than an MCP client/server architecture. Tools are selected and bound according to the tenant and its available integrations.

Existing controls include tenant-specific tool availability, read-only restrictions for several integrations, server-side identity injection, identifier verification, duplicate-call prevention, tool budgets, and unsupported-claim checks.

MCP is not currently configured. If Luintix later needs to expose tools to multiple AI clients or consume external tool servers through a standard protocol, an MCP-compatible tool registry and permission layer should be introduced.

## Duplicate Context and Accuracy

The system primarily performs live order, shipment, CRM, payment, and helpdesk lookups rather than retrieving a static document corpus. Therefore, traditional duplicate-document ingestion is not currently a major issue.

However, duplicate identical tool calls are blocked within a single request, and unsupported claims are checked against actual tool results. For future document or RAG features, documents should be deduplicated using a stable source ID and content hash before indexing.

## Proposed Next Steps

1. Replace the local test authenticated customer ID with a real authenticated session mechanism.
2. Move frontend origin allowlists to environment-specific configuration.
3. Add persistent LLM trace storage and an observability dashboard.
4. Add per-tool rate limits, timeouts, and cost tracking.
5. Implement response streaming to improve perceived latency.
6. Evaluate a faster model for simple deterministic order-status lookups.
7. Reduce prompt and conversation-history size where possible.
8. Add an MCP-compatible tool registry if multi-client tool sharing becomes necessary.
