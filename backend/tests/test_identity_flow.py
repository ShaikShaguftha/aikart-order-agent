import json
import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

import agent
from backend.database import init_db
from backend.integrations.gateway import gateway
from backend.integrations.models import CustomerIdentity
from backend.integrations.providers.local_db import LocalDBConnector
from backend.integrations.providers.shopify import ShopifyConnector
from backend.integrations.providers.woocommerce import WooCommerceConnector
from backend.tools import get_my_latest_order, get_my_orders, get_order_details, tools

CUSTOMER_GID = "gid://shopify/Customer/555"
EMAIL = "alice@example.com"


def _customer_node(email=EMAIL):
    return {
        "id": CUSTOMER_GID,
        "displayName": "Alice Wonder",
        "firstName": "Alice",
        "lastName": "Wonder",
        "defaultEmailAddress": {"emailAddress": email},
        "defaultPhoneNumber": {"phoneNumber": "+15550001234"},
    }


def _order_node(name, created_at, fulfillment="UNFULFILLED", gid_suffix="1"):
    return {
        "id": f"gid://shopify/Order/{gid_suffix}",
        "name": name,
        "createdAt": created_at,
        "cancelledAt": None,
        "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": fulfillment,
        "fullyPaid": True,
        "totalPriceSet": {"shopMoney": {"amount": "49.95", "currencyCode": "USD"}},
        "lineItems": {"nodes": [{"title": "Ski Wax", "quantity": 1,
                                 "originalUnitPriceSet": {"shopMoney": {"amount": "49.95"}}}]},
        "fulfillments": [],
    }


CUSTOMER_SEARCH = {"customers": {"nodes": [_customer_node()]}}
CUSTOMER_ORDERS = {
    "customer": {
        "orders": {
            "nodes": [
                _order_node("#1003", "2026-09-24T18:56:11Z", gid_suffix="3"),
                _order_node("#1001", "2026-09-24T07:28:00Z", "FULFILLED", gid_suffix="1"),
            ]
        }
    }
}


class ShopifyIdentityFlowTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_show_recent_orders(self, mock_graphql):
        mock_graphql.side_effect = [CUSTOMER_SEARCH, CUSTOMER_ORDERS]

        result = json.loads(get_my_orders.invoke({"email": EMAIL, "company_id": "COMP-SHOPIFY"}))

        self.assertEqual([o["id"] for o in result["orders"]], ["#1003", "#1001"])
        self.assertEqual(result["identified_by"], "email")
        self.assertNotIn("ORD-", json.dumps(result))

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_customer_to_orders_flow(self, mock_graphql):
        mock_graphql.side_effect = [CUSTOMER_SEARCH, CUSTOMER_ORDERS]

        gateway.get_customer_orders(CustomerIdentity(email=EMAIL), "COMP-SHOPIFY")

        (search_query, search_vars), (orders_query, orders_vars) = [c[0] for c in mock_graphql.call_args_list]
        self.assertIn("customers(first: 5, query: $searchQuery)", search_query)
        self.assertEqual(search_vars, {"searchQuery": f'email:"{EMAIL}"'})
        self.assertIn("orders(first: $first, sortKey: CREATED_AT, reverse: true)", orders_query)
        self.assertEqual(orders_vars["id"], CUSTOMER_GID)

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_fuzzy_customer_match_is_rejected(self, mock_graphql):
        mock_graphql.return_value = {"customers": {"nodes": [_customer_node("alice.other@example.com")]}}

        result = gateway.get_customer_orders(CustomerIdentity(email=EMAIL), "COMP-SHOPIFY")

        self.assertEqual(result["code"], "CUSTOMER_NOT_FOUND")
        self.assertEqual(mock_graphql.call_count, 1)

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_name_mismatch_is_rejected(self, mock_graphql):
        mock_graphql.return_value = CUSTOMER_SEARCH

        result = gateway.get_customer_orders(CustomerIdentity(email=EMAIL, name="Bob Smith"), "COMP-SHOPIFY")

        self.assertEqual(result["code"], "IDENTITY_MISMATCH")

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_latest_order_is_re_read_fresh_from_provider(self, mock_graphql):
        fresh = {"orders": {"nodes": [_order_node("#1003", "2026-09-24T18:56:11Z", "FULFILLED", gid_suffix="3")]}}
        mock_graphql.side_effect = [CUSTOMER_SEARCH, CUSTOMER_ORDERS, fresh]

        result = json.loads(get_my_latest_order.invoke({"email": EMAIL, "company_id": "COMP-SHOPIFY"}))

        self.assertEqual(result["order"]["id"], "#1003")
        # The list said UNFULFILLED; the answer must reflect the fresh provider read.
        self.assertEqual(result["order"]["fulfillment_status"], "FULFILLED")
        self.assertTrue(result["fresh_provider_lookup"])
        self.assertEqual(mock_graphql.call_args_list[2][0][1], {"searchQuery": 'name:"#1003"'})

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_explicit_order_number_still_works(self, mock_graphql):
        mock_graphql.return_value = {"orders": {"nodes": [_order_node("#1003", "2026-09-24T18:56:11Z")]}}

        result = json.loads(get_order_details.invoke({"order_id": "#1003", "company_id": "COMP-SHOPIFY"}))

        self.assertEqual(result["id"], "#1003")

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_name_alone_or_no_identity_requires_identity(self, mock_graphql):
        for args in ({"name": "Alice Wonder"}, {}):
            result = json.loads(get_my_orders.invoke({**args, "company_id": "COMP-SHOPIFY"}))
            self.assertEqual(result["code"], "IDENTITY_REQUIRED")
        mock_graphql.assert_not_called()

    @patch.object(LocalDBConnector, "get_customer_orders")
    @patch.object(LocalDBConnector, "find_customer")
    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_access_denied_has_no_local_db_fallback(self, mock_graphql, mock_find, mock_orders):
        mock_graphql.return_value = {"error": "Shopify access denied for this data", "code": "PROVIDER_ACCESS_DENIED"}

        result = json.loads(get_my_orders.invoke({"email": EMAIL, "company_id": "COMP-SHOPIFY"}))

        self.assertEqual(result["code"], "PROVIDER_ACCESS_DENIED")
        mock_find.assert_not_called()
        mock_orders.assert_not_called()

    def test_unverified_customer_id_is_not_trusted(self):
        result = gateway.get_customer_orders(CustomerIdentity(customer_id="555"), "COMP-SHOPIFY")
        self.assertEqual(result["code"], "IDENTITY_REQUIRED")


class AgentIdentityEnforcementTest(unittest.TestCase):

    def test_llm_cannot_claim_authentication(self):
        args = {"customer_id": "gid://shopify/Customer/999", "authenticated": True}
        source = agent.apply_identity(args, None, {})
        self.assertEqual((args["customer_id"], args["authenticated"], source), ("", False, "NONE"))

    def test_authenticated_session_identity_wins(self):
        args = {"email": "someone@else.com"}
        source = agent.apply_identity(args, "gid://shopify/Customer/555", {})
        self.assertEqual((args["customer_id"], args["authenticated"], source),
                         ("gid://shopify/Customer/555", True, "AUTHENTICATED_SESSION"))

    def test_session_identity_is_remembered_only_after_success(self):
        session = {}
        agent.remember_identity({"email": EMAIL}, json.dumps({"code": "CUSTOMER_NOT_FOUND", "error": "x"}), session)
        self.assertEqual(session, {})
        agent.remember_identity({"email": EMAIL}, json.dumps({"customer": {"customer_id": "1"}}), session)
        self.assertEqual(session, {"email": EMAIL})

        args = {}
        self.assertEqual(agent.apply_identity(args, None, session), "SESSION_REMEMBERED")
        self.assertEqual(args["email"], EMAIL)

    def test_identity_tools_never_require_order_id(self):
        exposed = {t.name: t for t in tools}
        self.assertNotIn("get_customer_details", exposed)
        for name in ("get_my_orders", "get_my_latest_order"):
            schema = exposed[name].args_schema.model_json_schema()
            self.assertNotIn("order_id", schema["properties"])
            self.assertFalse(schema.get("required"))

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_status_of_latest_order_uses_session_identity_and_fresh_call(self, mock_graphql):
        fresh = {"orders": {"nodes": [_order_node("#1003", "2026-09-24T18:56:11Z", gid_suffix="3")]}}
        mock_graphql.side_effect = [CUSTOMER_SEARCH, CUSTOMER_ORDERS, fresh]
        scripted_llm = MagicMock()
        scripted_llm.invoke.side_effect = [
            AIMessage(content="", tool_calls=[{"name": "get_my_latest_order", "args": {}, "id": "call_1"}]),
            AIMessage(content="Your latest order #1003 is unfulfilled."),
        ]

        with patch.object(agent, "llm_with_tools", scripted_llm):
            answer = agent.process_query(
                "What is the status of my latest order?",
                company_id="COMP-SHOPIFY",
                session_identity={"email": EMAIL},
            )

        self.assertIn("#1003", answer)
        self.assertEqual(mock_graphql.call_count, 3)
        tool_message = scripted_llm.invoke.call_args_list[1][0][0][-1]
        self.assertTrue(json.loads(tool_message.content)["fresh_provider_lookup"])


def _http(status, body):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = body
    return response


def _woo_order(number, email, phone, customer_id=0):
    return {
        "id": number, "number": str(number), "status": "processing", "customer_id": customer_id,
        "total": "20.00", "date_created_gmt": "2026-09-20T10:00:00",
        "billing": {"first_name": "Alice", "last_name": "Wonder", "email": email, "phone": phone},
        "line_items": [], "meta_data": [],
    }


class WooCommerceIdentityFlowTest(unittest.TestCase):

    def setUp(self):
        self.connector = WooCommerceConnector(
            site_url="https://shop.example.com", consumer_key="ck_test", consumer_secret="cs_test"
        )

    @patch("httpx.Client.request")
    def test_registered_customer_by_email(self, mock_request):
        mock_request.side_effect = [
            _http(200, [{"id": 45, "email": EMAIL, "first_name": "Alice", "last_name": "Wonder", "billing": {}}]),
            _http(200, [_woo_order(88, EMAIL, "", customer_id=45)]),
        ]

        found = self.connector.find_customer(CustomerIdentity(email=EMAIL), "COMP-WOOCOMMERCE")
        orders = self.connector.get_customer_orders(found["customer"], "COMP-WOOCOMMERCE")

        self.assertEqual(found["customer"]["customer_id"], "45")
        self.assertEqual(orders[0]["id"], "88")
        params = mock_request.call_args_list[1][1]["params"]
        self.assertEqual((params["customer"], params["orderby"], params["order"]), ("45", "date", "desc"))

    @patch("httpx.Client.request")
    def test_guest_customer_by_phone_matches_billing_exactly(self, mock_request):
        mock_request.side_effect = [
            _http(200, [_woo_order(90, "x@y.com", "+1 555 999 0000"), _woo_order(91, EMAIL, "+1 (555) 000-1234")]),
            _http(200, [_woo_order(90, "x@y.com", "+1 555 999 0000"), _woo_order(91, EMAIL, "+1 (555) 000-1234")]),
        ]

        found = self.connector.find_customer(CustomerIdentity(phone="15550001234"), "COMP-WOOCOMMERCE")
        orders = self.connector.get_customer_orders(found["customer"], "COMP-WOOCOMMERCE")

        self.assertTrue(found["customer"]["guest"])
        self.assertEqual([o["id"] for o in orders], ["91"])


class LocalDBIdentityFlowTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def test_authenticated_customer_id_lists_orders(self):
        identity = CustomerIdentity(customer_id="CUST-101", authenticated=True)
        result = gateway.get_customer_orders(identity, "COMP-ALPHA")
        self.assertEqual([o["id"] for o in result["orders"]], ["ORD-5001"])

    def test_email_is_not_supported_by_local_demo_schema(self):
        result = gateway.get_customer_orders(CustomerIdentity(email=EMAIL), "COMP-ALPHA")
        self.assertIn("authenticated customer ID", result["error"])


if __name__ == "__main__":
    unittest.main()
