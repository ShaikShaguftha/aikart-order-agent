"""
Regression tests for /api/chat tenant validation.

Bug: COMP-SHADOWFAX is registered with provider_type SHADOWFAX (a standalone logistics
tenant, no store). /api/chat validated tenants with gateway.get_connector(), which only
resolves store providers, so every UI request for COMP-SHADOWFAX failed with
"Store 'COMP-SHADOWFAX' is not configured" before reaching the Shadowfax connector.
"""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

import agent
from database import DEFAULT_TENANTS, init_db
from integrations.errors import TenantConfigurationError
from integrations.gateway import gateway
from integrations.providers.local_db import LocalDBConnector
from integrations.providers.shadowfax import ShadowfaxConnector
from integrations.providers.shopify import ShopifyConnector

STAGING = "https://dale.staging.shadowfax.in/api"
SFX_ENV = {"SHADOWFAX_API_TOKEN": "sfx_test_token_not_real", "SHADOWFAX_API_BASE_URL": STAGING,
           "SHADOWFAX_ENV_TENANT": "COMP-SHADOWFAX"}


class TenantPrimaryConnectorTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def test_shadowfax_tenant_resolves_to_shadowfax_connector(self):
        with patch.dict(os.environ, SFX_ENV):
            connector = gateway.get_tenant_primary_connector("COMP-SHADOWFAX")
        self.assertIsInstance(connector, ShadowfaxConnector)
        self.assertEqual(connector.base_url, STAGING)

    def test_store_tenants_are_unchanged(self):
        self.assertIsInstance(gateway.get_tenant_primary_connector("COMP-SHOPIFY"), ShopifyConnector)
        self.assertIsInstance(gateway.get_tenant_primary_connector("COMP-ALPHA"), LocalDBConnector)
        self.assertIs(type(gateway.get_tenant_primary_connector("COMP-SHOPIFY")), type(gateway.get_connector("COMP-SHOPIFY")))

    def test_every_seeded_tenant_has_a_resolvable_provider_type(self):
        """Guards the original bug: a seeded provider_type the gateway cannot route."""
        for tenant, _, provider in DEFAULT_TENANTS:
            with self.subTest(tenant=tenant, provider=provider):
                try:
                    gateway.get_tenant_primary_connector(tenant)
                except TenantConfigurationError as e:
                    # Missing credentials in the test environment are fine; an unroutable type is not.
                    self.assertNotIn("unsupported provider_type", e.message)

    def test_unknown_tenant_is_still_rejected(self):
        with self.assertRaises(TenantConfigurationError):
            gateway.get_tenant_primary_connector("COMP-DOES-NOT-EXIST")

    def test_startup_summary_reports_shadowfax_tenant(self):
        with patch.dict(os.environ, SFX_ENV):
            line = next(l for l in gateway.describe_tenants() if l.startswith("tenant=COMP-SHADOWFAX"))
        self.assertIn("provider=SHADOWFAX", line)
        self.assertNotIn("CONFIG ERROR", line)
        self.assertNotIn(SFX_ENV["SHADOWFAX_API_TOKEN"], line)


class ShadowfaxUiChatFlowTest(unittest.TestCase):
    """POST /api/chat exactly as the UI sends it, for tenant COMP-SHADOWFAX."""

    QUESTION = "Can you check if Shadowfax delivers from 400001 to 560001"

    @classmethod
    def setUpClass(cls):
        init_db()
        import server
        cls.client = TestClient(server.app)

    def setUp(self):
        self.shadowfax_calls = []
        original = httpx.Client.request

        def transport(client, method, url, *args, **kwargs):
            if "shadowfax.in" in str(url):
                params = kwargs.get("params") or {}
                self.shadowfax_calls.append((method, str(url), dict(params), kwargs["headers"].get("Authorization")))
                response = MagicMock(status_code=200)
                # Documented v1 serviceability response shape.
                services = {"400001": ["Connect_Mkt", "Surface_Mkt"], "560001": ["Regular"]}
                response.json.return_value = [{"code": int(params["pincodes"]), "services": services[params["pincodes"]]}]
                return response
            return original(client, method, url, *args, **kwargs)

        target = patch.object(httpx.Client, "request", transport)
        target.start()
        self.addCleanup(target.stop)

    def model(self, messages, config=None, **kwargs):
        bound = {t["function"]["name"] for t in kwargs.get("tools", [])}
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            assert "shadowfax_check_serviceability" in bound, bound
            return AIMessage(content="", tool_calls=[{"name": "shadowfax_check_serviceability",
                                                      "args": {"pickup_pincode": "400001", "delivery_pincode": "560001"},
                                                      "id": "c1"}])
        result = json.loads(results[-1].content)
        return AIMessage(content=f"Shadowfax serviceable 400001 -> 560001: {result.get('serviceable')}.")

    def post(self):
        logs = io.StringIO()
        with patch.object(type(agent.llm), "invoke", lambda s, m, config=None, **kw: self.model(m, config, **kw)), \
                patch("sys.__stdout__", logs), redirect_stdout(logs):
            response = self.client.post("/api/chat", json={"user_input": self.QUESTION},
                                        headers={"X-Company-ID": "COMP-SHADOWFAX"})
        return response, logs.getvalue()

    def test_ui_request_reaches_shadowfax_serviceability(self):
        with patch.dict(os.environ, SFX_ENV):
            response, logs = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("is not configured", response.text)
        self.assertEqual(response.json()["agent_response"], "Shadowfax serviceable 400001 -> 560001: True.")
        self.assertEqual([(m, u, p["service"], p["pincodes"]) for m, u, p, _ in self.shadowfax_calls],
                         [("GET", f"{STAGING}/v1/clients/serviceability/", "seller_pickup", "400001"),
                          ("GET", f"{STAGING}/v1/clients/serviceability/", "customer_delivery", "560001")])
        self.assertEqual(self.shadowfax_calls[0][3], f"Token {SFX_ENV['SHADOWFAX_API_TOKEN']}")
        self.assertIn("Tool: shadowfax_check_serviceability", logs)
        self.assertNotIn(SFX_ENV["SHADOWFAX_API_TOKEN"], logs)

    def test_shadowfax_tenant_without_credentials_fails_cleanly_before_any_call(self):
        with patch.dict(os.environ, {"SHADOWFAX_API_TOKEN": "", "SHADOWFAX_CLIENT_ID": "", "SHADOWFAX_CLIENT_SECRET": ""}):
            response, _ = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.shadowfax_calls, [])

    def test_shopify_tenant_validation_unchanged(self):
        response = self.client.post("/api/chat", json={"user_input": "hi"}, headers={"X-Company-ID": "COMP-DOES-NOT-EXIST"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("is not configured", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
