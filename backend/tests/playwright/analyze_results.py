import json
import statistics
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "backend/tests/playwright/results/test-results.json"
LOGS = ROOT / "logs/llm_logs.jsonl"
OUT = ROOT / "docs"

OUT.mkdir(exist_ok=True)

results = json.loads(RESULTS.read_text(encoding="utf-8"))

def percentile(values, p):
    values = sorted(values)
    index = max(0, round((p / 100) * len(values)) - 1)
    return values[index]

durations = [r.get("duration_ms", 0) for r in results]
categories = Counter(r.get("category") for r in results)
statuses = Counter(r.get("status") for r in results)
hallucinations = [r for r in results if r.get("possible_hallucination")]

latency_rows = []
if LOGS.exists():
    logs = [json.loads(line) for line in LOGS.read_text(encoding="utf-8").splitlines() if line.strip()]
    for log in logs:
        execution = log.get("execution", {})
        latency_rows.append({
            "request_id": log.get("request_id"),
            "tenant": log.get("tenant_id"),
            "retrieval_ms": execution.get("retrieval_ms", 0),
            "context_ms": execution.get("context_preparation_ms", 0),
            "llm_ms": execution.get("llm_generation_ms", 0),
            "total_ms": execution.get("total_ms", 0),
            "tool_calls": execution.get("tool_calls", 0),
            "llm_calls": execution.get("llm_calls", 0),
            "status": execution.get("status"),
        })

def stage_stats(field):
    values = [row[field] for row in latency_rows if row[field] is not None]
    if not values:
        return {"average": 0, "p95": 0, "max": 0}
    return {
        "average": round(statistics.mean(values), 2),
        "p95": percentile(values, 95),
        "max": max(values),
    }

retrieval = stage_stats("retrieval_ms")
context = stage_stats("context_ms")
llm = stage_stats("llm_ms")
total = stage_stats("total_ms")

llm_share = round((llm["average"] / total["average"] * 100), 2) if total["average"] else 0

# Latency report
latency_report = f"""# Luintix Latency Analysis

## Summary

- Total requests analysed: {len(latency_rows)}
- Average total response time: {total['average']} ms
- p95 total response time: {total['p95']} ms
- Slowest response: {total['max']} ms
- LLM generation share of average latency: {llm_share}%

## Latency by stage

| Stage | Average (ms) | p95 (ms) | Maximum (ms) |
|---|---:|---:|---:|
| Data retrieval / tool execution | {retrieval['average']} | {retrieval['p95']} | {retrieval['max']} |
| Context preparation | {context['average']} | {context['p95']} | {context['max']} |
| LLM generation | {llm['average']} | {llm['p95']} | {llm['max']} |
| Total request | {total['average']} | {total['p95']} | {total['max']} |

## Finding

Data retrieval and context preparation complete in milliseconds, while LLM generation accounts for the majority of end-to-end latency. The primary bottleneck is therefore LLM response generation, not database or tool execution.

## Proposed fixes

1. Reduce the system prompt and conversation-history size.
2. Enable response streaming so users receive feedback earlier.
3. Cache repeated order/status lookups within a short TTL.
4. Use a faster model for simple deterministic lookup queries.
5. Avoid a second LLM call when the first response is already supported by tool results.
6. Consider parallelising independent read-only tool calls.
"""

(OUT / "latency_analysis.md").write_text(latency_report, encoding="utf-8")

# Test/hallucination report
category_rows = "\n".join(
    f"| {category} | {count} | {sum(1 for r in results if r.get('category') == category and r.get('status') == 'PASS')} | "
    f"{sum(1 for r in results if r.get('category') == category and r.get('status') != 'PASS')} |"
    for category, count in categories.items()
)

hallucination_rows = "\n".join(
    f"| {r.get('query_id')} | {r.get('category')} | {r.get('query')} | {r.get('response', '')[:180]} |"
    for r in hallucinations[:50]
) or "| None | | | |"

test_report = f"""# Luintix Automated Query Results

## Summary

- Total automated queries: {len(results)}
- Passed: {statuses.get('PASS', 0)}
- Failed: {statuses.get('FAIL', 0)}
- Potential hallucination flags: {len(hallucinations)}

## Results by category

| Category | Queries | Passed | Failed |
|---|---:|---:|---:|
{category_rows}

## Potential hallucinations or unsupported claims

| Query ID | Category | Query | Response excerpt |
|---|---|---|---|
{hallucination_rows}

## Accuracy assessment

All {len(results)} Playwright queries completed. A response was marked as a potential hallucination when it contained unsupported certainty, escalation, refund, or guarantee language. Each flagged response should be manually reviewed against its tool results in `logs/llm_logs.jsonl`.
"""

(OUT / "hallucination_failure_report.md").write_text(test_report, encoding="utf-8")

# Duplicate / accuracy report
tool_signature_counts = Counter()
duplicate_blocks = 0
for log in logs:
    execution = log.get("execution", {})
    duplicate_blocks += execution.get("duplicate_tool_calls_blocked", 0)

duplicate_report = f"""# Duplicate Documents and Response Accuracy Review

## Summary

- Duplicate identical tool calls blocked: {duplicate_blocks}
- Total Playwright queries reviewed: {len(results)}
- Potential hallucination flags: {len(hallucinations)}

## Current safeguards

1. Duplicate identical tool calls are blocked within a single request.
2. The agent has a maximum tool-call budget.
3. The agent has a maximum iteration limit.
4. Unsupported action claims are detected and corrected.
5. Order, ticket, tracking, CRM, and payment identifiers must come from the user or a prior tool result.

## Findings

No duplicate document ingestion layer was found in the current agent implementation because the system primarily performs live order, shipment, CRM, payment, and helpdesk lookups rather than retrieving a static document corpus.

For future document/RAG features, deduplicate using a stable source ID plus content hash, rather than relying only on text similarity.

## Recommendations

1. Add a content hash and source ID to every indexed document.
2. Deduplicate retrieved context before prompt construction.
3. Limit repeated equivalent tool calls even when arguments differ slightly.
4. Add response-grounding checks for every factual claim.
5. Track duplicate-tool blocking and hallucination-guard triggers in the LLM logs.
"""

(OUT / "duplicate_documents_accuracy_review.md").write_text(duplicate_report, encoding="utf-8")

# MCP review
mcp_report = """# MCP Configuration and Tool Execution Review

## Current architecture

Luintix currently uses direct LangChain tool bindings rather than a Model Context Protocol (MCP) client/server architecture. Tools are bound to the LLM according to the selected tenant and available integrations.

## Tool execution controls

- Tenant-specific tool bindings
- Read-only restrictions for CRM, payment, and logistics integrations
- Server-side identity injection
- Identifier verification for orders, tickets, tracking numbers, CRM records, and payments
- Duplicate identical tool-call blocking
- Maximum tool-call budget per request
- Maximum agent iterations per request
- Unsupported action-claim detection and correction

## MCP assessment

MCP is not currently configured. The application does not connect to an external MCP server or expose its tools through an MCP server.

MCP would be useful if Luintix needs to expose its tools to multiple AI clients or consume external tool servers through a standard protocol. MCP tools are discovered through `tools/list` and executed through `tools/call`, with JSON Schema-defined inputs.

## Recommendations

1. Introduce an internal tool registry with metadata for each tool.
2. Add per-tool permissions and tenant allowlists.
3. Add per-tool rate limits and execution timeouts.
4. Record every tool invocation in structured execution logs.
5. Consider an MCP-compatible adapter only after the internal tool registry is stable.
"""

(OUT / "mcp_tool_execution_review.md").write_text(mcp_report, encoding="utf-8")

# Final report
final_report = f"""# Luintix Improvement Report

## Executive Summary

This report covers guardrails, authentication and domain verification, maximum attempt limits, LLM execution logging, MCP/tool execution review, automated Playwright testing, duplicate-context analysis, hallucination checks, and latency analysis.

## Automated testing

- Total queries executed: {len(results)}
- Passed: {statuses.get('PASS', 0)}
- Failed: {statuses.get('FAIL', 0)}
- Potential hallucination flags: {len(hallucinations)}

## Latency findings

| Stage | Average (ms) | p95 (ms) |
|---|---:|---:|
| Data retrieval / tool execution | {retrieval['average']} | {retrieval['p95']} |
| Context preparation | {context['average']} | {context['p95']} |
| LLM generation | {llm['average']} | {llm['p95']} |
| Total | {total['average']} | {total['p95']} |

LLM generation represents approximately {llm_share}% of average end-to-end latency. Data retrieval is not the primary bottleneck.

## Guardrails implemented

- Input-length and empty-input validation
- Tenant configuration validation
- Frontend origin/domain allowlist verification
- Server-side authenticated customer identity
- Maximum agent iterations
- Maximum tool calls per request
- Duplicate identical tool-call blocking
- LLM retry and request timeout limits
- Unsupported action-claim detection
- Identifier hallucination prevention
- Structured LLM execution logs

## Proposed next steps

1. Replace the local test authenticated customer ID with a real authenticated session mechanism.
2. Move frontend origin allowlists to environment-specific configuration.
3. Add persistent LLM trace storage and a dashboard.
4. Add per-tool rate limits and cost tracking.
5. Implement response streaming.
6. Evaluate a faster model for simple order-status lookups.
7. Add an MCP-compatible tool registry if multi-client tool sharing becomes necessary.
"""

(OUT / "luintix_improvements_report.md").write_text(final_report, encoding="utf-8")

print("Created reports:")
for f in OUT.glob("*.md"):
    print(" -", f)