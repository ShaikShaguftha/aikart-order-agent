# MCP Configuration and Tool Execution Review

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
