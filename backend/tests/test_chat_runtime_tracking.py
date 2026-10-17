"""
End-to-end regression for the live #1005 incident, through the SAME runtime path as the UI:

    POST /api/chat (server.py) -> process_query (agent.py) -> per-request provider flags
    -> the actually bound tool list -> tool dispatch -> gateway -> Shopify -> Shippo

Only the language model's invoke() is replaced. The fake model can only choose among the
tools the runtime really bound (it receives them exactly as the LLM would), so this test
fails if get_shipment_status is ever exposed to a tenant that has Shippo configured.
"""
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

import agent
from backend.database import execute_db, init_db
from backend.integrations import credential_store
from backend.integrations.providers.shippo import _EndpointBudget
from backend.integrations.providers.shopify import ShopifyConnector

SHIPPO_TENANT, STORE_ONLY_TENANT = "COMP-TEST-RT-SHIPPO", "COMP-TEST-RT-STORE"
ORDER_1005 = {
    "id": "#1005", "status": "FULFILLED", "financial_status": "PENDING", "fulfillment_status": "FULFILLED",
    "total": 10.0, "customer_email": None,
    "shipment": {"id": "gid://shopify/Fulfillment/7259276574947", "order_id": "#1005", "courier": "Other",
                 "tracking_number": "SHIPPO_TRANSIT", "status": "SUCCESS", "tracking_url": None},
}
SHIPPO_TRACK = {
    "carrier": "shippo", "tracking_number": "SHIPPO_TRANSIT", "eta": "2026-09-28T17:00:00Z", "test": True,
    "servicelevel": {"token": "", "name": ""},
    "tracking_status": {"object_id": "ts_1", "object_updated": "2026-09-27T17:00:00Z", "status": "TRANSIT",
                        "substatus": None, "status_details": "Your shipment has departed from the origin.",
                        "status_date": "2026-09-27T17:00:00Z",
                        "location": {"city": "San Francisco", "state": "CA", "zip": "94103", "country": "US"}},
    "tracking_history": [],
}


def model_that_uses_bound_tools(self, messages, config=None, **kwargs):
    """Behaves like a tool-calling LLM: picks the best tracking tool it was actually given."""
    bound = [t["function"]["name"] for t in kwargs.get("tools", [])]
    tool_results = [m for m in messages if isinstance(m, ToolMessage)]
    if not tool_results:
        name = "get_tracking_status" if "get_tracking_status" in bound else "get_shipment_status"
        return AIMessage(content="", tool_calls=[{"name": name, "args": {"order_id": "#1005"}, "id": "call_1"}])
    result = json.loads(tool_results[-1].content)
    status = (result.get("tracking") or {}).get("status")
    return AIMessage(content=f"Order #1005 carrier tracking status: {status}." if status
                     else "Order #1005 has a tracking number, but live carrier status is not available.")


class ChatRuntimeTrackingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {credential_store.KEY_ENV: Fernet.generate_key().decode()})
        cls.env.start()
        init_db()
        for tenant in (SHIPPO_TENANT, STORE_ONLY_TENANT):
            execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES (?, ?, 'STANDARD')", (tenant, tenant))
            execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type, shop_domain, access_token) "
                       "VALUES (?, 'SHOPIFY', 'test.myshopify.com', 'shpat_test')", (tenant,))
        # Mirror COMP-SHOPIFY: Shopify + Freshdesk + Shippo.
        execute_db("INSERT OR REPLACE INTO helpdesk_integrations (company_id, provider_type, domain, api_key) "
                   "VALUES (?, 'FRESHDESK', 'aikart', ?)", (SHIPPO_TENANT, credential_store.encrypt("fd_key")))
        execute_db("INSERT OR REPLACE INTO shipping_integrations (company_id, provider_type, environment, api_token) "
                   "VALUES (?, 'SHIPPO', 'test', ?)", (SHIPPO_TENANT, credential_store.encrypt("shippo_test_runtime_token")))
        import server
        cls.client = TestClient(server.app)  # no lifespan: no startup scanner/LLM calls

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        _EndpointBudget._buckets.clear()
        self.events = []
        self.shippo_calls = []
        original_request = httpx.Client.request

        def transport(client, method, url, *args, **kwargs):
            if "api.goshippo.com" in str(url):
                self.events.append("SHIPPO")
                self.shippo_calls.append((method, str(url).split("api.goshippo.com/", 1)[1], kwargs["headers"]["Authorization"]))
                response = MagicMock(status_code=200, headers={})
                response.json.return_value = SHIPPO_TRACK
                return response
            return original_request(client, method, url, *args, **kwargs)  # the TestClient's own /api/chat call

        def shopify_order(order_id, company_id):
            self.events.append("SHOPIFY")
            return json.loads(json.dumps(ORDER_1005)) if order_id in ("#1005", "1005") else None

        for target in (patch.object(httpx.Client, "request", transport),
                       patch.object(type(agent.llm), "invoke", model_that_uses_bound_tools),
                       patch.object(ShopifyConnector, "get_order_details", side_effect=shopify_order)):
            target.start()
            self.addCleanup(target.stop)

    def chat(self, tenant):
        logs = io.StringIO()
        with patch("sys.__stdout__", logs), redirect_stdout(logs):
            response = self.client.post("/api/chat", json={"user_input": "Where is my order #1005?"},
                                        headers={"X-Company-ID": tenant})
        self.assertEqual(response.status_code, 200)
        return response.json()["agent_response"], logs.getvalue()

    def test_shippo_tenant_runtime_uses_shopify_then_shippo(self):
        answer, logs = self.chat(SHIPPO_TENANT)
        self.assertIn("Tool: get_tracking_status", logs)
        self.assertNotIn("Tool: get_shipment_status", logs)
        self.assertEqual(self.events, ["SHOPIFY", "SHIPPO"])
        self.assertEqual(self.shippo_calls, [("GET", "tracks/shippo/SHIPPO_TRANSIT", "ShippoToken shippo_test_runtime_token")])
        self.assertIn("[PROVIDER] Provider: SHIPPO | Operation: GET tracks/{carrier}/{tracking_number}", logs)
        self.assertEqual(answer, "Order #1005 carrier tracking status: IN_TRANSIT.")

    def test_store_only_tenant_is_unchanged_and_never_calls_shippo(self):
        answer, logs = self.chat(STORE_ONLY_TENANT)
        self.assertIn("Tool: get_shipment_status", logs)
        self.assertEqual(self.shippo_calls, [])
        self.assertNotIn("IN_TRANSIT", answer)  # fulfillment status SUCCESS is never turned into a carrier status


if __name__ == "__main__":
    unittest.main()
