# Luintix Automated Query Results

## Summary

- Total automated queries: 100
- Passed: 100
- Failed: 0
- Potential hallucination flags: 0

## Results by category

| Category | Queries | Passed | Failed |
|---|---:|---:|---:|
| order_status | 15 | 15 | 0 |
| order_details | 15 | 15 | 0 |
| tracking | 25 | 25 | 0 |
| identity | 15 | 15 | 0 |
| invalid_input | 10 | 10 | 0 |
| guardrail | 15 | 15 | 0 |
| multi_turn | 5 | 5 | 0 |

## Potential hallucinations or unsupported claims

| Query ID | Category | Query | Response excerpt |
|---|---|---|---|
| None | | | |

## Accuracy assessment

All 100 Playwright queries completed. A response was marked as a potential hallucination when it contained unsupported certainty, escalation, refund, or guarantee language. Each flagged response should be manually reviewed against its tool results in `logs/llm_logs.jsonl`.
