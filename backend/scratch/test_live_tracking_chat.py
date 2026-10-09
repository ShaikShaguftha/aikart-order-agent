import os
import sys
import json
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from backend.database import init_db
from fastapi.testclient import TestClient
from backend.integrations.gateway import gateway
from server import app

client = TestClient(app)

def run_tracking_tests():
    print("=== LIVE SHOPIFY ORDER TRACKING TEST (#1001) ===")
    init_db()

    print("\n1. DIRECT GATEWAY GET_SHIPMENT_STATUS RESULT:")
    shipment_res = gateway.get_shipment_status("#1001", "COMP-SHOPIFY")
    print(json.dumps(shipment_res, indent=2))

    questions = [
        "Has order #1001 been shipped?",
        "Where is my order #1001?",
        "What is the tracking number for #1001?",
        "Give me the tracking link for #1001",
    ]

    for i, q in enumerate(questions, 1):
        print(f"\n" + "=" * 80)
        print(f"QUESTION {i}: '{q}'")
        print("=" * 80)

        response = client.post(
            "/api/chat",
            headers={
                "Content-Type": "application/json",
                "X-Company-ID": "COMP-SHOPIFY",
            },
            json={"user_input": q, "session_id": f"tracking-session-{i}"},
        )

        if response.status_code == 200:
            print(f"Agent Customer Response:\n{response.json().get('agent_response')}")
        else:
            print(f"Error Response: {response.text}")


if __name__ == "__main__":
    run_tracking_tests()
