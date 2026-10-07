import os
import sys
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

from integrations.gateway import gateway
from database import query_db


def test_live_cancellation_execution():
    session_id = "test_live_session_1002"
    company_id = "COMP-SHOPIFY"
    order_id = "#1002"

    print(f"--- Step 1: Prepare Cancellation for Live Order {order_id} ---")
    prep = gateway.cancel_order(
        order_id=order_id,
        company_id=company_id,
        reason="CUSTOMER",
        session_id=session_id,
        confirmed=False,
    )
    print(f"Preparation Result: {prep}\n")

    if prep.get("status") != "REQUIRES_CONFIRMATION":
        print("Preparation failed or was rejected!")
        return

    print(f"--- Step 2: Confirm Cancellation for Live Order {order_id} ---")
    confirm = gateway.cancel_order(
        order_id=order_id,
        company_id=company_id,
        reason="CUSTOMER",
        session_id=session_id,
        confirmed=True,
    )
    print(f"Confirmation Result: {confirm}\n")

    print("--- Step 3: Verifying Fresh Order Status via Gateway ---")
    fresh_order = gateway.get_order_details(order_id, company_id)
    print(f"Fresh Order Details: {fresh_order}\n")

    print("--- Step 4: Inspecting SQLite action_audit_logs ---")
    logs = query_db(
        "SELECT * FROM action_audit_logs WHERE order_id = ? ORDER BY id DESC LIMIT 5",
        (order_id,),
    )
    for log in logs:
        print(dict(log))


if __name__ == "__main__":
    test_live_cancellation_execution()
