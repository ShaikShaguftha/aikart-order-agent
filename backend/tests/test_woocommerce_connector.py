import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import httpx

from backend.integrations.gateway import gateway
from backend.integrations.providers.woocommerce import WooCommerceConnector


TEST_SITE_URL = "https://test-woo-store.com"
TEST_KEY = "ck_test_1234567890abcdef"
TEST_SECRET = "cs_test_1234567890abcdef"


def response(status_code=200, body=None, headers=None, bad_json=False):
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.headers = headers or {}

    if bad_json:
        mock_response.json.side_effect = ValueError("Invalid or empty JSON response")
    else:
        mock_response.json.return_value = body

    return mock_response


def raw_order(
    order_id=1001,
    number="1001",
    status="processing",
    total="99.50",
    description=None,
):
    return {
        "id": order_id,
        "number": number,
        "status": status,
        "date_created_gmt": "2026-09-25T10:00:00",
        "total": total,
        "billing": {
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@example.com",
            "phone": "+15550199",
        },
        "line_items": [
            {
                "id": 1,
                "name": "Wireless Mouse",
                "quantity": 1,
                "price": total,
            }
        ],
        "meta_data": [],
        "customer_note": description or "",
    }


class WooCommerceFixture(unittest.TestCase):

    def setUp(self):
        # Prevent Windows/Jupyter sys.__stdout__ logging failures.
        stdout_patch = patch.object(sys, "__stdout__", sys.stdout)
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)

        self.connector = WooCommerceConnector(
            site_url=TEST_SITE_URL,
            consumer_key=TEST_KEY,
            consumer_secret=TEST_SECRET,
            timeout=2.0,
            retry_backoff=0.001,
        )


class TestWooCommerceConnectorUnit(WooCommerceFixture):

    # --------------------------------------------------------------
    # HTTP LIFECYCLE / AUTH
    # --------------------------------------------------------------

    def test_http_client_reused_and_closed(self):
        first_client = self.connector._get_client()
        second_client = self.connector._get_client()

        self.assertIs(first_client, second_client)
        self.assertFalse(first_client.is_closed)

        self.connector.close()

        self.assertIsNone(self.connector._client)
        self.assertTrue(first_client.is_closed)

    @patch("httpx.Client.request")
    def test_https_uses_basic_auth(self, mock_request):
        mock_request.return_value = response(
            200,
            {
                "environment": {
                    "site_title": "WooCommerce Test Store",
                },
                "settings": {
                    "currency": "USD",
                },
            },
        )

        self.connector.get_shop("COMP-WOOCOMMERCE")

        request_kwargs = mock_request.call_args.kwargs

        self.assertEqual(
            request_kwargs["auth"],
            (TEST_KEY, TEST_SECRET),
        )

        self.assertEqual(
            request_kwargs["method"],
            "GET",
        )

    # --------------------------------------------------------------
    # SUCCESS CASES
    # --------------------------------------------------------------

    @patch("httpx.Client.request")
    def test_get_shop_success(self, mock_request):
        mock_request.return_value = response(
            200,
            {
                "environment": {
                    "site_title": "WooCommerce Test Store",
                    "site_url": TEST_SITE_URL,
                    "admin_email": "admin@test-woo-store.com",
                },
                "settings": {
                    "currency": "USD",
                },
            },
        )

        shop_info = self.connector.get_shop("COMP-WOOCOMMERCE")

        self.assertEqual(shop_info["name"], "WooCommerce Test Store")
        self.assertEqual(shop_info["provider"], "WOOCOMMERCE")
        self.assertEqual(shop_info["currency"], "USD")

    @patch("httpx.Client.request")
    def test_get_orders_success(self, mock_request):
        mock_request.return_value = response(
            200,
            [
                raw_order(),
            ],
        )

        orders = self.connector.get_orders(
            "COMP-WOOCOMMERCE",
            limit=5,
        )

        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["id"], "1001")
        self.assertEqual(orders[0]["status"], "PROCESSING")
        self.assertEqual(orders[0]["financial_status"], "PAID")
        self.assertEqual(orders[0]["total"], 99.50)
        self.assertEqual(
            orders[0]["items"][0]["product_name"],
            "Wireless Mouse",
        )

        self.assertEqual(
            mock_request.call_args.kwargs["params"],
            {"per_page": 5},
        )

    @patch("httpx.Client.request")
    def test_get_order_details_success(self, mock_request):
        order_response = raw_order(
            order_id=1002,
            number="1002",
            status="completed",
            total="150.00",
        )

        order_response["meta_data"] = [
            {
                "key": "_tracking_number",
                "value": "TRK-WOO-999",
            },
            {
                "key": "_tracking_provider",
                "value": "FedEx",
            },
        ]

        mock_request.return_value = response(
            200,
            order_response,
        )

        order = self.connector.get_order_details(
            "1002",
            "COMP-WOOCOMMERCE",
        )

        self.assertIsNotNone(order)
        self.assertEqual(order["id"], "1002")
        self.assertEqual(order["status"], "COMPLETED")
        self.assertEqual(order["fulfillment_status"], "FULFILLED")
        self.assertEqual(order["total"], 150.00)
        self.assertEqual(
            order["shipment"]["tracking_number"],
            "TRK-WOO-999",
        )

    @patch("httpx.Client.request")
    def test_get_product_success(self, mock_request):
        mock_request.return_value = response(
            200,
            {
                "id": 88,
                "name": "Gaming Desk Pad",
                "slug": "gaming-desk-pad",
                "type": "simple",
                "status": "publish",
                "price": "29.99",
                "regular_price": "35.00",
                "sale_price": "29.99",
                "sku": "PAD-88",
                "stock_status": "instock",
                "stock_quantity": 50,
                "short_description": "Waterproof RGB pad",
            },
        )

        product = self.connector.get_product(
            "88",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(product["product_id"], "88")
        self.assertEqual(product["name"], "Gaming Desk Pad")
        self.assertEqual(product["price"], 29.99)
        self.assertEqual(product["stock_status"], "instock")

    # 4XX ERRORS

    @patch("httpx.Client.request")
    def test_400_returns_validation_error(self, mock_request):
        mock_request.return_value = response(
            400,
            {
                "code": "woocommerce_rest_invalid_order",
                "message": "Invalid order ID",
            },
        )

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_VALIDATION_FAILED",
        )

        self.assertEqual(
            result["http_status"],
            400,
        )

    @patch("httpx.Client.request")
    def test_401_returns_auth_error(self, mock_request):
        mock_request.return_value = response(
            401,
            {
                "code": "woocommerce_rest_authentication_error",
            },
        )

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_AUTH_FAILED",
        )

        self.assertEqual(
            result["http_status"],
            401,
        )

    @patch("httpx.Client.request")
    def test_403_returns_forbidden_error(self, mock_request):
        mock_request.return_value = response(
            403,
            {
                "code": "woocommerce_rest_cannot_view",
            },
        )

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_FORBIDDEN",
        )

        self.assertEqual(
            result["http_status"],
            403,
        )

    @patch("httpx.Client.request")
    def test_404_record_not_found_and_route_not_found(self, mock_request):
        mock_request.return_value = response(
            404,
            {
                "code": "woocommerce_rest_shop_order_invalid_id",
            },
        )

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(
            result["code"],
            "NOT_FOUND",
        )

        mock_request.return_value = response(
            404,
            {
                "code": "rest_no_route",
            },
        )

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_API_UNAVAILABLE",
        )

    # 429 / RETRIES

    @patch("integrations.providers.woocommerce.time.sleep")
    @patch("httpx.Client.request")
    def test_429_retries_then_succeeds(self, mock_request, mock_sleep):
        mock_request.side_effect = [
            response(
                429,
                {
                    "code": "woocommerce_rest_rate_limit",
                },
                headers={
                    "Retry-After": "0",
                },
            ),
            response(
                200,
                raw_order(),
            ),
        ]

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(result["id"], "1001")
        self.assertEqual(mock_request.call_count, 2)
        mock_sleep.assert_called_once_with(0.001)

    # 5XX / RETRIES

    @patch("integrations.providers.woocommerce.time.sleep")
    @patch("httpx.Client.request")
    def test_5xx_get_retries_three_times(self, mock_request, mock_sleep):
        mock_request.side_effect = [
            response(500, {"code": "server_error"}),
            response(502, {"code": "bad_gateway"}),
            response(503, {"code": "service_unavailable"}),
        ]

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_UNAVAILABLE",
        )

        self.assertEqual(
            result["http_status"],
            503,
        )

        self.assertEqual(mock_request.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("httpx.Client.request")
    def test_put_is_not_retried_on_5xx(self, mock_request):
        mock_request.return_value = response(
            500,
            {
                "code": "server_error",
            },
        )

        result = self.connector._execute_request(
            "PUT",
            "orders/1001",
            json_data={
                "status": "cancelled",
            },
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_UNAVAILABLE",
        )

        self.assertEqual(mock_request.call_count, 1)

    # TIMEOUT / NETWORK

    @patch("integrations.providers.woocommerce.time.sleep")
    @patch("httpx.Client.request")
    def test_timeout_retries_then_returns_timeout(self, mock_request, mock_sleep):
        mock_request.side_effect = httpx.ReadTimeout(
            "Request timed out",
        )

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_TIMEOUT",
        )

        self.assertEqual(mock_request.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("integrations.providers.woocommerce.time.sleep")
    @patch("httpx.Client.request")
    def test_network_error_retries_then_returns_unreachable(self, mock_request, mock_sleep):
        mock_request.side_effect = httpx.ConnectError(
            "Connection failed",
        )

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_UNREACHABLE",
        )

        self.assertEqual(mock_request.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    # MALFORMED / EMPTY RESPONSES

    @patch("httpx.Client.request")
    def test_malformed_or_empty_200_response_returns_invalid_response(self, mock_request):
        mock_request.return_value = response(
            200,
            bad_json=True,
        )

        result = self.connector.get_order_details(
            "1001",
            "COMP-WOOCOMMERCE",
        )

        self.assertEqual(
            result["code"],
            "PROVIDER_INVALID_RESPONSE",
        )

        self.assertEqual(
            result["http_status"],
            200,
        )

    # LARGE RESPONSE LIMITS

    @patch("httpx.Client.request")
    def test_large_order_list_is_limited(self, mock_request):
        large_orders = [
            raw_order(
                order_id=1000 + index,
                number=str(1000 + index),
            )
            for index in range(60)
        ]

        mock_request.return_value = response(
            200,
            large_orders,
        )

        result = self.connector.get_orders(
            "COMP-WOOCOMMERCE",
            limit=999,
        )

        self.assertEqual(
            len(result),
            50,
        )

        self.assertEqual(
            mock_request.call_args.kwargs["params"],
            {"per_page": 50},
        )

    @patch("httpx.Client.request")
    def test_large_product_description_is_truncated(self, mock_request):
        mock_request.return_value = response(
            200,
            {
                "id": 88,
                "name": "Large Product",
                "slug": "large-product",
                "type": "simple",
                "status": "publish",
                "price": "29.99",
                "regular_price": "29.99",
                "sale_price": None,
                "sku": "LARGE-88",
                "stock_status": "instock",
                "stock_quantity": 50,
                "description": "X" * 5000,
            },
        )

        product = self.connector.get_product(
            "88",
            "COMP-WOOCOMMERCE",
        )

        self.assertLessEqual(
            len(product["description"]),
            2050,
        )

        self.assertIn(
            "[TRUNCATED FOR TOKEN LIMITS]",
            product["description"],
        )

    # CREDENTIAL REDACTION

    def test_auth_credentials_never_exposed(self):
        connector = WooCommerceConnector(
            site_url="https://secret-store.com",
            consumer_key="ck_super_secret_key_123",
            consumer_secret="cs_super_secret_val_456",
        )

        raw_message = (
            "Failed with credentials "
            "ck_super_secret_key_123 and "
            "cs_super_secret_val_456"
        )

        clean_message = connector._sanitize_error(
            raw_message,
        )

        self.assertNotIn(
            "ck_super_secret_key_123",
            clean_message,
        )

        self.assertNotIn(
            "cs_super_secret_val_456",
            clean_message,
        )

        self.assertIn(
            "[REDACTED_KEY]",
            clean_message,
        )

        self.assertIn(
            "[REDACTED_SECRET]",
            clean_message,
        )

    # GATEWAY ROUTING

    def test_gateway_provider_routing(self):
        connector = gateway.get_connector(
            "COMP-WOOCOMMERCE",
        )

        self.assertIsInstance(
            connector,
            WooCommerceConnector,
        )


class TestWooCommerceConnectorIntegration(unittest.TestCase):
    """
    Real WooCommerce E2E test.
    Skipped by default because the configured WordPress Playground store is
    not externally reachable as a WooCommerce REST API.
    """

    @unittest.skipUnless(
        os.getenv("RUN_WOOCOMMERCE_INTEGRATION_TESTS") == "true",
        "Skipping live WooCommerce integration tests. "
        "Set RUN_WOOCOMMERCE_INTEGRATION_TESTS=true to enable.",
    )
    def test_live_get_shop(self):
        connector = WooCommerceConnector()

        shop_info = connector.get_shop(
            "COMP-WOOCOMMERCE",
        )

        self.assertIsNotNone(shop_info)
        self.assertEqual(
            shop_info.get("provider"),
            "WOOCOMMERCE",
        )


if __name__ == "__main__":
    unittest.main()
