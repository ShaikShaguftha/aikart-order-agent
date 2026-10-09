"""
Deterministic mapping from a store payment transaction to a payment-provider payment.

A store (e.g. Shopify) records which payment gateway processed each transaction. This module
is the ONLY place that decides which payment provider a transaction belongs to and which
provider payment reference it carries:

  * provider   <- the transaction's gateway name, matched against an explicit rule
                  (never the shape or prefix of an ID);
  * reference  <- only the fields the rule lists, in order, and only if the value has the
                  provider's payment-ID format.

When the gateway is missing or not covered by a rule, or no listed field holds a valid
reference, the mapping is reported as unavailable. Nothing is guessed, and callers never
receive the raw gateway receipt (it can contain payment details).

Rule status: the Razorpay rule's reference fields follow Razorpay's payment response
(`razorpay_payment_id`). They have NOT yet been observed on a real Shopify order paid through
Razorpay; confirm them against such an order before relying on live mapping.
"""
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

MAPPED = "MAPPED"
GATEWAY_MISSING = "GATEWAY_MISSING"
GATEWAY_NOT_SUPPORTED = "GATEWAY_NOT_SUPPORTED"
PAYMENT_REFERENCE_MISSING = "PAYMENT_REFERENCE_MISSING"


@dataclass(frozen=True)
class GatewayRule:
    provider: str                       # payment provider id, e.g. "RAZORPAY"
    gateway_pattern: "re.Pattern"       # full match on the lower-cased store gateway name
    reference_fields: Tuple[str, ...]   # "receipt.<key>" lookups, tried in order
    reference_pattern: "re.Pattern"     # the provider's payment-ID format


GATEWAY_RULES = (
    GatewayRule(
        provider="RAZORPAY",
        # "razorpay" itself or a razorpay-prefixed gateway handle ("razorpay_cards_upi", "razorpay upi").
        gateway_pattern=re.compile(r"razorpay(?:[ _-].*)?"),
        reference_fields=("receipt.razorpay_payment_id", "receipt.payment_id"),
        reference_pattern=re.compile(r"pay_[A-Za-z0-9]{8,40}"),
    ),
)


def parse_receipt(receipt: Any) -> Dict[str, Any]:
    """Gateway receipts arrive as a JSON object or a JSON string; anything else is empty."""
    if isinstance(receipt, dict):
        return receipt
    if isinstance(receipt, str) and receipt.strip():
        try:
            parsed = json.loads(receipt)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def rule_for_gateway(gateway: Optional[str]) -> Optional[GatewayRule]:
    name = (gateway or "").strip().lower()
    for rule in GATEWAY_RULES:
        if name and rule.gateway_pattern.fullmatch(name):
            return rule
    return None


def map_transaction(gateway: Optional[str], receipt: Any) -> Dict[str, Optional[str]]:
    """
    {"payment_provider", "payment_reference", "payment_reference_source", "mapping_status"} for
    one store transaction. Only a MAPPED result carries a provider and a reference.
    """
    empty = {"payment_provider": None, "payment_reference": None, "payment_reference_source": None}
    if not (gateway or "").strip():
        return {**empty, "mapping_status": GATEWAY_MISSING}
    rule = rule_for_gateway(gateway)
    if not rule:
        return {**empty, "mapping_status": GATEWAY_NOT_SUPPORTED}
    fields = parse_receipt(receipt)
    for source in rule.reference_fields:
        value = fields.get(source.split(".", 1)[1])
        if isinstance(value, str) and rule.reference_pattern.fullmatch(value.strip()):
            return {"payment_provider": rule.provider, "payment_reference": value.strip(),
                    "payment_reference_source": source, "mapping_status": MAPPED}
    return {**empty, "payment_provider": rule.provider, "mapping_status": PAYMENT_REFERENCE_MISSING}
