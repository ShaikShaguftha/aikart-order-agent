"""
Hermetic test environment.

1. Every test run uses a fresh, throwaway SQLite database instead of the developer's
   orders.db, so tests never depend on (or modify) live tenant configuration such as
   which tenants have Freshdesk connected.
2. Real network access is blocked at the httpx transport layer. Tests that mock
   httpx.Client.request/post are unaffected; anything that would reach a real provider
   or the LLM API fails immediately instead of calling live services.
"""
import os
import tempfile

import httpx
import pytest

import database
from backend import env_config  # noqa: F401  (load .env first, so the cleanup below is not undone later)

# 3. Real credentials in the developer's .env that bind a provider to a tenant must not leak
#    into tests (e.g. DELHIVERY_ENV_TENANT=COMP-SHOPIFY would change which tools COMP-SHOPIFY
#    gets). Tests that exercise these bindings set their own values with patch.dict.
for _key in [k for k in os.environ if k.startswith(("DELHIVERY_", "ZOHO_", "SHADOWFAX_", "HUBSPOT_", "SALESFORCE_", "RAZORPAY_"))]:
    del os.environ[_key]

_TEST_DB_DIR = tempfile.mkdtemp(prefix="luintix-tests-")
database.DB_FILE = os.path.join(_TEST_DB_DIR, "orders.db")
database.init_db()


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def blocked(self, request):
        raise RuntimeError(f"Real network access is disabled in tests (attempted host: {request.url.host})")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
