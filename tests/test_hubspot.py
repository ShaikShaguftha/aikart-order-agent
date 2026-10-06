"""
Mocked HubSpot CRM (read-only) tests - no real HubSpot API is called.
FakeHubSpot records every request (method, URL, params, JSON body, auth header) so tests can
prove read-only behaviour, the exact search request shapes and that the token never leaks.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

import agent
from database import execute_db, init_db
from integrations.errors import TenantConfigurationError
from integrations.gateway import gateway
from integrations.providers.hubspot import API_VERSION, BASE_URL, HubSpotConnector
from integrations.providers.shopify import ShopifyConnector
from integrations.providers.zoho_crm import ZohoCRMConnector
from tools import hubspot_get_company, hubspot_search_contacts

TOKEN = "pat-na1-test-token-do-not-log-0001"
ENV = {"HUBSPOT_ACCESS_TOKEN": TOKEN, "HUBSPOT_ENV_TENANT": "COMP-HUBSPOT"}
OBJ = f"{BASE_URL}/crm/objects/{API_VERSION}"
CONTACT = {"id": "101", "properties": {"firstname": "Rahul", "lastname": "Sharma", "email": "rahul@gmail.com",
                                       "phone": "+91 98765 43210", "mobilephone": None, "company": "Acme Pvt Ltd",
                                       "associatedcompanyid": "202", "createdate": "2026-09-01T10:00:00Z",
                                       "notes_internal": "must not leak"},
           "createdAt": "2026-09-01T10:00:00Z", "updatedAt": "2026-09-02T10:00:00Z", "archived": False}
COMPANY = {"id": "202", "properties": {"name": "Acme Pvt Ltd", "domain": "acme.example", "website": None, "phone": "+91 22 4000 0000"}}
DEAL = {"id": "303", "properties": {"dealname": "Acme renewal", "amount": "125000.50", "dealstage": "contractsent",
                                    "pipeline": "default", "closedate": "2026-10-15T00:00:00Z"}}
TICKET = {"id": "404", "properties": {"subject": "Damaged parcel", "content": "Box arrived crushed. " * 60,
                                      "hs_pipeline": "0", "hs_pipeline_stage": "1", "hs_ticket_priority": "HIGH",
                                      "createdate": "2026-09-20T09:00:00Z", "hs_lastmodifieddate": "2026-09-21T09:00:00Z"}}


def _resp(status=200, body=None, headers=None, bad_json=False):
    r = MagicMock()
    r.status_code, r.headers = status, headers or {}
    if bad_json or body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    return r


class FakeHubSpot:
    def __init__(self):
        self.calls, self.queue = [], []
        self.search_results = [CONTACT]

    def __call__(
        self,
        client,
        method=None,
        url=None,
        params=None,
        json=None,
        headers=None,
        **_,
    ):
        """
        Supports both the old request-level-header implementation and the
        improved persistent-httpx-client implementation where auth is stored
        in client.headers.
        """
        effective_headers = headers or client.headers

        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "json": json,
                "auth": effective_headers.get("Authorization"),
            }
        )

        if self.queue:
            item = self.queue.pop(0)

            if isinstance(item, Exception):
                raise item

            return item

        if method == "POST" and url == f"{OBJ}/contacts/search":
            return _resp(
                200,
                {
                    "total": len(self.search_results),
                    "results": self.search_results,
                },
            )

        records = {
            f"{OBJ}/contacts/101": CONTACT,
            f"{OBJ}/contacts/rahul@gmail.com": CONTACT,
            f"{OBJ}/companies/202": COMPANY,
            f"{OBJ}/deals/303": DEAL,
            f"{OBJ}/tickets/404": TICKET,
        }

        if method == "GET" and url in records:
            return _resp(200, records[url])

        return _resp(
            404,
            {
                "status": "error",
                "message": "resource not found",
                "category": "OBJECT_NOT_FOUND",
            },
        )


class HubSpotFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)

        # pytest/Jupyter on Windows can expose an invalid sys.__stdout__ handle.
        # Gateway/provider logging uses sys.__stdout__, so redirect it to the
        # valid active stdout stream during every test.
        stdout_patch = patch.object(sys, "__stdout__", sys.stdout)
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)

        self.fake = FakeHubSpot()
        original_request = httpx.Client.request

        def transport(client, method, url, *args, **kwargs):
            if "hubapi.com" in str(url):
                return self.fake(
                    client,
                    method=method,
                    url=str(url),
                    **kwargs,
                )

            return original_request(
                client,
                method,
                url,
                *args,
                **kwargs,
            )

        for target in (
            patch.object(httpx.Client, "request", transport),
            patch("integrations.providers.hubspot.time.sleep"),
        ):
            target.start()
            self.addCleanup(target.stop)

        self.hs = HubSpotConnector(
            TOKEN,
            timeout=2.0,
            retry_backoff=0,
        )


class ReadTest(HubSpotFixture):

    def test_01_authentication_headers(self):
        self.hs.get_contact("101")
        call = self.fake.calls[0]
        self.assertEqual(call["auth"], f"Bearer {TOKEN}")
        self.assertEqual((call["method"], call["url"]), ("GET", f"{OBJ}/contacts/101"))

    def test_http_client_reused_and_closed(self):
        first_client = self.hs._get_client()
        second_client = self.hs._get_client()

        self.assertIs(first_client, second_client)
        self.assertFalse(first_client.is_closed)

        self.hs.close()

        self.assertIsNone(self.hs._client)
        self.assertTrue(first_client.is_closed)

    def test_02_get_contact_success(self):
        self.assertEqual(self.hs.get_contact("101"), {
            "id": "101", "first_name": "Rahul", "last_name": "Sharma", "email": "rahul@gmail.com",
            "phone": "+91 98765 43210", "company": "Acme Pvt Ltd", "account_id": "202",
            "created_at": "2026-09-01T10:00:00Z", "provider": "HUBSPOT"})
        self.assertIn("associatedcompanyid", self.fake.calls[0]["params"]["properties"])

    def test_03_get_contact_404(self):
        self.assertEqual(self.hs.get_contact("999")["code"], "NOT_FOUND")

    def test_04_search_by_email_is_exact_get_lookup(self):
        result = self.hs.search_contacts(email="rahul@gmail.com")
        self.assertEqual((result["count"], result["searched_by"], result["contacts"][0]["id"]), (1, "email", "101"))
        call = self.fake.calls[0]
        self.assertEqual((call["method"], call["url"], call["params"]["idProperty"]), ("GET", f"{OBJ}/contacts/rahul@gmail.com", "email"))

    def test_05_search_by_phone(self):
        self.hs.search_contacts(phone="+91 98765 43210")
        call = self.fake.calls[0]
        self.assertEqual((call["method"], call["url"]), ("POST", f"{OBJ}/contacts/search"))
        self.assertEqual(call["json"]["filterGroups"], [
            {"filters": [{"propertyName": "phone", "operator": "EQ", "value": "+91 98765 43210"}]},
            {"filters": [{"propertyName": "mobilephone", "operator": "EQ", "value": "+91 98765 43210"}]}])
        self.assertEqual(call["json"]["limit"], 10)

    def test_06_search_by_name(self):
        self.hs.search_contacts(name="Rahul")
        self.assertEqual(self.fake.calls[0]["json"]["filterGroups"], [
            {"filters": [{"propertyName": "firstname", "operator": "EQ", "value": "Rahul"}]},
            {"filters": [{"propertyName": "lastname", "operator": "EQ", "value": "Rahul"}]}])
        self.hs.search_contacts(name="Rahul Sharma")
        self.assertEqual(self.fake.calls[1]["json"]["filterGroups"], [
            {"filters": [{"propertyName": "firstname", "operator": "EQ", "value": "Rahul"},
                         {"propertyName": "lastname", "operator": "EQ", "value": "Sharma"}]}])

    def test_07_no_search_results(self):
        self.fake.search_results = []
        self.assertEqual(self.hs.search_contacts(name="Nobody")["contacts"], [])
        self.assertEqual(self.hs.search_contacts(email="nobody@example.com"), {"contacts": [], "count": 0, "searched_by": "email"})



    def test_large_contact_search_is_limited_to_ten_results(self):
        large_results = []

        for index in range(15):
            large_results.append(
                {
                    "id": str(1000 + index),
                    "properties": {
                        "firstname": f"Customer{index}",
                        "lastname": "Test",
                        "email": f"customer{index}@example.com",
                        "phone": None,
                        "mobilephone": None,
                        "company": None,
                        "associatedcompanyid": None,
                        "createdate": "2026-09-01T10:00:00Z",
                    },
                }
            )

        self.fake.queue = [
            _resp(
                200,
                {
                    "results": large_results,
                    "paging": {
                        "next": {
                            "after": "10",
                        }
                    },
                },
            )
        ]

        result = self.hs.search_contacts(
            name="Rahul",
        )

        self.assertEqual(result["count"], 10)
        self.assertEqual(len(result["contacts"]), 10)
        self.assertTrue(result["more_records"])
    def test_08_get_company(self):
        self.assertEqual(self.hs.get_company("202"), {"id": "202", "name": "Acme Pvt Ltd", "website": "acme.example",
                                                      "phone": "+91 22 4000 0000", "provider": "HUBSPOT"})

    def test_09_get_deal(self):
        self.assertEqual(self.hs.get_deal("303"), {"id": "303", "name": "Acme renewal", "amount": 125000.5,
                                                   "stage": "contractsent", "closing_date": "2026-10-15T00:00:00Z",
                                                   "provider": "HUBSPOT"})

    def test_10_get_ticket(self):
        ticket = self.hs.get_ticket("404")
        self.assertEqual((ticket["subject"], ticket["stage"], ticket["priority"], ticket["pipeline"]),
                         ("Damaged parcel", "1", "HIGH", "0"))
        self.assertLessEqual(len(ticket["description"]), 500)

    def test_17_missing_and_null_fields(self):
        self.fake.queue = [_resp(200, {"id": "101", "properties": {"firstname": None, "email": ""}})]
        contact = self.hs.get_contact("101")
        self.assertEqual({k: contact[k] for k in ("first_name", "last_name", "email", "phone", "company", "account_id")},
                         dict.fromkeys(("first_name", "last_name", "email", "phone", "company", "account_id")))
        self.fake.queue = [_resp(200, {"id": "303", "properties": {"amount": "not-a-number"}})]
        self.assertIsNone(self.hs.get_deal("303")["amount"])

    def test_invalid_inputs_never_reach_hubspot(self):
        for result in (self.hs.get_contact("abc"), self.hs.get_ticket("../owners"), self.hs.search_contacts(email="x"),
                       self.hs.search_contacts(phone="12"), self.hs.search_contacts(name="R2D2"), self.hs.search_contacts()):
            self.assertIn(result["code"], ("INVALID_RECORD_ID", "INVALID_ARGUMENT"))
        self.assertEqual(self.fake.calls, [])


class ErrorTest(HubSpotFixture):

    def _get(self, *responses):
        self.fake.queue = list(responses)
        return self.hs.get_contact("101")

    def test_11_401(self):
        result = self._get(_resp(401, {"status": "error", "category": "INVALID_AUTHENTICATION"}))
        self.assertEqual((result["code"], result["error"], result["reconnect_required"]),
                         ("PROVIDER_AUTH_FAILED", "HubSpot authentication failed. Reconnect required.", True))

    def test_11b_400_returns_validation_error(self):
        result = self._get(
            _resp(
                400,
                {
                    "status": "error",
                    "category": "VALIDATION_ERROR",
                    "message": "Invalid request",
                },
            )
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_VALIDATION_FAILED",
        )

        self.assertEqual(
            result["http_status"],
            400,
        )

    def test_12_403(self):
        result = self._get(_resp(403, {"status": "error", "category": "MISSING_SCOPES"}))
        self.assertEqual((result["code"], result["error"]),
                         ("PROVIDER_FORBIDDEN", "HubSpot access denied. Required CRM scope/permission may be missing."))

    def test_13_429_retry_after_then_success_and_bounded(self):
        with patch("integrations.providers.hubspot.time.sleep") as sleep:
            self.assertEqual(self._get(_resp(429, {"errorType": "RATE_LIMIT"}, headers={"Retry-After": "2"}),
                                       _resp(200, CONTACT))["id"], "101")
        sleep.assert_called_once_with(2.0)
        self.assertEqual(self._get(*[_resp(429, {"errorType": "RATE_LIMIT"})] * 3)["code"], "RATE_LIMITED")

    def test_14_5xx(self):
        self.assertEqual(self._get(_resp(502, {}), _resp(200, CONTACT))["id"], "101")
        self.assertEqual(self._get(_resp(500, {}), _resp(503, {}), _resp(504, {}))["code"], "PROVIDER_UNAVAILABLE")

    def test_15_timeout_and_network(self):
        self.assertEqual(self._get(*[httpx.ReadTimeout("slow")] * 3)["code"], "PROVIDER_TIMEOUT")
        self.assertEqual(self._get(*[httpx.ConnectError("down")] * 3)["code"], "PROVIDER_UNREACHABLE")

    def test_16_malformed_response(self):
        self.assertEqual(self._get(_resp(200, bad_json=True))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(200, {"properties": {}}))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(200, {"id": "999", "properties": {}}))["code"], "PROVIDER_INVALID_RESPONSE")
        self.fake.queue = [_resp(200, {"results": "nope"})]
        self.assertEqual(self.hs.search_contacts(name="Rahul")["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(404, bad_json=True))["code"], "PROVIDER_API_UNAVAILABLE")


    def test_16b_empty_200_response_is_invalid(self):
        """
        A 200 response with no valid JSON body must be controlled and must
        never be treated as a valid HubSpot CRM record.
        """
        result = self._get(
            _resp(
                200,
                bad_json=True,
            )
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_INVALID_RESPONSE",
        )

        self.assertEqual(
            result["http_status"],
            200,
        )

    def test_read_only_no_mutation_possible(self):
        for method in ("DELETE", "PATCH", "PUT"):
            self.assertEqual(self.hs._request(method, f"crm/objects/{API_VERSION}/contacts/101")["code"], "NOT_SUPPORTED")
        self.assertEqual(self.hs._request("POST", f"crm/objects/{API_VERSION}/contacts")["code"], "NOT_SUPPORTED")
        self.assertEqual(self.fake.calls, [])
        for name in dir(HubSpotConnector):
            self.assertFalse(name.startswith(("create", "delete", "upsert", "send", "archive")), name)

    def test_20_token_never_exposed(self):
        buffer = io.StringIO()
        with patch("sys.__stdout__", buffer), redirect_stdout(buffer), self.assertLogs("uvicorn.error", level="WARNING") as logs:
            failed = self._get(RuntimeError(f"boom {TOKEN}"))
            ok = self.hs.search_contacts(name="Rahul")
            denied = self._get(_resp(401, {"message": f"bad token {TOKEN}"}))
        blob = buffer.getvalue() + json.dumps([failed, ok, denied]) + "".join(logs.output)
        self.assertNotIn(TOKEN, blob)


class GatewayTest(HubSpotFixture):

    def test_18_gateway_tenant_routing(self):
        self.assertIsInstance(gateway.get_connector("COMP-HUBSPOT"), HubSpotConnector)
        self.assertIsInstance(gateway.get_tenant_primary_connector("COMP-HUBSPOT"), HubSpotConnector)
        self.assertTrue(gateway.has_hubspot("COMP-HUBSPOT"))
        for tenant in ("COMP-SHOPIFY", "COMP-ZOHO", "COMP-ALPHA"):
            self.assertFalse(gateway.has_hubspot(tenant))
        self.assertIsInstance(gateway.get_connector("COMP-SHOPIFY"), ShopifyConnector)
        self.assertEqual(gateway.hubspot_get_customer_profile("COMP-HUBSPOT", "101")["company"]["name"], "Acme Pvt Ltd")

    def test_19_credentials_not_configured(self):
        with patch.dict(os.environ, {"HUBSPOT_ACCESS_TOKEN": ""}):
            with self.assertRaises(TenantConfigurationError):
                gateway.get_connector("COMP-HUBSPOT")
            self.assertEqual(json.loads(hubspot_search_contacts.invoke({"name": "Rahul", "company_id": "COMP-HUBSPOT"}))["code"],
                             "TENANT_CONFIGURATION_ERROR")
        self.assertEqual(self.fake.calls, [])

    def test_tenant_isolation(self):
        execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES ('COMP-HUBSPOT-2', 'x', 'STANDARD')")
        execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type) VALUES ('COMP-HUBSPOT-2', 'HUBSPOT')")
        with self.assertRaises(TenantConfigurationError):
            gateway.get_connector("COMP-HUBSPOT-2")  # env token belongs only to HUBSPOT_ENV_TENANT
        for tenant in ("COMP-SHOPIFY", "COMP-ZOHO"):
            result = json.loads(hubspot_get_company.invoke({"hubspot_company_id": "202", "company_id": tenant}))
            self.assertIn("code", result)
        self.assertEqual(self.fake.calls, [])

    def test_zoho_tenant_unaffected(self):
        with patch.dict(os.environ, {"ZOHO_CLIENT_ID": "c", "ZOHO_CLIENT_SECRET": "s", "ZOHO_REFRESH_TOKEN": "r"}):
            self.assertIsInstance(gateway.get_connector("COMP-ZOHO"), ZohoCRMConnector)


class AgentTest(HubSpotFixture):

    def chat(self, model, text, tenant="COMP-HUBSPOT"):
        import server
        logs = io.StringIO()
        with patch.object(type(agent.llm), "invoke", model), patch("sys.__stdout__", logs), redirect_stdout(logs):
            response = TestClient(server.app).post("/api/chat", json={"user_input": text}, headers={"X-Company-ID": tenant})
        return response, logs.getvalue()

    def test_tools_bound_only_for_hubspot_tenant(self):
        names = {t["function"]["name"] for t in agent.llm_with_hubspot_tools.kwargs["tools"]}
        self.assertEqual(names, {"hubspot_search_contacts", "hubspot_get_customer", "hubspot_get_company",
                                 "hubspot_get_deal", "hubspot_get_ticket"})
        for binding in (agent.llm_with_tools, agent.llm_with_crm_tools, agent.llm_with_helpdesk_shipping_tools):
            self.assertFalse(any(t["function"]["name"].startswith("hubspot") for t in binding.kwargs["tools"]))

    def test_find_rahul_then_crm_details_through_api_chat(self):
        def model(self_, messages, config=None, **kwargs):
            bound = {t["function"]["name"] for t in kwargs.get("tools", [])}
            assert "hubspot_search_contacts" in bound and "crm_search_customers" not in bound, bound
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                return AIMessage(content="", tool_calls=[{"name": "hubspot_search_contacts", "args": {"name": "Rahul"}, "id": "c1"}])
            if len(results) == 1:
                found = json.loads(results[0].content)["contacts"][0]
                return AIMessage(content="", tool_calls=[{"name": "hubspot_get_customer", "args": {"contact_id": found["id"]}, "id": "c2"}])
            profile = json.loads(results[-1].content)
            return AIMessage(content=f"{profile['contact']['first_name']} {profile['contact']['last_name']} - {profile['company']['name']}")
        response, logs = self.chat(model, "Find Rahul in HubSpot and show his CRM details")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["agent_response"], "Rahul Sharma - Acme Pvt Ltd")
        self.assertIn("[PROVIDER] Provider: HUBSPOT | Operation: POST crm/objects/2026-09/contacts/search", logs)
        self.assertNotIn(TOKEN, logs)

    def test_invented_record_id_is_blocked(self):
        def model(self_, messages, config=None, **kwargs):
            if not [m for m in messages if isinstance(m, ToolMessage)]:
                return AIMessage(content="", tool_calls=[{"name": "hubspot_get_deal", "args": {"deal_id": "98765"}, "id": "c1"}])
            return AIMessage(content="Which deal do you mean?")
        response, logs = self.chat(model, "Show deal details")
        self.assertIn("UNVERIFIED_RECORD_ID", logs)
        self.assertEqual(self.fake.calls, [])


if __name__ == "__main__":
    unittest.main()