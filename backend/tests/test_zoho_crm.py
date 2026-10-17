"""
Mocked Zoho CRM (API v8) phase-1 tests - no real Zoho API is called.

FakeZoho models the two Zoho hosts: the CRM API (accepts only currently valid access tokens,
answers INVALID_TOKEN for expired ones) and the accounts server (mints tokens from the
refresh token). Every request is recorded so tests can prove GET-only CRM access, the OAuth
refresh flow, and that secrets never appear in URLs, logs or results.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

import httpx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

import agent
from backend.database import init_db
from backend.integrations.errors import TenantConfigurationError
from backend.integrations.gateway import gateway
from backend.integrations.providers.shopify import ShopifyConnector
from backend.integrations.providers.zoho_crm import ZohoCRMConnector

CLIENT_ID, CLIENT_SECRET = "1000.CLIENTIDTEST0001", "clientsecret_do_not_log_0001"
REFRESH, OLD_TOKEN = "1000.refreshtoken_do_not_log.0001", "1000.expired_access_token.0001"
CONTACT_ID, ACCOUNT_ID, DEAL_ID = "5725767000000411001", "5725767000000411002", "5725767000000411003"
ENV = {"ZOHO_CLIENT_ID": CLIENT_ID, "ZOHO_CLIENT_SECRET": CLIENT_SECRET, "ZOHO_REFRESH_TOKEN": REFRESH,
       "ZOHO_ACCESS_TOKEN": OLD_TOKEN, "ZOHO_API_DOMAIN": "https://www.zohoapis.in"}

CONTACT = {"id": CONTACT_ID, "First_Name": "Rahul", "Last_Name": "Sharma", "Email": "rahul@example.com",
           "Phone": "+91 98765 43210", "Mobile": None, "Account_Name": {"name": "Acme Pvt Ltd", "id": ACCOUNT_ID},
           "Created_Time": "2026-09-01T10:00:00+05:30", "Description": "internal notes must not leak"}
ACCOUNT = {"id": ACCOUNT_ID, "Account_Name": "Acme Pvt Ltd", "Website": "https://acme.example", "Phone": "+91 22 4000 0000"}
DEAL = {"id": DEAL_ID, "Deal_Name": "Acme renewal", "Amount": 125000, "Stage": "Negotiation/Review", "Closing_Date": "2026-10-15"}


def _resp(status=200, body=None, headers=None, bad_json=False):
    r = MagicMock()
    r.status_code, r.headers = status, headers or {}
    if bad_json or body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    return r


class FakeZoho:
    def __init__(self):
        self.calls, self.valid_tokens, self.queue, self.minted = [], set(), [], 0
        self.token_response = None

    def __call__(self, method=None, url=None, params=None, data=None, headers=None, **_):
        host, path = url.split("://", 1)[1].split("/", 1)
        self.calls.append({"method": method, "host": host, "path": path, "params": dict(params or {}),
                           "data": dict(data or {}), "auth": (headers or {}).get("Authorization"), "url": url})
        if path == "oauth/v2/token":
            if self.token_response is not None:
                return self.token_response
            if data.get("refresh_token") != REFRESH or data.get("client_secret") != CLIENT_SECRET:
                return _resp(200, {"error": "invalid_client"})
            self.minted += 1
            token = f"1000.fresh_access_token_{self.minted}"
            self.valid_tokens.add(token)
            return _resp(200, {"access_token": token, "expires_in": 3600, "api_domain": "https://www.zohoapis.in", "token_type": "Bearer"})
        if (headers or {}).get("Authorization", "").split(" ", 1)[-1] not in self.valid_tokens:
            return _resp(401, {"code": "INVALID_TOKEN", "details": {}, "message": "invalid oauth token", "status": "error"})
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if path == "crm/v8/Contacts/search":
            return _resp(200, {"data": [CONTACT], "info": {"count": 1, "more_records": False}})
        records = {f"crm/v8/Contacts/{CONTACT_ID}": CONTACT, f"crm/v8/Accounts/{ACCOUNT_ID}": ACCOUNT, f"crm/v8/Deals/{DEAL_ID}": DEAL}
        if path in records:
            return _resp(200, {"data": [records[path]]})
        return _resp(204)

    def crm(self):
        return [c for c in self.calls if c["path"].startswith("crm/")]

    def refreshes(self):
        return [c for c in self.calls if c["path"] == "oauth/v2/token"]


class ZohoFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        ZohoCRMConnector._token_cache.clear()

        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

        # pytest/Jupyter on Windows can expose an invalid sys.__stdout__ handle.
        # Gateway/provider logging writes to sys.__stdout__, so redirect it to
        # the valid active stdout stream for the duration of each test.
        self.stdout_patch = patch.object(sys, "__stdout__", sys.stdout)
        self.stdout_patch.start()
        self.addCleanup(self.stdout_patch.stop)

        self.fake = FakeZoho()
        original = httpx.Client.request

        def transport(client, method, url, *args, **kwargs):
            if "zoho" in str(url):
                return self.fake(method=method, url=str(url), **kwargs)
            return original(client, method, url, *args, **kwargs)

        for target in (patch.object(httpx.Client, "request", transport), patch("integrations.providers.zoho_crm.time.sleep")):
            target.start()
            self.addCleanup(target.stop)

    def connector(self, **kw):
        args = dict(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, refresh_token=REFRESH,
                    access_token=None, api_domain="https://www.zohoapis.in", retry_backoff=0)
        args.update(kw)
        return ZohoCRMConnector(**args)


class SearchAndReadTest(ZohoFixture):

    def test_search_by_email(self):
        result = gateway.crm_search_contacts("COMP-ZOHO", email="rahul@example.com")
        self.assertEqual((result["count"], result["searched_by"]), (1, "email"))
        self.assertEqual(result["contacts"][0], {
            "id": CONTACT_ID, "first_name": "Rahul", "last_name": "Sharma", "email": "rahul@example.com",
            "phone": "+91 98765 43210", "company": "Acme Pvt Ltd", "account_id": ACCOUNT_ID,
            "created_at": "2026-09-01T10:00:00+05:30", "provider": "ZOHO_CRM"})
        call = self.fake.crm()[-1]
        self.assertEqual((call["method"], call["host"], call["params"]), ("GET", "www.zohoapis.in", {"email": "rahul@example.com", "per_page": 10}))
        self.assertTrue(call["auth"].startswith("Zoho-oauthtoken "))
        self.assertNotIn("internal notes", json.dumps(result))

    def test_search_by_phone(self):
        gateway.crm_search_contacts("COMP-ZOHO", phone="+91 98765 43210")
        self.assertEqual(self.fake.crm()[-1]["params"]["phone"], "+91 98765 43210")

    def test_search_by_name_uses_escaped_starts_with_criteria(self):
        gateway.crm_search_contacts("COMP-ZOHO", name="Rahul")
        self.assertEqual(self.fake.crm()[-1]["params"]["criteria"], "((First_Name:starts_with:Rahul)or(Last_Name:starts_with:Rahul))")
        gateway.crm_search_contacts("COMP-ZOHO", name="Anne-Marie O'Neil")
        self.assertEqual(self.fake.crm()[-1]["params"]["criteria"],
                         "((First_Name:starts_with:Anne\\-Marie)and(Last_Name:starts_with:O'Neil))")

    def test_no_results_200_empty_and_204(self):
        self.fake.valid_tokens.add(OLD_TOKEN)
        self.fake.queue = [_resp(200, {"data": [], "info": {"count": 0}}), _resp(204)]
        for _ in range(2):
            self.assertEqual(gateway.crm_search_contacts("COMP-ZOHO", email="nobody@example.com")["contacts"], [])

    def test_invalid_search_input_is_rejected_without_calls(self):
        for kwargs in ({"email": "not-an-email"}, {"phone": "123"}, {"name": "R2D2"}, {"name": "x"}, {}):
            self.assertEqual(gateway.crm_search_contacts("COMP-ZOHO", **kwargs)["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.fake.calls, [])

    def test_get_contact_account_deal(self):
        self.assertEqual(gateway.crm_get_contact("COMP-ZOHO", CONTACT_ID)["company"], "Acme Pvt Ltd")
        self.assertEqual(gateway.crm_get_account("COMP-ZOHO", ACCOUNT_ID),
                         {"id": ACCOUNT_ID, "name": "Acme Pvt Ltd", "website": "https://acme.example",
                          "phone": "+91 22 4000 0000", "provider": "ZOHO_CRM"})
        self.assertEqual(gateway.crm_get_deal("COMP-ZOHO", DEAL_ID),
                         {"id": DEAL_ID, "name": "Acme renewal", "amount": 125000.0, "stage": "Negotiation/Review",
                          "closing_date": "2026-10-15", "provider": "ZOHO_CRM"})
        self.assertEqual(self.fake.crm()[-1]["params"], {"fields": "Deal_Name,Amount,Stage,Closing_Date"})

    def test_customer_profile_reads_contact_then_account(self):
        self.fake.valid_tokens.add(OLD_TOKEN)  # token already valid: no refresh round-trip in this test
        profile = gateway.crm_get_customer_profile("COMP-ZOHO", CONTACT_ID)
        self.assertEqual((profile["contact"]["id"], profile["account"]["name"]), (CONTACT_ID, "Acme Pvt Ltd"))
        self.assertEqual([c["path"] for c in self.fake.crm()], [f"crm/v8/Contacts/{CONTACT_ID}", f"crm/v8/Accounts/{ACCOUNT_ID}"])

    def test_not_found_and_invalid_ids(self):
        self.assertEqual(gateway.crm_get_contact("COMP-ZOHO", "5725767000000999999")["code"], "NOT_FOUND")  # 204
        self.fake.queue = [_resp(400, {"code": "INVALID_DATA", "message": "the related id given seems to be invalid"})]
        self.assertEqual(gateway.crm_get_deal("COMP-ZOHO", "5725767000000888888")["code"], "NOT_FOUND")
        calls = len(self.fake.calls)
        self.assertEqual(gateway.crm_get_contact("COMP-ZOHO", "../Users")["code"], "INVALID_RECORD_ID")
        self.assertEqual(len(self.fake.calls), calls)


class OAuthTest(ZohoFixture):

    def test_missing_access_token_is_refreshed_first(self):
        c = self.connector()
        self.assertEqual(c.get_contact("COMP-ZOHO", CONTACT_ID)["id"], CONTACT_ID)
        refresh = self.fake.refreshes()[0]
        self.assertEqual((refresh["method"], refresh["host"]), ("POST", "accounts.zoho.in"))
        self.assertEqual(refresh["data"], {"grant_type": "refresh_token", "refresh_token": REFRESH,
                                           "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})
        self.assertNotIn(CLIENT_SECRET, unquote(refresh["url"]))  # secrets in the body, never the URL
        self.assertNotIn(REFRESH, unquote(refresh["url"]))

    def test_expired_token_is_refreshed_once_and_cached(self):
        self.assertEqual(gateway.crm_get_contact("COMP-ZOHO", CONTACT_ID)["id"], CONTACT_ID)  # OLD_TOKEN -> INVALID_TOKEN
        self.assertEqual([c["auth"] for c in self.fake.crm()],
                         [f"Zoho-oauthtoken {OLD_TOKEN}", "Zoho-oauthtoken 1000.fresh_access_token_1"])
        gateway.crm_get_account("COMP-ZOHO", ACCOUNT_ID)
        self.assertEqual(len(self.fake.refreshes()), 1)  # cached across connector instances

    def test_refresh_failure_requires_reconnect(self):
        self.fake.token_response = _resp(200, {"error": "invalid_client"})
        result = gateway.crm_get_contact("COMP-ZOHO", CONTACT_ID)
        self.assertEqual((result["code"], result["reconnect_required"]), ("CRM_AUTH_FAILED", True))
        self.assertEqual(len(self.fake.refreshes()), 1)

    def test_freshly_minted_token_rejected_does_not_loop(self):
        self.fake.token_response = _resp(200, {"access_token": "1000.never_valid", "expires_in": 3600})
        result = self.connector().get_contact("COMP-ZOHO", CONTACT_ID)
        self.assertEqual(result["code"], "CRM_AUTH_FAILED")
        self.assertEqual(len(self.fake.refreshes()), 1)
        self.assertLessEqual(len(self.fake.crm()), 2)

    def test_scope_mismatch_is_not_a_token_problem(self):
        self.fake.valid_tokens.add(OLD_TOKEN)
        self.fake.queue = [_resp(401, {"code": "OAUTH_SCOPE_MISMATCH", "message": "invalid oauth scope"})]
        self.assertEqual(gateway.crm_search_contacts("COMP-ZOHO", email="rahul@example.com")["code"], "CRM_SCOPE_MISSING")
        self.assertEqual(self.fake.refreshes(), [])


class ErrorHandlingTest(ZohoFixture):

    def setUp(self):
        super().setUp()
        self.fake.valid_tokens.add(OLD_TOKEN)

    def _get(self, *responses):
        self.fake.queue = list(responses)
        return gateway.crm_get_contact("COMP-ZOHO", CONTACT_ID)

    def test_403_404_errors(self):
        self.assertEqual(self._get(_resp(403, {"code": "NO_PERMISSION"}))["code"], "PROVIDER_FORBIDDEN")
        self.assertEqual(self._get(_resp(404, {"code": "INVALID_URL_PATTERN"}))["code"], "PROVIDER_API_UNAVAILABLE")

    def test_429_retry_after_then_success(self):
        with patch("integrations.providers.zoho_crm.time.sleep") as sleep:
            result = self._get(_resp(429, {"code": "TOO_MANY_REQUESTS"}, headers={"Retry-After": "3"}),
                               _resp(200, {"data": [CONTACT]}))
        self.assertEqual(result["id"], CONTACT_ID)
        sleep.assert_called_once_with(3.0)

    def test_5xx_and_timeout_are_retried_then_reported(self):
        self.assertEqual(self._get(_resp(500, {}), _resp(200, {"data": [CONTACT]}))["id"], CONTACT_ID)
        self.assertEqual(self._get(_resp(503, {}), _resp(502, {}), _resp(504, {}))["code"], "PROVIDER_UNAVAILABLE")
        self.assertEqual(self._get(*[httpx.ReadTimeout("slow")] * 3)["code"], "PROVIDER_TIMEOUT")

    def test_malformed_responses(self):
        self.assertEqual(self._get(_resp(200, bad_json=True))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(200, {"data": "nope"}))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(200, {"data": [{"id": "5725767000000000000"}]}))["code"], "NOT_FOUND")


    def test_empty_200_response_is_invalid(self):
        """
        A 204 response is valid for an empty Zoho result.
        But a 200 response with an empty/non-JSON body is malformed.
        """
        result = self._get(_resp(200, bad_json=True))

        self.assertEqual(result["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(result["http_status"], 200)

    def test_large_search_response_is_limited_to_search_limit(self):
        """
        Zoho may return more records than requested or than expected.
        The connector must return no more than SEARCH_LIMIT contacts.
        """
        self.fake.valid_tokens.add(OLD_TOKEN)

        many_contacts = []

        for index in range(15):
            contact = dict(CONTACT)
            contact["id"] = f"572576700000041{index:04d}"
            contact["First_Name"] = f"Customer{index}"
            contact["Email"] = f"customer{index}@example.com"
            many_contacts.append(contact)

        self.fake.queue = [
            _resp(
                200,
                {
                    "data": many_contacts,
                    "info": {
                        "count": 15,
                        "more_records": True,
                    },
                },
            )
        ]

        result = gateway.crm_search_contacts(
            "COMP-ZOHO",
            name="Rahul",
        )

        self.assertEqual(result["searched_by"], "name")
        self.assertEqual(result["count"], 10)
        self.assertEqual(len(result["contacts"]), 10)
        self.assertTrue(result["more_records"])

    def test_reused_http_client_lifecycle(self):
        """
        Ensures the connector reuses one HTTP client until close() is called.
        This test applies after adding _get_client(), close(), and _client support
        to ZohoCRMConnector.
        """
        connector = self.connector()

        first_client = connector._get_client()
        second_client = connector._get_client()

        self.assertIs(first_client, second_client)
        self.assertFalse(first_client.is_closed)

        connector.close()

        self.assertIsNone(connector._client)
        self.assertTrue(first_client.is_closed)

    def test_secrets_never_logged_or_returned(self):
        buffer = io.StringIO()
        with patch("sys.__stdout__", buffer), redirect_stdout(buffer), self.assertLogs("uvicorn.error", level="WARNING") as logs:
            failed = self._get(RuntimeError(f"boom {CLIENT_SECRET} {OLD_TOKEN} {REFRESH}"))
            ok = gateway.crm_search_contacts("COMP-ZOHO", name="Rahul")
        blob = buffer.getvalue() + json.dumps(failed) + json.dumps(ok) + "".join(logs.output)
        for secret in (CLIENT_SECRET, OLD_TOKEN, REFRESH, CLIENT_ID):
            self.assertNotIn(secret, blob)


class RoutingAndSafetyTest(ZohoFixture):

    def test_comp_zoho_routes_to_zoho_connector(self):
        self.assertIsInstance(gateway.get_connector("COMP-ZOHO"), ZohoCRMConnector)
        self.assertTrue(gateway.has_crm("COMP-ZOHO"))
        self.assertFalse(gateway.has_crm("COMP-SHOPIFY"))
        self.assertIsInstance(gateway.get_connector("COMP-SHOPIFY"), ShopifyConnector)

    def test_env_credentials_belong_only_to_the_zoho_env_tenant(self):
        from database import execute_db
        execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES ('COMP-ZOHO-2', 'x', 'STANDARD')")
        execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type) VALUES ('COMP-ZOHO-2', 'ZOHO_CRM')")
        with self.assertRaises(TenantConfigurationError):
            gateway.get_connector("COMP-ZOHO-2")
        with patch.dict(os.environ, {"ZOHO_CLIENT_SECRET": ""}):
            with self.assertRaises(TenantConfigurationError):
                gateway.get_connector("COMP-ZOHO")

    def test_api_domain_allowlist_and_data_center_accounts_server(self):
        self.assertEqual(self.connector(api_domain="https://evil.example.com").get_contact("COMP-ZOHO", CONTACT_ID)["code"],
                         "PROVIDER_CONFIG_INSECURE")
        self.assertEqual(self.fake.calls, [])
        for domain, accounts in (("https://www.zohoapis.eu", "https://accounts.zoho.eu"),
                                 ("https://www.zohoapis.ca", "https://accounts.zohocloud.ca")):
            self.assertEqual(self.connector(api_domain=domain).accounts_url, accounts)

    def test_read_only_no_write_paths(self):
        for name in dir(ZohoCRMConnector):
            self.assertFalse(name.startswith(("create", "delete", "upsert", "send")), name)
        # The only "update" name is BaseConnector's update_order, explicitly NOT_SUPPORTED and offline.
        self.assertEqual([n for n in dir(ZohoCRMConnector) if n.startswith("update")], ["update_order"])
        self.assertEqual(gateway.get_connector("COMP-ZOHO").update_order("#1", "COMP-ZOHO", status="x")["code"], "NOT_SUPPORTED")
        self.assertEqual(self.fake.calls, [])
        gateway.crm_search_contacts("COMP-ZOHO", name="Rahul")
        gateway.crm_get_customer_profile("COMP-ZOHO", CONTACT_ID)
        gateway.crm_get_deal("COMP-ZOHO", DEAL_ID)
        self.assertTrue(all(c["method"] == "GET" for c in self.fake.crm()))

    def test_commerce_capabilities_are_not_supported_and_never_fake_tickets(self):
        c = gateway.get_connector("COMP-ZOHO")
        self.assertEqual(c.get_order_details("#1", "COMP-ZOHO")["code"], "NOT_SUPPORTED")
        escalation = c.escalate_to_human("#1", "help", "COMP-ZOHO")
        self.assertNotIn("ticket_id", escalation)
        self.assertEqual(self.fake.calls, [])


class AgentCRMTest(ZohoFixture):

    def chat(self, model, text):
        import server
        logs = io.StringIO()
        with patch.object(type(agent.llm), "invoke", model), patch("sys.__stdout__", logs), redirect_stdout(logs):
            response = TestClient(server.app).post("/api/chat", json={"user_input": text}, headers={"X-Company-ID": "COMP-ZOHO"})
        return response.json()["agent_response"], logs.getvalue()

    def test_crm_tenant_gets_only_crm_tools_and_store_tenants_are_unchanged(self):
        self.assertEqual({t["function"]["name"] for t in agent.llm_with_crm_tools.kwargs["tools"]},
                         {"crm_search_customers", "crm_get_customer", "crm_get_account", "crm_get_deal"})
        self.assertFalse({"crm_search_customers"} & {t["function"]["name"] for t in agent.llm_with_tools.kwargs["tools"]})

    def test_find_customer_rahul_then_details_through_api_chat(self):
        def model(self_, messages, config=None, **kwargs):
            bound = {t["function"]["name"] for t in kwargs.get("tools", [])}
            assert bound == {"crm_search_customers", "crm_get_customer", "crm_get_account", "crm_get_deal"}
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                return AIMessage(content="", tool_calls=[{"name": "crm_search_customers", "args": {"name": "Rahul"}, "id": "c1"}])
            if len(results) == 1:
                found = json.loads(results[0].content)["contacts"][0]
                return AIMessage(content="", tool_calls=[{"name": "crm_get_customer", "args": {"contact_id": found["id"]}, "id": "c2"}])
            profile = json.loads(results[-1].content)
            return AIMessage(content=f"{profile['contact']['first_name']} {profile['contact']['last_name']} works at {profile['account']['name']}.")
        answer, logs = self.chat(model, "Find customer Rahul and show his CRM information")
        self.assertEqual(answer, "Rahul Sharma works at Acme Pvt Ltd.")
        self.assertIn("Tool: crm_search_customers", logs)
        self.assertIn("[PROVIDER] Provider: ZOHO_CRM | Operation: GET Contacts/search", logs)
        for secret in (CLIENT_SECRET, OLD_TOKEN, REFRESH):
            self.assertNotIn(secret, logs)

    def test_invented_record_id_is_blocked_without_a_crm_call(self):
        def model(self_, messages, config=None, **kwargs):
            if not [m for m in messages if isinstance(m, ToolMessage)]:
                return AIMessage(content="", tool_calls=[{"name": "crm_get_customer", "args": {"contact_id": "5725767000000123456"}, "id": "c1"}])
            return AIMessage(content="Which customer do you mean? Please give a name or email.")
        answer, logs = self.chat(model, "Show this customer's CRM information")
        self.assertIn("UNVERIFIED_RECORD_ID", logs)
        self.assertEqual(self.fake.crm(), [])


if __name__ == "__main__":
    unittest.main()
