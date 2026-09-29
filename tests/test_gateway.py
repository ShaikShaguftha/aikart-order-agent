import json
import unittest
from database import init_db, query_db, execute_db
from integrations.gateway import gateway
from tools import (
    cancel_order_tool,
    prepare_cancellation_tool,
    escalate_to_human_tool,
    get_customer_details,
    get_order_details,
    get_shipment_status,
    request_return_tool,
)


class TestIntegrationGateway(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """Ensure SQLite DB is initialized with sample data and reset to initial state."""
        init_db()
        execute_db("UPDATE orders SET status = 'DELIVERED' WHERE id = 'ORD-5001'")
        execute_db("UPDATE orders SET status = 'PROCESSING' WHERE id = 'ORD-5004'")

    def setUp(self):
        execute_db("UPDATE orders SET status = 'DELIVERED' WHERE id = 'ORD-5001'")
        execute_db("UPDATE orders SET status = 'PROCESSING' WHERE id = 'ORD-5004'")

    def test_01_get_order_details(self):
        """Test retrieving order details via Integration Gateway."""
        order = gateway.get_order_details("ORD-5001", "COMP-ALPHA")
        self.assertIsNotNone(order)
        self.assertEqual(order["id"], "ORD-5001")
        self.assertEqual(len(order["items"]), 1)
        self.assertEqual(order["items"][0]["product_name"], "Wireless Headphones")

    def test_02_get_shipment_status(self):
        """Test retrieving shipment tracking via Integration Gateway."""
        shipment = gateway.get_shipment_status("ORD-5001", "COMP-ALPHA")
        self.assertIsNotNone(shipment)
        self.assertEqual(shipment["courier"], "FedEx")
        self.assertEqual(shipment["tracking_number"], "TRK-9001")

    def test_03_safe_action_cancel_order_flow(self):
        """Test safe two-step order cancellation flow (Prepare -> Confirm)."""
        # Step 1: Prepare (confirmed=False)
        prep = gateway.cancel_order("ORD-5004", "COMP-ALPHA", session_id="test_sess_101", confirmed=False)
        self.assertEqual(prep["status"], "REQUIRES_CONFIRMATION")
        self.assertTrue(prep["cancellable"])
        self.assertIn("action_id", prep)

        # Step 2: Confirm (confirmed=True)
        conf = gateway.cancel_order("ORD-5004", "COMP-ALPHA", session_id="test_sess_101", confirmed=True)
        self.assertEqual(conf["status"], "CANCELLED")
        self.assertTrue(conf["success"])
        self.assertEqual(conf["verification_status"], "VERIFIED")

        # Verify DB state
        db_order = query_db(
            "SELECT status FROM orders WHERE id = 'ORD-5004'", one=True
        )
        self.assertEqual(db_order["status"], "CANCELLED")

    def test_03b_cancel_fulfilled_order_policy_rejection(self):
        """Test cancellation policy rejection for fulfilled/delivered order."""
        prep = gateway.cancel_order("ORD-5001", "COMP-ALPHA", session_id="test_sess_102", confirmed=False)
        self.assertEqual(prep["status"], "REJECTED")
        self.assertFalse(prep["cancellable"])
        self.assertIn("cannot be cancelled", prep["reason"].lower())

    def test_04_request_return_approved(self):
        """Test auto-approved return for amount <= $100."""
        result = gateway.request_return("ORD-5001", 45.0, "COMP-ALPHA")
        self.assertEqual(result["status"], "APPROVED")

        db_order = query_db(
            "SELECT status FROM orders WHERE id = 'ORD-5001'", one=True
        )
        self.assertEqual(db_order["status"], "REFUND_APPROVED")

    def test_05_request_return_escalated(self):
        """Test return escalation for refund amount > $100."""
        result = gateway.request_return("ORD-5002", 250.0, "COMP-ALPHA")
        self.assertEqual(result["status"], "REQUIRES_HUMAN_REVIEW")
        self.assertIn("exceeds maximum automated limit", result["reason"])

    def test_06_escalate_to_human(self):
        """Test creating a human support ticket via Integration Gateway."""
        result = gateway.escalate_to_human(
            "ORD-5002", "Customer requested management escalation", "COMP-ALPHA"
        )
        self.assertEqual(result["status"], "ESCALATED")
        self.assertEqual(result["ticket_id"], "TICK-ORD-5002")

    def test_07_tool_delegation_to_gateway(self):
        """Test that refactored LangChain tools successfully delegate to Gateway."""
        raw_json = get_order_details.invoke({"order_id": "ORD-5001", "company_id": "COMP-ALPHA"})
        data = json.loads(raw_json)
        self.assertEqual(data["id"], "ORD-5001")


if __name__ == "__main__":
    unittest.main()
