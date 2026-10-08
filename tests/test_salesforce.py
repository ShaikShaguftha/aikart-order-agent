"""
Mocked Salesforce CRM (read-only) tests - no real Salesforce API is called.
FakeSalesforce records every request (method, URL, SOQL, auth header) and answers from the SOQL
it receives, so tests can prove read-only behaviour, the exact (escaped) SOQL and that the
access token never leaks.
"""
import io
import json
import os
import sys
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent
from database import execute_db, init_db
from integrations.errors import TenantConfigurationError
from integrations.gateway import gateway
from integrations.providers import salesforce as sf_module
from integrations.providers.hubspot import HubSpotConnector
from integrations.providers.salesforce import SalesforceConnector, normalize_instance_url, soql_literal, soql_prefix
from integrations.providers.shopify import ShopifyConnector
from tools import salesforce_get_account, salesforce_search_contacts

TOKEN = "00Dxx0000001gPL!AQ4AQtest.token.do-not-log.0001"
INSTANCE = "https://acme-test.my.salesforce.com"
VERSION = "v62.0"
ENV = {"SALESFORCE_ACCESS_TOKEN": TOKEN, "SALESFORCE_INSTANCE_URL": INSTANCE,
       "SALESFORCE_API_VERSION": VERSION, "SALESFORCE_ENV_TENANT": "COMP-SALESFORCE"}
QUERY_URL = f"{INSTANCE}/services/data/{VERSION}/query"

CONTACT_ID, ACCOUNT_ID = "0035g00000ABCdeAAB", "0015g00000XYZabAAC"
OPP_ID, CASE_ID = "0065g00000OPPabAAD", "5005g00000CASabAAE"
CONTACT = {"attributes": {"type": "Contact", "url": f"/services/data/{VERSION}/sobjects/Contact/{CONTACT_ID}"},
           "Id": CONTACT_ID, "FirstName": "Rahul", "LastName": "Sharma", "Email": "rahul@gmail.com",
           "Phone": "+91 98765 43210", "MobilePhone": None, "AccountId": ACCOUNT_ID,
           "Account": {"attributes": {"type": "Account"}, "Name": "Acme Pvt Ltd"},
           "CreatedDate": "2026-09-01T10:00:00.000+0000", "Internal_Notes__c": "must not leak"}
ACCOUNT = {"attributes": {"type": "Account"}, "Id": ACCOUNT_ID, "Name": "Acme Pvt Ltd",
           "Website": "https://acme.example", "Phone": "+91 22 4000 0000"}
OPPORTUNITY = {"attributes": {"type": "Opportunity"}, "Id": OPP_ID, "Name": "Acme renewal", "Amount": 125000.5,
               "StageName": "Negotiation/Review", "CloseDate": "2026-10-15"}
CASE = {"attributes": {"type": "Case"}, "Id": CASE_ID, "CaseNumber": "00001026", "Subject": "Damaged parcel",
        "Status": "Working", "Priority": "High", "Description": "Box arrived crushed. " * 60,
        "CreatedDate": "2026-09-20T09:00:00.000+0000", "LastModifiedDate": "2026-09-21T09:00:00.000+0000"}
BY_ID = {CONTACT_ID: CONTACT, ACCOUNT_ID: ACCOUNT, OPP_ID: OPPORTUNITY, CASE_ID: CASE}


def _resp(status=200, body=None, headers=None, bad_json=False):
    r = MagicMock()
    r.status_code, r.headers = status, headers or {}
    if bad_json or body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    return r


def _page(records, done=True):
    return _resp(200, {"totalSize": len(records), "done": done, "records": records})


class FakeSalesforce:
    def __init__(self):
        self.calls, self.queue = [], []
        self.search_results = [CONTACT]

    @property
    def soql(self):
        return [
            call["params"].get("q")
            for call in self.calls
            if call["params"].get("q")
        ]

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

        if method == "GET" and url == f"{INSTANCE}/services/data/":
            return _resp(
                200,
                [
                    {
                        "label": "Winter '25",
                        "url": "/services/data/v62.0",
                        "version": "62.0",
                    },
                    {
                        "label": "Summer '26",
                        "url": "/services/data/v67.0",
                        "version": "67.0",
                    },
                    {
                        "label": "Spring '26",
                        "url": "/services/data/v66.0",
                        "version": "66.0",
                    },
                ],
            )

        if method != "GET" or not url.endswith("/query"):
            return _resp(
                404,
                bad_json=True,
            )

        query = params["q"]

        by_id = re.search(
            r"WHERE Id = '([A-Za-z0-9]+)'",
            query,
        )

        if by_id:
            return _page(
                [
                    record
                    for record_id, record in BY_ID.items()
                    if record_id[:15] == by_id.group(1)[:15]
                ]
            )

        if "CaseNumber IN" in query:
            return _page(
                [CASE]
                if "'00001026'" in query
                else []
            )

        if "FROM Contact" in query:
            return _page(self.search_results)

        return _page([])

class SalesforceFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        # pytest/Jupyter on Windows can expose an invalid sys.__stdout__ handle.
        # Gateway/provider logging writes to sys.__stdout__, so redirect it to
        # the valid active stdout stream during each test.
        stdout_patch = patch.object(sys, "__stdout__", sys.stdout)
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        sf_module._DISCOVERED_VERSIONS.clear()
        self.addCleanup(sf_module._DISCOVERED_VERSIONS.clear)
        self.fake = FakeSalesforce()
        original = httpx.Client.request

        def transport(client, method, url, *args, **kwargs):
            if "salesforce.com" in str(url) or "force.com" in str(url):
                return self.fake(
                    client,
                    method=method,
                    url=str(url),
                    **kwargs,
                )

            return original(
                client,
                method,
                url,
                *args,
                **kwargs,
            )

        for target in (patch.object(httpx.Client, "request", transport),
                       patch("integrations.providers.salesforce.time.sleep")):
            target.start()
            self.addCleanup(target.stop)
        self.sf = SalesforceConnector(TOKEN, INSTANCE, VERSION, retry_backoff=0)


class ConfigTest(SalesforceFixture):

    def test_01_connector_initialization(self):
        sf = SalesforceConnector(f" {TOKEN} ", "acme-test.my.salesforce.com/", "62.0")
        self.assertEqual((sf.instance_url, sf.api_version, sf.provider_name), (INSTANCE, "v62.0", "SALESFORCE"))
        self.assertTrue(sf.credentials_configured() and sf.domain_configured())
        self.assertIsNone(sf.configuration_error())
        self.assertEqual(normalize_instance_url("https://acme--uat.sandbox.my.salesforce.com"),
                         "https://acme--uat.sandbox.my.salesforce.com")
        self.assertEqual(normalize_instance_url("https://acme.lightning.force.com"), "https://acme.lightning.force.com")
        for bad in ("http://acme.my.salesforce.com", "https://evil.example.com", "https://acme.salesforce.com.evil.io",
                    "https://acme.my.salesforce.com/services", "https://user:pw@acme.my.salesforce.com",
                    "https://acme.my.salesforce.com:8443", "https://acme.my.salesforce.com?x=1"):
            self.assertIsNone(normalize_instance_url(bad), bad)


    def test_http_client_reused_and_closed(self):
        first_client = self.sf._get_client()
        second_client = self.sf._get_client()

        self.assertIs(first_client, second_client)
        self.assertFalse(first_client.is_closed)

        self.sf.close()

        self.assertIsNone(self.sf._client)
        self.assertTrue(first_client.is_closed)

    def test_02_credentials_missing(self):
        cases = {SalesforceConnector("", INSTANCE): "access token",
                 SalesforceConnector(TOKEN, ""): "instance URL is not configured",
                 SalesforceConnector(TOKEN, "https://evil.example.com"): "instance URL must be",
                 SalesforceConnector(TOKEN, INSTANCE, "latest"): "API version"}
        for connector, message in cases.items():
            err = connector.configuration_error()
            self.assertEqual(err["code"], "PROVIDER_NOT_CONFIGURED")
            self.assertIn(message, err["error"])
            self.assertEqual(connector.get_contact(CONTACT_ID)["code"], "PROVIDER_NOT_CONFIGURED")
            self.assertEqual(connector.search_contacts(name="Rahul")["code"], "PROVIDER_NOT_CONFIGURED")
        self.assertEqual(self.fake.calls, [])

    def test_api_version_discovered_when_not_configured(self):
        sf = SalesforceConnector(TOKEN, INSTANCE, None, retry_backoff=0)
        self.assertEqual(sf.get_contact(CONTACT_ID)["id"], CONTACT_ID)
        sf.get_account(ACCOUNT_ID)
        urls = [c["url"] for c in self.fake.calls]
        self.assertEqual(urls, [f"{INSTANCE}/services/data/", f"{INSTANCE}/services/data/v67.0/query",
                                f"{INSTANCE}/services/data/v67.0/query"])  # newest version, discovered once
        self.fake.queue = [_resp(200, {"unexpected": True})]
        sf_module._DISCOVERED_VERSIONS.clear()
        self.assertEqual(sf.get_contact(CONTACT_ID)["code"], "PROVIDER_INVALID_RESPONSE")


class ReadTest(SalesforceFixture):

    def test_03_authentication_headers_and_request_shape(self):
        self.sf.get_contact(CONTACT_ID)
        call = self.fake.calls[0]
        self.assertEqual(call["auth"], f"Bearer {TOKEN}")
        self.assertEqual((call["method"], call["url"], call["json"]), ("GET", QUERY_URL, None))

    def test_04_get_contact(self):
        self.assertEqual(self.sf.get_contact(CONTACT_ID), {
            "id": CONTACT_ID, "first_name": "Rahul", "last_name": "Sharma", "email": "rahul@gmail.com",
            "phone": "+91 98765 43210", "company": "Acme Pvt Ltd", "account_id": ACCOUNT_ID,
            "created_at": "2026-09-01T10:00:00.000+0000", "provider": "SALESFORCE"})
        self.assertEqual(self.fake.soql[0], "SELECT Id, FirstName, LastName, Email, Phone, MobilePhone, AccountId, "
                                            f"Account.Name, CreatedDate FROM Contact WHERE Id = '{CONTACT_ID}' LIMIT 1")
        self.assertEqual(self.sf.get_contact(CONTACT_ID[:15])["id"], CONTACT_ID)  # 15-char form accepted

    def test_05_search_by_email(self):
        result = self.sf.search_contacts(email="rahul@gmail.com")
        self.assertEqual((result["count"], result["searched_by"], result["contacts"][0]["id"]), (1, "email", CONTACT_ID))
        self.assertIn("FROM Contact WHERE Email = 'rahul@gmail.com' ORDER BY LastModifiedDate DESC LIMIT 10",
                      self.fake.soql[0])

    def test_06_search_by_phone(self):
        self.sf.search_contacts(phone="+91 98765 43210")
        self.assertIn("WHERE (Phone = '+91 98765 43210' OR MobilePhone = '+91 98765 43210')", self.fake.soql[0])

    def test_07_search_by_name(self):
        self.sf.search_contacts(name="Rahul")
        self.assertIn("WHERE (FirstName LIKE 'Rahul%' OR LastName LIKE 'Rahul%')", self.fake.soql[0])
        self.sf.search_contacts(name="  Rahul   Sharma ")
        self.assertIn("WHERE (FirstName LIKE 'Rahul%' AND LastName LIKE 'Sharma%')", self.fake.soql[1])

    def test_08_no_results(self):
        self.fake.search_results = []
        self.assertEqual(self.sf.search_contacts(name="Nobody"),
                         {"contacts": [], "count": 0, "searched_by": "name", "more_records": False})
        self.assertEqual(self.sf.search_contacts(email="nobody@example.com")["contacts"], [])
        self.assertEqual(self.sf.get_contact("0035g00000ZZZzzAAA")["code"], "NOT_FOUND")
        self.assertEqual(self.sf.get_case("99999")["code"], "NOT_FOUND")

    def test_09_get_account(self):
        self.assertEqual(self.sf.get_account(ACCOUNT_ID), {"id": ACCOUNT_ID, "name": "Acme Pvt Ltd",
                                                          "website": "https://acme.example",
                                                          "phone": "+91 22 4000 0000", "provider": "SALESFORCE"})
        self.assertIn("FROM Account WHERE", self.fake.soql[0])

    def test_10_get_opportunity(self):
        self.assertEqual(self.sf.get_opportunity(OPP_ID), {"id": OPP_ID, "name": "Acme renewal", "amount": 125000.5,
                                                           "stage": "Negotiation/Review", "closing_date": "2026-10-15",
                                                           "provider": "SALESFORCE"})

    def test_11_get_case_by_id_and_case_number(self):
        case = self.sf.get_case(CASE_ID)
        self.assertEqual((case["id"], case["number"], case["subject"], case["stage"], case["priority"]),
                         (CASE_ID, "00001026", "Damaged parcel", "Working", "High"))
        self.assertLessEqual(len(case["description"]), 500)
        self.assertEqual(self.sf.get_case("1026")["id"], CASE_ID)
        self.assertIn("WHERE CaseNumber IN ('00001026', '1026') LIMIT 1", self.fake.soql[-1])
        self.assertEqual(self.sf.get_case("#00001026")["number"], "00001026")

    def test_record_id_prefix_is_checked_before_any_request(self):
        for result in (self.sf.get_contact(ACCOUNT_ID), self.sf.get_account(CONTACT_ID),
                       self.sf.get_opportunity(CASE_ID), self.sf.get_case(OPP_ID), self.sf.get_contact("003abc"),
                       self.sf.get_contact("../sobjects"), self.sf.get_contact(None)):
            self.assertEqual(result["code"], "INVALID_RECORD_ID")
        self.assertEqual(self.fake.calls, [])

    def test_more_records_flag(self):
        self.fake.queue = [_page([CONTACT], done=False)]
        self.assertTrue(self.sf.search_contacts(name="Rahul")["more_records"])


    def test_large_contact_search_is_limited_to_ten_results(self):
        large_contacts = []

        for index in range(15):
            large_contacts.append(
                {
                    "Id": f"0035g00000TEST{index:03d}",
                    "FirstName": f"Customer{index}",
                    "LastName": "Test",
                    "Email": f"customer{index}@example.com",
                    "Phone": None,
                    "MobilePhone": None,
                    "AccountId": None,
                    "Account": None,
                    "CreatedDate": "2026-09-01T10:00:00.000+0000",
                }
            )

        self.fake.queue = [
            _page(
                large_contacts,
                done=False,
            )
        ]

        result = self.sf.search_contacts(
            name="Rahul",
        )

        self.assertEqual(
            result["count"],
            10,
        )

        self.assertEqual(
            len(result["contacts"]),
            10,
        )

        self.assertTrue(
            result["more_records"],
        )


class ErrorTest(SalesforceFixture):

    def _get(self, *responses):
        self.fake.queue = list(responses)
        return self.sf.get_contact(CONTACT_ID)

    def test_12_401(self):
        result = self._get(_resp(401, [{"message": "Session expired or invalid", "errorCode": "INVALID_SESSION_ID"}]))
        self.assertEqual((result["code"], result["error"], result["reconnect_required"], result["http_status"]),
                         ("PROVIDER_AUTH_FAILED", "Salesforce authentication failed. Reconnect required.", True, 401))
        self.assertEqual(len(self.fake.calls), 1)  # never retried

    def test_13_403(self):
        result = self._get(_resp(403, [{"message": "insufficient access", "errorCode": "INSUFFICIENT_ACCESS"}]))
        self.assertEqual(result["code"], "PROVIDER_FORBIDDEN")
        result = self._get(_resp(403, [{"message": "TotalRequests Limit exceeded.", "errorCode": "REQUEST_LIMIT_EXCEEDED"}]))
        self.assertEqual(result["code"], "RATE_LIMITED")
        self.assertEqual(len(self.fake.calls), 2)  # daily API limit: not retried

    def test_14_404(self):
        result = self._get(_resp(404, [{"message": "The requested resource does not exist", "errorCode": "NOT_FOUND"}]))
        self.assertEqual(result["code"], "NOT_FOUND")
        self.assertEqual(self._get(_resp(404, bad_json=True))["code"], "PROVIDER_API_UNAVAILABLE")

    def test_15_429_retry_after_then_success_and_bounded(self):
        with patch("integrations.providers.salesforce.time.sleep") as sleep:
            self.assertEqual(self._get(_resp(429, bad_json=True, headers={"Retry-After": "2"}),
                                       _page([CONTACT]))["id"], CONTACT_ID)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(self._get(*[_resp(429, bad_json=True)] * 3)["code"], "RATE_LIMITED")
        with patch("integrations.providers.salesforce.time.sleep") as sleep:
            self._get(_resp(429, bad_json=True, headers={"Retry-After": "3600"}), _page([CONTACT]))
        sleep.assert_called_once_with(5.0)  # capped

    def test_16_5xx(self):
        self.assertEqual(self._get(_resp(502, bad_json=True), _page([CONTACT]))["id"], CONTACT_ID)
        self.assertEqual(self._get(_resp(500, bad_json=True), _resp(503, bad_json=True),
                                   _resp(504, bad_json=True))["code"], "PROVIDER_UNAVAILABLE")

    def test_17_timeout_and_connection_errors(self):
        self.assertEqual(self._get(*[httpx.ReadTimeout("slow")] * 3)["code"], "PROVIDER_TIMEOUT")
        self.assertEqual(self._get(*[httpx.ConnectError("down")] * 3)["code"], "PROVIDER_UNREACHABLE")
        self.assertEqual(self._get(httpx.ConnectError("blip"), _page([CONTACT]))["id"], CONTACT_ID)

    def test_18_malformed_response(self):
        self.assertEqual(self._get(_resp(200, bad_json=True))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(200, {"totalSize": 1, "records": "nope"}))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(200, [CONTACT]))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_page([{"FirstName": "No Id"}]))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_page([dict(CONTACT, Id="0035g00000OTHERAAA")]))["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(self._get(_resp(400, [{"errorCode": "MALFORMED_QUERY"}]))["code"], "PROVIDER_VALIDATION_FAILED")
        self.assertEqual(self._get(_resp(302, bad_json=True, headers={"Location": "https://elsewhere"}))["code"],
                         "PROVIDER_API_UNAVAILABLE")
        self.fake.queue = [_page(["junk", None, CONTACT])]
        self.assertEqual(self.sf.search_contacts(name="Rahul")["count"], 1)

    def test_19_missing_and_null_fields(self):
        contact = self._get(_page([{"Id": CONTACT_ID, "FirstName": None, "Email": "", "Account": None}]))
        self.assertEqual({k: contact[k] for k in ("first_name", "last_name", "email", "phone", "company", "account_id")},
                         dict.fromkeys(("first_name", "last_name", "email", "phone", "company", "account_id")))
        self.fake.queue = [_page([{"Id": OPP_ID, "Amount": "not-a-number"}])]
        self.assertIsNone(self.sf.get_opportunity(OPP_ID)["amount"])
        self.fake.queue = [_page([{"Id": CASE_ID}])]
        case = self.sf.get_case(CASE_ID)
        self.assertEqual((case["number"], case["subject"], case["description"]), (None, None, None))

    def test_22_secret_leakage_prevention(self):
        buffer = io.StringIO()
        with patch("sys.__stdout__", buffer), redirect_stdout(buffer), \
                self.assertLogs("uvicorn.error", level="WARNING") as logs:
            failed = self._get(RuntimeError(f"boom {TOKEN}"))
            ok = self.sf.search_contacts(email="rahul@gmail.com")
            denied = self._get(_resp(401, [{"message": f"bad token {TOKEN}", "errorCode": "INVALID_SESSION_ID"}]))
        blob = buffer.getvalue() + json.dumps([failed, ok, denied]) + "".join(logs.output)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn("Bearer", blob)
        self.assertNotIn("rahul@gmail.com", buffer.getvalue())  # SOQL (customer data) is not logged
        self.assertIn("Operation: GET services/data/v62.0/query (Contact)", buffer.getvalue())
        self.assertNotIn("Internal_Notes__c", json.dumps(ok))  # only canonical fields leave the connector

    def test_23_read_only_behavior(self):
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            self.assertEqual(self.sf._request(method, f"services/data/{VERSION}/sobjects/Contact")["code"], "NOT_SUPPORTED")
        self.assertEqual(self.fake.calls, [])
        for name in dir(SalesforceConnector):
            self.assertFalse(name.startswith(("create", "delete", "upsert", "send", "insert")), name)
        self.assertEqual(self.sf.cancel_order("1001", "COMP-SALESFORCE")["code"], "NOT_SUPPORTED")
        self.assertEqual(self.sf.escalate_to_human("1001", "x", "COMP-SALESFORCE")["status"], "ERROR")
        for soql in self.fake.soql:
            self.assertTrue(soql.startswith("SELECT "))


class SoqlEscapingTest(SalesforceFixture):

    def test_24_soql_literal_escaping(self):
        self.assertEqual(soql_literal("O'Brien"), r"'O\'Brien'")
        self.assertEqual(soql_literal('a\\b"c\nd\te'), r"'a\\b\"c\nd\te'")
        self.assertEqual(soql_prefix("50%_off"), r"'50\%\_off%'")
        self.assertEqual(soql_prefix("x'"), r"'x\'%'")
        for bad in ("a\x00b", "a\x1bb", "a\x7fb"):
            with self.assertRaises(ValueError):
                soql_literal(bad)

    def test_24_injection_attempts_are_escaped_or_rejected(self):
        self.sf.search_contacts(name="O'Brien")
        self.assertIn(r"(FirstName LIKE 'O\'Brien%' OR LastName LIKE 'O\'Brien%')", self.fake.soql[-1])
        self.sf.search_contacts(email="x'OR'Id!=''@evil.io")
        self.assertIn(r"WHERE Email = 'x\'OR\'Id!=\'\'@evil.io' ORDER BY", self.fake.soql[-1])
        self.sf.search_contacts(email="a\\'b@x.io")
        self.assertIn(r"WHERE Email = 'a\\\'b@x.io'", self.fake.soql[-1])
        # After removing escaped characters every WHERE clause has only balanced, intact literals.
        for soql in self.fake.soql:
            unescaped = re.sub(r"\\.", "", soql)
            self.assertEqual(unescaped.count("'") % 2, 0, soql)
            self.assertNotIn("OR 'Id", unescaped)
        before = len(self.fake.calls)
        for kwargs in ({"name": "' OR Name LIKE '%"}, {"name": "Rahul%"}, {"name": "R_hul"}, {"name": "R2D2"},
                       {"name": "x"}, {"phone": "12345' OR '1'='1"}, {"phone": "12"}, {"email": "not-an-email"},
                       {"email": "a b@x.io"}, {"email": "x@y\n.io"}, {"name": "   "}, {}, {"email": "", "phone": "", "name": ""}):
            self.assertEqual(self.sf.search_contacts(**kwargs)["code"], "INVALID_ARGUMENT", kwargs)
        for bad_id in ("0035g00000ABCde' OR Id != '", "5005g00000CAS%", "1 OR 1=1"):
            self.assertIn(self.sf.get_case(bad_id)["code"], ("INVALID_RECORD_ID",))
        self.assertEqual(len(self.fake.calls), before)  # nothing malformed reached Salesforce


class GatewayTest(SalesforceFixture):

    def test_20_gateway_routing(self):
        self.assertIsInstance(gateway.get_connector("COMP-SALESFORCE"), SalesforceConnector)
        self.assertIsInstance(gateway.get_tenant_primary_connector("COMP-SALESFORCE"), SalesforceConnector)
        self.assertTrue(gateway.has_salesforce("COMP-SALESFORCE"))
        for tenant in ("COMP-SHOPIFY", "COMP-ZOHO", "COMP-HUBSPOT", "COMP-ALPHA", "COMP-UNKNOWN"):
            self.assertFalse(gateway.has_salesforce(tenant))
        self.assertIsInstance(gateway.get_connector("COMP-SHOPIFY"), ShopifyConnector)
        with patch.dict(os.environ, {"HUBSPOT_ACCESS_TOKEN": "pat-x", "HUBSPOT_ENV_TENANT": "COMP-HUBSPOT"}):
            self.assertIsInstance(gateway.get_connector("COMP-HUBSPOT"), HubSpotConnector)
        profile = gateway.salesforce_get_customer_profile("COMP-SALESFORCE", CONTACT_ID)
        self.assertEqual((profile["contact"]["id"], profile["account"]["name"]), (CONTACT_ID, "Acme Pvt Ltd"))
        self.assertEqual(gateway.salesforce_get_case("COMP-SALESFORCE", "1026")["id"], CASE_ID)
        self.assertEqual(gateway.salesforce_get_opportunity("COMP-SALESFORCE", OPP_ID)["name"], "Acme renewal")
        self.assertEqual(gateway.salesforce_search_contacts("COMP-SALESFORCE", name="Rahul")["count"], 1)

    def test_02_gateway_credentials_missing(self):
        for missing in ("SALESFORCE_ACCESS_TOKEN", "SALESFORCE_INSTANCE_URL"):
            with patch.dict(os.environ, {missing: ""}):
                with self.assertRaises(TenantConfigurationError):
                    gateway.get_connector("COMP-SALESFORCE")
                result = json.loads(salesforce_search_contacts.invoke({"name": "Rahul", "company_id": "COMP-SALESFORCE"}))
                self.assertEqual(result["code"], "TENANT_CONFIGURATION_ERROR")
                self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(self.fake.calls, [])

    def test_21_tenant_isolation(self):
        execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES ('COMP-SALESFORCE-2', 'x', 'STANDARD')")
        execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type) "
                   "VALUES ('COMP-SALESFORCE-2', 'SALESFORCE')")
        with self.assertRaises(TenantConfigurationError):
            gateway.get_connector("COMP-SALESFORCE-2")  # env credentials belong only to SALESFORCE_ENV_TENANT
        for tenant in ("COMP-SHOPIFY", "COMP-ZOHO", "COMP-HUBSPOT", "COMP-SALESFORCE-2"):
            result = json.loads(salesforce_get_account.invoke({"account_id": ACCOUNT_ID, "company_id": tenant}))
            self.assertIn("code", result)
            self.assertNotIn("Acme", json.dumps(result))
        self.assertEqual(self.fake.calls, [])


class AgentTest(SalesforceFixture):

    def chat(self, model, text, tenant="COMP-SALESFORCE"):
        import server
        logs = io.StringIO()
        with patch.object(type(agent.llm), "invoke", model), patch("sys.__stdout__", logs), redirect_stdout(logs):
            response = TestClient(server.app).post("/api/chat", json={"user_input": text}, headers={"X-Company-ID": tenant})
        return response, logs.getvalue()

    def test_tools_bound_only_for_salesforce_tenant(self):
        names = {t["function"]["name"] for t in agent.llm_with_salesforce_tools.kwargs["tools"]}
        self.assertEqual(names, {"salesforce_search_contacts", "salesforce_get_customer", "salesforce_get_account",
                                 "salesforce_get_opportunity", "salesforce_get_case"})
        for binding in (agent.llm_with_tools, agent.llm_with_crm_tools, agent.llm_with_hubspot_tools,
                        agent.llm_with_helpdesk_shipping_tools):
            self.assertFalse(any(t["function"]["name"].startswith("salesforce") for t in binding.kwargs["tools"]))

    def test_find_rahul_then_crm_details_through_api_chat(self):
        def model(self_, messages, config=None, **kwargs):
            bound = {t["function"]["name"] for t in kwargs.get("tools", [])}
            assert "salesforce_search_contacts" in bound and "hubspot_search_contacts" not in bound, bound
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                return AIMessage(content="", tool_calls=[{"name": "salesforce_search_contacts", "args": {"name": "Rahul"}, "id": "c1"}])
            if len(results) == 1:
                found = json.loads(results[0].content)["contacts"][0]
                return AIMessage(content="", tool_calls=[{"name": "salesforce_get_customer", "args": {"contact_id": found["id"]}, "id": "c2"}])
            profile = json.loads(results[-1].content)
            return AIMessage(content=f"{profile['contact']['first_name']} {profile['contact']['last_name']} - {profile['account']['name']}")
        response, logs = self.chat(model, "Find Rahul in Salesforce and show his CRM details")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["agent_response"], "Rahul Sharma - Acme Pvt Ltd")
        self.assertIn("[PROVIDER] Provider: SALESFORCE | Operation: GET services/data/v62.0/query (Contact)", logs)
        self.assertNotIn(TOKEN, logs)

    def test_invented_record_id_is_blocked(self):
        def model(self_, messages, config=None, **kwargs):
            if not [m for m in messages if isinstance(m, ToolMessage)]:
                return AIMessage(content="", tool_calls=[{"name": "salesforce_get_opportunity",
                                                          "args": {"opportunity_id": OPP_ID}, "id": "c1"}])
            return AIMessage(content="Which opportunity do you mean?")
        response, logs = self.chat(model, "Show opportunity details")
        self.assertIn("UNVERIFIED_RECORD_ID", logs)
        self.assertEqual(self.fake.calls, [])

    def test_id_guard_accepts_ids_from_user_or_results(self):
        msgs = [HumanMessage(content=f"Show opportunity {OPP_ID} and case #1026 for {CONTACT_ID[:15]}")]
        self.assertIsNone(agent.verify_salesforce_ids({"opportunity_id": OPP_ID}, msgs))
        self.assertIsNone(agent.verify_salesforce_ids({"case_id": "00001026"}, msgs))
        self.assertIsNone(agent.verify_salesforce_ids({"contact_id": CONTACT_ID[:15]}, msgs))
        for args in ({"account_id": ACCOUNT_ID}, {"opportunity_id": OPP_ID.lower()}, {"opportunity_id": OPP_ID[:10]},
                     {"case_id": "1027"}):
            self.assertEqual(agent.verify_salesforce_ids(args, msgs)["code"], "UNVERIFIED_RECORD_ID", args)


if __name__ == "__main__":
    unittest.main()