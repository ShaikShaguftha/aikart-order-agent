import json
from database import execute_db, get_company_rules, query_db
from langchain_core.tools import tool

DEFAULT_COMPANY_ID = "COMP-ALPHA"


@tool
def get_customer_details(
    identifier: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Find customer order history or records by Customer ID, Email, or Phone number."""
    try:
        res = query_db(
            "SELECT * FROM orders WHERE (customer_id = ? OR id = ?) AND company_id = ?",
            (identifier, identifier, company_id),
        )
        print(
            f"[TOOL EXECUTED]: get_customer_details({identifier})", flush=True
        )
        return (
            json.dumps(res)
            if res
            else json.dumps({"error": "Customer records not found."})
        )
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def get_order_details(
    order_id: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Retrieve order status, line items, total, and timestamp for a specific company."""
    try:
        order = query_db(
            "SELECT * FROM orders WHERE id = ? AND company_id = ?",
            (order_id.upper(), company_id),
            one=True,
        )
        if not order:
            return json.dumps({"error": f"Order {order_id} not found."})

        items = query_db(
            "SELECT product_name, quantity, price FROM order_items WHERE order_id = ?",
            (order_id.upper(),),
        )
        order["items"] = items
        print(f"[TOOL EXECUTED]: get_order_details({order_id})", flush=True)
        return json.dumps(order)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def get_shipment_status(
    order_id: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Retrieve courier tracking details, status, and tracking number."""
    try:
        shipment = query_db(
            """
            SELECT s.* FROM shipments s
            JOIN orders o ON s.order_id = o.id
            WHERE s.order_id = ? AND o.company_id = ?
            """,
            (order_id.upper(), company_id),
            one=True,
        )
        print(f"[TOOL EXECUTED]: get_shipment_status({order_id})", flush=True)
        return (
            json.dumps(shipment)
            if shipment
            else json.dumps(
                {"message": "No active shipment record found for this order."}
            )
        )
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def cancel_order_tool(
    order_id: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Attempts to cancel an order using deterministic policy rules."""
    try:
        order = query_db(
            "SELECT * FROM orders WHERE id = ? AND company_id = ?",
            (order_id.upper(), company_id),
            one=True,
        )
        if not order:
            return json.dumps(
                {"status": "BLOCKED", "reason": f"Order {order_id} not found."}
            )

        if order["status"] not in ["PENDING", "PROCESSING"]:
            return json.dumps({
                "status": "BLOCKED",
                "reason": (
                    f"Order status is '{order['status']}'. Only PENDING or"
                    " PROCESSING orders can be cancelled."
                ),
            })

        items = query_db(
            "SELECT product_name FROM order_items WHERE order_id = ?",
            (order_id.upper(),),
        )
        product_names = (
            ", ".join([item["product_name"] for item in items])
            if items
            else "Item"
        )

        execute_db(
            "UPDATE orders SET status = 'CANCELLED' WHERE id = ?",
            (order_id.upper(),),
        )

        print(
            f"[TOOL EXECUTED]: cancel_order_tool({order_id}) -> STATUS UPDATED"
            " TO CANCELLED",
            flush=True,
        )
        return json.dumps({
            "status": "SUCCESS",
            "product_name": product_names,
            "message": (
                f"Order {order_id} for '{product_names}' has been successfully"
                " CANCELLED."
            ),
        })
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})


@tool
def request_return_tool(
    order_id: str,
    refund_amount: float,
    company_id: str = DEFAULT_COMPANY_ID,
) -> str:
    """Processes a return or refund request against dynamic company auto-refund rules."""
    try:
        order = query_db(
            "SELECT * FROM orders WHERE id = ? AND company_id = ?",
            (order_id.upper(), company_id),
            one=True,
        )
        if not order:
            return json.dumps(
                {"status": "BLOCKED", "reason": "Order not found."}
            )

        rules = get_company_rules(company_id)
        max_allowed = rules["max_auto_refund_amount"] if rules else 100.0

        if refund_amount > max_allowed:
            return json.dumps({
                "status": "REQUIRES_HUMAN_REVIEW",
                "reason": (
                    f"Refund amount (${refund_amount}) exceeds maximum automated"
                    f" limit of ${max_allowed}. Escalation required."
                ),
            })

        execute_db(
            "UPDATE orders SET status = 'REFUND_APPROVED' WHERE id = ?",
            (order_id.upper(),),
        )
        print(
            f"[TOOL EXECUTED]: request_return_tool({order_id},"
            f" ${refund_amount}) -> REFUND APPROVED",
            flush=True,
        )
        return json.dumps({
            "status": "APPROVED",
            "message": (
                f"Return approved for order {order_id}. Refund of"
                f" ${refund_amount} initiated successfully."
            ),
        })
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})


@tool
def check_proactive_notifications(
    customer_id: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Scans system for proactive delay flags or carrier exceptions for a customer account."""
    try:
        alerts = query_db(
            "SELECT * FROM orders WHERE customer_id = ? AND company_id = ? AND"
            " status = 'PROACTIVE_ALERT'",
            (customer_id, company_id),
        )
        if alerts:
            order = alerts[0]
            return (
                f"PROACTIVE NOTICE DETECTED: Order {order['id']} is currently"
                " flagged for a potential carrier delay. An automated resolution"
                " action is queued."
            )
        return (
            "No proactive alerts or exceptions found for this customer account."
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def escalate_to_human_tool(
    order_id: str, reason: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Escalates complex or policy-exceeding cases by creating a human support ticket in the CRM."""
    try:
        ticket_id = f"TICK-{order_id.upper()}"
        print(
            f"[TOOL EXECUTED]: escalate_to_human_tool({order_id}) -> ESCALATED TO CRM ({ticket_id})",
            flush=True,
        )
        return json.dumps({
            "status": "ESCALATED",
            "ticket_id": ticket_id,
            "message": (
                f"Support ticket {ticket_id} created for human agent review."
                f" Reason: {reason}"
            ),
        })
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})


@tool
def get_ticket_details(
    ticket_id: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Retrieves status and audit logs for a human escalation ticket (e.g., TICK-ORD-5002)."""
    try:
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
            return json.dumps({"error": f"Ticket {ticket_id} not found."})

        result = {
            "ticket_id": clean_ticket,
            "status": "OPEN_IN_REVIEW",
            "associated_order": order_id if order else "N/A",
            "details": log_entry["agent_response"] if log_entry else "Escalated for human agent review.",
            "created_at": log_entry["timestamp"] if log_entry else "Recently created"
        }
        print(f"[TOOL EXECUTED]: get_ticket_details({ticket_id})", flush=True)
        return json.dumps(result)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


tools = [
    get_customer_details,
    get_order_details,
    get_shipment_status,
    cancel_order_tool,
    request_return_tool,
    check_proactive_notifications,
    escalate_to_human_tool,
    get_ticket_details,
]

tools_dict = {t.name: t for t in tools}