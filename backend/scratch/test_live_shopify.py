import os
import sys
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from backend.integrations.gateway import gateway
from backend.integrations.providers.shopify import ShopifyConnector


def test_live_shopify():
    print("=== STARTING LIVE SHOPIFY INTEGRATION TEST ===")
    
    shop_domain = os.getenv("SHOPIFY_SHOP_DOMAIN")
    access_token = os.getenv("SHOPIFY_ACCESS_TOKEN")
    api_version = os.getenv("SHOPIFY_API_VERSION", "2024-04")
    
    print(f"Target Store: {shop_domain}")
    print(f"API Version:  {api_version}")

    connector = ShopifyConnector(
        shop_domain=shop_domain,
        access_token=access_token,
        api_version=api_version,
    )

    print("\n1. TESTING LIVE SHOP DETAILS (get_shop):")
    shop_res = connector.get_shop("COMP-SHOPIFY")
    print(f"Shop Info: {shop_res}")

    print("\n2. TESTING LIVE ORDER LOOKUP BY NAME '#1001' (get_order_details):")
    order_hash = connector.get_order_details("#1001", "COMP-SHOPIFY")
    print(f"Order #1001 Result: {order_hash}")

    print("\n3. TESTING LIVE ORDER LOOKUP BY PLAIN '1001' (get_order_details):")
    order_plain = connector.get_order_details("1001", "COMP-SHOPIFY")
    print(f"Order 1001 Result: {order_plain}")

    print("\n4. TESTING VIA GATEWAY FOR COMP-SHOPIFY:")
    gw_order = gateway.get_order_details("#1001", "COMP-SHOPIFY")
    print(f"Gateway COMP-SHOPIFY Result: {gw_order}")

    print("\n5. TESTING VIA GATEWAY FOR COMP-ALPHA (LOCAL DB ISOLATION):")
    alpha_order = gateway.get_order_details("ORD-5001", "COMP-ALPHA")
    print(f"Gateway COMP-ALPHA Result: {alpha_order}")

    print("\n=== LIVE SHOPIFY TEST COMPLETED ===")


if __name__ == "__main__":
    test_live_shopify()
