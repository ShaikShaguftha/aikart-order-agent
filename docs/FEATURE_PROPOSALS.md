# Feature Proposals

## Prioritized improvements

1. Real customer authentication
Current state: /api/chat uses a hardcoded demo customer ID (CUST-101) in backend/server.py.
Impact: Blocks production use; identity checks cannot be fully trusted.
Priority: High.
Note: Requires a product decision on the auth approach (storefront token vs login flow) before implementation.

2. Order resolution status
Current state: Customers can ask about an order but cannot see whether their issue is open, in progress, or resolved.
Impact: Customers cannot track the status of their complaint.
Priority: High.

3. More consistent error recovery in chat
Current state: Tool failures and claim-guard fallbacks exist, but recovery messaging varies between error paths.
Impact: Some failure paths offer a clear next step, others only report the error.
Priority: Medium.

4. Proactive escalation follow-up
Current state: Escalation creates a Freshdesk ticket and the ticket ID is reported, but subsequent status updates are not surfaced unless the customer asks.
Impact: Customers must manually check ticket progress.
Priority: Medium.

5. Actionable audit logs
Current state: Audit logs store input and response but have no session-level view for support staff.
Impact: Harder to investigate a customer's full history.
Priority: Medium.

## Implementation note
No features were implemented in this phase. Implementation of the highest-priority item (real customer authentication) requires a product decision on the authentication approach, as it directly affects the security model. This is flagged for planning rather than implemented without alignment.