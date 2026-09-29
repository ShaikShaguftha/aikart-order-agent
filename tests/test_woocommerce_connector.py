import os
import unittest
from unittest.mock import MagicMock, patch
from integrations.gateway import gateway
from integrations.providers.woocommerce import WooCommerceConnector
from integrations.providers.local_db import LocalDBConnector
from integrations.providers.shopify import ShopifyConnector


class TestWooCommerceConnectorUnit(unittest.TestCase):

    def setUp(self):
        self.connector = WooCommerceConnector(
            site_url="https://test-woo-store.com",
            consumer_key="ck_test_1234567890abcdef",
            consumer_secret="cs_test_1234567890abcdef",
        )

    @patch("httpx.Client.request")
    def test_01_get_shop_success(self, mock_request):
        """Test retrieving shop details from mocked WooCommerce system status API."""
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "environment": {
                "site_title": "WooCommerce Test Store",
                "site_url": "https://test-woo-store.com",
                "admin_email": "admin@test-woo-store.com",
            },
            "settings": {
                "currency": "USD",
            },
        }
        mock_request.return_value = mock_res

        shop_info = self.connector.get_shop("COMP-WOOCOMMERCE")
        self.assertEqual(shop_info["name"], "WooCommerce Test Store")
        self.assertEqual(shop_info["provider"], "WOOCOMMERCE")
        self.assertEqual(shop_info["currency"], "USD")

    @patch("httpx.Client.request")
    def test_02_get_orders(self, mock_request):
        """Test listing orders from mocked WooCommerce REST API v3."""
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = [
            {
                "id": 1001,
                "number": "1001",
                "status": "processing",
                "date_created_gmt": "2026-09-25T10:00:00",
                "total": "99.50",
                "billing": {
                    "first_name": "Jane",
                    "last_name": "Doe",
                    "email": "jane@example.com",
                },
                "line_items": [
                    {
                        "id": 1,
                        "name": "Wireless Mouse",
                        "quantity": 1,
                        "price": "99.50",
                    }
                ],
                "meta_data": [],
            }
        ]
        mock_request.return_value = mock_res

        orders = self.connector.get_orders("COMP-WOOCOMMERCE", limit=5)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["id"], "1001")
        self.assertEqual(orders[0]["status"], "PROCESSING")
        self.assertEqual(orders[0]["financial_status"], "PAID")
        self.assertEqual(orders[0]["total"], 99.50)
        self.assertEqual(orders[0]["items"][0]["product_name"], "Wireless Mouse")

    @patch("httpx.Client.request")
    def test_03_get_order_details_by_id(self, mock_request):
        """Test retrieving single order details by numeric ID."""
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "id": 1002,
            "number": "1002",
            "status": "completed",
            "date_created_gmt": "2026-09-24T15:30:00",
            "total": "150.00",
            "billing": {
                "first_name": "John",
                "last_name": "Smith",
                "email": "john@example.com",
            },
            "line_items": [
                {
                    "id": 2,
                    "name": "Mechanical Keyboard",
                    "quantity": 1,
                    "price": "150.00",
                }
            ],
            "meta_data": [
                {"key": "_tracking_number", "value": "TRK-WOO-999"},
                {"key": "_tracking_provider", "value": "FedEx"},
            ],
        }
        mock_request.return_value = mock_res

        order = self.connector.get_order_details("1002", "COMP-WOOCOMMERCE")
        self.assertIsNotNone(order)
        self.assertEqual(order["id"], "1002")
        self.assertEqual(order["status"], "COMPLETED")
        self.assertEqual(order["fulfillment_status"], "FULFILLED")
        self.assertEqual(order["total"], 150.00)
        self.assertIsNotNone(order.get("shipment"))
        self.assertEqual(order["shipment"]["tracking_number"], "TRK-WOO-999")

    @patch("httpx.Client.request")
    def test_04_get_customer(self, mock_request):
        """Test retrieving customer record by email."""
        # 1. GET customers?email=...
        # 2. GET orders?customer=...
        mock_cust_res = MagicMock()
        mock_cust_res.status_code = 200
        mock_cust_res.json.return_value = [
            {
                "id": 45,
                "email": "alice@example.com",
                "first_name": "Alice",
                "last_name": "Wonder",
            }
        ]

        mock_orders_res = MagicMock()
        mock_orders_res.status_code = 200
        mock_orders_res.json.return_value = []

        mock_request.side_effect = [mock_cust_res, mock_orders_res]

        customer = self.connector.get_customer("alice@example.com", "COMP-WOOCOMMERCE")
        self.assertEqual(customer["customer_id"], "45")

    @patch("httpx.Client.request")
    def test_05_get_product(self, mock_request):
        """Test fetching product catalog entry by numeric product ID."""
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
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
        }
        mock_request.return_value = mock_res

        prod = self.connector.get_product("88", "COMP-WOOCOMMERCE")
        self.assertEqual(prod["product_id"], "88")
        self.assertEqual(prod["name"], "Gaming Desk Pad")
        self.assertEqual(prod["price"], 29.99)
        self.assertEqual(prod["stock_status"], "instock")

    @patch("httpx.Client.request")
    def test_06_get_order_status(self, mock_request):
        """Test retrieving high-level order status."""
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "id": 1003,
            "number": "1003",
            "status": "on-hold",
            "total": "40.00",
            "line_items": [],
        }
        mock_request.return_value = mock_res

        status_info = self.connector.get_order_status("1003", "COMP-WOOCOMMERCE")
        self.assertEqual(status_info["order_id"], "1003")
        self.assertEqual(status_info["status"], "ON-HOLD")

    @patch("httpx.Client.request")
    def test_07_cancel_order_execution(self, mock_request):
        """Test canceling order via PUT /wp-json/wc/v3/orders/{id}."""
        # 1. get_order_details pre-check
        # 2. PUT orders/{id} status change
        # 3. get_order_details verification
        mock_order_pre = MagicMock()
        mock_order_pre.status_code = 200
        mock_order_pre.json.return_value = {
            "id": 1004,
            "number": "1004",
            "status": "processing",
            "total": "80.00",
            "line_items": [{"name": "Headset", "quantity": 1, "price": "80.00"}],
        }

        mock_put_res = MagicMock()
        mock_put_res.status_code = 200
        mock_put_res.json.return_value = {
            "id": 1004,
            "number": "1004",
            "status": "cancelled",
        }

        mock_order_post = MagicMock()
        mock_order_post.status_code = 200
        mock_order_post.json.return_value = {
            "id": 1004,
            "number": "1004",
            "status": "cancelled",
            "total": "80.00",
            "line_items": [],
        }

        mock_request.side_effect = [mock_order_pre, mock_put_res, mock_order_post]

        res = self.connector.cancel_order("1004", "COMP-WOOCOMMERCE", reason="CUSTOMER")
        self.assertTrue(res["success"])
        self.assertEqual(res["status"], "CANCELLED")
        self.assertEqual(res["verification_status"], "VERIFIED")

    @patch("httpx.Client.request")
    def test_08_update_order(self, mock_request):
        """Test updating order customer note/status via update_order capability."""
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {
            "id": 1005,
            "number": "1005",
            "status": "completed",
            "customer_note": "Customer requested priority delivery.",
            "total": "120.00",
            "line_items": [],
        }
        mock_request.return_value = mock_res

        res = self.connector.update_order("1005", "COMP-WOOCOMMERCE", status="completed", customer_note="Customer requested priority delivery.")
        self.assertEqual(res["id"], "1005")
        self.assertEqual(res["status"], "COMPLETED")

    @patch("httpx.Client.request")
    def test_09_refunds_and_controlled_return(self, mock_request):
        """Test read-only refund status operation and controlled return capability response."""
        # 1. get_order_details inside request_return
        # 2. get_refunds inside request_return
        mock_order = MagicMock()
        mock_order.status_code = 200
        mock_order.json.return_value = {
            "id": 1006,
            "number": "1006",
            "status": "completed",
            "total": "200.00",
            "line_items": [],
        }

        mock_refunds = MagicMock()
        mock_refunds.status_code = 200
        mock_refunds.json.return_value = [
            {
                "id": 1,
                "amount": "50.00",
                "reason": "Partial refund for damaged packaging",
                "date_created_gmt": "2026-09-25T11:00:00",
            }
        ]

        mock_request.side_effect = [mock_order, mock_refunds]

        res = self.connector.request_return("1006", 50.0, "COMP-WOOCOMMERCE")
        self.assertEqual(res["status"], "CONTROLLED_CAPABILITY")
        self.assertEqual(res["refunds_summary"]["total_refunded"], 50.0)

    def test_10_gateway_provider_routing(self):
        """Test that IntegrationGateway routes COMP-WOOCOMMERCE to WooCommerceConnector."""
        woo_connector = gateway.get_connector("COMP-WOOCOMMERCE")
        self.assertIsInstance(woo_connector, WooCommerceConnector)

    def test_11_auth_credentials_never_exposed(self):
        """Test that consumer secrets and keys are sanitized and redacted from error messages."""
        connector = WooCommerceConnector(
            site_url="https://secret-store.com",
            consumer_key="ck_super_secret_key_123",
            consumer_secret="cs_super_secret_val_456",
        )
        raw_msg = "Failed with credentials ck_super_secret_key_123 and cs_super_secret_val_456"
        clean = connector._sanitize_error(raw_msg)
        self.assertNotIn("ck_super_secret_key_123", clean)
        self.assertNotIn("cs_super_secret_val_456", clean)
        self.assertIn("[REDACTED_KEY]", clean)
        self.assertIn("[REDACTED_SECRET]", clean)


class TestWooCommerceConnectorIntegration(unittest.TestCase):
    """
    Live WooCommerce Integration Test Suite.
    Skipped by default unless environment variable `RUN_WOOCOMMERCE_INTEGRATION_TESTS=true` is set.
    """

    @unittest.skipUnless(
        os.getenv("RUN_WOOCOMMERCE_INTEGRATION_TESTS") == "true",
        "Skipping live WooCommerce integration tests. Set RUN_WOOCOMMERCE_INTEGRATION_TESTS=true to enable.",
    )
    def test_live_get_shop(self):
        connector = WooCommerceConnector()
        shop_info = connector.get_shop("COMP-WOOCOMMERCE")
        self.assertIsNotNone(shop_info)
        self.assertEqual(shop_info.get("provider"), "WOOCOMMERCE")


if __name__ == "__main__":
    unittest.main()
