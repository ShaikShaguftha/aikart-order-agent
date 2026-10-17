"""
Provider-neutral rules linking store orders and helpdesk tickets.

    Store (Shopify/WooCommerce/...) = source of truth for ORDER state
    Helpdesk (Freshdesk/...)        = source of truth for SUPPORT CASE state

ORDER STATE != TICKET STATE. A cancelled order does not mean the complaint is resolved:
the only automatic resolution is a ticket whose complaint was "please cancel my order"
once the store confirms the order is cancelled. Every other case stays with humans.
Tickets already RESOLVED/CLOSED by a human are never touched (and never reopened).
"""
import re
from typing import Any, Dict, Optional, Tuple

CANCEL_REQUEST = "CANCEL_REQUEST"
REFUND_STATUS = "REFUND_STATUS"
CANCELLATION_REASON = "CANCELLATION_REASON"
STILL_NEEDS_PRODUCT = "STILL_NEEDS_PRODUCT"
DELIVERY_ISSUE = "DELIVERY_ISSUE"
OTHER = "OTHER"
INTENTS = (CANCEL_REQUEST, REFUND_STATUS, CANCELLATION_REASON, STILL_NEEDS_PRODUCT, DELIVERY_ISSUE, OTHER)
INTENT_TAG_PREFIX = "luintix-intent-"

# Ordered by precedence: the first matching rule wins.
_INTENT_RULES = (
    (STILL_NEEDS_PRODUCT, r"still (?:need|want)|need (?:the|my|this) (?:product|item)|re-?ship|replacement|send (?:it|the item) again"),
    (REFUND_STATUS, r"refund|money back|reimburs|charged|chargeback"),
    (CANCELLATION_REASON, r"why (?:was|is|did|has)[^.?!]{0,40}cancel|who cancel|cancel+ed without|didn'?t (?:ask|request)[^.?!]{0,20}cancel"),
    (CANCEL_REQUEST, r"(?:please|pls|kindly|want to|wanna|would like to|need to|can you|could you)[^.?!]{0,20}cancel|cancel (?:my|the|this) order|cancellation request|cancel (?:it|order)\b"),
    (DELIVERY_ISSUE, r"delay|late\b|not (?:yet )?(?:received|arrived|delivered)|where is|tracking|shipping|shipment|stuck"),
)

OPEN_STATUSES = {"OPEN", "PENDING"}
FINAL_TICKET_STATUSES = {"RESOLVED", "CLOSED"}
ORDER_REF_PATTERN = re.compile(r"#\s?(\d{3,})\b")


def classify_complaint(text: str) -> str:
    lowered = (text or "").casefold()
    for intent, pattern in _INTENT_RULES:
        if re.search(pattern, lowered):
            return intent
    return OTHER


def intent_from_tags(tags) -> Optional[str]:
    for tag in tags or []:
        if str(tag).startswith(INTENT_TAG_PREFIX):
            intent = str(tag)[len(INTENT_TAG_PREFIX):].upper()
            if intent in INTENTS:
                return intent
    return None


def intent_tag(intent: str) -> str:
    return INTENT_TAG_PREFIX + intent.lower()


def ticket_intent(ticket: Dict[str, Any]) -> str:
    """Intent recorded at creation (tag) wins; otherwise classify the ticket's own text."""
    return intent_from_tags(ticket.get("tags")) or classify_complaint(
        f"{ticket.get('subject') or ''}. {ticket.get('description_text') or ''}"
    )


def normalize_order_ref(ref: Any) -> str:
    return str(ref or "").strip().lstrip("#").strip().casefold()


def extract_order_reference(ticket: Dict[str, Any], order_field: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(order reference exactly as stored/written, source) for a raw connector ticket."""
    value = (ticket.get("custom_fields") or {}).get(order_field) if order_field else None
    if value not in (None, ""):
        return str(value), "custom_field"
    for source in ("subject", "description_text"):
        match = ORDER_REF_PATTERN.search(str(ticket.get(source) or ""))
        if match:
            return match.group(0).replace(" ", ""), source
    return None, None


def ticket_matches_order(ticket: Dict[str, Any], order_field: Optional[str], order_ref: str) -> bool:
    ref, _ = extract_order_reference(ticket, order_field)
    return ref is not None and normalize_order_ref(ref) == normalize_order_ref(order_ref)


def decide_reconciliation(ticket_status: str, intent: str, order: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """What Luintix may do with a ticket given the current order state (read fresh)."""
    status = str(ticket_status or "").upper()
    if status in FINAL_TICKET_STATUSES:
        return {"action": "NO_ACTION", "reason": f"Ticket is already {status}; the support team's decision is kept."}
    if not order or order.get("withheld") or order.get("error") or order.get("found") is False:
        return {"action": "KEEP_OPEN", "reason": "Current order state could not be verified, so the ticket stays with support."}

    order_cancelled = "CANCELLED" in {str(order.get(k) or "").upper() for k in ("status", "fulfillment_status")}
    if intent == CANCEL_REQUEST:
        if order_cancelled:
            return {"action": "RESOLVE", "reason": "The customer asked for cancellation and the store confirms the order is cancelled."}
        return {"action": "KEEP_OPEN", "reason": "The customer asked for cancellation but the order is not cancelled yet."}
    reasons = {
        REFUND_STATUS: "The complaint is about a refund; order cancellation does not settle it, so support keeps the ticket.",
        CANCELLATION_REASON: "The customer asked why the order was cancelled; this needs human investigation.",
        STILL_NEEDS_PRODUCT: "The customer still needs the product; support must arrange next steps.",
        DELIVERY_ISSUE: "The complaint is about delivery; support should confirm the outcome with the customer.",
        OTHER: "The complaint is not something Luintix can close automatically.",
    }
    return {"action": "KEEP_OPEN", "reason": reasons.get(intent, reasons[OTHER])}
