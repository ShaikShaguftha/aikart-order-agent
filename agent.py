import json
import logging
import os
import re
import sys
from typing import Any, Optional

# Load environment variables before importing tools/gateway connectors
import env_config  # noqa: F401

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_groq import ChatGroq
from integrations.gateway import gateway
from integrations.models import CustomerIdentity
from tools import (
    hubspot_tools,
    hubspot_tools_dict,
    salesforce_tools,
    salesforce_tools_dict,
    razorpay_tools,
    razorpay_tools_dict,
    crm_tools,
    crm_tools_dict,
    freshdesk_tools,
    freshdesk_tools_dict,
    shadowfax_tools,
    shadowfax_tools_dict,
    shippo_tools,
    shippo_tools_dict,
    tools,
    tools_dict,
    tracking_read_tools,
)


def log_agent(msg: str):
    """Flushes logging text directly to the terminal."""
    sys.stdout.write(f"{msg}\n")
    sys.stdout.flush()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
llm = ChatGroq(
    model="openai/gpt-oss-120b",
    groq_api_key=GROQ_API_KEY,
    temperature=0,
    max_retries=3,
    request_timeout=60
)

llm_with_tools = llm.bind_tools(tools)
# Only for tenants with a helpdesk integration; every other tenant keeps llm_with_tools.
# In helpdesk tenants the legacy local escalation/ticket tools are replaced by the real helpdesk.
HELPDESK_REPLACED_TOOLS = {"escalate_to_human_tool", "get_ticket_details"}
llm_with_helpdesk_tools = llm.bind_tools(
    [t for t in tools if t.name not in HELPDESK_REPLACED_TOOLS] + freshdesk_tools
)
# Only for tenants with a shipping integration (Shippo); combined with the helpdesk set when both exist.
# The store-only shipment tool is replaced by live carrier tracking in those tenants.
SHIPPING_REPLACED_TOOLS = {"get_shipment_status"}
llm_with_shipping_tools = llm.bind_tools(
    [t for t in tools if t.name not in SHIPPING_REPLACED_TOOLS] + shippo_tools
)
llm_with_helpdesk_shipping_tools = llm.bind_tools(
    [t for t in tools if t.name not in HELPDESK_REPLACED_TOOLS | SHIPPING_REPLACED_TOOLS] + freshdesk_tools + shippo_tools
)
# CRM-only tenants (Zoho CRM, read-only): they have no store, so they get only the CRM tools.
llm_with_crm_tools = llm.bind_tools(crm_tools)
# HubSpot CRM tenant (read-only): only the HubSpot tools.
llm_with_hubspot_tools = llm.bind_tools(hubspot_tools)
# Salesforce CRM tenant (read-only): only the Salesforce tools.
llm_with_salesforce_tools = llm.bind_tools(salesforce_tools)
# Razorpay payments tenant (read-only): only the Razorpay lookup tools.
llm_with_razorpay_tools = llm.bind_tools(razorpay_tools)
# Tenants with a direct carrier (Delhivery) but no Shippo: read-only tracking tools.
llm_with_tracking_tools = llm.bind_tools(
    [t for t in tools if t.name not in SHIPPING_REPLACED_TOOLS] + tracking_read_tools
)
llm_with_helpdesk_tracking_tools = llm.bind_tools(
    [t for t in tools if t.name not in HELPDESK_REPLACED_TOOLS | SHIPPING_REPLACED_TOOLS] + freshdesk_tools + tracking_read_tools
)
# Tenants with Shadowfax carrier integration (read-only logistics)
llm_with_shadowfax_tools = llm.bind_tools(
    [t for t in tools if t.name not in SHIPPING_REPLACED_TOOLS] + shadowfax_tools
)
llm_with_helpdesk_shadowfax_tools = llm.bind_tools(
    [t for t in tools if t.name not in HELPDESK_REPLACED_TOOLS | SHIPPING_REPLACED_TOOLS] + freshdesk_tools + shadowfax_tools
)

SHADOWFAX_PROMPT = (
    "\nSHADOWFAX LOGISTICS TOOLS (READ-ONLY):\n"
    "- To track a Shadowfax shipment / AWB, call `shadowfax_track_shipment(awb)`.\n"
    "- To view full scan history for a Shadowfax AWB, call `shadowfax_tracking_history(awb)`.\n"
    "- To check pincode serviceability, call `shadowfax_check_serviceability(pickup_pincode, delivery_pincode)`.\n"
    "- To track a reverse pickup, call `shadowfax_track_reverse_pickup(reverse_awb)`.\n"
    "- You operate strictly in READ-ONLY mode for Shadowfax. Do NOT attempt to create shipments, cancel shipments, modify shipments, modify delivery addresses, create pickups, or issue refunds.\n"
)

SYSTEM_PROMPT = """You are Luintix (AIKart's Automated Order Resolution Agent).
Your goal is to assist e-commerce customers and store operators with order lookup, order tracking, delays, cancellations, returns, damages, and store product catalog questions.
The chat is connected to exactly one store (the tenant selected by the client). All tools automatically query that store.

STRICT GUARDRAILS & RULES:

1. OUT-OF-CONTEXT GUARDRAILS (PREVENT TOKEN BURNOUT):
   - You MUST ONLY answer queries directly related to orders, order resolution, shipment tracking, returns, cancellations, customer account support, and the store's product catalog.
   - Product questions about the connected store (e.g., "show me product details for X", price, variants, availability) ARE in scope: call `get_product_details`.
   - DO NOT answer questions regarding general knowledge, world events, programming/coding, jokes, recipes, math, or trivia.
   - If a user asks an out-of-context question (e.g., "who is the president", "write python code", "tell me a joke"), reply EXACTLY with:
     "I am specialized strictly in Luintix order resolution. I cannot assist with off-topic queries or general knowledge. How can I help you with your order today?"

2. PROCEDURAL QUESTIONS VS. EXECUTING ACTIONS:
   - If a user asks general procedural questions (e.g., "if I want to cancel an order what is the procedure?" or "how do returns work?"), explain the step-by-step policy directly.
   - DO NOT execute tool actions like `cancel_order_tool` or `request_return_tool` when the user is merely asking for instructions or procedural advice.

3. CANCELLATION INTENT POLICY & TWO-STEP CONFIRMATION:
   - If a user asks general policy questions (e.g., "How do I cancel?"), explain the rules, check eligibility via `prepare_cancellation_tool` or `get_order_details`, and do NOT execute cancellation.
   - When a user explicitly requests to cancel an order:
     a. FIRST check eligibility by invoking `prepare_cancellation_tool` or `cancel_order_tool(order_id, confirmed=False)`.
     b. If the tool returns status `REJECTED`, inform the user clearly of the refusal reason (e.g. order is already shipped/fulfilled or already cancelled).
     c. If the tool returns status `REQUIRES_CONFIRMATION`, explain the refund details (e.g. full refund will be automatically issued) and ask for explicit confirmation: "Are you sure you want to cancel order [Order ID]? Reply YES to confirm."
     d. ONLY call `cancel_order_tool` with `confirmed=True` AFTER the user explicitly responds with 'YES' or clear confirmation in their next turn.

4. SAFE ACTIONS VS. HUMAN ESCALATION (SALESFORCE AGENTFORCE ALIGNMENT):
   - RETURNS: For "can I return this?" call `check_return_eligibility` (and `get_return_policy` for policy questions). Only call `request_return_tool` when the customer explicitly asks to return.
   - `request_return_tool` results: APPROVED = refund approved by the store rules. SUBMITTED = the return request is recorded (give the return ID) and the merchant will process the refund; NEVER say money has been refunded. REQUIRES_HUMAN_REVIEW = already routed to a human (give the ticket ID if present; if there is no ticket ID, call `escalate_to_human_tool`). REJECTED = explain the policy reason.
   - CANCELLATION REQUIRES_HUMAN_REVIEW: if `prepare_cancellation_tool`/`cancel_order_tool` returns REQUIRES_HUMAN_REVIEW, do not cancel; explain why and call `escalate_to_human_tool`.
   - Refund questions ("was I refunded?") -> `get_refund_status`. Stock questions -> `check_inventory`.
   - DISSATISFIED CUSTOMERS: If a user expresses extreme dissatisfaction, demands legal action, or asks for human management, call `escalate_to_human_tool`.
   - ALWAYS inform the user clearly when their case has been escalated to human support and provide their created ticket ID (`ticket_id`).

5. CONVERSATION CONTEXT & TOOL OUTPUT:
   - ALWAYS verify order details using your tools first. Never hallucinate tracking status, order totals, or policies. Always rely strictly on database outputs.
   - If a specific order (e.g., #1003) was discussed earlier in this conversation and the user asks a follow-up about "it"/"that order", use that exact order identifier. If no specific order is in context, "my order" means the customer's latest order (see rule 7).
   - When a cancellation or return tool is called, READ its output carefully. If a tool returns CANCELLED or SUCCESS or APPROVED, confirm to the user that the action was executed and verified.
   - Keep responses empathetic, clear, and concise.

6. ORDER IDENTIFIER PRESERVATION:
   - Never modify, normalize, prefix, suffix, or rewrite an order identifier supplied by the user.
   - Pass the exact identifier to the tool.
     Examples:
     #1003 -> #1003
     1003 -> 1003
     ORD-5001 -> ORD-5001
     gid://shopify/Order/... -> unchanged
   - Never invent an "ORD-" prefix. "#1003" is NOT "ORD-1003".

7. IDENTITY-FIRST ORDER WORKFLOW (customers do NOT need an order ID):
   - "Show me my orders", "my recent orders", "what did I buy", "my recent purchases" -> call `get_my_orders`.
   - "Where is my latest order?", "status of my latest order", "has my order shipped?", "is my order paid?" (no order number given) -> call `get_my_latest_order`.
   - Call these tools IMMEDIATELY with empty identity fields; the system automatically supplies the identity already known for this session.
   - ONLY if the tool returns code IDENTITY_REQUIRED, ask the customer for the email address or phone number they used when ordering. NEVER ask for an order ID as the first step.
   - When the customer gives an email or phone, pass it exactly as written to `email`/`phone`. Never fill `customer_id` or `authenticated` yourself.
   - A name alone never identifies a customer. You may pass `name` only together with an email or phone.
   - CUSTOMER_NOT_FOUND / IDENTITY_MISMATCH: say no orders could be found for those details and offer to look up a specific order number instead. Never reveal other customers' data.
   - PROVIDER_ACCESS_DENIED: say that looking up orders by email/phone is not available for this store right now, and offer to look up a specific order number instead.
   - Explicit order numbers remain supported: if the customer gives one (e.g., "Has #1003 shipped?"), use `get_order_details` / `get_shipment_status` with that exact identifier.

8. ALWAYS FETCH LIVE DATA:
   - For EVERY question about an order's status, payment, fulfillment, shipment, tracking, or location, call a lookup tool again in this turn (`get_my_latest_order`, `get_order_details` or `get_shipment_status`), even if the order was discussed earlier. Earlier answers may be stale; never answer these from conversation history alone.
   - Only offer follow-up actions you have tools for (your orders, order details, shipment status, product details, cancellation, returns/refunds, human escalation). Do NOT offer to take payments, add payment methods, or modify order items.
   - Payment questions for an order ("payment transaction details", "which gateway was used", "verify the payment") -> `get_order_payment_transactions` / `verify_order_payment` with the order identifier. Report gateway, payment provider and payment reference ONLY exactly as returned. If mapping_status is not MAPPED, or the status is NO_PAYMENT_TRANSACTION / MAPPING_UNAVAILABLE / PAYMENT_PROVIDER_NOT_CONNECTED, say the payment-provider mapping is unavailable for that order; never guess a provider or payment ID. These tools are read-only and cannot refund.

9. NO UNSUPPORTED ACTION CLAIMS:
   - Only say that you escalated, flagged, forwarded, created/opened a ticket, added a note, resolved/closed a ticket, cancelled an order or issued a refund if a tool result in THIS conversation turn shows that exact action succeeded.
   - Never promise internal actions you cannot perform ("I'll flag this to the fulfillment team", "I'll escalate internally"). Offer to escalate instead, and only confirm after the tool succeeds.
   - Quote ticket numbers only exactly as a tool returned them. Never construct references such as "TICK-1003".
   - A store shipment/fulfillment record status (e.g. SUCCESS, FULFILLED) only means the store recorded a shipment; it is NOT carrier tracking. Never say a package is in transit, out for delivery or delivered unless a tool returned that tracking status, and never infer status from the text of a tracking number.

10. TOOL ERRORS:
   - If a tool returns an "error", do not guess or invent data. Tell the user plainly that the store system returned an error or that the order/product was not found, and include the order identifier exactly as given.
"""


ORDER_ID_PATTERN = re.compile(
    r"gid://shopify/Order/\d+|#[A-Za-z0-9-]+|\b[A-Za-z]{2,5}-\d+\b|\b\d{3,}\b"
)


def _extract_order_ids(texts: list) -> list:
    """Order identifiers exactly as the user wrote them, most recent first."""
    found = []
    for text in reversed(texts):
        for match in ORDER_ID_PATTERN.findall(text or ""):
            if match not in found:
                found.append(match)
    return found


def preserve_order_id(tool_order_id: str, user_ids: list) -> str:
    """
    Safety net for the identifier-preservation rule: if the LLM rewrote a
    user-supplied order ID (e.g. "#1001" -> "ORD-1001"), restore the user's
    exact string. IDs the user actually typed are passed through untouched.
    """
    if not tool_order_id or not user_ids or tool_order_id in user_ids:
        return tool_order_id
    digits = re.sub(r"\D", "", tool_order_id)
    if not digits:
        return tool_order_id
    for uid in user_ids:
        if re.sub(r"\D", "", uid) == digits:
            return uid
    return tool_order_id


IDENTITY_TOOLS = {"get_my_orders", "get_my_latest_order"}

HELPDESK_PROMPT = """

11. SUPPORT TICKETS (FRESHDESK HELPDESK) - Freshdesk is the human support workspace:
   - Complaint/ticket status ("what happened to my complaint?", "any update?", "is my ticket resolved?") -> call `freshdesk_get_complaint_status` EVERY time (fresh read; never answer from history). It returns the live ticket state AND the linked order's live store state.
   - ORDER STATE != TICKET STATE. A cancelled order does not mean the complaint is resolved. Describe both separately, e.g. "Your support ticket #4 is still open. It is related to order #1003, which is currently cancelled."
   - Only say the team is "awaiting a reply" or similar if latest_public_message / ticket status supports it.
   - Human escalation -> `freshdesk_create_ticket` (it reuses an existing open ticket for the same order instead of creating a duplicate). Tell the customer the returned ticket_reference exactly (e.g. "#7") and whether it was a new or an existing ticket (reused).
   - After a successful order cancellation, the linked tickets are synchronised automatically (see support_ticket_sync in the cancellation result); report only what actions_applied shows.
   - Ticket lists/details -> `freshdesk_get_my_tickets` / `freshdesk_get_ticket`. Call with empty identity fields first; if IDENTITY_REQUIRED, ask for the email or phone used with support.
   - Use only ticket numbers the customer gave you or a tool returned. Human support may change a ticket's status at any time; always report the status from the latest tool result.
   - Change status/priority or add a private note only when the customer asks or the situation clearly requires it. Resolve a ticket only if the customer confirms the issue is fully solved.
   - NOT_FOUND for a ticket means it does not exist for this customer; do not speculate.
"""
SHIPPING_PROMPT = """

12. SHIPMENT TRACKING - the store is the source of the order and its tracking number/AWB; the tracking provider (Shippo, or Delhivery for Delhivery AWBs) is the source of shipment status:
   - "Where is my order/package?", "when will it arrive?", "has it shipped?" -> `get_tracking_status` with order_id, EVERY time (fresh read). Full scan history -> `get_tracking_history`.
   - If tracking_available is false, say tracking information is not available and give the reason. Never guess a carrier, tracking number, status, location or delivery date.
   - Keep identifiers distinct and exactly as returned: order number (e.g. #1004), tracking number / AWB, carrier / carrier_slug, shipment ID, provider reference, merchant order reference. Never turn one into another.
   - If ndr_state shows a failed delivery attempt, explain the reason from the tool result and offer human support; do not promise a reattempt, address change or return (those actions are not available).
   - Report status, substatus_text or status_details, estimated_delivery and current_location from the tool result. If action_required is true, say action is needed and offer human support.
   - `register_tracking` only when the customer asks to be kept updated; report the returned status exactly (REGISTERED, ALREADY_REGISTERED, UNCONFIRMED, MERCHANT_APPROVAL_REQUIRED, ...).
"""
CRM_PROMPT = """

13. CRM TENANT (ZOHO CRM, READ-ONLY) - this tenant is connected to a CRM, not a store:
   - Customer lookups are IN SCOPE here: "find customer Rahul" -> `crm_search_customers` (name); "find customer by email/phone" -> `crm_search_customers` with email/phone; "show customer details" / "this customer's CRM information" -> `crm_get_customer` with the contact ID from the previous result. Accounts -> `crm_get_account`; deals -> `crm_get_deal`.
   - Always show each contact's CRM contact ID in lists so follow-up questions can refer to it. If several contacts match, list them and ask which one; never merge people.
   - Use only record IDs the user gave or a CRM tool returned. Report only fields the tool returned; say "not recorded" for missing fields.
   - The CRM is READ-ONLY: you cannot create, update or delete contacts, accounts or deals, or send emails. Say so if asked.
   - There are no store orders for this tenant; do not offer order tracking, cancellations or returns.
"""
CRM_ID_ARGS = ("contact_id", "account_id", "deal_id")

HUBSPOT_PROMPT = """

14. HUBSPOT CRM TENANT (READ-ONLY) - this tenant is connected to HubSpot CRM, not a store:
   - Customer lookups are IN SCOPE: "find customer Rahul" / "find Rahul in HubSpot" -> `hubspot_search_contacts` (name); by email or phone -> `hubspot_search_contacts` with email/phone; "show Rahul's CRM details" -> `hubspot_get_customer` with the contact ID from the search result.
   - Companies -> `hubspot_get_company` (hubspot_company_id); deals -> `hubspot_get_deal`; support tickets -> `hubspot_get_ticket`.
   - Always show each contact's HubSpot contact ID in lists so follow-ups can refer to it. If several contacts match, list them and ask which one.
   - Use only record IDs the user gave or a HubSpot tool returned. Report only returned fields; say "not recorded" for missing ones. Deal/ticket stages are HubSpot stage IDs - report them as given.
   - HubSpot is READ-ONLY here: you cannot create, update or delete records or send emails. There are no store orders for this tenant.
"""
HUBSPOT_ID_ARGS = ("contact_id", "hubspot_company_id", "deal_id", "ticket_id")


def verify_hubspot_ids(tool_args: dict, messages: list) -> Optional[dict]:
    """Blocks HubSpot record IDs the LLM invented (must come from the user or a HubSpot result)."""
    known = known_numbers(messages)
    for field in HUBSPOT_ID_ARGS:
        value = str(tool_args.get(field) or "").strip()
        if value and value not in known:
            return {"error": f"That {field.replace('_', ' ')} was not provided by the user or returned by a HubSpot "
                             "lookup. Search for the record first.", "code": "UNVERIFIED_RECORD_ID"}
    return None


SALESFORCE_PROMPT = """

15. SALESFORCE CRM TENANT (READ-ONLY) - this tenant is connected to Salesforce CRM, not a store:
   - Customer lookups are IN SCOPE: "find customer Rahul" / "find Rahul in Salesforce" -> `salesforce_search_contacts` (name); by email or phone -> `salesforce_search_contacts` with email/phone; "show Rahul's CRM details" -> `salesforce_get_customer` with the contact ID from the search result.
   - Accounts (companies) -> `salesforce_get_account`; opportunities (deals) -> `salesforce_get_opportunity`; support cases -> `salesforce_get_case` (Case ID or Case Number).
   - Always show each contact's Salesforce contact ID in lists so follow-ups can refer to it. If several contacts match, list them and ask which one.
   - Use only record IDs / case numbers the user gave or a Salesforce tool returned, copied exactly (they are case-sensitive). Report only returned fields; say "not recorded" for missing ones. Stages and case statuses are the org's own values - report them as given.
   - Salesforce is READ-ONLY here: you cannot create, update or delete records, close cases or send emails. There are no store orders for this tenant.
"""
SALESFORCE_ID_ARGS = ("contact_id", "account_id", "opportunity_id", "case_id")


def verify_salesforce_ids(tool_args: dict, messages: list) -> Optional[dict]:
    """
    Blocks Salesforce record IDs the LLM invented (must come from the user or a Salesforce result).
    Record IDs are alphanumeric and case-sensitive, so they must appear verbatim (a 15-character ID
    may be the prefix of an 18-character one); numeric case numbers are compared as numbers.
    """
    texts = [str(m.content or "") for m in messages if not isinstance(m, SystemMessage)]
    for field in SALESFORCE_ID_ARGS:
        value = str(tool_args.get(field) or "").strip().lstrip("#")
        if not value:
            continue
        if value.isdigit():
            known = value.lstrip("0") in {n.lstrip("0") for n in known_numbers(messages)}
        else:
            pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?:[A-Za-z0-9]{{3}})?(?![A-Za-z0-9])")
            known = any(pattern.search(t) for t in texts)
        if not known:
            return {"error": f"That {field.replace('_', ' ')} was not provided by the user or returned by a Salesforce "
                             "lookup. Search for the record first.", "code": "UNVERIFIED_RECORD_ID"}
    return None


RAZORPAY_PROMPT = """

16. RAZORPAY PAYMENTS TENANT (READ-ONLY) - this tenant is connected to Razorpay payments, not a store:
   - "Check payment pay_..." -> `razorpay_get_payment`; "has this payment been refunded / show its refunds" -> `razorpay_get_payment_refunds`; "status of refund rfnd_..." -> `razorpay_get_refund`.
   - Use only payment IDs (pay_...) and refund IDs (rfnd_...) the user gave or a Razorpay tool returned, copied exactly. If none was given, ask for it.
   - Amounts are already converted to major units (e.g. rupees) with their currency. Report statuses exactly as returned (e.g. captured, refunded, pending, processed); say "not recorded" for missing fields.
   - The Razorpay tools are READ-ONLY. You CANNOT create, initiate, approve, process or cancel a refund, and cannot capture or cancel a payment. Never say or imply that you did or will do any of these. Only describe refunds a tool returned, with the status it returned. If the customer wants a refund, say you can only check payment and refund status here and offer to connect them with the support team.
"""
RAZORPAY_ID_ARGS = ("payment_id", "refund_id")


def verify_razorpay_ids(tool_args: dict, messages: list) -> Optional[dict]:
    """Blocks Razorpay payment/refund IDs the LLM invented (must appear verbatim in the conversation)."""
    texts = [str(m.content or "") for m in messages if not isinstance(m, SystemMessage)]
    for field in RAZORPAY_ID_ARGS:
        value = str(tool_args.get(field) or "").strip()
        if not value:
            continue
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])")
        if not any(pattern.search(t) for t in texts):
            return {"error": f"That {field.replace('_', ' ')} was not provided by the user or returned by a Razorpay "
                             "lookup. Ask the customer for it.", "code": "UNVERIFIED_RECORD_ID"}
    return None


# Refund claims for the read-only Razorpay tenant: no tool can create/initiate/process a refund, so
# any first-person refund action is unsupported; passive refund-state claims need a Razorpay result.
_RZP_APOS = r"(?:'|’)"
_REFUND_ACTOR = (rf"\b(?:i|we)(?:{_RZP_APOS}ve|{_RZP_APOS}d|{_RZP_APOS}ll| have| had| will| am going to| are going to| shall)?\s+"
                 r"(?:(?:just|already|also|now|successfully|immediately|go ahead and|right away)\s+)*")
REFUND_ACTION_CLAIM = re.compile(
    _REFUND_ACTOR + r"(?:initiat|creat|process|start|trigger|rais|issu|approv|submit|request|arrang|complet)\w*"
    r"\b[^.!?\n]{0,40}?\brefund", re.I)
REFUND_DIRECT_CLAIM = re.compile(_REFUND_ACTOR + r"refund(?:ed)?\b", re.I)
REFUND_STATE_CLAIM = re.compile(
    r"\brefund\b[^.!?\n]{0,60}?\b(has been|have been|was|were|is being|will be|is)\s+"
    r"(initiated|created|processed|issued|completed|credited|approved|successful)\b"
    r"|\b(?:has been|have been|was|were)\s+(?:fully\s+|partially\s+)?(refunded)\b", re.I)
_REFUND_DONE_WORDS = {"processed", "completed", "credited", "successful", "refunded"}


def razorpay_refund_evidence(tool_events: list) -> tuple:
    """(refund statuses returned by Razorpay tools this turn, whether a payment was returned as refunded)."""
    statuses, payment_refunded = set(), False
    for name, output in tool_events:
        d = _parse(output)
        if not d or "error" in d or not str(name).startswith("razorpay_"):
            continue
        refunds = d.get("refunds") if name == "razorpay_get_payment_refunds" else ([d] if name == "razorpay_get_refund" else [])
        statuses.update(str(r.get("status") or "").lower() for r in refunds or [] if isinstance(r, dict))
        if name == "razorpay_get_payment" and str(d.get("status")).lower() == "refunded":
            payment_refunded = True
    statuses.discard("")
    return statuses, payment_refunded


def find_refund_claims(text: str, tool_events: list) -> list:
    """Refund claims in a Razorpay-tenant answer that the (read-only) tool results do not back up."""
    problems = []
    for pattern in (REFUND_ACTION_CLAIM, REFUND_DIRECT_CLAIM):
        for match in pattern.finditer(text or ""):
            problems.append(f"claims or promises a refund action ('{match.group(0).strip()}') but refunds cannot be "
                            "created here (read-only)")
    statuses, payment_refunded = razorpay_refund_evidence(tool_events)
    for match in REFUND_STATE_CLAIM.finditer(text or ""):
        aux, verb = (match.group(1) or "").lower(), (match.group(2) or match.group(3) or "").lower()
        if verb in _REFUND_DONE_WORDS and aux not in ("is being", "will be"):
            backed = "processed" in statuses or payment_refunded
        else:
            backed = bool(statuses) or payment_refunded
        if not backed:
            problems.append(f"says '{match.group(0).strip()}' but no Razorpay result shows such a refund")
    return problems


def verify_record_ids(tool_args: dict, messages: list) -> Optional[dict]:
    """Blocks CRM record IDs the LLM invented (must come from the user or a CRM result)."""
    known = known_numbers(messages)
    for field in CRM_ID_ARGS:
        value = str(tool_args.get(field) or "").strip()
        if value and value not in known:
            return {"error": f"That {field.replace('_', ' ')} was not provided by the user or returned by a CRM lookup. "
                             "Search for the customer first.", "code": "UNVERIFIED_RECORD_ID"}
    return None


SHIPPO_IDENTITY_TOOLS = set(shippo_tools_dict)
SHIPPO_WRITE_TOOLS = {"register_tracking"}


def verify_tracking_number(tool_args: dict, messages: list) -> Optional[dict]:
    """Blocks tracking numbers the LLM invented (must come from the customer or a tool result)."""
    number = str(tool_args.get("tracking_number") or "").strip()
    if not number:
        return None
    seen = " ".join(str(m.content or "") for m in messages if not isinstance(m, SystemMessage))
    if number in seen:
        return None
    return {"error": "That tracking number was not provided by the customer or returned by a lookup. "
                     "Look the order up instead, or ask the customer for the tracking number.",
            "code": "UNVERIFIED_TRACKING_NUMBER"}


FRESHDESK_IDENTITY_TOOLS = set(freshdesk_tools_dict) - {"freshdesk_get_ticket_fields"}
FRESHDESK_WRITE_TOOLS = {
    "freshdesk_create_ticket", "freshdesk_update_ticket_status",
    "freshdesk_update_ticket_priority", "freshdesk_add_internal_note",
    "freshdesk_sync_ticket_with_order",
}
FRESHDESK_TICKET_ID_TOOLS = {
    "freshdesk_get_ticket", "freshdesk_update_ticket_status",
    "freshdesk_update_ticket_priority", "freshdesk_add_internal_note",
    "freshdesk_get_complaint_status", "freshdesk_sync_ticket_with_order",
}


def known_numbers(messages: list) -> set:
    """Every number the customer wrote or a tool/assistant message returned in this conversation."""
    found = set()
    for m in messages:
        if isinstance(m, SystemMessage):
            continue
        found.update(re.findall(r"\d+", str(m.content or "")))
    return found


def verify_ticket_id(tool_args: dict, messages: list) -> Optional[dict]:
    """Blocks ticket IDs the LLM invented (not from the customer or a provider result)."""
    if tool_args.get("ticket_id") in (None, "", 0):
        return None  # optional ticket_id not supplied
    ticket_digits = re.sub(r"\D", "", str(tool_args.get("ticket_id", "")))
    if ticket_digits and ticket_digits.lstrip("0") in {n.lstrip("0") for n in known_numbers(messages)}:
        return None
    return {
        "error": "That ticket number was not provided by the customer or returned by a previous lookup. "
                 "Ask the customer for it or list their tickets first.",
        "code": "UNVERIFIED_TICKET_ID",
    }


def sync_tickets_after_cancellation(
    tool_output: str, order_id: Optional[str], tenant_id: str, session_id: str, session_identity: Optional[dict]
) -> str:
    """
    After a VERIFIED cancellation, re-evaluate the customer's open tickets for that order
    (cancellation-request tickets get resolved; all other complaints stay with support).
    The outcome is attached to the tool result so the LLM can only report what happened.
    """
    result = _parse(tool_output)
    if not result or str(result.get("status")).upper() != "CANCELLED" or not order_id:
        return tool_output
    known = {k: (session_identity or {}).get(k) for k in ("email", "phone", "name")}
    if not (known["email"] or known["phone"]):
        result["support_ticket_sync"] = {"skipped": "The customer's support identity (email/phone) is not known in this session."}
        return json.dumps(result)
    try:
        result["support_ticket_sync"] = gateway.helpdesk_sync_ticket_with_order(
            CustomerIdentity(**{k: v for k, v in known.items() if v}), tenant_id,
            order_id=order_id, session_id=session_id,
        )
    except Exception as e:
        log_agent(f"[TICKET SYNC ERROR]: {type(e).__name__}")
        result["support_ticket_sync"] = {"error": "Support ticket sync could not be completed.", "code": "HELPDESK_ERROR"}
    return json.dumps(result)


def remember_helpdesk_identity(tool_args: dict, tool_output: str, session_identity: Optional[dict]) -> None:
    """Remember customer-provided email/phone once they resolved a helpdesk contact."""
    if session_identity is None:
        return
    try:
        result = json.loads(tool_output)
    except (TypeError, ValueError):
        return
    if isinstance(result, dict) and ("contact" in result or "tickets" in result):
        for field in CUSTOMER_PROVIDED_FIELDS:
            if tool_args.get(field):
                session_identity[field] = tool_args[field]
CUSTOMER_PROVIDED_FIELDS = ("email", "phone", "name")


def apply_identity(
    tool_args: dict,
    authenticated_customer_id: Optional[str],
    session_identity: Optional[dict],
) -> str:
    """
    Enforces identity rules on identity-first tool calls (never trusts the LLM for them):
      - customer_id/authenticated come ONLY from the server-side authenticated session.
      - email/phone/name the LLM omitted are filled from this session's known identity.
    Returns the identity source for logging.
    """
    tool_args["customer_id"] = authenticated_customer_id or ""
    tool_args["authenticated"] = bool(authenticated_customer_id)
    if authenticated_customer_id:
        return "AUTHENTICATED_SESSION"

    source = "CUSTOMER_PROVIDED" if any(tool_args.get(f) for f in CUSTOMER_PROVIDED_FIELDS) else "NONE"
    for field in CUSTOMER_PROVIDED_FIELDS:
        if not tool_args.get(field) and (session_identity or {}).get(field):
            tool_args[field] = session_identity[field]
            source = "SESSION_REMEMBERED" if source == "NONE" else source
    return source


def remember_identity(tool_args: dict, tool_output: str, session_identity: Optional[dict]) -> None:
    """Keeps customer-provided identifiers for the session once they resolved a customer."""
    if session_identity is None:
        return
    try:
        result = json.loads(tool_output)
    except (TypeError, ValueError):
        return
    if isinstance(result, dict) and result.get("customer"):
        for field in CUSTOMER_PROVIDED_FIELDS:
            if tool_args.get(field):
                session_identity[field] = tool_args[field]


# ---- Unsupported action-claim guard (all tenants) -----------------------------------

_CLAIM_VERBS = {
    "escalate": "escalated flagged forwarded raised logged created opened filed submitted notified assigned reported",
    "note": "added noted",
    "ticket_status": "resolved closed marked updated",
    "cancel": "cancelled canceled",
    "refund": "refunded issued",
    "register": "registered subscribed",
}
_VERB_CATEGORY = {v: cat for cat, verbs in _CLAIM_VERBS.items() for v in verbs.split()}
_FUTURE_VERBS = {
    "escalate": "escalate flag forward raise log create open file submit notify assign pass report",
    "note": "add note",
    "ticket_status": "resolve close mark",
    "register": "register subscribe",
}
_FUTURE_CATEGORY = {v: cat for cat, verbs in _FUTURE_VERBS.items() for v in verbs.split()}
_APOS = r"(?:'|’)"
PAST_CLAIM = re.compile(
    rf"\b(?:i|we)(?:{_APOS}ve|{_APOS}d| have| had)?\s+(?:(?:just|already|also|now|successfully)\s+)*("
    + "|".join(_VERB_CATEGORY) + r")\b", re.I)
FUTURE_CLAIM = re.compile(
    rf"\b(?:i|we)(?:{_APOS}ll| will| am going to| are going to| shall)\s+(?:(?:also|now|immediately|go ahead and|right away)\s+)*("
    + "|".join(_FUTURE_CATEGORY) + r")\b", re.I)
PASSIVE_ESCALATION = re.compile(r"\b(?:has|have|was|were|is|been)\s+(?:been\s+)?(escalated|flagged|forwarded)\b", re.I)
TICKET_REFERENCE = re.compile(
    r"\b(?:ticket|complaint)s?\b(?:\s+(?:number|no\.?|id|reference))?\s*(?:is\s+)?[*_`:\s]*#\s*(\d+)"
    r"|\b(?:ticket|complaint)\s+(?:number|no\.?|id)\s*(?:is\s+)?[*_`:\s]*(\d+)", re.I)
FAKE_TICKET = re.compile(r"\bTICK-[\w#-]+", re.I)
# Shipment-state claims must be backed by a tracking status value in a tool result
# (not by a fulfillment record status, and not by the text of a tracking number).
SHIPMENT_STATE_CLAIMS = (
    (re.compile(r"\b(?:in transit|out for delivery)\b", re.I),
     re.compile(r'"(?:status|provider_status)"\s*:\s*"(?:IN_TRANSIT|TRANSIT|OUT_FOR_DELIVERY)"'), "transit"),
    (re.compile(r"\b(?:has been|have been|was|were|is|got)\s+delivered\b", re.I),
     re.compile(r'"(?:status|provider_status)"\s*:\s*"DELIVERED"'), "delivered"),
)
_FAILED_STATUSES = {"ERROR", "FAILED", "REJECTED", "BLOCKED", "REQUIRES_CONFIRMATION", "CANCELLED_BY_USER"}

CLAIM_CORRECTION = (
    "Your previous answer is NOT supported by the tool results of this turn: {problems}. "
    "Rewrite the answer for the customer using ONLY facts from tool results. Do not claim any action that no "
    "tool performed successfully, do not promise internal actions, and quote ticket numbers only exactly as a "
    "tool returned them. If an action is needed, offer it or call the right tool."
)
CLAIM_FALLBACK = (
    "I'm sorry, I couldn't confirm that action was completed, so I won't claim it was. "
    "Would you like me to connect you with our human support team?"
)


def _parse(output: Any) -> Optional[dict]:
    try:
        parsed = json.loads(output) if isinstance(output, str) else output
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def supported_actions(tool_events: list) -> set:
    """Action categories that a tool result in this turn shows actually succeeded."""
    support = set()
    for name, output in tool_events:
        d = _parse(output)
        if not d or "error" in d or str(d.get("status", "")).upper() in _FAILED_STATUSES:
            continue
        if name in ("escalate_to_human_tool", "freshdesk_create_ticket"):
            support.add("escalate")
        if name == "freshdesk_create_ticket" and d.get("note_added"):
            support.add("note")
        if name == "request_return_tool":
            support.add("escalate")
            if str(d.get("status")).upper() == "APPROVED":
                support.add("refund")
        if name == "freshdesk_add_internal_note":
            support.add("note")
        if name in ("freshdesk_update_ticket_status", "freshdesk_update_ticket_priority"):
            support.add("ticket_status")
        if name == "register_tracking" and str(d.get("status")).upper() in ("REGISTERED", "ALREADY_REGISTERED"):
            support.add("register")
        if name == "cancel_order_tool" and str(d.get("status")).upper() == "CANCELLED":
            support.add("cancel")
        syncs = [d] if name == "freshdesk_sync_ticket_with_order" else [d.get("support_ticket_sync") or {}]
        for sync in syncs:
            for t in sync.get("tickets", []) if isinstance(sync, dict) else []:
                if "NOTE_ADDED" in t.get("actions_applied", []):
                    support.add("note")
                if "RESOLVED" in t.get("actions_applied", []):
                    support.add("ticket_status")
    return support


def find_unsupported_claims(text: str, tool_events: list, user_texts: list) -> list:
    """Claims in the answer that no successful tool result of this turn backs up."""
    problems = []
    support = supported_actions(tool_events)
    evidence = " ".join(str(o) for _, o in tool_events)
    for match in PAST_CLAIM.finditer(text or ""):
        category = _VERB_CATEGORY[match.group(1).lower()]
        if category not in support:
            problems.append(f"claims '{match.group(0).strip()}' but no {category} action succeeded")
    for match in FUTURE_CLAIM.finditer(text or ""):
        category = _FUTURE_CATEGORY[match.group(1).lower()]
        if category not in support:
            problems.append(f"promises '{match.group(0).strip()}' which no tool performed")
    for match in PASSIVE_ESCALATION.finditer(text or ""):
        if "escalate" not in support:
            problems.append(f"says '{match.group(0).strip()}' but nothing was escalated")
    for match in FAKE_TICKET.finditer(text or ""):
        if match.group(0) not in evidence:
            problems.append(f"quotes ticket reference '{match.group(0)}' that no tool returned")
    for pattern, status_evidence, label in SHIPMENT_STATE_CLAIMS:
        for match in pattern.finditer(text or ""):
            preceding = (text or "")[max(0, match.start() - 14):match.start()].casefold()
            if any(neg in preceding for neg in ("not ", "n't ", "n’t ", "never ", "yet to be ")):
                continue
            if not status_evidence.search(evidence):
                problems.append(f"says '{match.group(0)}' but no tool returned a {label} tracking status")
    known = set(re.findall(r"\d+", evidence + " " + " ".join(user_texts)))
    for match in TICKET_REFERENCE.finditer(text or ""):
        number = match.group(1) or match.group(2)
        if number not in known:
            problems.append(f"quotes ticket #{number} that no tool returned")
    return problems


def _mask(value: str) -> str:
    value = str(value or "")
    return value[:2] + "***" + value[-2:] if len(value) > 4 else ("***" if value else "")


def process_query(
    user_input: str,
    history_messages: list = None,
    user_id: str = None,
    company_id: str = "COMP-ALPHA",
    session_id: str = "default",
    authenticated_customer_id: Optional[str] = None,
    session_identity: Optional[dict] = None,
    **kwargs,
) -> str:
    """
    Processes user query with conversation history context.

    authenticated_customer_id must only be passed from a verified login/session
    (none exists in the demo UI). session_identity is a per-session dict the caller
    keeps; customer-provided email/phone are remembered there once they resolve.
    """
    tenant_id = kwargs.get("company_id", company_id)
    sess_id = kwargs.get("session_id", session_id)
    try:
        log_agent(
            f"\n---> [AGENT STARTED | Tenant: {tenant_id} | Session: {sess_id[:8]}]: '{user_input}'"
        )

        helpdesk_enabled = gateway.has_helpdesk(tenant_id)
        shipping_enabled = gateway.has_shipping(tenant_id)  # Shippo (read + registration)
        carrier_tracking_enabled = gateway.has_delhivery(tenant_id)  # direct carriers (read-only)
        shadowfax_enabled = gateway.has_shadowfax(tenant_id)  # Shadowfax (read-only)
        tracking_enabled = shipping_enabled or carrier_tracking_enabled or shadowfax_enabled
        crm_enabled = gateway.has_crm(tenant_id)
        hubspot_enabled = gateway.has_hubspot(tenant_id)
        salesforce_enabled = gateway.has_salesforce(tenant_id)
        razorpay_enabled = gateway.has_razorpay(tenant_id)
        if razorpay_enabled:
            bound_llm = llm_with_razorpay_tools
        elif salesforce_enabled:
            bound_llm = llm_with_salesforce_tools
        elif hubspot_enabled:
            bound_llm = llm_with_hubspot_tools
        elif crm_enabled:
            bound_llm = llm_with_crm_tools
        elif shadowfax_enabled:
            bound_llm = llm_with_helpdesk_shadowfax_tools if helpdesk_enabled else llm_with_shadowfax_tools
        elif helpdesk_enabled and shipping_enabled:
            bound_llm = llm_with_helpdesk_shipping_tools
        elif helpdesk_enabled and carrier_tracking_enabled:
            bound_llm = llm_with_helpdesk_tracking_tools
        elif helpdesk_enabled:
            bound_llm = llm_with_helpdesk_tools
        elif shipping_enabled:
            bound_llm = llm_with_shipping_tools
        elif carrier_tracking_enabled:
            bound_llm = llm_with_tracking_tools
        else:
            bound_llm = llm_with_tools
        dispatch = {**tools_dict, **freshdesk_tools_dict} if helpdesk_enabled else tools_dict
        provider_tools = {
            **(freshdesk_tools_dict if helpdesk_enabled else {}),
            **({t.name: t for t in tracking_read_tools} if carrier_tracking_enabled else {}),
            **(shippo_tools_dict if shipping_enabled else {}),
            **(shadowfax_tools_dict if shadowfax_enabled else {}),
            **(crm_tools_dict if crm_enabled else {}),
            **(hubspot_tools_dict if hubspot_enabled else {}),
            **(salesforce_tools_dict if salesforce_enabled else {}),
            **(razorpay_tools_dict if razorpay_enabled else {}),
        }

        messages = [SystemMessage(content=SYSTEM_PROMPT + (HELPDESK_PROMPT if helpdesk_enabled else "")
                                  + (SHIPPING_PROMPT if tracking_enabled else "")
                                  + (SHADOWFAX_PROMPT if shadowfax_enabled else "")
                                  + (CRM_PROMPT if crm_enabled else "")
                                  + (HUBSPOT_PROMPT if hubspot_enabled else "")
                                  + (SALESFORCE_PROMPT if salesforce_enabled else "")
                                  + (RAZORPAY_PROMPT if razorpay_enabled else ""))]

        if history_messages:
            messages.extend(history_messages[-4:])

        messages.append(HumanMessage(content=user_input))

        user_texts = [
            m.content for m in (history_messages or [])[-4:] if isinstance(m, HumanMessage)
        ] + [user_input]
        user_order_ids = _extract_order_ids(user_texts)

        max_iterations = 5
        iteration = 0
        tool_events: list = []
        claim_corrected = False

        while iteration < max_iterations:
            ai_msg = bound_llm.invoke(messages)

            if not ai_msg.tool_calls:
                res_text = (
                    ai_msg.content if ai_msg.content else "Request completed."
                )
                problems = find_unsupported_claims(res_text, tool_events, user_texts)
                if razorpay_enabled:
                    problems += find_refund_claims(res_text, tool_events)
                if problems:
                    log_agent(f"[CLAIM GUARD]: unsupported claims detected: {problems}")
                    if claim_corrected:
                        log_agent(f"---> [AGENT COMPLETED (claim guard fallback)]: {CLAIM_FALLBACK}\n")
                        return CLAIM_FALLBACK
                    claim_corrected = True
                    messages.append(AIMessage(content=res_text))
                    messages.append(SystemMessage(content=CLAIM_CORRECTION.format(problems="; ".join(problems))))
                    iteration += 1
                    continue
                log_agent(f"---> [AGENT COMPLETED]: {res_text}\n")
                return res_text

            messages.append(ai_msg)

            for tool_call in ai_msg.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_call_id = tool_call["id"]

                tool_args["company_id"] = tenant_id
                if helpdesk_enabled and tool_name == "escalate_to_human_tool":
                    # Never mint a local "TICK-..." reference in a helpdesk tenant: escalate for real.
                    reason = str(tool_args.get("reason") or "Customer requested human support")
                    order_ref = str(tool_args.get("order_id") or "")
                    log_agent(f"[ESCALATION REDIRECT]: escalate_to_human_tool -> freshdesk_create_ticket (order {order_ref or '-'})")
                    tool_name = "freshdesk_create_ticket"
                    tool_args = {
                        "subject": (f"Support request for order {order_ref}" if order_ref else "Support request from chat")[:255],
                        "description": reason if len(reason) >= 5 else f"Customer request: {reason}",
                        "priority": "medium",
                        "order_id": order_ref,
                        "company_id": tenant_id,
                    }
                if tracking_enabled and tool_name in SHIPPING_REPLACED_TOOLS:
                    log_agent(f"[TRACKING REDIRECT]: {tool_name} -> get_tracking_status (order {tool_args.get('order_id') or '-'})")
                    tool_name = "get_tracking_status"
                    tool_args = {"order_id": str(tool_args.get("order_id") or ""), "company_id": tenant_id}
                identity_note = ""
                if (tool_name in IDENTITY_TOOLS or (helpdesk_enabled and tool_name in FRESHDESK_IDENTITY_TOOLS)
                        or (tracking_enabled and tool_name in SHIPPO_IDENTITY_TOOLS)):
                    source = apply_identity(tool_args, authenticated_customer_id, session_identity)
                    identity_note = (
                        f" | Identity source: {source} | email: {_mask(tool_args.get('email'))}"
                        f" | phone: {_mask(tool_args.get('phone'))}"
                    )
                if "order_id" in tool_args:
                    original = tool_args["order_id"]
                    tool_args["order_id"] = preserve_order_id(str(original), user_order_ids)
                    if tool_args["order_id"] != original:
                        log_agent(
                            f"[ORDER ID GUARD]: LLM passed '{original}', restored user identifier '{tool_args['order_id']}'"
                        )
                if tool_name in ["cancel_order_tool", "prepare_cancellation_tool", "request_return_tool", "escalate_to_human_tool"]:
                    tool_args["session_id"] = sess_id
                if helpdesk_enabled and tool_name in FRESHDESK_WRITE_TOOLS:
                    tool_args["session_id"] = sess_id
                if shipping_enabled and tool_name in SHIPPO_WRITE_TOOLS:
                    tool_args["session_id"] = sess_id

                safe_args = {
                    k: (_mask(v) if k in ("email", "phone") else v) for k, v in tool_args.items()
                }
                log_agent(
                    f"[Executing Tool]: Tenant: {tenant_id} | Tool: {tool_name} | "
                    f"company_id: {tool_args['company_id']}{identity_note} | args: {safe_args}"
                )

                blocked = (
                    verify_ticket_id(tool_args, messages)
                    if helpdesk_enabled and tool_name in FRESHDESK_TICKET_ID_TOOLS else None
                ) or (
                    verify_tracking_number(tool_args, messages)
                    if tracking_enabled and tool_name in SHIPPO_IDENTITY_TOOLS else None
                ) or (
                    verify_record_ids(tool_args, messages) if crm_enabled and tool_name in crm_tools_dict else None
                ) or (
                    verify_hubspot_ids(tool_args, messages) if hubspot_enabled and tool_name in hubspot_tools_dict else None
                ) or (
                    verify_salesforce_ids(tool_args, messages)
                    if salesforce_enabled and tool_name in salesforce_tools_dict else None
                ) or (
                    verify_razorpay_ids(tool_args, messages)
                    if razorpay_enabled and tool_name in razorpay_tools_dict else None
                )
                if helpdesk_enabled and tool_name in HELPDESK_REPLACED_TOOLS:
                    tool_output = json.dumps({
                        "error": "Support tickets for this store live in the helpdesk; use freshdesk_get_complaint_status.",
                        "code": "USE_HELPDESK",
                    })
                elif blocked:
                    log_agent(f"[IDENTIFIER GUARD]: blocked {tool_name}: {blocked['code']}")
                    tool_output = json.dumps(blocked)
                elif tool_name in provider_tools:
                    try:
                        tool_output = provider_tools[tool_name].invoke(tool_args)
                    except Exception as e:  # schema validation (invalid status, ticket id, ...)
                        log_agent(f"[TOOL VALIDATION]: {tool_name} rejected: {type(e).__name__}")
                        tool_output = json.dumps({"error": "Invalid arguments for this action.", "code": "INVALID_ARGUMENT"})
                    if tool_name in FRESHDESK_IDENTITY_TOOLS and not authenticated_customer_id:
                        remember_helpdesk_identity(tool_args, tool_output, session_identity)
                elif tool_name in dispatch:
                    selected_tool = dispatch[tool_name]
                    tool_output = selected_tool.invoke(tool_args)
                    if tool_name in IDENTITY_TOOLS and not authenticated_customer_id:
                        remember_identity(tool_args, tool_output, session_identity)
                    if helpdesk_enabled and tool_name == "cancel_order_tool":
                        tool_output = sync_tickets_after_cancellation(
                            tool_output, tool_args.get("order_id"), tenant_id, sess_id, session_identity
                        )
                else:
                    tool_output = f"Error: Tool {tool_name} not found."

                log_agent(f"[Tool Result]: {tool_output}")
                tool_events.append((tool_name, tool_output))

                messages.append(
                    ToolMessage(
                        content=str(tool_output), tool_call_id=tool_call_id
                    )
                )

            iteration += 1

        final_msg = bound_llm.invoke(
            messages
            + [SystemMessage(content="Tool budget exhausted. Answer the user now from the tool results above without calling any tools. If the tools returned errors, say the store system could not complete the request.")]
        )
        res_text = (
            final_msg.content
            if final_msg.content
            else "Request completed after maximum tool executions."
        )
        log_agent(f"---> [AGENT COMPLETED]: {res_text}\n")
        return res_text

    except Exception as e:
        log_agent(f"[AGENT ERROR]: {e}")
        return (
            "Sorry, I couldn't complete that request because the store system returned "
            "an error. Please try again in a moment."
        )