import unittest
from backend.database import init_db, query_db, execute_db
from backend.integrations.audit import AuditLogger
from backend.integrations.confirmation import confirmation_manager
from backend.integrations.gateway import gateway
from backend.integrations.models import CancelOrderResult, PolicyDecision
from backend.integrations.policy import CancellationPolicyEngine


class TestSafeActionFramework(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        execute_db("UPDATE orders SET status = 'PROCESSING' WHERE id = 'ORD-5004'")
        execute_db("UPDATE orders SET status = 'DELIVERED' WHERE id = 'ORD-5001'")

    def test_01_policy_engine_unfulfilled(self):
        """Policy engine should approve cancellation for UNFULFILLED / PROCESSING orders."""
        order = {
            "id": "ORD-TEST-1",
            "status": "PROCESSING",
            "fulfillment_status": "UNFULFILLED",
            "financial_status": "PAID",
            "fully_paid": True,
            "total": 50.0,
        }
        decision = CancellationPolicyEngine.evaluate(order)
        self.assertTrue(decision.is_cancellable)
        self.assertTrue(decision.requires_confirmation)
        self.assertTrue(decision.refund_recommended)
        self.assertIn("$50.00 was paid", decision.refund_note)

    def test_02_policy_engine_fulfilled(self):
        """Policy engine should reject cancellation for FULFILLED or DELIVERED orders."""
        order = {
            "id": "ORD-TEST-2",
            "status": "DELIVERED",
            "fulfillment_status": "FULFILLED",
            "financial_status": "PAID",
            "total": 120.0,
        }
        decision = CancellationPolicyEngine.evaluate(order)
        self.assertFalse(decision.is_cancellable)
        self.assertIn("cannot be cancelled", decision.refusal_reason)

    def test_03_policy_engine_already_cancelled(self):
        """Policy engine should reject cancellation for CANCELLED orders."""
        order = {
            "id": "ORD-TEST-3",
            "status": "CANCELLED",
            "fulfillment_status": "CANCELLED",
            "financial_status": "REFUNDED",
            "total": 80.0,
        }
        decision = CancellationPolicyEngine.evaluate(order)
        self.assertFalse(decision.is_cancellable)
        self.assertIn("already been cancelled", decision.refusal_reason)

    def test_04_confirmation_manager_lifecycle(self):
        """Test staging, retrieving, and clearing pending actions."""
        policy_decision = PolicyDecision(
            is_cancellable=True,
            refund_recommended=True,
            refund_note="Paid order",
        )
        pending = confirmation_manager.register_pending_action(
            session_id="sess_123",
            company_id="COMP-ALPHA",
            order_id="ORD-5004",
            reason="CUSTOMER",
            policy_decision=policy_decision,
        )
        self.assertIsNotNone(pending.action_id)
        self.assertEqual(pending.order_id, "ORD-5004")

        # Fetch
        retrieved = confirmation_manager.get_pending_action("sess_123", "COMP-ALPHA", "ORD-5004")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.action_id, pending.action_id)

        # Clear
        confirmation_manager.clear_pending_action("sess_123", "COMP-ALPHA", "ORD-5004")
        cleared = confirmation_manager.get_pending_action("sess_123", "COMP-ALPHA", "ORD-5004")
        self.assertIsNone(cleared)

    def test_05_audit_logger_recording(self):
        """Test recording audit entries into SQLite action_audit_logs table."""
        row_id = AuditLogger.log_action(
            session_id="sess_audit_test",
            company_id="COMP-ALPHA",
            order_id="ORD-5004",
            action_type="CANCEL_ORDER",
            provider="LOCAL_DB",
            status="CANCELLED",
            reason="CUSTOMER",
            refund_issued=True,
            details={"test": "data"},
        )
        self.assertGreater(row_id, 0)

        # Verify SQL row
        log_entry = query_db(
            "SELECT * FROM action_audit_logs WHERE id = ?", (row_id,), one=True
        )
        self.assertIsNotNone(log_entry)
        self.assertEqual(log_entry["session_id"], "sess_audit_test")
        self.assertEqual(log_entry["order_id"], "ORD-5004")
        self.assertEqual(log_entry["action_type"], "CANCEL_ORDER")
        self.assertEqual(log_entry["refund_issued"], 1)

    def test_06_gateway_user_declines_confirmation(self):
        """Test gateway response when user declines staged cancellation (confirmed=False in step 2)."""
        # Step 1: Prepare
        gateway.prepare_order_cancellation(
            session_id="sess_abort_test",
            company_id="COMP-ALPHA",
            order_id="ORD-5004",
        )

        # Step 2: Confirm with confirmed=False
        res = gateway.confirm_order_cancellation(
            session_id="sess_abort_test",
            company_id="COMP-ALPHA",
            order_id="ORD-5004",
            confirmed=False,
        )
        self.assertEqual(res["status"], "CANCELLED_BY_USER")
        self.assertIn("aborted", res["message"].lower())

        # Verify order status in DB was NOT changed
        db_order = query_db("SELECT status FROM orders WHERE id = 'ORD-5004'", one=True)
        self.assertEqual(db_order["status"], "PROCESSING")


if __name__ == "__main__":
    unittest.main()
