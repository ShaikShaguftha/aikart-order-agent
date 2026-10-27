# API Audit

## Endpoints

| Endpoint | Method | Auth | Status | Notes |
|---|---|---|---|---|
| /api/chat | POST | Demo customer ID hardcoded | Working | Needs real login before production |
| /api/orders/{order_id} | GET | None | Working | No auth; anyone can read orders |
| /api/logs | GET | None | Working | No auth; exposes all tenants' logs |
| /api/book-demo | POST | Public (intended) | Working | Saves to DB, emails via SMTP |
| /api/demo-requests | GET | None | Working | No auth; exposes all demo leads |
| /api/webhooks/woocommerce | POST | HMAC signature | Working | Signature verified, deduplication present |
| /api/webhooks/shippo/{company_id} | POST | HMAC or URL token | Working | Raw body verified, background processing |

## Frontend Integration

- Chat widget and main script call /api/chat. Working.
- Demo form calls /api/book-demo. Working.
- No missing or orphaned endpoints.

## Findings

1. /api/orders, /api/logs, and /api/demo-requests have no authentication and should be restricted before production.
2. /api/chat uses a hardcoded demo customer ID; real session auth is needed.
3. 500 errors return internal error details to the client; return a generic message instead.
4. Terminal logs store full user input and responses; consider masking personal data.

## Test Status

441 passed, 0 failed, 1 skipped, 34 subtests passed.