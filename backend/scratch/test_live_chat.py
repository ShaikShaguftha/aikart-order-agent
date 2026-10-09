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

def run_chat_tests():
    print("=== STARTING LIVE SHOPIFY AGENT CHAT TEST ===")
    init_db()

    prompts = [
        "Show me details of order #1001",
        "What is the status of order #1001?",
        "Is order #1001 paid?",
        "Has order #1001 been shipped?",
    ]

    session_id = "test-shopify-session-live"

    for i, prompt in enumerate(prompts, 1):
        print(f"\n==================================================")
        print(f"TEST {i}: '{prompt}'")
        print(f"==================================================")

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
    run_chat_tests()
