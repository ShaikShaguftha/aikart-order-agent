import json
from langchain_core.tools import tool
from database import query_db, execute_db

@tool
def get_customer_details(identifier: str) -> str:
    """Find customer details by Customer ID, Email, or Phone number."""
    try:
        res = query_db(
            "SELECT * FROM customers WHERE id = ? OR email = ? OR phone = ?", 
            (identifier, identifier, identifier), 
            one=True
        )
        print(f"[TOOL EXECUTED]: get_customer_details({identifier})", flush=True)
        return json.dumps(res) if res else json.dumps({"error": "Customer not found."})
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})

@tool
def get_order_details(order_id: str) -> str:
    """Retrieve order status, items, total, and timestamp."""
    try:
        order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
        if not order:
            return json.dumps({"error": "Order not found."})
        items = query_db("SELECT product_name, quantity, price FROM order_items WHERE order_id = ?", (order_id,))
        order["items"] = items
        print(f"[TOOL EXECUTED]: get_order_details({order_id})", flush=True)
        return json.dumps(order)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})

@tool
def get_shipment_status(order_id: str) -> str:
    """Retrieve courier tracking details, ETA, and delay status."""
    try:
        shipment = query_db("SELECT * FROM shipments WHERE order_id = ?", (order_id,), one=True)
        print(f"[TOOL EXECUTED]: get_shipment_status({order_id})", flush=True)
        return json.dumps(shipment) if shipment else json.dumps({"message": "No active shipment record found."})
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})

@tool
def cancel_order_tool(order_id: str) -> str:
    """Attempts to cancel an order using deterministic policy rules."""
    try:
        order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
        if not order:
            return json.dumps({"status": "BLOCKED", "reason": f"Order {order_id} not found."})

        if order["status"] not in ["PENDING", "PROCESSING"]:
            return json.dumps({"status": "BLOCKED", "reason": f"Order status is '{order['status']}'. Only PENDING or PROCESSING orders can be cancelled."})

        execute_db("UPDATE orders SET status = 'CANCELLED' WHERE id = ?", (order_id,))
        execute_db("INSERT INTO cases (customer_id, order_id, intent, status, resolution) VALUES (?, ?, 'CANCEL', 'RESOLVED', 'Auto-cancelled by Agent')", (order["customer_id"], order_id))
        
        print(f"[TOOL EXECUTED]: cancel_order_tool({order_id}) -> CANCELLED", flush=True)
        return json.dumps({"status": "SUCCESS", "message": f"Order {order_id} has been successfully CANCELLED in the database."})
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})

@tool
def request_return_tool(order_id: str, refund_amount: float) -> str:
    """Processes a return request."""
    try:
        order = query_db("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
        if not order:
            return json.dumps({"status": "BLOCKED", "reason": "Order not found."})

        if refund_amount > 100.0:
            execute_db("INSERT INTO cases (customer_id, order_id, intent, status, resolution) VALUES (?, ?, 'RETURN', 'ESCALATED', 'Exceeds $100 limit')", (order["customer_id"], order_id))
            return json.dumps({"status": "ESCALATED_TO_HUMAN", "reason": f"Refund amount (${refund_amount}) exceeds $100 limit."})

        execute_db("INSERT INTO cases (customer_id, order_id, intent, status, resolution) VALUES (?, ?, 'RETURN', 'RESOLVED', 'Auto-approved return')", (order["customer_id"], order_id))
        print(f"[TOOL EXECUTED]: request_return_tool({order_id}, ${refund_amount})", flush=True)
        return json.dumps({"status": "APPROVED", "message": f"Return approved for order {order_id}. Refund of ${refund_amount} initiated."})
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})

@tool
def escalate_to_human_tool(customer_id: str, order_id: str, issue_description: str) -> str:
    """Escalates cases to human support queue."""
    try:
        case_id = execute_db("INSERT INTO cases (customer_id, order_id, intent, status, resolution) VALUES (?, ?, 'HUMAN_ESCALATION', 'ESCALATED', ?)", (customer_id, order_id, issue_description))
        print(f"[TOOL EXECUTED]: escalate_to_human_tool Case #{case_id}", flush=True)
        return json.dumps({"status": "ESCALATED", "case_id": case_id, "message": "Ticket queued for human support team."})
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})

@tool
def check_proactive_notifications(customer_id: str) -> str:
    """Scans the system for automated proactive flags, predicted delays, or stock exceptions for a customer."""
    try:
        alerts = query_db(
            "SELECT * FROM orders WHERE customer_id = ? AND status = 'PROACTIVE_ALERT'", 
            (customer_id,)
        )
        if alerts:
            order = alerts[0]
            return (
                f"PROACTIVE NOTICE DETECTED: Order {order['id']} is currently flagged for a potential carrier delay. "
                f"An automated notification has been generated to offer a free express reshipment or compensation voucher."
            )
        return "No proactive alerts or exceptions found for this customer account."
    except Exception as e:
        return json.dumps({"error": str(e)})

tools = [
    get_customer_details, 
    get_order_details, 
    get_shipment_status, 
    cancel_order_tool, 
    request_return_tool, 
    escalate_to_human_tool, 
    check_proactive_notifications
]

tools_dict = {t.name: t for t in tools}