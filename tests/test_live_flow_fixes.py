import json
import os
import unittest
from unittest.mock import MagicMock, patch

from database import init_db
from integrations.errors import TenantConfigurationError
from integrations.gateway import gateway
from integrations.providers.local_db import LocalDBConnector
from integrations.providers.shopify import ShopifyConnector
from integrations.providers.woocommerce import WooCommerceConnector


def _order_node(name, gid="gid://shopify/Order/7664726016227"):
    return {
        "id": gid,
        "name": name,
        "createdAt": "2026-09-25T10:00:00Z",
        "cancelledAt": None,
        "displayFinancialStatus": "PENDING",
        "displayFulfillmentStatus": "UNFULFILLED",
        "fullyPaid": False,
        "totalPriceSet": {"shopMoney": {"amount": "49.95", "currencyCode": "USD"}},
        "lineItems": {"nodes": []},
        "fulfillments": [],
    }


def _http_response(status, body):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = body
    return response


class TestShopifyOrderLookup(unittest.TestCase):

    def setUp(self):
        ShopifyConnector._token_cache.clear()
        self.connector = ShopifyConnector(
            shop_domain="test-store.myshopify.com", access_token="shpat_test"
        )

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_hash_name_lookup_searches_by_name(self, mock_graphql):
        mock_graphql.return_value = {"orders": {"nodes": [_order_node("#1003")]}}

        order = self.connector.get_order_details("#1003", "COMP-SHOPIFY")

        self.assertEqual(order["id"], "#1003")
        self.assertEqual(mock_graphql.call_count, 1)
        self.assertEqual(mock_graphql.call_args[0][1], {"searchQuery": 'name:"#1003"'})

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_numeric_lookup_is_order_number_not_gid(self, mock_graphql):
        mock_graphql.return_value = {"orders": {"nodes": [_order_node("#1003")]}}

        order = self.connector.get_order_details("1003", "COMP-SHOPIFY")

        self.assertEqual(order["id"], "#1003")
        for call in mock_graphql.call_args_list:
            self.assertNotIn("gid://shopify/Order/1003", json.dumps(call[0][1]))
        self.assertEqual(mock_graphql.call_args_list[0][0][1], {"searchQuery": 'name:"#1003"'})

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_name_search_requires_exact_match(self, mock_graphql):
        mock_graphql.return_value = {"orders": {"nodes": [_order_node("#10030")]}}

        self.assertIsNone(self.connector.get_order_details("#1003", "COMP-SHOPIFY"))

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_gid_lookup_uses_order_by_id(self, mock_graphql):
        gid = "gid://shopify/Order/7664726016227"
        mock_graphql.return_value = {"order": _order_node("#1003", gid)}

        order = self.connector.get_order_details(gid, "COMP-SHOPIFY")

        self.assertEqual(order["id"], "#1003")
        self.assertEqual(mock_graphql.call_count, 1)
        self.assertIn("order(id: $id)", mock_graphql.call_args[0][0])
        self.assertEqual(mock_graphql.call_args[0][1], {"id": gid})

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_ord_prefix_is_not_stripped(self, mock_graphql):
        mock_graphql.return_value = {"orders": {"nodes": []}}

        self.connector.get_order_details("ORD-1003", "COMP-SHOPIFY")

        self.assertEqual(mock_graphql.call_args[0][1], {"searchQuery": 'name:"ORD-1003"'})

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_provider_error_is_returned_not_swallowed(self, mock_graphql):
        mock_graphql.return_value = {"error": "Shopify authentication failed (Invalid Access Token)."}

        result = self.connector.get_order_details("#1003", "COMP-SHOPIFY")

        self.assertIn("authentication failed", result["error"])

    @patch("httpx.Client.post")
    def test_expired_token_is_refreshed_with_client_credentials(self, mock_post):
        connector = ShopifyConnector(
            shop_domain="test-store.myshopify.com",
            access_token="shpca_expired",
            client_id="cid",
            client_secret="csecret",
        )
        mock_post.side_effect = [
            _http_response(401, {"errors": "Invalid API key or access token"}),
            _http_response(200, {"access_token": "shpca_fresh", "expires_in": 86399}),
            _http_response(200, {"data": {"orders": {"nodes": [_order_node("#1003")]}}}),
        ]

        order = connector.get_order_details("#1003", "COMP-SHOPIFY")

        self.assertEqual(order["id"], "#1003")
        retry_headers = mock_post.call_args_list[2][1]["headers"]
        self.assertEqual(retry_headers["X-Shopify-Access-Token"], "shpca_fresh")


class TestCredentialErrors(unittest.TestCase):

    def test_shopify_missing_credentials_error(self):
        ShopifyConnector._token_cache.clear()
        keys = ["SHOPIFY_ACCESS_TOKEN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET"]
        with patch.dict(os.environ, {k: "" for k in keys}):
            connector = ShopifyConnector(shop_domain="test-store.myshopify.com")
            self.assertFalse(connector.credentials_configured())
            result = connector.get_order_details("#1003", "COMP-SHOPIFY")
        self.assertIn("credentials are missing", result["error"])

    def test_woocommerce_missing_credentials_error(self):
        keys = ["WOOCOMMERCE_CONSUMER_KEY", "WOOCOMMERCE_CONSUMER_SECRET"]
        with patch.dict(os.environ, {k: "" for k in keys}):
            connector = WooCommerceConnector(site_url="https://shop.example.com")
            self.assertFalse(connector.credentials_configured())
            result = connector.get_order_details("123", "COMP-WOOCOMMERCE")
        self.assertIn("credentials missing", result["error"])


class TestWooCommerceUrlConfig(unittest.TestCase):

    def test_base_url_preferred_over_site_url(self):
        env = {"WOOCOMMERCE_BASE_URL": "https://base.example.com/", "WOOCOMMERCE_SITE_URL": "https://site.example.com"}
        with patch.dict(os.environ, env):
            connector = WooCommerceConnector()
        self.assertEqual(connector.base_api_url, "https://base.example.com/wp-json/wc/v3")

    def test_site_url_still_supported(self):
        with patch.dict(os.environ, {"WOOCOMMERCE_BASE_URL": "", "WOOCOMMERCE_SITE_URL": "https://site.example.com"}):
            connector = WooCommerceConnector()
        self.assertEqual(connector.base_api_url, "https://site.example.com/wp-json/wc/v3")

    @patch("httpx.Client.request")
    def test_missing_url_never_calls_a_default_store(self, mock_request):
        with patch.dict(os.environ, {"WOOCOMMERCE_BASE_URL": "", "WOOCOMMERCE_SITE_URL": ""}):
            connector = WooCommerceConnector(consumer_key="ck_x", consumer_secret="cs_x")
            result = connector.get_orders("COMP-WOOCOMMERCE")
        self.assertIn("WOOCOMMERCE_BASE_URL", result[0]["error"])
        self.assertFalse(connector.domain_configured())
        mock_request.assert_not_called()


class TestTenantRouting(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def test_comp_shopify_routes_to_shopify(self):
        self.assertIsInstance(gateway.get_connector("COMP-SHOPIFY"), ShopifyConnector)

    def test_comp_woocommerce_routes_to_woocommerce(self):
        self.assertIsInstance(gateway.get_connector("COMP-WOOCOMMERCE"), WooCommerceConnector)

    def test_comp_alpha_routes_to_local_db(self):
        self.assertIsInstance(gateway.get_connector("COMP-ALPHA"), LocalDBConnector)

    def test_unknown_tenant_raises_instead_of_local_db(self):
        with self.assertRaises(TenantConfigurationError):
            gateway.get_connector("COMP-DOES-NOT-EXIST")

    def test_local_db_ord_5001_lookup(self):
        order = gateway.get_order_details("ORD-5001", "COMP-ALPHA")
        self.assertEqual(order["id"], "ORD-5001")

    @patch.object(LocalDBConnector, "get_order_details")
    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_no_silent_local_db_fallback_on_shopify_error(self, mock_graphql, mock_local):
        mock_graphql.return_value = {"error": "Shopify API HTTP Error 503."}

        result = gateway.get_order_details("#1003", "COMP-SHOPIFY")

        self.assertEqual(result, {"error": "Shopify API HTTP Error 503."})
        mock_local.assert_not_called()

    def test_unknown_tenant_tool_returns_error(self):
        from tools import get_order_details

        result = json.loads(get_order_details.invoke({"order_id": "#1003", "company_id": "COMP-NOPE"}))
        self.assertIn("company_integrations", result["error"])


class TestOrderIdPreservation(unittest.TestCase):

    def test_rewritten_ids_are_restored(self):
        from agent import _extract_order_ids, preserve_order_id

        user_ids = _extract_order_ids(["Show me details of order #1003"])
        self.assertEqual(preserve_order_id("ORD-1003", user_ids), "#1003")
        self.assertEqual(preserve_order_id("1003", user_ids), "#1003")
        self.assertEqual(preserve_order_id("#1003", user_ids), "#1003")

    def test_exact_ids_pass_through(self):
        from agent import _extract_order_ids, preserve_order_id

        gid = "gid://shopify/Order/7664726016227"
        user_ids = _extract_order_ids([f"check 1003, ORD-5001 and {gid}"])
        self.assertEqual(preserve_order_id("1003", user_ids), "1003")
        self.assertEqual(preserve_order_id("ORD-5001", user_ids), "ORD-5001")
        self.assertEqual(preserve_order_id(gid, user_ids), gid)


if __name__ == "__main__":
    unittest.main()
