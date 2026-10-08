"""
Mocked Razorpay (read-only) tests - no real Razorpay API is called and no real key is used.
FakeRazorpay records every request (method, URL, params, Authorization header) so tests can prove
the endpoints, Basic auth, read-only behaviour and that the key secret never leaks.
"""
import base64
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
from integrations.providers.razorpay import BASE_URL, RazorpayConnector
from integrations.providers.shopify import ShopifyConnector
from tools import razorpay_get_payment, razorpay_get_payment_refunds, razorpay_get_refund

KEY_ID = "rzp_test_FakeKeyId12345"
SECRET = "fakeSecretDoNotLog0001xyz"
BASIC = base64.b64encode(f"{KEY_ID}:{SECRET}".encode()).decode()
ENV = {"RAZORPAY_KEY_ID": KEY_ID, "RAZORPAY_KEY_SECRET": SECRET, "RAZORPAY_ENV_TENANT": "COMP-RAZORPAY"}

PAY_ID, REFUND_ID, OTHER_PAY = "pay_29QQoUBi66xm2f", "rfnd_FP8QHiV938haTz", "pay_OTHERoUBi66xm2f"
PAYMENT = {"id": PAY_ID, "entity": "payment", "amount": 50000, "currency": "INR", "status": "captured",
           "order_id": "order_9A33XWu170gUtm", "invoice_id": None, "international": False, "method": "card",
           "amount_refunded": 20000, "refund_status": "partial", "captured": True, "description": "Test order",
           "card_id": "card_6krZ6bcjoeqyV9", "email": "gaurav.kumar@example.com", "contact": "+919000090000",
           "notes": {"internal": "must not leak"}, "fee": 1180, "tax": 180, "error_code": None, "created_at": 1735689600}
REFUND = {"id": REFUND_ID, "entity": "refund", "amount": 20000, "currency": "INR", "payment_id": PAY_ID,
          "notes": {}, "receipt": None, "acquirer_data": {"arn": "10000000000000"}, "created_at": 1735776000,
          "batch_id": None, "status": "processed", "speed_processed": "normal", "speed_requested": "optimum"}
NOT_EXIST = {"error": {"code": "BAD_REQUEST_ERROR", "description": "The id provided does not exist",
                       "source": "business", "step": "payment_initiation", "reason": "input_validation_failed"}}


def _resp(status=200, body=None, headers=None, bad_json=False):
    r = MagicMock()
    r.status_code, r.headers = status, headers or {}
    if bad_json or body is None:
        r.json.side_effect = ValueError("no json")
    else:
        r.json.return_value = body
    return r
# Change
class FakeRazorpay:
    def __init__(self):
        self.calls, self.queue = [], []
        self.refunds = [REFUND]

    def __call__(self, client, method=None, url=None, params=None, headers=None, **kwargs):
        """
        Supports both the old request-level-header implementation and the new
        persistent-httpx-client implementation where headers live on client.headers.
        """
        effective_headers = headers or client.headers

        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "auth": effective_headers.get("Authorization"),
                "body": kwargs.get("json") or kwargs.get("data"),
            }
        )

        if self.queue:
            item = self.queue.pop(0)

            if isinstance(item, Exception):
                raise item

            return item

        if method != "GET":
            return _resp(405, bad_json=True)

        routes = {
            f"{BASE_URL}/payments/{PAY_ID}": PAYMENT,
            f"{BASE_URL}/refunds/{REFUND_ID}": REFUND,
            f"{BASE_URL}/payments/{PAY_ID}/refunds": {
                "entity": "collection",
                "count": len(self.refunds),
                "items": self.refunds,
            },
        }

        if url in routes:
            return _resp(200, routes[url])

        return _resp(400, NOT_EXIST)

class RazorpayFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

        
    def setUp(self):
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)

        # pytest/Jupyter on Windows can expose an invalid sys.__stdout__ handle.
        # Gateway/provider logging writes to sys.__stdout__, so use the valid
        # active stdout stream during each test.
        stdout_patch = patch.object(sys, "__stdout__", sys.stdout)
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)

        self.fake = FakeRazorpay()
        original = httpx.Client.request

        def transport(client, method, url, *args, **kwargs):
            if "api.razorpay.com" in str(url):
                return self.fake(
                    client,
                    method=method,
                    url=str(url),
                    **kwargs,
                )

            return original(client, method, url, *args, **kwargs)

        for target in (
            patch.object(httpx.Client, "request", transport),
            patch("integrations.providers.razorpay.time.sleep"),
        ):
            target.start()
            self.addCleanup(target.stop)

        self.rzp = RazorpayConnector(
            KEY_ID,
            SECRET,
            retry_backoff=0,
        )


class ReadTest(RazorpayFixture):

    def test_01_get_payment(self):
        self.assertEqual(self.rzp.get_payment(PAY_ID), {
            "id": PAY_ID, "amount": 500.0, "amount_minor": 50000, "currency": "INR", "status": "captured",
            "order_id": "order_9A33XWu170gUtm", "method": "card", "email": "gaurav.kumar@example.com",
            "contact": "+919000090000", "captured": True, "amount_refunded": 200.0, "refund_status": "partial",
            "created_at": "2025-01-01T00:00:00Z", "provider": "RAZORPAY"})

    def test_02_get_payment_refunds(self):
        result = self.rzp.get_payment_refunds(PAY_ID)
        self.assertEqual((result["payment_id"], result["count"], result["more_records"]), (PAY_ID, 1, False))
        self.assertEqual(result["refunds"][0]["id"], REFUND_ID)
        self.assertEqual(self.fake.calls[0]["params"], {"count": 10})
        self.fake.refunds = []
        self.assertEqual(self.rzp.get_payment_refunds(PAY_ID)["refunds"], [])

    def test_03_get_refund(self):
        self.assertEqual(self.rzp.get_refund(REFUND_ID), {
            "id": REFUND_ID, "payment_id": PAY_ID, "amount": 200.0, "amount_minor": 20000, "currency": "INR",
            "status": "processed", "speed": "normal", "created_at": "2025-01-02T00:00:00Z", "provider": "RAZORPAY"})

    def test_04_basic_auth(self):
        self.rzp.get_payment(PAY_ID)
        auth = self.fake.calls[0]["auth"]
        self.assertEqual(auth, f"Basic {BASIC}")
        self.assertEqual(base64.b64decode(auth.split()[1]).decode(), f"{KEY_ID}:{SECRET}")
        self.assertEqual(self.rzp.mode, "test")
        self.assertEqual(RazorpayConnector("rzp_live_AbCdEf123456", "s").mode, "live")


    def test_http_client_reused_and_closed(self):
        """
        The connector must reuse one httpx client until close() is called.
        """
        first_client = self.rzp._get_client()
        second_client = self.rzp._get_client()

        self.assertIs(first_client, second_client)
        self.assertFalse(first_client.is_closed)

        self.rzp.close()

        self.assertIsNone(self.rzp._client)
        self.assertTrue(first_client.is_closed)

    def test_05_endpoint_construction_and_id_validation(self):
        self.rzp.get_payment(PAY_ID)
        self.rzp.get_payment_refunds(PAY_ID)
        self.rzp.get_refund(REFUND_ID)
        self.assertEqual([(c["method"], c["url"]) for c in self.fake.calls], [
            ("GET", "https://api.razorpay.com/v1/payments/pay_29QQoUBi66xm2f"),
            ("GET", "https://api.razorpay.com/v1/payments/pay_29QQoUBi66xm2f/refunds"),
            ("GET", "https://api.razorpay.com/v1/refunds/rfnd_FP8QHiV938haTz")])
        before = len(self.fake.calls)
        for result in (self.rzp.get_payment("order_9A33XWu170gUtm"), self.rzp.get_payment("pay_abc/../refunds"),
                       self.rzp.get_payment(REFUND_ID), self.rzp.get_payment(""), self.rzp.get_payment(None),
                       self.rzp.get_payment_refunds("pay_x"), self.rzp.get_refund(PAY_ID),
                       self.rzp.get_refund("rfnd_abc?count=1"), self.rzp.get_refund("1003")):
            self.assertEqual(result["code"], "INVALID_RECORD_ID")
        self.assertEqual(len(self.fake.calls), before)  # invalid IDs never reach Razorpay

    def test_06_successful_200_details(self):
        self.fake.queue = [_resp(200, dict(PAYMENT, currency="JPY", amount=5000, amount_refunded=0))]
        payment = self.rzp.get_payment(PAY_ID)
        self.assertEqual((payment["amount"], payment["amount_minor"], payment["amount_refunded"]), (5000.0, 5000, 0.0))
        self.fake.queue = [_resp(200, {"entity": "collection", "count": 15, "items": [REFUND] * 15})]
        self.assertTrue(self.rzp.get_payment_refunds(PAY_ID)["more_records"])
        self.assertNotIn("notes", json.dumps(self.rzp.get_payment(PAY_ID)))  # only canonical fields
        self.assertNotIn("card_id", json.dumps(self.rzp.get_payment(PAY_ID)))


class ErrorTest(RazorpayFixture):

    def _get(self, *responses):
        self.fake.queue = list(responses)
        return self.rzp.get_payment(PAY_ID)

    def test_07_401(self):
        result = self._get(_resp(401, {"error": {"code": "BAD_REQUEST_ERROR", "description": "The api key provided is invalid"}}))
        self.assertEqual((result["code"], result["reconnect_required"], result["http_status"]),
                         ("PROVIDER_AUTH_FAILED", True, 401))
        self.assertEqual(len(self.fake.calls), 1)  # never retried

    def test_08_404_and_unknown_id(self):
        self.assertEqual(self.rzp.get_payment("pay_UnknownPay12345")["code"], "NOT_FOUND")  # Razorpay: 400 does not exist
        self.assertEqual(self.rzp.get_refund("rfnd_UnknownRef12345")["code"], "NOT_FOUND")
        self.assertEqual(self._get(_resp(404, NOT_EXIST))["code"], "NOT_FOUND")
        self.assertEqual(self._get(_resp(404, bad_json=True))["code"], "PROVIDER_API_UNAVAILABLE")
        url_missing = {"error": {"code": "BAD_REQUEST_ERROR", "description": "The requested URL was not found on the server."}}
        self.assertEqual(self._get(_resp(400, url_missing))["code"], "PROVIDER_API_UNAVAILABLE")
        other = {"error": {"code": "BAD_REQUEST_ERROR", "description": "count must be at most 100"}}
        self.assertEqual(self._get(_resp(400, other))["code"], "PROVIDER_VALIDATION_FAILED")
        self.assertEqual(self._get(_resp(403, bad_json=True))["code"], "PROVIDER_FORBIDDEN")

    def test_retries_429_5xx_timeout(self):
        with patch("integrations.providers.razorpay.time.sleep") as sleep:
            self.assertEqual(self._get(_resp(429, bad_json=True, headers={"Retry-After": "2"}), _resp(200, PAYMENT))["id"], PAY_ID)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(self._get(*[_resp(429, bad_json=True)] * 3)["code"], "RATE_LIMITED")
        self.assertEqual(self._get(_resp(502, bad_json=True), _resp(200, PAYMENT))["id"], PAY_ID)
        self.assertEqual(self._get(*[_resp(503, bad_json=True)] * 3)["code"], "PROVIDER_UNAVAILABLE")
        self.assertEqual(self._get(*[httpx.ReadTimeout("slow")] * 3)["code"], "PROVIDER_TIMEOUT")
        self.assertEqual(self._get(*[httpx.ConnectError("down")] * 3)["code"], "PROVIDER_UNREACHABLE")

    def test_09_malformed_response(self):
        for body in (_resp(200, bad_json=True), _resp(200, [PAYMENT]), _resp(200, {"id": PAY_ID}),
                     _resp(200, dict(PAYMENT, entity="refund")), _resp(200, dict(PAYMENT, id=OTHER_PAY)),
                     _resp(200, dict(PAYMENT, id=None))):
            self.assertEqual(self._get(body)["code"], "PROVIDER_INVALID_RESPONSE")
        self.fake.queue = [_resp(200, {"entity": "collection", "items": "nope"})]
        self.assertEqual(self.rzp.get_payment_refunds(PAY_ID)["code"], "PROVIDER_INVALID_RESPONSE")
        self.fake.queue = [_resp(200, {"entity": "collection", "items": [dict(REFUND, payment_id=OTHER_PAY)]})]
        self.assertEqual(self.rzp.get_payment_refunds(PAY_ID)["code"], "PROVIDER_INVALID_RESPONSE")
        self.fake.queue = [_resp(200, dict(REFUND, id="rfnd_SomeoneElse1234"))]
        self.assertEqual(self.rzp.get_refund(REFUND_ID)["code"], "PROVIDER_INVALID_RESPONSE")


    def test_empty_200_response_is_invalid(self):
        """
        A successful HTTP status with an empty/non-JSON body must not be
        treated as a successful Razorpay payment response.
        """
        result = self._get(_resp(200, bad_json=True))

        self.assertEqual(result["code"], "PROVIDER_INVALID_RESPONSE")
        self.assertEqual(result["http_status"], 200)

    def test_large_refund_response_is_limited_to_ten_records(self):
        """
        Even if the provider response contains more items than requested,
        Luintix must return no more than the configured refund page size.
        """
        large_refunds = [
            dict(
                REFUND,
                id=f"rfnd_{index:014d}",
            )
            for index in range(15)
        ]

        self.fake.queue = [
            _resp(
                200,
                {
                    "entity": "collection",
                    "count": 15,
                    "items": large_refunds,
                },
            )
        ]

        result = self.rzp.get_payment_refunds(
            PAY_ID,
            limit=10,
        )

        self.assertEqual(result["payment_id"], PAY_ID)
        self.assertEqual(result["count"], 10)
        self.assertEqual(len(result["refunds"]), 10)
        self.assertTrue(result["more_records"])

    def test_09_missing_and_odd_fields(self):
        payment = self._get(_resp(200, {"id": PAY_ID, "entity": "payment", "amount": "500", "created_at": "yesterday",
                                        "captured": "yes", "email": ""}))
        expected_missing_values = {
            "amount": None,
            "amount_minor": None,
            "currency": None,
            "status": None,
            "email": None,
            "captured": None,
            "created_at": None,
        }

        self.assertEqual(
            {
                key: payment.get(key)
                for key in expected_missing_values
            },
            expected_missing_values,
        )
        self.fake.queue = [_resp(200, {"entity": "collection", "items": [None, "junk", REFUND]})]
        self.assertEqual(self.rzp.get_payment_refunds(PAY_ID)["count"], 1)

    def test_10_missing_credentials(self):
        for connector in (RazorpayConnector("", ""), RazorpayConnector(KEY_ID, ""), RazorpayConnector("", SECRET),
                          RazorpayConnector(None, None)):
            err = connector.configuration_error()
            self.assertEqual(err["code"], "PROVIDER_NOT_CONFIGURED")
            self.assertIn("credentials are not configured", err["error"])
            self.assertEqual(connector.get_payment(PAY_ID)["code"], "PROVIDER_NOT_CONFIGURED")
            self.assertEqual(connector.get_refund(REFUND_ID)["code"], "PROVIDER_NOT_CONFIGURED")
        self.assertIn("rzp_test_", RazorpayConnector("key_without_prefix", SECRET).configuration_error()["error"])
        with patch.dict(os.environ, {"RAZORPAY_KEY_SECRET": ""}):
            with self.assertRaises(TenantConfigurationError):
                gateway.get_connector("COMP-RAZORPAY")
            result = json.loads(razorpay_get_payment.invoke({"payment_id": PAY_ID, "company_id": "COMP-RAZORPAY"}))
            self.assertEqual(result["code"], "TENANT_CONFIGURATION_ERROR")
            self.assertIn("credentials are not configured", result["error"])
        self.assertEqual(self.fake.calls, [])

    def test_11_write_operations_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
            self.assertEqual(self.rzp._request(method, f"payments/{PAY_ID}/refund")["code"], "NOT_SUPPORTED")
        self.assertEqual(self.fake.calls, [])
        for name in dir(RazorpayConnector):
            self.assertFalse(name.startswith(("create", "capture", "initiate", "refund", "cancel_payment", "post", "update_payment")), name)
        self.assertEqual(self.rzp.request_return("1001", 100.0, "COMP-RAZORPAY")["status"], "BLOCKED")
        self.assertEqual(self.rzp.cancel_order("1001", "COMP-RAZORPAY")["code"], "NOT_SUPPORTED")
        self.assertEqual({t.name for t in agent.razorpay_tools},
                         {"razorpay_get_payment", "razorpay_get_payment_refunds", "razorpay_get_refund"})

    def test_12_credential_and_log_redaction(self):
        buffer = io.StringIO()
        with patch("sys.__stdout__", buffer), redirect_stdout(buffer), \
                self.assertLogs("uvicorn.error", level="WARNING") as logs:
            failed = self._get(RuntimeError(f"boom {SECRET} {BASIC}"))
            ok = self.rzp.get_payment(PAY_ID)
            refunds = self.rzp.get_payment_refunds(PAY_ID)
            denied = self._get(_resp(401, {"error": {"description": f"bad key {SECRET}"}}))
            tool = razorpay_get_refund.invoke({"refund_id": REFUND_ID, "company_id": "COMP-RAZORPAY"})
        blob = buffer.getvalue() + json.dumps([failed, ok, refunds, denied]) + tool + "".join(logs.output)
        for secret in (SECRET, BASIC, "Authorization", "Basic "):
            self.assertNotIn(secret, blob)
        self.assertIn("Operation: GET payments/{payment_id}", buffer.getvalue())
        self.assertIn("Operation: GET payments/{payment_id}/refunds", buffer.getvalue())
        self.assertIn("Operation: GET refunds/{refund_id}", buffer.getvalue())
        self.assertEqual(self.rzp._sanitize_error(f"x {SECRET} {BASIC}"), "x [REDACTED] [REDACTED]")


class GatewayTest(RazorpayFixture):

    def test_13_gateway_routing(self):
        self.assertIsInstance(gateway.get_connector("COMP-RAZORPAY"), RazorpayConnector)
        self.assertIsInstance(gateway.get_tenant_primary_connector("COMP-RAZORPAY"), RazorpayConnector)
        self.assertTrue(gateway.has_razorpay("COMP-RAZORPAY"))
        for tenant in ("COMP-SHOPIFY", "COMP-ZOHO", "COMP-HUBSPOT", "COMP-SALESFORCE", "COMP-ALPHA", "COMP-UNKNOWN"):
            self.assertFalse(gateway.has_razorpay(tenant))
        self.assertIsInstance(gateway.get_connector("COMP-SHOPIFY"), ShopifyConnector)
        self.assertEqual(gateway.razorpay_get_payment("COMP-RAZORPAY", PAY_ID)["status"], "captured")
        self.assertEqual(gateway.razorpay_get_payment_refunds("COMP-RAZORPAY", PAY_ID)["count"], 1)
        self.assertEqual(gateway.razorpay_get_refund("COMP-RAZORPAY", REFUND_ID)["status"], "processed")

    def test_13_alternate_env_names(self):
        alt = {"RAZORPAY_KEY_ID": "", "RAZORPAY_KEY_SECRET": "", "RAZORPAY_API_KEY": KEY_ID, "RAZORPAY_SECRET": SECRET}
        with patch.dict(os.environ, alt):
            connector = gateway.get_connector("COMP-RAZORPAY")
            self.assertEqual((connector.key_id, connector.mode), (KEY_ID, "test"))
            self.assertEqual(gateway.razorpay_get_payment("COMP-RAZORPAY", PAY_ID)["id"], PAY_ID)
        self.assertEqual(self.fake.calls[0]["auth"], f"Basic {BASIC}")

    def test_13_tenant_isolation(self):
        execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES ('COMP-RAZORPAY-2', 'x', 'STANDARD')")
        execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type) VALUES ('COMP-RAZORPAY-2', 'RAZORPAY')")
        with self.assertRaises(TenantConfigurationError):
            gateway.get_connector("COMP-RAZORPAY-2")  # env key belongs only to RAZORPAY_ENV_TENANT
        for tenant in ("COMP-SHOPIFY", "COMP-ZOHO", "COMP-RAZORPAY-2"):
            result = json.loads(razorpay_get_payment.invoke({"payment_id": PAY_ID, "company_id": tenant}))
            self.assertIn("code", result)
            self.assertNotIn("gaurav", json.dumps(result))
        self.assertEqual(self.fake.calls, [])

    def test_14_tool_delegation(self):
        self.assertEqual(json.loads(razorpay_get_payment.invoke({"payment_id": PAY_ID, "company_id": "COMP-RAZORPAY"})),
                         self.rzp.get_payment(PAY_ID))
        self.assertEqual(json.loads(razorpay_get_payment_refunds.invoke({"payment_id": PAY_ID, "company_id": "COMP-RAZORPAY"})),
                         self.rzp.get_payment_refunds(PAY_ID))
        self.assertEqual(json.loads(razorpay_get_refund.invoke({"refund_id": REFUND_ID, "company_id": "COMP-RAZORPAY"})),
                         self.rzp.get_refund(REFUND_ID))
        with patch.object(gateway, "razorpay_get_refund", return_value={"id": "stub"}) as stub:
            razorpay_get_refund.invoke({"refund_id": REFUND_ID, "company_id": "COMP-RAZORPAY"})
        stub.assert_called_once_with("COMP-RAZORPAY", REFUND_ID)
        self.assertTrue(all(c["method"] == "GET" for c in self.fake.calls))


class AgentTest(RazorpayFixture):

    def chat(self, model, text, tenant="COMP-RAZORPAY"):
        import server
        logs = io.StringIO()
        with patch.object(type(agent.llm), "invoke", model), patch("sys.__stdout__", logs), redirect_stdout(logs):
            response = TestClient(server.app).post("/api/chat", json={"user_input": text}, headers={"X-Company-ID": tenant})
        return response, logs.getvalue()

    def test_tools_bound_only_for_razorpay_tenant(self):
        names = {t["function"]["name"] for t in agent.llm_with_razorpay_tools.kwargs["tools"]}
        self.assertEqual(names, {"razorpay_get_payment", "razorpay_get_payment_refunds", "razorpay_get_refund"})
        for binding in (agent.llm_with_tools, agent.llm_with_salesforce_tools, agent.llm_with_helpdesk_shipping_tools):
            self.assertFalse(any(t["function"]["name"].startswith("razorpay") for t in binding.kwargs["tools"]))

    def test_payment_and_refunds_through_api_chat(self):
        def model(self_, messages, config=None, **kwargs):
            bound = {t["function"]["name"] for t in kwargs.get("tools", [])}
            assert bound == {"razorpay_get_payment", "razorpay_get_payment_refunds", "razorpay_get_refund"}, bound
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                return AIMessage(content="", tool_calls=[{"name": "razorpay_get_payment_refunds",
                                                          "args": {"payment_id": PAY_ID}, "id": "c1"}])
            refund = json.loads(results[0].content)["refunds"][0]
            return AIMessage(content=f"Refund {refund['id']} of INR {refund['amount']} has been processed.")
        response, logs = self.chat(model, f"Has payment {PAY_ID} been refunded?")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["agent_response"], f"Refund {REFUND_ID} of INR 200.0 has been processed.")
        self.assertIn("[PROVIDER] Provider: RAZORPAY | Operation: GET payments/{payment_id}/refunds", logs)
        self.assertNotIn(SECRET, logs)
        self.assertNotIn(BASIC, logs)

    def test_invented_payment_id_is_blocked(self):
        def model(self_, messages, config=None, **kwargs):
            if not [m for m in messages if isinstance(m, ToolMessage)]:
                return AIMessage(content="", tool_calls=[{"name": "razorpay_get_payment", "args": {"payment_id": PAY_ID}, "id": "c1"}])
            return AIMessage(content="Could you share the payment ID (it starts with pay_)?")
        response, logs = self.chat(model, "Check my last payment")
        self.assertIn("UNVERIFIED_RECORD_ID", logs)
        self.assertEqual(self.fake.calls, [])

    def test_refund_creation_claim_is_never_allowed(self):
        def model(self_, messages, config=None, **kwargs):
            if not [m for m in messages if isinstance(m, ToolMessage)]:
                return AIMessage(content="", tool_calls=[{"name": "razorpay_get_payment", "args": {"payment_id": PAY_ID}, "id": "c1"}])
            return AIMessage(content="I have initiated a full refund for your payment.")
        response, logs = self.chat(model, f"Please refund payment {PAY_ID}")
        self.assertEqual(response.json()["agent_response"], agent.CLAIM_FALLBACK)
        self.assertIn("[CLAIM GUARD]", logs)
        self.assertTrue(all(c["method"] == "GET" for c in self.fake.calls))

    def test_refund_claim_guard_rules(self):
        refunds_pending = [("razorpay_get_payment_refunds", json.dumps({"refunds": [dict(REFUND, status="pending")]}))]
        refunds_done = [("razorpay_get_refund", json.dumps({"id": REFUND_ID, "status": "processed"}))]
        payment_only = [("razorpay_get_payment", json.dumps({"id": PAY_ID, "status": "captured"}))]
        for text in ("I have initiated a refund for you.", "We'll process your refund today.", "I've refunded the payment.",
                     "I will go ahead and create a refund of INR 500.", "We have raised a refund request."):
            self.assertTrue(agent.find_refund_claims(text, refunds_done), text)  # never allowed, even with evidence
        self.assertTrue(agent.find_refund_claims("Your refund has been processed.", payment_only))
        self.assertTrue(agent.find_refund_claims("Your refund has been processed.", refunds_pending))
        self.assertTrue(agent.find_refund_claims("The payment has been fully refunded.", payment_only))
        self.assertTrue(agent.find_refund_claims("A refund was initiated.", []))
        for text, events in (("Your refund has been processed.", refunds_done),
                             ("Your refund is being processed.", refunds_pending),
                             ("A refund was created on 2 Jan.", refunds_pending),
                             ("The payment was refunded.", [("razorpay_get_payment", json.dumps({"id": PAY_ID, "status": "refunded"}))]),
                             ("I can't create or initiate refunds here; I can only check their status.", []),
                             ("Payment pay_29QQoUBi66xm2f is captured. No refund has been made yet.", payment_only),
                             ("The refund has not been processed yet.", payment_only)):
            self.assertEqual(agent.find_refund_claims(text, events), [], text)


if __name__ == "__main__":
    unittest.main()