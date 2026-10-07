"""
Order payment transactions and deterministic store -> payment-provider mapping (mocked).

Shopify GraphQL is faked at ShopifyConnector._execute_graphql and Razorpay at the httpx layer, so
no real API is called. Payment IDs below are synthetic test values, not real payments.
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
from database import init_db
from integrations.gateway import gateway
from integrations.payment_mapping import map_transaction, parse_receipt, rule_for_gateway
from integrations.providers.razorpay import BASE_URL as RZP_BASE
from integrations.providers.shopify import ShopifyConnector
from tools import get_order_payment_transactions, verify_order_payment

RZP_ENV = {"RAZORPAY_KEY_ID": "rzp_test_FakeKeyId12345", "RAZORPAY_KEY_SECRET": "fakeSecretDoNotLog0001xyz",
           "RAZORPAY_ENV_TENANT": "COMP-RAZORPAY", "RAZORPAY_STORE_TENANTS": "COMP-SHOPIFY"}
PAY_REF, PAY_REF_2 = "pay_SyntheticRef0001", "pay_SyntheticRef0002"


def tx(tid, kind="SALE", status="SUCCESS", gateway="razorpay", amount="100.0", currency="INR", receipt=None,
       payment_id="c1.1", parent=None):
    return {"id": f"gid://shopify/OrderTransaction/{tid}", "kind": kind, "status": status, "gateway": gateway,
            "formattedGateway": (gateway or "").title() or None, "paymentId": payment_id, "test": True,
            "manualPaymentGateway": gateway == "manual", "createdAt": "2026-09-28T10:00:00Z",
            "processedAt": "2026-09-28T10:00:01Z", "parentTransaction": {"id": parent} if parent else None,
            "amountSet": {"shopMoney": {"amount": amount, "currencyCode": currency}}, "receiptJson": receipt}


ORDERS = {
    "#2001": [],
    "#2002": [tx(1, receipt=json.dumps({"razorpay_payment_id": PAY_REF, "card_last4": "1111"}))],
    "#2003": [tx(2, kind="AUTHORIZATION", receipt={"razorpay_payment_id": PAY_REF_2}),
              tx(3, kind="CAPTURE", receipt={"razorpay_payment_id": PAY_REF_2}, parent="gid://shopify/OrderTransaction/2"),
              tx(4, kind="REFUND", amount="40.0", receipt={"razorpay_payment_id": PAY_REF_2})],
    "#2004": [tx(5, gateway="manual", currency="USD", amount="699.95", receipt="{}")],
    "#2005": [tx(6, gateway=None, receipt={"razorpay_payment_id": PAY_REF})],
    "#2006": [tx(7, receipt={"note": "no reference"})],
    "#2007": [tx(8, gateway="stripe", receipt={"razorpay_payment_id": PAY_REF})],
    "#2008": [tx(9, gateway="manual", payment_id=PAY_REF, receipt={})],
    "#2009": [tx(10, amount="250.0", receipt={"razorpay_payment_id": PAY_REF})],
    "#2010": [tx(11, currency="USD", receipt={"razorpay_payment_id": PAY_REF})],
    "#2011": [tx(12, status="FAILURE", receipt={"razorpay_payment_id": PAY_REF})],
}
PAYMENTS = {
    PAY_REF: {"id": PAY_REF, "entity": "payment", "amount": 10000, "currency": "INR", "status": "captured",
              "method": "wallet", "amount_refunded": 0, "refund_status": None, "email": "someone@example.com",
              "contact": "+910000000000", "created_at": 1759050000},
    PAY_REF_2: {"id": PAY_REF_2, "entity": "payment", "amount": 10000, "currency": "INR", "status": "captured",
                "method": "card", "amount_refunded": 4000, "refund_status": "partial", "created_at": 1759050000},
}


def _resp(status, body):
    r = MagicMock()
    r.status_code, r.headers = status, {}
    r.json.return_value = body
    return r


class PaymentFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        env = patch.dict(os.environ, RZP_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.queries, self.rzp_calls, self.orders = [], [], dict(ORDERS)

        def fake_graphql(connector, query, variables=None):
            self.queries.append(query)
            name = (variables or {}).get("searchQuery", "").removeprefix('name:"').removesuffix('"')
            if name in self.orders:
                return {"orders": {"nodes": [{"id": "gid://shopify/Order/1", "name": name,
                                              "transactions": self.orders[name]}]}}
            return {"orders": {"nodes": []}}

        original = httpx.Client.request

        def transport(client, method, url, *args, **kwargs):
            if "api.razorpay.com" in str(url):
                self.rzp_calls.append((method, str(url)))
                pid = str(url).rsplit("/", 1)[-1]
                if pid in PAYMENTS:
                    return _resp(200, PAYMENTS[pid])
                return _resp(400, {"error": {"code": "BAD_REQUEST_ERROR", "description": "The id provided does not exist"}})
            return original(client, method, url, *args, **kwargs)

        for target in (patch.object(ShopifyConnector, "_execute_graphql", fake_graphql),
                       patch.object(httpx.Client, "request", transport),
                       patch("integrations.providers.razorpay.time.sleep")):
            target.start()
            self.addCleanup(target.stop)
        self.shopify = ShopifyConnector()


class ShopifyTransactionTest(PaymentFixture):

    def test_01_order_without_transactions(self):
        self.assertEqual(self.shopify.get_order_transactions("#2001", "COMP-SHOPIFY"),
                         {"order_id": "#2001", "transactions": [], "count": 0, "provider": "SHOPIFY"})
        result = gateway.verify_order_payment("#2001", "COMP-SHOPIFY")
        self.assertEqual((result["status"], result["transactions_found"]), ("NO_PAYMENT_TRANSACTION", 0))
        self.assertEqual(self.rzp_calls, [])

    def test_02_order_with_one_transaction(self):
        result = self.shopify.get_order_transactions("2002", "COMP-SHOPIFY")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["transactions"][0], {
            "id": "gid://shopify/OrderTransaction/1", "kind": "SALE", "status": "SUCCESS", "gateway": "razorpay",
            "gateway_display_name": "Razorpay", "amount": 100.0, "currency": "INR", "store_payment_id": "c1.1",
            "manual_gateway": False, "test": True, "parent_transaction_id": None,
            "created_at": "2026-09-28T10:00:00Z", "processed_at": "2026-09-28T10:00:01Z",
            "payment_provider": "RAZORPAY", "payment_reference": PAY_REF,
            "payment_reference_source": "receipt.razorpay_payment_id", "mapping_status": "MAPPED", "provider": "SHOPIFY"})
        self.assertNotIn("card_last4", json.dumps(result))  # the raw receipt never leaves the connector
        query = self.queries[0]
        for field in ("transactions(first: 50)", "gateway", "formattedGateway", "paymentId", "receiptJson", "amountSet"):
            self.assertIn(field, query)
        self.assertNotIn("mutation", query.lower())

    def test_03_order_with_multiple_transactions(self):
        result = self.shopify.get_order_transactions("#2003", "COMP-SHOPIFY")
        self.assertEqual([t["kind"] for t in result["transactions"]], ["AUTHORIZATION", "CAPTURE", "REFUND"])
        self.assertEqual(result["transactions"][1]["parent_transaction_id"], "gid://shopify/OrderTransaction/2")
        verified = gateway.verify_order_payment("#2003", "COMP-SHOPIFY")
        self.assertEqual([p["kind"] for p in verified["payments"]], ["AUTHORIZATION", "CAPTURE"])  # refund excluded
        self.assertEqual(verified["status"], "VERIFIED")

    def test_order_not_found_and_malformed(self):
        self.assertEqual(self.shopify.get_order_transactions("#9999", "COMP-SHOPIFY")["code"], "ORDER_NOT_FOUND")
        self.orders["#2012"] = "not-a-list"
        self.assertEqual(self.shopify.get_order_transactions("#2012", "COMP-SHOPIFY")["code"], "PROVIDER_INVALID_RESPONSE")
        self.orders["#2013"] = [{"kind": "SALE"}, None, tx(20, amount="abc")]
        result = self.shopify.get_order_transactions("#2013", "COMP-SHOPIFY")
        self.assertEqual((result["count"], result["transactions"][0]["amount"]), (1, None))

    def test_non_store_providers_report_not_supported(self):
        self.assertEqual(gateway.get_order_transactions("ORD-5001", "COMP-ALPHA")["code"], "NOT_SUPPORTED")


class MappingTest(unittest.TestCase):

    def test_04_gateway_extraction(self):
        for name in ("razorpay", "Razorpay", " razorpay ", "razorpay_cards_upi_netbanking", "razorpay upi", "razorpay-secure"):
            self.assertEqual(rule_for_gateway(name).provider, "RAZORPAY", name)
        for name in ("manual", "stripe", "shopify_payments", "razorpayx", "notrazorpay", "", None, "cash on delivery"):
            self.assertIsNone(rule_for_gateway(name), name)

    def test_05_payment_reference_extraction(self):
        self.assertEqual(map_transaction("razorpay", json.dumps({"razorpay_payment_id": PAY_REF})),
                         {"payment_provider": "RAZORPAY", "payment_reference": PAY_REF,
                          "payment_reference_source": "receipt.razorpay_payment_id", "mapping_status": "MAPPED"})
        both = map_transaction("razorpay", {"payment_id": PAY_REF_2, "razorpay_payment_id": PAY_REF})
        self.assertEqual((both["payment_reference"], both["payment_reference_source"]), (PAY_REF, "receipt.razorpay_payment_id"))
        fallback = map_transaction("razorpay", {"payment_id": PAY_REF_2})
        self.assertEqual((fallback["payment_reference"], fallback["payment_reference_source"]), (PAY_REF_2, "receipt.payment_id"))
        self.assertEqual(parse_receipt("not json"), {})
        self.assertEqual(parse_receipt("[1, 2]"), {})

    def test_06_missing_or_invalid_payment_reference(self):
        for receipt in (None, "", "{}", "garbage", {"razorpay_payment_id": "order_ABC123456789"},
                        {"razorpay_payment_id": "pay_"}, {"razorpay_payment_id": 12345},
                        {"razorpay_payment_id": f"{PAY_REF}' OR 1=1"}, {"other": PAY_REF}):
            result = map_transaction("razorpay", receipt)
            self.assertEqual((result["mapping_status"], result["payment_reference"]), ("PAYMENT_REFERENCE_MISSING", None), receipt)
            self.assertEqual(result["payment_provider"], "RAZORPAY")

    def test_07_missing_gateway(self):
        for gw in (None, "", "   "):
            self.assertEqual(map_transaction(gw, {"razorpay_payment_id": PAY_REF}),
                             {"payment_provider": None, "payment_reference": None, "payment_reference_source": None,
                              "mapping_status": "GATEWAY_MISSING"})

    def test_08_unknown_gateway_is_never_mapped_by_id_shape(self):
        for gw in ("stripe", "manual", "paypal"):
            result = map_transaction(gw, {"razorpay_payment_id": PAY_REF, "payment_id": PAY_REF})
            self.assertEqual((result["mapping_status"], result["payment_provider"], result["payment_reference"]),
                             ("GATEWAY_NOT_SUPPORTED", None, None))


class RoutingTest(PaymentFixture):

    def test_09_deterministic_razorpay_routing(self):
        result = gateway.verify_order_payment("#2002", "COMP-SHOPIFY")
        self.assertEqual(self.rzp_calls, [("GET", f"{RZP_BASE}/payments/{PAY_REF}")])  # the reference from the receipt
        self.assertEqual(result["status"], "VERIFIED")
        payment = result["payments"][0]
        self.assertEqual((payment["verification_status"], payment["payment_provider"], payment["payment_reference"]),
                         ("VERIFIED", "RAZORPAY", PAY_REF))
        self.assertEqual(payment["checks"], {"payment_found": True, "status_matches": True, "currency_matches": True,
                                             "amount_matches": True})
        self.assertEqual(payment["provider_payment"]["status"], "captured")
        self.assertNotIn("someone@example.com", json.dumps(result))  # payer contact details are not passed on

    def test_09_mismatches_are_reported_not_hidden(self):
        amount = gateway.verify_order_payment("#2009", "COMP-SHOPIFY")
        self.assertEqual((amount["status"], amount["payments"][0]["checks"]["amount_matches"]), ("MISMATCH", False))
        currency = gateway.verify_order_payment("#2010", "COMP-SHOPIFY")["payments"][0]
        self.assertEqual((currency["verification_status"], currency["checks"]["currency_matches"],
                          currency["checks"]["amount_matches"]), ("MISMATCH", False, None))

    def test_09_unmapped_transactions_never_call_the_provider(self):
        expected = {"#2004": "GATEWAY_NOT_SUPPORTED", "#2005": "GATEWAY_MISSING", "#2006": "PAYMENT_REFERENCE_MISSING",
                    "#2007": "GATEWAY_NOT_SUPPORTED", "#2008": "GATEWAY_NOT_SUPPORTED"}
        for order, mapping in expected.items():
            result = gateway.verify_order_payment(order, "COMP-SHOPIFY")
            self.assertEqual((result["status"], result["payments"][0]["mapping_status"],
                              result["payments"][0]["payment_reference"]), ("MAPPING_UNAVAILABLE", mapping, None), order)
        self.assertEqual(gateway.verify_order_payment("#2011", "COMP-SHOPIFY")["status"], "NO_PAYMENT_TRANSACTION")
        self.assertEqual(self.rzp_calls, [])

    def test_09_provider_link_is_explicit(self):
        with patch.dict(os.environ, {"RAZORPAY_STORE_TENANTS": ""}):
            result = gateway.verify_order_payment("#2002", "COMP-SHOPIFY")
            self.assertEqual((result["status"], result["payments"][0]["verification_status"]),
                             ("NOT_VERIFIED", "PAYMENT_PROVIDER_NOT_CONNECTED"))
        with patch.dict(os.environ, {"RAZORPAY_KEY_SECRET": "", "RAZORPAY_SECRET": ""}):
            self.assertEqual(gateway.verify_order_payment("#2002", "COMP-SHOPIFY")["payments"][0]["verification_status"],
                             "PAYMENT_PROVIDER_NOT_CONFIGURED")
        self.assertEqual(self.rzp_calls, [])
        self.assertEqual(gateway.payment_provider_connector("COMP-SHOPIFY", "STRIPE"), (None, "PAYMENT_PROVIDER_NOT_SUPPORTED"))

    def test_09_provider_errors_are_surfaced(self):
        self.orders["#2014"] = [tx(30, receipt={"razorpay_payment_id": "pay_DoesNotExist0001"})]
        payment = gateway.verify_order_payment("#2014", "COMP-SHOPIFY")["payments"][0]
        self.assertEqual((payment["verification_status"], payment["provider_error"]["code"]), ("PROVIDER_ERROR", "NOT_FOUND"))

    def test_read_only(self):
        gateway.verify_order_payment("#2003", "COMP-SHOPIFY")
        self.assertTrue(self.rzp_calls and all(method == "GET" for method, _ in self.rzp_calls))
        self.assertTrue(all("mutation" not in q.lower() for q in self.queries))


class AgentTest(PaymentFixture):

    def test_10_llm_cannot_supply_or_invent_a_payment_mapping(self):
        for t in (get_order_payment_transactions, verify_order_payment):
            self.assertEqual(set(t.args), {"order_id", "company_id"})  # no provider / payment-ID argument
        store_bindings = (agent.llm_with_tools, agent.llm_with_helpdesk_tools, agent.llm_with_shipping_tools,
                          agent.llm_with_helpdesk_shipping_tools)
        for binding in store_bindings:
            names = {t["function"]["name"] for t in binding.kwargs["tools"]}
            self.assertIn("verify_order_payment", names)
            self.assertFalse(any(n.startswith("razorpay_") for n in names))
        self.assertFalse(gateway.has_razorpay("COMP-SHOPIFY"))

    def test_10_invented_payment_lookup_through_api_chat_reaches_nothing(self):
        import server

        def model(self_, messages, config=None, **kwargs):
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                return AIMessage(content="", tool_calls=[{"name": "verify_order_payment", "args": {"order_id": "#2004"}, "id": "c1"}])
            if len(results) == 1:  # try to "help" by guessing a Razorpay payment for the order
                return AIMessage(content="", tool_calls=[{"name": "razorpay_get_payment", "args": {"payment_id": PAY_REF}, "id": "c2"}])
            return AIMessage(content="The payment-provider mapping is unavailable for order #2004.")
        logs = io.StringIO()
        with patch.object(type(agent.llm), "invoke", model), patch("sys.__stdout__", logs), redirect_stdout(logs):
            response = TestClient(server.app).post("/api/chat", json={"user_input": "Verify the payment for order #2004"},
                                                   headers={"X-Company-ID": "COMP-SHOPIFY"})
        self.assertEqual(response.status_code, 200)
        self.assertIn('"status": "MAPPING_UNAVAILABLE"', logs.getvalue())
        self.assertIn("Tool razorpay_get_payment not found", logs.getvalue())
        self.assertEqual(self.rzp_calls, [])

    def test_tool_delegation(self):
        self.assertEqual(json.loads(get_order_payment_transactions.invoke({"order_id": "#2002", "company_id": "COMP-SHOPIFY"})),
                         gateway.get_order_transactions("#2002", "COMP-SHOPIFY"))
        self.assertEqual(json.loads(verify_order_payment.invoke({"order_id": "#2002", "company_id": "COMP-SHOPIFY"}))["status"],
                         "VERIFIED")


if __name__ == "__main__":
    unittest.main()
