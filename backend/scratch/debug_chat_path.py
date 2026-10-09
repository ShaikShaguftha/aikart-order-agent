import os
import sys
import json
sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from backend.database import init_db
from fastapi.testclient import TestClient
from server import app

client = TestClient(app)

def run_debug_chats():
    init_db()

    queries = [
        ("A", "Show me details of order #1001"),
        ("B", "Is #1001 paid?"),
        ("C", "What is the status of #1001?"),
        ("D", "Is #D10 paid?"),
    ]

    for label, prompt in queries:
        print("\n" + "=" * 80)
        print(f"QUERY {label}: '{prompt}'")
        print("=" * 80)

        # Fresh session ID per query to avoid cross-query conversation context pollution
        session_id = f"debug-session-{label.lower()}"

        response = client.post(
            "/api/chat",
            headers={
                "Content-Type": "application/json",
                "X-Company-ID": "COMP-SHOPIFY",
            },
            json={"user_input": prompt, "session_id": session_id},
        )

        print(f"HTTP Status: {response.status_code}")
        if response.status_code == 200:
            res_data = response.json()
            print(f"Agent Response:\n{res_data.get('agent_response')}")
        else:
            print(f"Error Response: {response.text}")


if __name__ == "__main__":
    run_debug_chats()
