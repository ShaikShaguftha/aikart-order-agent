# Luintix E-Commerce Resolution Agent (`aikart-order-agent`)

Luintix is a production-grade, autonomous B2B order management agent built using LangChain, FastMCP/Uvicorn, and Groq LLMs.

---

## Shadowfax Integration

### Purpose
The Shadowfax integration enables Luintix to interface with Shadowfax's logistics network for shipment tracking, scan event history, pincode serviceability checks, and reverse pickup tracking.

### Supported Capabilities (Phase 1 - Read-Only)
1. **`track_shipment(awb)`**: Retrieves real-time shipment status, EDD, pickup date, delivered date, origin, destination, latest scan event, and full scan history.
2. **`get_tracking_history(awb)`**: Retrieves normalized scan event timeline for a given AWB.
3. **`check_serviceability(pickup_pincode, delivery_pincode)`**: Verifies delivery serviceability and available service options (COD, SLA, prepaid, etc.) between pincodes.
4. **`track_reverse_pickup(reverse_awb)`**: Tracks reverse pickup shipments and return timeline.

### Read-Only Limitation
Phase 1 operates strictly in **READ-ONLY** mode. The agent cannot create shipments, cancel shipments, modify delivery addresses, schedule pickups, or issue refunds for Shadowfax.

### Environment Variables
Set the following environment variables in `.env`:

```env
SHADOWFAX_API_TOKEN=your_shadowfax_api_token
SHADOWFAX_CLIENT_ID=your_shadowfax_client_id
SHADOWFAX_CLIENT_SECRET=your_shadowfax_client_secret
SHADOWFAX_API_BASE_URL=https://api.shadowfax.in
SHADOWFAX_ENV_TENANT=COMP-SHADOWFAX
```

### Authentication
Shadowfax authentication is handled dynamically via `_auth_headers()`:
- **API Token Header**: `Authorization: Token <SHADOWFAX_API_TOKEN>`
- **Client Credentials**: `X-Client-ID: <SHADOWFAX_CLIENT_ID>` and `X-Client-Secret: <SHADOWFAX_CLIENT_SECRET>`
- Secrets and tokens are automatically redacted from all error logs and agent responses.

### Tenant Routing
- **Tenant ID**: `COMP-SHADOWFAX`
- Gateway routing maps `COMP-SHADOWFAX` directly to `ShadowfaxConnector`.

### Running Tests
Run the Shadowfax test suite using pytest:

```bash
PYTHONPATH=. ./venv/bin/pytest tests/test_shadowfax.py
```

Run the full existing test suite across all providers:

```bash
PYTHONPATH=. ./venv/bin/pytest
```

### Running Live Verification
Run the credential-independent live verification script:

```bash
PYTHONPATH=. ./venv/bin/python scratch/verify_shadowfax_live.py
```
