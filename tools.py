import json
from typing import Literal, Optional

from pydantic import Field
from langchain_core.tools import tool
from integrations.gateway import gateway
from integrations.errors import IntegrationError
from integrations.models import CustomerIdentity

DEFAULT_COMPANY_ID = "COMP-ALPHA"


def _log_found(tool_name: str, result) -> None:
    """Safe diagnostic: whether the provider returned usable data."""
    if isinstance(result, dict) and "error" in result:
        found = f"NO (error: {result['error']})"
    elif isinstance(result, list) and result and all(isinstance(r, dict) and "error" in r for r in result):
        found = f"NO (error: {result[0]['error']})"
    elif isinstance(result, dict) and ("orders" in result or "order" in result):
        found = "YES" if (result.get("orders") or result.get("order")) else "NO"
    else:
        found = "YES" if result else "NO"
    print(f"[TOOL RESULT] {tool_name} | Provider response found: {found}", flush=True)


@tool
def get_customer_details(
    identifier: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Find customer order history or records by Customer ID, Email, or Phone number."""
    try:
        res = gateway.get_customer_details(identifier, company_id)
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
    """Look up ONE order by the exact order identifier the customer gave (e.g. #1003, 1003, ORD-5001).
    Use only when the customer explicitly provides an order number; otherwise use get_my_orders / get_my_latest_order."""
    try:
        order = gateway.get_order_details(order_id, company_id)
        _log_found("get_order_details", order)
        if not order:
            return json.dumps({"error": f"Order {order_id} not found."})
        print(f"[TOOL EXECUTED]: get_order_details({order_id})", flush=True)
        return json.dumps(order)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


IDENTITY_ARGS_DOC = (
    "Identify the customer with the email address or phone number THEY provided "
    "(leave empty to use the identity already known for this session). "
    "`name` is only an optional cross-check and never identifies a customer on its own. "
    "`customer_id` is filled automatically from an authenticated session; never fill it from chat."
)


def _identity(email, phone, name, customer_id, authenticated) -> CustomerIdentity:
    return CustomerIdentity(
        email=email or None,
        phone=phone or None,
        name=name or None,
        customer_id=customer_id or None,
        authenticated=bool(authenticated and customer_id),
    )


@tool
def get_my_orders(
    email: str = "",
    phone: str = "",
    name: str = "",
    limit: int = 5,
    customer_id: str = "",
    company_id: str = DEFAULT_COMPANY_ID,
    authenticated: bool = False,
) -> str:
    """List the customer's OWN orders, newest first, with items, totals, payment and fulfillment status.
    Use for: "show me my orders", "my recent orders", "what did I buy", "my recent purchases".
    No order ID is needed. If the result has code IDENTITY_REQUIRED, ask for the customer's email or phone.
    """
    try:
        limit = max(1, min(int(limit), 20))
        res = gateway.get_recent_orders(
            _identity(email, phone, name, customer_id, authenticated), company_id, limit
        )
        _log_found("get_my_orders", res)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def get_my_latest_order(
    email: str = "",
    phone: str = "",
    name: str = "",
    customer_id: str = "",
    company_id: str = DEFAULT_COMPANY_ID,
    authenticated: bool = False,
) -> str:
    """Fresh, live details of the customer's MOST RECENT order: status, payment, fulfillment, shipment/tracking and items.
    Use for: "where is my latest order", "status of my latest order", "has my order shipped", "is my order paid"
    when the customer did not give an order number. No order ID is needed.
    If the result has code IDENTITY_REQUIRED, ask for the customer's email or phone.
    """
    try:
        res = gateway.get_latest_order(
            _identity(email, phone, name, customer_id, authenticated), company_id
        )
        _log_found("get_my_latest_order", res)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


for _t in (get_my_orders, get_my_latest_order):
    _t.description += "\n" + IDENTITY_ARGS_DOC


@tool
def get_product_details(
    product_query: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Look up a product in the store catalog by product name, ID, SKU or search text (price, variants, status, description)."""
    try:
        product = gateway.get_product(product_query, company_id)
        _log_found("get_product_details", product)
        print(f"[TOOL EXECUTED]: get_product_details({product_query})", flush=True)
        return json.dumps(product)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def get_shipment_status(
    order_id: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Live courier tracking details for ONE order, by the exact order identifier the customer gave."""
    try:
        shipment = gateway.get_shipment_status(order_id, company_id)
        _log_found("get_shipment_status", shipment)
        if isinstance(shipment, dict) and "error" in shipment:
            return json.dumps(shipment)
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
def prepare_cancellation_tool(
    order_id: str,
    reason: str = "CUSTOMER",
    company_id: str = DEFAULT_COMPANY_ID,
    session_id: str = "default",
) -> str:
    """Pre-checks cancellation policy eligibility and stages a pending confirmation action for an order."""
    try:
        res = gateway.prepare_order_cancellation(
            session_id=session_id,
            company_id=company_id,
            order_id=order_id,
            reason=reason,
        )
        print(f"[TOOL EXECUTED]: prepare_cancellation_tool({order_id}) -> {res.get('status')}", flush=True)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})


@tool
def cancel_order_tool(
    order_id: str,
    confirmed: bool = False,
    reason: str = "CUSTOMER",
    company_id: str = DEFAULT_COMPANY_ID,
    session_id: str = "default",
) -> str:
    """
    Attempts to cancel an order using safe two-step action confirmation framework.
    When confirmed=False, policy is checked and action is staged for user confirmation.
    When confirmed=True (after explicit user confirmation), cancellation is executed on provider API and verified.
    """
    try:
        res = gateway.cancel_order(
            order_id=order_id,
            company_id=company_id,
            reason=reason,
            session_id=session_id,
            confirmed=confirmed,
        )
        print(
            f"[TOOL EXECUTED]: cancel_order_tool({order_id}, confirmed={confirmed}) -> {res.get('status')}",
            flush=True,
        )
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})


@tool
def request_return_tool(
    order_id: str,
    refund_amount: float,
    reason: str = "",
    company_id: str = DEFAULT_COMPANY_ID,
    session_id: str = "default",
) -> str:
    """Submits a return/refund request for an order. The merchant's return policy (window,
    refundable balance, auto-approval limit) is enforced; large requests go to human review.
    Only call after the customer explicitly asks to return an item."""
    try:
        res = gateway.request_return(
            order_id, refund_amount, company_id, reason=reason or None, session_id=session_id
        )
        if res.get("status") == "APPROVED":
            print(
                f"[TOOL EXECUTED]: request_return_tool({order_id},"
                f" ${refund_amount}) -> REFUND APPROVED",
                flush=True,
            )
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})


@tool
def get_return_policy(company_id: str = DEFAULT_COMPANY_ID) -> str:
    """The store's return policy: return window, conditions, and how refunds are processed."""
    try:
        return json.dumps(gateway.get_return_policy(company_id))
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def check_return_eligibility(
    order_id: str, refund_amount: float = 0.0, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Read-only check whether an order can be returned under the store policy (live order data).
    Pass refund_amount when the customer named an amount; 0 means just check eligibility."""
    try:
        res = gateway.check_return_eligibility(order_id, company_id, refund_amount or None)
        _log_found("check_return_eligibility", res)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def get_refund_status(order_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """Live refund status for an order: refunds already issued and any open return request."""
    try:
        res = gateway.get_refund_status(order_id, company_id)
        _log_found("get_refund_status", res)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def check_inventory(product_query: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """Live stock availability for a product (and its variants/sizes) by name, SKU or ID."""
    try:
        res = gateway.get_inventory(product_query, company_id)
        _log_found("check_inventory", res)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def check_proactive_notifications(
    customer_id: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Scans system for proactive delay flags or carrier exceptions for a customer account."""
    try:
        notice = gateway.check_proactive_notifications(customer_id, company_id)
        if notice:
            return notice
        return (
            "No proactive alerts or exceptions found for this customer account."
        )
    except Exception as e:
        return json.dumps({"error": str(e)})


@tool
def escalate_to_human_tool(
    order_id: str,
    reason: str,
    company_id: str = DEFAULT_COMPANY_ID,
    session_id: str = "default",
) -> str:
    """Creates a human support ticket for complex, policy-exceeding or unresolved cases."""
    try:
        res = gateway.escalate_to_human(order_id, reason, company_id, session_id=session_id)
        print(
            f"[TOOL EXECUTED]: escalate_to_human_tool({order_id}) -> ESCALATED TO CRM ({res.get('ticket_id')})",
            flush=True,
        )
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"status": "ERROR", "message": str(e)})


@tool
def get_ticket_details(
    ticket_id: str, company_id: str = DEFAULT_COMPANY_ID
) -> str:
    """Retrieves status and audit logs for a human escalation ticket (e.g., TICK-ORD-5002)."""
    try:
        res = gateway.get_ticket_details(ticket_id, company_id)
        print(f"[TOOL EXECUTED]: get_ticket_details({ticket_id})", flush=True)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


# Customer-facing tool set. get_customer_details is intentionally not exposed: it accepts
# an unverified identifier and bypasses the identity-first rules in the gateway.
@tool
def get_order_payment_transactions(order_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Payment transactions the store recorded on ONE order: kind, status, gateway, amount, currency,
    dates, and - only when the gateway is mapped deterministically - the payment provider and its payment
    reference (mapping_status). Use for "show the payment transaction details for order #1008"."""
    try:
        res = gateway.get_order_transactions(order_id, company_id)
        _log_found("get_order_payment_transactions", res)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


@tool
def verify_order_payment(order_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Verify ONE order's payment with the payment provider that processed it (e.g. Razorpay). The
    provider and payment ID are taken only from the order's own transaction record, never from chat. Returns a
    verification status per payment. Does not refund, capture or change anything."""
    try:
        res = gateway.verify_order_payment(order_id, company_id)
        _log_found("verify_order_payment", res)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {e}", flush=True)
        return json.dumps({"error": str(e)})


tools = [
    get_my_orders,
    get_my_latest_order,
    get_order_details,
    get_product_details,
    get_shipment_status,
    prepare_cancellation_tool,
    cancel_order_tool,
    get_return_policy,
    check_return_eligibility,
    request_return_tool,
    get_refund_status,
    get_order_payment_transactions,
    verify_order_payment,
    check_inventory,
    check_proactive_notifications,
    escalate_to_human_tool,
    get_ticket_details,
]

tools_dict = {t.name: t for t in tools}

# ---- Freshdesk helpdesk tools --------------------------------------------------------
# Bound to the LLM only for tenants with a helpdesk integration (see agent.py), so the
# tool set of every other tenant is unchanged. Customer identity (email/phone) is
# injected by the agent from the session; ticket IDs must come from the customer or a
# previous provider result (enforced in agent.py); statuses/priorities are enums.

FRESHDESK_IDENTITY_DOC = (
    "Identify the customer with the email address or phone number THEY provided "
    "(leave empty to use the identity already known for this session). "
    "`customer_id`/`authenticated` are set automatically; never fill them."
)
TicketStatus = Literal["open", "pending", "resolved", "closed"]
TicketPriority = Literal["low", "medium", "high", "urgent"]


def _freshdesk_call(name: str, fn, *args, **kwargs) -> str:
    try:
        res = fn(*args, **kwargs)
        _log_found(name, res)
        return json.dumps(res)
    except Exception as e:
        print(f"[TOOL ERROR]: {name}: {type(e).__name__}", flush=True)
        message = e.message if isinstance(e, IntegrationError) else "The helpdesk request could not be completed."
        return json.dumps({"error": message, "code": getattr(e, "code", "HELPDESK_ERROR")})


@tool
def freshdesk_get_contact(
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False, company_id: str = DEFAULT_COMPANY_ID,
) -> str:
    """Find the customer's own support (Freshdesk) contact record."""
    return _freshdesk_call("freshdesk_get_contact", gateway.helpdesk_get_contact,
                           _identity(email, phone, name, customer_id, authenticated), company_id)


@tool
def freshdesk_get_my_tickets(
    email: str = "", phone: str = "", name: str = "", limit: int = Field(default=10, ge=1, le=50),
    customer_id: str = "", authenticated: bool = False, company_id: str = DEFAULT_COMPANY_ID,
) -> str:
    """List the customer's OWN support tickets (newest first) with status and priority.
    Use for "my support tickets", "status of my complaint/ticket"."""
    return _freshdesk_call("freshdesk_get_my_tickets", gateway.helpdesk_get_customer_tickets,
                           _identity(email, phone, name, customer_id, authenticated), company_id, limit)


@tool
def freshdesk_get_ticket(
    ticket_id: int = Field(..., gt=0, description="Ticket number given by the customer or returned by a previous tool."),
    include_conversations: bool = False,
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False, company_id: str = DEFAULT_COMPANY_ID,
) -> str:
    """Details of ONE of the customer's own support tickets; include_conversations=True adds the
    public conversation (agent-only notes are never shown)."""
    return _freshdesk_call("freshdesk_get_ticket", gateway.helpdesk_get_ticket,
                           _identity(email, phone, name, customer_id, authenticated), ticket_id, company_id,
                           include_conversations)


@tool
def freshdesk_get_ticket_fields(company_id: str = DEFAULT_COMPANY_ID) -> str:
    """The helpdesk's ticket fields (names, types, allowed choices)."""
    return _freshdesk_call("freshdesk_get_ticket_fields", gateway.helpdesk_get_ticket_fields, company_id)


@tool
def freshdesk_create_ticket(
    subject: str = Field(..., min_length=3, max_length=255),
    description: str = Field(..., min_length=5, max_length=5000),
    priority: TicketPriority = "medium",
    order_id: str = "",
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False,
    company_id: str = DEFAULT_COMPANY_ID, session_id: str = "default",
) -> str:
    """Escalate to HUMAN SUPPORT by opening a real helpdesk ticket (or reusing the customer's open
    ticket for the same order - no duplicates). Pass the related order number exactly as given.
    The result's ticket_reference (e.g. "#7") is the ONLY ticket number you may tell the customer."""
    return _freshdesk_call("freshdesk_create_ticket", gateway.helpdesk_create_ticket,
                           _identity(email, phone, name, customer_id, authenticated),
                           subject, description, priority, company_id,
                           order_id=order_id or None, session_id=session_id)


@tool
def freshdesk_update_ticket_status(
    ticket_id: int = Field(..., gt=0),
    status: TicketStatus = Field(...),
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False,
    company_id: str = DEFAULT_COMPANY_ID, session_id: str = "default",
) -> str:
    """Change the status of one of the customer's own tickets (e.g. 'resolved' when the customer
    confirms the issue is fixed, 'open' to reopen). Only when the customer asks."""
    return _freshdesk_call("freshdesk_update_ticket_status", gateway.helpdesk_update_ticket_status,
                           _identity(email, phone, name, customer_id, authenticated),
                           ticket_id, status, company_id, session_id)


@tool
def freshdesk_update_ticket_priority(
    ticket_id: int = Field(..., gt=0),
    priority: TicketPriority = Field(...),
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False,
    company_id: str = DEFAULT_COMPANY_ID, session_id: str = "default",
) -> str:
    """Change the priority of one of the customer's own tickets (e.g. 'urgent' for time-critical issues)."""
    return _freshdesk_call("freshdesk_update_ticket_priority", gateway.helpdesk_update_ticket_priority,
                           _identity(email, phone, name, customer_id, authenticated),
                           ticket_id, priority, company_id, session_id)


@tool
def freshdesk_add_internal_note(
    ticket_id: int = Field(..., gt=0),
    note: str = Field(..., min_length=3, max_length=5000),
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False,
    company_id: str = DEFAULT_COMPANY_ID, session_id: str = "default",
) -> str:
    """Add a PRIVATE note (visible to support agents only, never to the customer) to one of the
    customer's tickets, e.g. a summary of what was verified in this chat."""
    return _freshdesk_call("freshdesk_add_internal_note", gateway.helpdesk_add_internal_note,
                           _identity(email, phone, name, customer_id, authenticated),
                           ticket_id, note, company_id, session_id)


@tool
def freshdesk_get_complaint_status(
    ticket_id: Optional[int] = Field(default=None, gt=0, description="Only if the customer gave a ticket number."),
    order_id: str = "",
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False, company_id: str = DEFAULT_COMPANY_ID,
) -> str:
    """LIVE status of the customer's complaint: the relevant support ticket (fresh from the helpdesk),
    its latest public message, and the linked order's current state (fresh from the store).
    Use EVERY time the customer asks about a complaint/ticket ("any update?", "is it resolved?")."""
    return _freshdesk_call("freshdesk_get_complaint_status", gateway.helpdesk_complaint_status,
                           _identity(email, phone, name, customer_id, authenticated), company_id,
                           ticket_id=ticket_id, order_id=order_id or None)


@tool
def freshdesk_sync_ticket_with_order(
    ticket_id: Optional[int] = Field(default=None, gt=0),
    order_id: str = "",
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False,
    company_id: str = DEFAULT_COMPANY_ID, session_id: str = "default",
) -> str:
    """Re-check the customer's open ticket(s) for an order against the order's current state. Luintix
    only resolves a ticket automatically when it was a cancellation request and the order is now cancelled;
    refund, 'why cancelled', 'still need the product' and other complaints stay open for human support."""
    return _freshdesk_call("freshdesk_sync_ticket_with_order", gateway.helpdesk_sync_ticket_with_order,
                           _identity(email, phone, name, customer_id, authenticated), company_id,
                           ticket_id=ticket_id, order_id=order_id or None, session_id=session_id)


for _t in (freshdesk_get_contact, freshdesk_get_my_tickets, freshdesk_get_ticket, freshdesk_create_ticket,
           freshdesk_get_complaint_status, freshdesk_sync_ticket_with_order,
           freshdesk_update_ticket_status, freshdesk_update_ticket_priority, freshdesk_add_internal_note):
    _t.description += "\n" + FRESHDESK_IDENTITY_DOC

freshdesk_tools = [
    freshdesk_get_contact,
    freshdesk_get_my_tickets,
    freshdesk_get_ticket,
    freshdesk_get_ticket_fields,
    freshdesk_create_ticket,
    freshdesk_update_ticket_status,
    freshdesk_update_ticket_priority,
    freshdesk_add_internal_note,
    freshdesk_get_complaint_status,
    freshdesk_sync_ticket_with_order,
]
freshdesk_tools_dict = {t.name: t for t in freshdesk_tools}


# ---- Shippo shipment-tracking tools --------------------------------------------------
# Bound to the LLM only for tenants with a shipping integration (see agent.py).
# Order numbers are resolved through the tenant's STORE (ownership verified there);
# tracking numbers come from the store's fulfillment or from the customer, never invented.

SHIPPO_IDENTITY_DOC = (
    "Prefer order_id (the store order number exactly as the customer gave it). Use carrier + tracking_number "
    "only when the customer gave a tracking number. `email`/`phone` identify the customer (leave empty to use the "
    "session identity); `customer_id`/`authenticated` are set automatically."
)


def _shippo_tracking(include_history: bool, order_id, carrier, tracking_number, identity, company_id) -> dict:
    if order_id:
        return gateway.shipping_track_order(identity, order_id, company_id, include_history=include_history)
    if carrier and tracking_number:
        return gateway.shipping_get_tracking(company_id, carrier, tracking_number, include_history=include_history)
    return {"error": "An order number, or a carrier and tracking number, is required.", "code": "INVALID_ARGUMENT"}


@tool
def get_tracking_status(
    order_id: str = "", carrier: str = "", tracking_number: str = "",
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False, company_id: str = DEFAULT_COMPANY_ID,
) -> str:
    """LIVE shipment tracking from the store's tracking provider (Shippo, or Delhivery for Delhivery AWBs):
    current status, substatus, ETA, current location and whether action is required.
    Use for "where is my order/package", "when will it arrive"."""
    return _freshdesk_call("get_tracking_status", _shippo_tracking, False, order_id, carrier, tracking_number,
                           _identity(email, phone, name, customer_id, authenticated), company_id)


@tool
def get_tracking_history(
    order_id: str = "", carrier: str = "", tracking_number: str = "",
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False, company_id: str = DEFAULT_COMPANY_ID,
) -> str:
    """LIVE full tracking history (every carrier scan with date, status and location) for a shipment."""
    return _freshdesk_call("get_tracking_history", _shippo_tracking, True, order_id, carrier, tracking_number,
                           _identity(email, phone, name, customer_id, authenticated), company_id)


@tool
def register_tracking(
    order_id: str = "", carrier: str = "", tracking_number: str = "",
    email: str = "", phone: str = "", name: str = "",
    customer_id: str = "", authenticated: bool = False,
    company_id: str = DEFAULT_COMPANY_ID, session_id: str = "default",
) -> str:
    """Subscribe a shipment to automatic tracking updates (registered at most once; repeat calls are safe
    and report ALREADY_REGISTERED). Only when the customer asks to be kept updated about delivery."""
    def run():
        if order_id:
            return gateway.shipping_register_order_tracking(
                _identity(email, phone, name, customer_id, authenticated), order_id, company_id, session_id=session_id)
        if carrier and tracking_number:
            return gateway.shipping_register_tracking(company_id, carrier, tracking_number, session_id=session_id)
        return {"error": "An order number, or a carrier and tracking number, is required.", "code": "INVALID_ARGUMENT"}
    return _freshdesk_call("register_tracking", run)


for _t in (get_tracking_status, get_tracking_history, register_tracking):
    _t.description += "\n" + SHIPPO_IDENTITY_DOC

shippo_tools = [get_tracking_status, get_tracking_history, register_tracking]
# Read-only tracking tools for tenants with a direct carrier (Delhivery) but no Shippo:
# no register_tracking, because Delhivery phase 1 performs no writes.
tracking_read_tools = [get_tracking_status, get_tracking_history]
shippo_tools_dict = {t.name: t for t in shippo_tools}


# ---- CRM tools (Zoho CRM, read-only) ------------------------------------------------
# Bound to the LLM only for CRM tenants (see agent.py). Record IDs must come from the user
# or from a previous CRM result (enforced in agent.py). There are no write tools.


@tool
def crm_search_customers(name: str = "", email: str = "", phone: str = "", company_id: str = DEFAULT_COMPANY_ID) -> str:
    """Search CRM contacts (customers) by name, email or phone. Use for "find customer Rahul",
    "find customer by email ...". Returns up to 10 matches with their CRM contact IDs."""
    # Keyword args go through a lambda: the wrapper's own first parameter is also called "name".
    return _freshdesk_call("crm_search_customers", lambda: gateway.crm_search_contacts(
        company_id, email=email or None, phone=phone or None, name=name or None))


@tool
def crm_get_customer(contact_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """Full CRM information for ONE contact: contact details plus the linked account/company.
    Use for "show customer details" / "show this customer's CRM information". contact_id must come
    from the user or a previous CRM search result."""
    return _freshdesk_call("crm_get_customer", gateway.crm_get_customer_profile, company_id, contact_id)


@tool
def crm_get_account(account_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """CRM account (company) by account ID taken from a previous CRM result or given by the user."""
    return _freshdesk_call("crm_get_account", gateway.crm_get_account, company_id, account_id)


@tool
def crm_get_deal(deal_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """CRM deal by deal ID given by the user or taken from a previous CRM result: name, amount, stage, closing date."""
    return _freshdesk_call("crm_get_deal", gateway.crm_get_deal, company_id, deal_id)


crm_tools = [crm_search_customers, crm_get_customer, crm_get_account, crm_get_deal]
crm_tools_dict = {t.name: t for t in crm_tools}


# ---- Shadowfax tools (logistics, read-only) -------------------------------------------


@tool
def shadowfax_track_shipment(awb: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """[READ-ONLY] Track a shipment on Shadowfax using AWB or shipment identifier.
    Returns status, EDD, pickup/delivery dates, origin/destination, and latest tracking event."""
    return _freshdesk_call("shadowfax_track_shipment", gateway.shadowfax_track_shipment, company_id, awb)


@tool
def shadowfax_tracking_history(awb: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """[READ-ONLY] Retrieve full normalized tracking scan history for a Shadowfax AWB."""
    return _freshdesk_call("shadowfax_tracking_history", gateway.shadowfax_get_tracking_history, company_id, awb)


@tool
def shadowfax_check_serviceability(pickup_pincode: str, delivery_pincode: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """[READ-ONLY] Check pincode serviceability between pickup pincode and delivery pincode on Shadowfax."""
    return _freshdesk_call("shadowfax_check_serviceability", gateway.shadowfax_check_serviceability, company_id, pickup_pincode, delivery_pincode)


@tool
def shadowfax_track_reverse_pickup(reverse_awb: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """[READ-ONLY] Track a reverse pickup on Shadowfax by reverse AWB / pickup identifier."""
    return _freshdesk_call("shadowfax_track_reverse_pickup", gateway.shadowfax_track_reverse_pickup, company_id, reverse_awb)


shadowfax_tools = [
    shadowfax_track_shipment,
    shadowfax_tracking_history,
    shadowfax_check_serviceability,
    shadowfax_track_reverse_pickup,
]
shadowfax_tools_dict = {t.name: t for t in shadowfax_tools}



# ---- HubSpot CRM tools (read-only) --------------------------------------------------
# Bound to the LLM only for the HubSpot tenant (see agent.py). Record IDs must come from the
# user or a previous HubSpot result (enforced in agent.py). No write tools exist.
# Note: `company_id` is the tenant; a HubSpot company record is `hubspot_company_id`.


@tool
def hubspot_search_contacts(name: str = "", email: str = "", phone: str = "", company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Search HubSpot contacts (customers) by name, email or phone. Use for "find customer Rahul",
    "find Rahul in HubSpot", "find customer by email ...". Returns up to 10 matches with their HubSpot contact IDs."""
    return _freshdesk_call("hubspot_search_contacts", lambda: gateway.hubspot_search_contacts(
        company_id, email=email or None, phone=phone or None, name=name or None))


@tool
def hubspot_get_customer(contact_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Full HubSpot CRM details for ONE contact plus their primary company. Use for "show Rahul's CRM
    details". contact_id must come from the user or a previous HubSpot search result."""
    return _freshdesk_call("hubspot_get_customer", lambda: gateway.hubspot_get_customer_profile(company_id, contact_id))


@tool
def hubspot_get_company(hubspot_company_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. HubSpot company record by its HubSpot company ID (from the user or a previous HubSpot result)."""
    return _freshdesk_call("hubspot_get_company", lambda: gateway.hubspot_get_company(company_id, hubspot_company_id))


@tool
def hubspot_get_deal(deal_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. HubSpot deal by deal ID (from the user or a previous HubSpot result): name, amount, stage, close date."""
    return _freshdesk_call("hubspot_get_deal", lambda: gateway.hubspot_get_deal(company_id, deal_id))


@tool
def hubspot_get_ticket(ticket_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. HubSpot support ticket by ticket ID (from the user or a previous HubSpot result): subject, stage,
    priority, dates."""
    return _freshdesk_call("hubspot_get_ticket", lambda: gateway.hubspot_get_ticket(company_id, ticket_id))


hubspot_tools = [hubspot_search_contacts, hubspot_get_customer, hubspot_get_company, hubspot_get_deal, hubspot_get_ticket]
hubspot_tools_dict = {t.name: t for t in hubspot_tools}


# ---- Salesforce CRM tools (read-only) -----------------------------------------------
# Bound to the LLM only for the Salesforce tenant (see agent.py). Record IDs must come from the
# user or a previous Salesforce result (enforced in agent.py). No write tools exist.
# Note: `company_id` is the tenant; a Salesforce Account record is `account_id`.

@tool
def salesforce_search_contacts(name: str = "", email: str = "", phone: str = "", company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Search Salesforce contacts (customers) by name, email or phone. Use for "find customer Rahul",
    "find Rahul in Salesforce", "find customer by email ...". Returns up to 10 matches with their Salesforce contact IDs."""
    return _freshdesk_call("salesforce_search_contacts", lambda: gateway.salesforce_search_contacts(
        company_id, email=email or None, phone=phone or None, name=name or None))


@tool
def salesforce_get_customer(contact_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Full Salesforce CRM details for ONE contact plus their Account. Use for "show Rahul's CRM
    details". contact_id (starts with 003) must come from the user or a previous Salesforce search result."""
    return _freshdesk_call("salesforce_get_customer", lambda: gateway.salesforce_get_customer_profile(company_id, contact_id))


@tool
def salesforce_get_account(account_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Salesforce Account (company) by Account ID (starts with 001; from the user or a previous
    Salesforce result): name, website, phone."""
    return _freshdesk_call("salesforce_get_account", lambda: gateway.salesforce_get_account(company_id, account_id))


@tool
def salesforce_get_opportunity(opportunity_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Salesforce Opportunity (deal) by Opportunity ID (starts with 006; from the user or a previous
    Salesforce result): name, amount, stage, close date."""
    return _freshdesk_call("salesforce_get_opportunity",
                           lambda: gateway.salesforce_get_opportunity(company_id, opportunity_id))


@tool
def salesforce_get_case(case_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Salesforce support Case by Case ID (starts with 500) or Case Number (e.g. 00001026), from the
    user or a previous Salesforce result: subject, status, priority, dates."""
    return _freshdesk_call("salesforce_get_case", lambda: gateway.salesforce_get_case(company_id, case_id))


salesforce_tools = [salesforce_search_contacts, salesforce_get_customer, salesforce_get_account,
                    salesforce_get_opportunity, salesforce_get_case]
salesforce_tools_dict = {t.name: t for t in salesforce_tools}


# ---- Razorpay payment tools (read-only) ---------------------------------------------
# Bound to the LLM only for the Razorpay tenant (see agent.py). Payment / refund IDs must come
# from the user or a previous Razorpay result (enforced in agent.py). There is NO tool that
# creates, captures or refunds anything.

@tool
def razorpay_get_payment(payment_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Look up one Razorpay payment by payment ID (pay_...): amount, currency, status, method,
    order ID, amount refunded, refund status, created date. Cannot capture, cancel or refund."""
    return _freshdesk_call("razorpay_get_payment", lambda: gateway.razorpay_get_payment(company_id, payment_id))


@tool
def razorpay_get_payment_refunds(payment_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. List the refunds that ALREADY EXIST for one Razorpay payment (pay_...): refund ID, amount,
    status, speed, created date. Does not create a refund."""
    return _freshdesk_call("razorpay_get_payment_refunds",
                           lambda: gateway.razorpay_get_payment_refunds(company_id, payment_id))


@tool
def razorpay_get_refund(refund_id: str, company_id: str = DEFAULT_COMPANY_ID) -> str:
    """READ-ONLY. Look up one existing Razorpay refund by refund ID (rfnd_...): payment ID, amount, status,
    speed, created date."""
    return _freshdesk_call("razorpay_get_refund", lambda: gateway.razorpay_get_refund(company_id, refund_id))


razorpay_tools = [razorpay_get_payment, razorpay_get_payment_refunds, razorpay_get_refund]
razorpay_tools_dict = {t.name: t for t in razorpay_tools}
