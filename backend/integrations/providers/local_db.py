from typing import Any, Dict, List, Optional
from backend.database import execute_db, get_company_rules, query_db
from backend.integrations.base import BaseConnector
from backend.integrations.models import CustomerIdentity


class LocalDBConnector(BaseConnector):
    """
    Concrete Provider Connector wrapping the local SQLite database.
    Serves as the default provider for legacy/test environments (e.g., COMP-ALPHA).
    """

    provider_name = "LOCAL_DB"

    def get_shop(self, company_id: str) -> Dict[str, Any]:
        """Retrieve company shop details from SQLite."""
        company = query_db(
            "SELECT * FROM companies WHERE id = ?", (company_id,), one=True
        )
        if not company:
            return {
                "name": f"Local Store ({company_id})",
                "domain": "local.db",
                "currency": "USD",
                "provider": "LOCAL_DB",
            }
        return {
            "name": company["name"],
            "domain": f"{company['id'].lower()}.local",
            "currency": "USD",
            "provider": "LOCAL_DB",
        }

    def get_orders(self, company_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve list of orders from SQLite."""
        orders = query_db(
            "SELECT * FROM orders WHERE company_id = ? LIMIT ?",
            (company_id, limit),
        )
        for order in orders:
            items = query_db(
                "SELECT product_name, quantity, price FROM order_items WHERE order_id = ?",
                (order["id"],),
            )
            order["items"] = items
        return orders

    def find_customer(self, identity: CustomerIdentity, company_id: str) -> Dict[str, Any]:
        """The local demo schema only stores customer IDs (no email/phone)."""
        primary = identity.primary()
        if not primary:
            return {"error": identity.validation_error()}
        kind, value = primary
        if kind != "customer_id":
            return {
                "error": (
                    "The local demo store has no customer email/phone records; "
                    "customers can only be identified by an authenticated customer ID."
                )
            }
        row = query_db(
            "SELECT customer_id FROM orders WHERE customer_id = ? AND company_id = ? LIMIT 1",
            (value, company_id),
            one=True,
        )
        if not row:
            return {"customer": None}
        return {"customer": {"customer_id": row["customer_id"], "email": None, "phone": None, "name": None, "provider": self.provider_name}}

    def get_customer_orders(
        self, customer: Dict[str, Any], company_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Orders for a local customer, newest first."""
        orders = query_db(
            "SELECT id FROM orders WHERE customer_id = ? AND company_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (customer["customer_id"], company_id, limit),
        )
        return [self.get_order_details(o["id"], company_id) for o in orders]

    def get_customer_details(
        self, identifier: str, company_id: str
    ) -> List[Dict[str, Any]]:
        """Retrieve orders for a customer or order ID from SQLite."""
        results = query_db(
            "SELECT * FROM orders WHERE (customer_id = ? OR id = ?) AND company_id = ?",
            (identifier, identifier, company_id),
        )
        return results if results else []

    def get_order_details(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve order details with line items from SQLite."""
        clean_id = order_id.upper()
        order = query_db(
            "SELECT * FROM orders WHERE id = ? AND company_id = ?",
            (clean_id, company_id),
            one=True,
        )
        if not order:
            return None

        items = query_db(
            "SELECT product_name, quantity, price FROM order_items WHERE order_id = ?",
            (clean_id,),
        )
        order["items"] = items
        return order

    def get_shipment_status(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve shipment details for an order from SQLite."""
        clean_id = order_id.upper()
        shipment = query_db(
            """
            SELECT s.* FROM shipments s
            JOIN orders o ON s.order_id = o.id
            WHERE s.order_id = ? AND o.company_id = ?
            """,
            (clean_id, company_id),
            one=True,
        )
        return shipment if shipment else None

    def cancel_order(
        self, order_id: str, company_id: str, reason: Optional[str] = "CUSTOMER"
    ) -> Dict[str, Any]:
        """Cancel an order in SQLite if status is PENDING or PROCESSING."""
        clean_id = order_id.upper()
        order = query_db(
            "SELECT * FROM orders WHERE id = ? AND company_id = ?",
            (clean_id, company_id),
            one=True,
        )
        if not order:
            return {
                "success": False,
                "status": "BLOCKED",
                "order_id": order_id,
                "reason": f"Order {order_id} not found.",
                "message": f"Order {order_id} not found.",
                "refund_issued": False,
                "verification_status": "FAILED",
            }

        if order["status"] not in ["PENDING", "PROCESSING"]:
            return {
                "success": False,
                "status": "BLOCKED",
                "order_id": order_id,
                "reason": (
                    f"Order status is '{order['status']}'. Only PENDING or"
                    " PROCESSING orders can be cancelled."
                ),
                "message": f"Order {order_id} cannot be cancelled (status: {order['status']}).",
                "refund_issued": False,
                "verification_status": "FAILED",
            }

        items = query_db(
            "SELECT product_name FROM order_items WHERE order_id = ?",
            (clean_id,),
        )
        product_names = (
            ", ".join([item["product_name"] for item in items])
            if items
            else "Item"
        )

        execute_db(
            "UPDATE orders SET status = 'CANCELLED' WHERE id = ?",
            (clean_id,),
        )

        total = float(order.get("total") or 0.0)
        refund_issued = total > 0.0

        from integrations.models import CancelOrderResult
        res = CancelOrderResult(
            success=True,
            status="CANCELLED",
            order_id=clean_id,
            product_name=product_names,
            reason=reason or "CUSTOMER",
            message=f"Order {order_id} for '{product_names}' has been successfully CANCELLED.",
            refund_issued=refund_issued,
            refund_amount=total if refund_issued else 0.0,
            verification_status="VERIFIED",
        )
        return res.model_dump()

    def request_return(
        self, order_id: str, refund_amount: float, company_id: str
    ) -> Dict[str, Any]:
        """Process a return/refund request against SQLite company rules."""
        clean_id = order_id.upper()
        order = query_db(
            "SELECT * FROM orders WHERE id = ? AND company_id = ?",
            (clean_id, company_id),
            one=True,
        )
        if not order:
            return {"status": "BLOCKED", "reason": "Order not found."}

        rules = get_company_rules(company_id)
        max_allowed = rules["max_auto_refund_amount"] if rules else 100.0

        if refund_amount > max_allowed:
            return {
                "status": "REQUIRES_HUMAN_REVIEW",
                "reason": (
                    f"Refund amount (${refund_amount}) exceeds maximum automated"
                    f" limit of ${max_allowed}. Escalation required."
                ),
            }

        execute_db(
            "UPDATE orders SET status = 'REFUND_APPROVED' WHERE id = ?",
            (clean_id,),
        )
        return {
            "status": "APPROVED",
            "message": (
                f"Return approved for order {order_id}. Refund of"
                f" ${refund_amount} initiated successfully."
            ),
        }

    def check_proactive_notifications(
        self, customer_id: str, company_id: str
    ) -> Optional[str]:
        """Check for proactive alerts in SQLite."""
        alerts = query_db(
            "SELECT * FROM orders WHERE customer_id = ? AND company_id = ? AND status = 'PROACTIVE_ALERT'",
            (customer_id, company_id),
        )
        if alerts:
            order = alerts[0]
            return (
                f"PROACTIVE NOTICE DETECTED: Order {order['id']} is currently"
                " flagged for a potential carrier delay. An automated resolution"
                " action is queued."
            )
        return None

    def escalate_to_human(
        self, order_id: str, reason: str, company_id: str
    ) -> Dict[str, Any]:
        """Generate human escalation ticket."""
        clean_id = order_id.upper()
        ticket_id = f"TICK-{clean_id}"
        return {
            "status": "ESCALATED",
            "ticket_id": ticket_id,
            "message": (
                f"Support ticket {ticket_id} created for human agent review."
                f" Reason: {reason}"
            ),
        }

    def get_ticket_details(
        self, ticket_id: str, company_id: str
    ) -> Dict[str, Any]:
        """Retrieve ticket audit details from SQLite."""
        clean_ticket = ticket_id.upper().strip()
        log_entry = query_db(
            "SELECT * FROM audit_logs WHERE agent_response LIKE ? AND company_id = ?",
            (f"%{clean_ticket}%", company_id),
            one=True,
        )

        order_id = clean_ticket.replace("TICK-", "")
        order = query_db(
            "SELECT id, status FROM orders WHERE id = ? AND company_id = ?",
            (order_id, company_id),
            one=True,
        )

        if not log_entry and not order:
            return {"error": f"Ticket {ticket_id} not found."}

        return {
            "ticket_id": clean_ticket,
            "status": "OPEN_IN_REVIEW",
            "associated_order": order_id if order else "N/A",
            "details": log_entry["agent_response"] if log_entry else "Escalated for human agent review.",
            "created_at": log_entry["timestamp"] if log_entry else "Recently created",
        }
