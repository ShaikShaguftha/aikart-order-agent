import json
import sys
sys.path.insert(0, ".")

from database import init_db, query_db
from integrations.gateway import gateway
from tools import (
    cancel_order_tool,
    escalate_to_human_tool,
    get_customer_details,
    get_order_details,
    get_shipment_status,
    request_return_tool,
    get_ticket_details,
)


def run_e2e_verification():
    print("=== INITIALIZING DATABASE & SEEDING INTEGRATIONS ===")
    init_db()

    print("\n--- TEST GROUP 1: LOCAL DB TENANT (COMP-ALPHA) ---")
    
    print("1. GET ORDER DETAILS (COMP-ALPHA):")
    res1 = get_order_details.invoke({"order_id": "ORD-5001", "company_id": "COMP-ALPHA"})
    print(f"Result: {res1}")

    print("2. SHIPMENT STATUS (COMP-ALPHA):")
    res2 = get_shipment_status.invoke({"order_id": "ORD-5001", "company_id": "COMP-ALPHA"})
    print(f"Result: {res2}")

    print("\n--- TEST GROUP 2: SHOPIFY TENANT (COMP-SHOPIFY) ---")

    print("3. GET SHOP DETAILS (COMP-SHOPIFY via Gateway):")
    shop_info = gateway.get_shop("COMP-SHOPIFY")
    print(f"Result: {shop_info}")

    print("4. CANCEL ORDER SAFEGUARD (COMP-SHOPIFY):")
    res3 = gateway.cancel_order("1001", "COMP-SHOPIFY")
    print(f"Result: {res3}")

    print("5. REQUEST RETURN SAFEGUARD (COMP-SHOPIFY):")
    res4 = gateway.request_return("1001", 50.0, "COMP-SHOPIFY")
    print(f"Result: {res4}")

    print("\n=== ALL E2E INTEGRATION VERIFICATION CHECKS COMPLETED ===")


if __name__ == "__main__":
    run_e2e_verification()
