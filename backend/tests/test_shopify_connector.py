import unittest
from unittest.mock import MagicMock, patch
from backend.integrations.gateway import gateway
from backend.integrations.providers.local_db import LocalDBConnector
from backend.integrations.providers.shopify import ShopifyConnector


class TestShopifyConnector(unittest.TestCase):

    def setUp(self):
        self.connector = ShopifyConnector(
            shop_domain="test-store.myshopify.com",
            access_token="shpat_test_token_12345",
        )

    @patch("httpx.Client.post")
    def test_01_get_shop_success(self, mock_post):
        """Test retrieving shop info from mocked Shopify GraphQL API."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "shop": {
                    "name": "Test Apparel Store",
                    "email": "owner@teststore.com",
                    "myshopifyDomain": "test-store.myshopify.com",
                    "currencyCode": "USD",
                }
            }
        }
        mock_post.return_value = mock_response

        shop_info = self.connector.get_shop("COMP-SHOPIFY")
        self.assertEqual(shop_info["name"], "Test Apparel Store")
        self.assertEqual(shop_info["provider"], "SHOPIFY")
        self.assertEqual(shop_info["currency"], "USD")

    @patch("httpx.Client.post")
    def test_02_get_order_details_by_gid(self, mock_post):
        """Test retrieving order details via full Shopify GID."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "order": {
                    "id": "gid://shopify/Order/7661166461155",
                    "name": "#1001",
                    "createdAt": "2026-09-24T12:00:00Z",
                    "displayFinancialStatus": "PAID",
                    "displayFulfillmentStatus": "FULFILLED",
                    "totalPriceSet": {
                        "shopMoney": {"amount": "120.00", "currencyCode": "USD"}
                    },
                    "customer": {
                        "id": "gid://shopify/Customer/999",
                        "email": "customer@example.com",
                    },
                    "lineItems": {
                        "nodes": [
                            {
                                "id": "gid://shopify/LineItem/1",
                                "title": "Leather Jacket",
                                "quantity": 1,
                                "originalUnitPriceSet": {
                                    "shopMoney": {"amount": "120.00"}
                                },
                            }
                        ]
                    },
                    "fulfillments": [],
                }
            }
        }
        mock_post.return_value = mock_response

        order = self.connector.get_order_details(
            "gid://shopify/Order/7661166461155", "COMP-SHOPIFY"
        )
        self.assertIsNotNone(order)
        self.assertEqual(order["id"], "#1001")
        self.assertEqual(order["total"], 120.0)

    @patch("httpx.Client.post")
    def test_03_get_order_details_by_name_hash(self, mock_post):
        """Test retrieving order details using name `#1001` via GraphQL search query."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "orders": {
                    "nodes": [
                        {
                            "id": "gid://shopify/Order/7661166461155",
                            "name": "#1001",
                            "createdAt": "2026-09-24T12:00:00Z",
                            "displayFinancialStatus": "PAID",
                            "displayFulfillmentStatus": "UNFULFILLED",
                            "totalPriceSet": {
                                "shopMoney": {"amount": "75.00", "currencyCode": "USD"}
                            },
                            "lineItems": {
                                "nodes": [
                                    {
                                        "title": "Summer T-Shirt",
                                        "quantity": 2,
                                        "originalUnitPriceSet": {
                                            "shopMoney": {"amount": "37.50"}
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        }
        mock_post.return_value = mock_response

        order = self.connector.get_order_details("#1001", "COMP-SHOPIFY")
        self.assertIsNotNone(order)
        self.assertEqual(order["id"], "#1001")
        self.assertEqual(order["total"], 75.0)

    @patch.object(ShopifyConnector, "_execute_graphql")
    def test_06_shopify_cancellation_execution(self, mock_graphql):
        """Test executing orderCancel mutation on Shopify connector."""
        # 1. GID resolution response
        # 2. Mutation response
        # 3. Post-cancel order details verification lookup
        mock_graphql.side_effect = [
            # _resolve_shopify_gid
            {
                "orders": {
                    "nodes": [
                        {
                            "id": "gid://shopify/Order/7661166461155",
                            "name": "#1002",
                            "cancelledAt": None,
                            "displayFinancialStatus": "PAID",
                            "displayFulfillmentStatus": "UNFULFILLED",
                            "fullyPaid": True,
                            "totalPriceSet": {"shopMoney": {"amount": "160.00", "currencyCode": "USD"}},
                        }
                    ]
                }
            },
            # orderCancel mutation
            {
                "orderCancel": {
                    "job": {"id": "gid://shopify/Job/123", "done": True},
                    "orderCancelUserErrors": [],
                }
            },
            # get_order_details verification
            {
                "order": {
                    "id": "gid://shopify/Order/7661166461155",
                    "name": "#1002",
                    "createdAt": "2026-09-24T12:00:00Z",
                    "displayFinancialStatus": "REFUNDED",
                    "displayFulfillmentStatus": "CANCELLED",
                    "fullyPaid": True,
                    "totalPriceSet": {"shopMoney": {"amount": "160.00"}},
                    "lineItems": {"nodes": []},
                    "fulfillments": [],
                }
            },
        ]

        result = self.connector.cancel_order("#1002", "COMP-SHOPIFY", reason="CUSTOMER")
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "CANCELLED")
        self.assertEqual(result["order_id"], "#1002")
        self.assertTrue(result["refund_issued"])
        self.assertEqual(result["refund_amount"], 160.0)
        self.assertEqual(result["verification_status"], "VERIFIED")

    def test_07_gateway_provider_routing(self):
        """Test that IntegrationGateway routes tenants correctly."""
        alpha_connector = gateway.get_connector("COMP-ALPHA")
        self.assertIsInstance(alpha_connector, LocalDBConnector)

        shopify_connector = gateway.get_connector("COMP-SHOPIFY")
        self.assertIsInstance(shopify_connector, ShopifyConnector)


if __name__ == "__main__":
    unittest.main()
