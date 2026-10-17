import re
from typing import List, Optional, Dict, Any, Tuple
from pydantic import BaseModel, Field

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OrderItem(BaseModel):
    """Canonical representation of an order line item."""
    product_name: str
    quantity: int
    price: float


class Shipment(BaseModel):
    """Canonical representation of a shipment/fulfillment."""
    id: Optional[str] = None
    order_id: str
    courier: Optional[str] = None
    tracking_number: Optional[str] = None
    status: str
    tracking_url: Optional[str] = None


class Order(BaseModel):
    """Canonical representation of an order across e-commerce providers."""
    id: str
    company_id: str
    customer_id: str
    status: str
    total: float
    created_at: Optional[str] = None
    completed_at: Optional[str] = None
    # Provider-internal order ID when it differs from the customer-facing order number
    # (e.g. WooCommerce stores with custom order numbering). Never shown as the order ID.
    provider_order_id: Optional[str] = None
    financial_status: Optional[str] = None
    fulfillment_status: Optional[str] = None
    fully_paid: Optional[bool] = None
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    items: List[OrderItem] = Field(default_factory=list)
    shipment: Optional[Shipment] = None


class OrderTransaction(BaseModel):
    """
    Canonical payment transaction recorded on a store order (provider-neutral; e.g. Shopify
    OrderTransaction). kind/status/gateway are the store's values. payment_provider and
    payment_reference are set only when integrations.payment_mapping deterministically mapped
    the gateway and found a valid provider payment ID (mapping_status == "MAPPED").
    """
    id: str
    kind: Optional[str] = None
    status: Optional[str] = None
    gateway: Optional[str] = None
    gateway_display_name: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    # The store platform's own payment identifier (not the gateway's payment ID).
    store_payment_id: Optional[str] = None
    manual_gateway: Optional[bool] = None
    test: Optional[bool] = None
    parent_transaction_id: Optional[str] = None
    created_at: Optional[str] = None
    processed_at: Optional[str] = None
    payment_provider: Optional[str] = None
    payment_reference: Optional[str] = None
    payment_reference_source: Optional[str] = None
    mapping_status: str = "GATEWAY_MISSING"
    provider: str = "UNKNOWN"


class Customer(BaseModel):
    """Canonical representation of a customer record."""
    customer_id: str
    company_id: str
    email: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    orders: List[Order] = Field(default_factory=list)


def normalize_phone(phone: Optional[str]) -> str:
    """Digits only, for comparing phone numbers across provider formats."""
    return re.sub(r"\D", "", phone or "")


class CustomerIdentity(BaseModel):
    """
    Who the customer is, in priority order:
      1. customer_id  - only when it comes from an authenticated session (authenticated=True)
      2. email        - supplied by the customer
      3. phone        - supplied by the customer
    name is never sufficient on its own; it is only cross-checked against the
    resolved customer record. Explicit order IDs are handled by order lookup tools.
    """
    customer_id: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    name: Optional[str] = None
    authenticated: bool = False

    def primary(self) -> Optional[Tuple[str, str]]:
        """(kind, value) of the strongest usable identifier, or None."""
        if self.customer_id and self.authenticated:
            return ("customer_id", self.customer_id.strip())
        if self.email and EMAIL_PATTERN.match(self.email.strip()):
            return ("email", self.email.strip())
        if self.phone and len(normalize_phone(self.phone)) >= 7:
            return ("phone", self.phone.strip())
        return None

    def validation_error(self) -> Optional[str]:
        if self.primary():
            return None
        if self.email or self.phone:
            return "The email address or phone number provided is not valid."
        if self.name:
            return (
                "A name alone is not enough to identify a customer. "
                "Please provide the email address or phone number used for the order."
            )
        return "Customer identity required: please provide the email address or phone number used for the order."


class CancelOrderRequest(BaseModel):
    """Request schema for order cancellation write operation."""
    order_id: str
    company_id: Optional[str] = None
    reason: Optional[str] = "CUSTOMER"
    session_id: Optional[str] = None
    confirmed: bool = False


class PolicyDecision(BaseModel):
    """Canonical policy evaluation result for write actions."""
    is_cancellable: bool
    refusal_reason: Optional[str] = None
    requires_confirmation: bool = True
    refund_recommended: bool = False
    refund_note: Optional[str] = None
    requires_human_review: bool = False
    financial_status: Optional[str] = None
    fulfillment_status: Optional[str] = None


class PendingAction(BaseModel):
    """Record representing a staged write action requiring user confirmation."""
    action_id: str
    action_type: str = "CANCEL_ORDER"
    session_id: str
    company_id: str
    order_id: str
    reason: str
    created_at: str
    policy_decision: Optional[PolicyDecision] = None


class CancelOrderResult(BaseModel):
    """Canonical result schema for order cancellation execution."""
    success: bool = True
    status: str  # CANCELLED, REJECTED, REQUIRES_CONFIRMATION, ERROR
    order_id: str
    product_name: Optional[str] = None
    reason: Optional[str] = None
    message: str
    refund_issued: bool = False
    refund_amount: Optional[float] = None
    verification_status: Optional[str] = "VERIFIED"
    details: Optional[Dict[str, Any]] = None


class RequestReturnResult(BaseModel):
    """Result schema for return/refund request."""
    status: str
    reason: Optional[str] = None
    message: Optional[str] = None


class TicketDetails(BaseModel):
    """Canonical schema for human escalation ticket details."""
    ticket_id: str
    status: str
    associated_order: str
    details: str
    created_at: str


class ShopInfo(BaseModel):
    """Canonical representation of merchant shop details."""
    name: str
    domain: str
    email: Optional[str] = None
    currency: Optional[str] = None
    provider: str = "SHOPIFY"


class ReturnEligibility(BaseModel):
    """Provider-neutral result of evaluating an order against the merchant's return policy."""
    order_id: str
    eligible: bool
    reason: str
    return_window_days: int
    days_since_reference: Optional[int] = None
    reference_date: Optional[str] = None
    order_total: float = 0.0
    already_refunded: float = 0.0
    max_refundable: float = 0.0
    requested_amount: Optional[float] = None
    auto_approval_limit: float = 0.0
    requires_human_review: bool = False


class CRMContact(BaseModel):
    """Canonical CRM contact (provider-neutral; e.g. Zoho CRM Contacts)."""
    id: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    # Linked account ID when the provider returns one (lets Luintix read the account next).
    account_id: Optional[str] = None
    created_at: Optional[str] = None
    provider: str = "UNKNOWN"


class CRMAccount(BaseModel):
    """Canonical CRM account / company."""
    id: str
    name: Optional[str] = None
    website: Optional[str] = None
    phone: Optional[str] = None
    provider: str = "UNKNOWN"


class CRMTicket(BaseModel):
    """Canonical CRM support ticket (e.g. HubSpot Tickets, Salesforce Cases). Stage/priority are the provider's values."""
    id: str
    # Human-facing reference when it differs from the record ID (e.g. Salesforce CaseNumber).
    number: Optional[str] = None
    subject: Optional[str] = None
    stage: Optional[str] = None
    pipeline: Optional[str] = None
    priority: Optional[str] = None
    description: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    provider: str = "UNKNOWN"


class CRMDeal(BaseModel):
    """Canonical CRM deal / opportunity."""
    id: str
    name: Optional[str] = None
    amount: Optional[float] = None
    stage: Optional[str] = None
    closing_date: Optional[str] = None
    provider: str = "UNKNOWN"


class PaymentRecord(BaseModel):
    """Canonical payment (e.g. Razorpay Payments). amount is in major units; amount_minor is the
    provider's smallest-unit integer (e.g. paise)."""
    id: str
    amount: Optional[float] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    status: Optional[str] = None
    order_id: Optional[str] = None
    method: Optional[str] = None
    email: Optional[str] = None
    contact: Optional[str] = None
    captured: Optional[bool] = None
    amount_refunded: Optional[float] = None
    refund_status: Optional[str] = None
    created_at: Optional[str] = None
    provider: str = "UNKNOWN"


class RefundRecord(BaseModel):
    """Canonical refund of a payment (e.g. Razorpay Refunds). Status/speed are the provider's values."""
    id: str
    payment_id: Optional[str] = None
    amount: Optional[float] = None
    amount_minor: Optional[int] = None
    currency: Optional[str] = None
    status: Optional[str] = None
    speed: Optional[str] = None
    created_at: Optional[str] = None
    provider: str = "UNKNOWN"


class ShadowfaxTrackingEvent(BaseModel):
    """Normalized tracking event from Shadowfax."""
    timestamp: Optional[str] = None
    status: str
    location: Optional[str] = None
    description: Optional[str] = None


class ShadowfaxShipment(BaseModel):
    """Normalized shipment tracking result from Shadowfax."""
    awb: str
    status: str
    courier: str = "SHADOWFAX"
    estimated_delivery: Optional[str] = None
    pickup_date: Optional[str] = None
    delivered_date: Optional[str] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    latest_event: Optional[ShadowfaxTrackingEvent] = None
    tracking_history: List[ShadowfaxTrackingEvent] = Field(default_factory=list)


class ShadowfaxServiceability(BaseModel):
    """Normalized pincode serviceability result from Shadowfax."""
    pickup_pincode: str
    delivery_pincode: str
    serviceable: bool
    service_info: Optional[Dict[str, Any]] = None


class ShadowfaxReversePickup(BaseModel):
    """Normalized reverse pickup tracking result from Shadowfax."""
    reverse_awb: str
    pickup_status: str
    courier: str = "SHADOWFAX"
    scheduled_date: Optional[str] = None
    pickup_date: Optional[str] = None
    delivered_date: Optional[str] = None
    latest_event: Optional[ShadowfaxTrackingEvent] = None
    tracking_history: List[ShadowfaxTrackingEvent] = Field(default_factory=list)

