import datetime
from typing import Any, Dict, Optional
from integrations.models import PolicyDecision, ReturnEligibility


class CancellationPolicyEngine:
    """
    Provider-agnostic business policy engine for order cancellation eligibility.
    Decides whether an order can be cancelled, if confirmation is required,
    and what refund rules apply.
    """

    NON_CANCELLABLE_STATUSES = {
        "FULFILLED",
        "SHIPPED",
        "DELIVERED",
        "IN_TRANSIT",
        "CANCELLED",
        "PARTIALLY_FULFILLED",
    }

    @classmethod
    def evaluate(
        cls,
        order: Dict[str, Any],
        request_reason: Optional[str] = None,
        auto_refund_on_cancel: bool = True,
    ) -> PolicyDecision:
        """
        Evaluates cancellation policy for a canonical Order dictionary.

        auto_refund_on_cancel: whether the provider's cancel action also refunds the
        payment. When it does not (e.g. WooCommerce), paid orders are never self-serve
        cancelled: they require human review so the customer is not left unrefunded.
        """
        if not order or "error" in order:
            return PolicyDecision(
                is_cancellable=False,
                refusal_reason="Order details could not be retrieved.",
                requires_confirmation=False,
            )

        status = str(order.get("status") or "").upper()
        fulfillment_status = str(order.get("fulfillment_status") or "").upper()
        financial_status = str(order.get("financial_status") or "").upper()
        fully_paid = order.get("fully_paid")
        total = float(order.get("total") or 0.0)

        # Check if already cancelled
        if status == "CANCELLED" or fulfillment_status == "CANCELLED":
            return PolicyDecision(
                is_cancellable=False,
                refusal_reason=f"Order {order.get('id')} has already been cancelled.",
                requires_confirmation=False,
                financial_status=financial_status,
                fulfillment_status=fulfillment_status,
            )

        # Check fulfillment status
        if status in cls.NON_CANCELLABLE_STATUSES or fulfillment_status in cls.NON_CANCELLABLE_STATUSES:
            reason_msg = (
                f"Order {order.get('id')} cannot be cancelled because its status is "
                f"'{fulfillment_status or status}'. Only unfulfilled or processing orders can be cancelled."
            )
            return PolicyDecision(
                is_cancellable=False,
                refusal_reason=reason_msg,
                requires_confirmation=False,
                financial_status=financial_status,
                fulfillment_status=fulfillment_status or status,
            )

        # Check shipment sub-object if present
        shipment = order.get("shipment")
        if isinstance(shipment, dict) and shipment.get("status"):
            ship_status = str(shipment["status"]).upper()
            if ship_status in cls.NON_CANCELLABLE_STATUSES:
                return PolicyDecision(
                    is_cancellable=False,
                    refusal_reason=f"Order {order.get('id')} has shipment in status '{ship_status}' and cannot be cancelled.",
                    requires_confirmation=False,
                    financial_status=financial_status,
                    fulfillment_status=ship_status,
                )

        # Determine refund recommendation
        is_paid = (fully_paid is True) or (financial_status in ["PAID", "REFUNDED", "PARTIALLY_REFUNDED"])
        if is_paid and not auto_refund_on_cancel:
            return PolicyDecision(
                is_cancellable=False,
                refusal_reason=(
                    f"Order {order.get('id')} is paid. Cancelling it in this store does not refund "
                    "the payment automatically, so the cancellation must be reviewed by the merchant "
                    "who will issue the refund."
                ),
                requires_confirmation=False,
                requires_human_review=True,
                refund_recommended=True,
                financial_status=financial_status,
                fulfillment_status=fulfillment_status or status,
            )
        if is_paid:
            refund_recommended = True
            refund_note = (
                f"Order total of ${total:.2f} was paid. An automatic full refund to "
                "the original payment method will be issued upon cancellation."
            )
        else:
            refund_recommended = False
            refund_note = (
                "Order is unpaid or pending payment. Payment authorization will be voided "
                "with no charge to the customer."
            )

        return PolicyDecision(
            is_cancellable=True,
            refusal_reason=None,
            requires_confirmation=True,
            refund_recommended=refund_recommended,
            refund_note=refund_note,
            financial_status=financial_status,
            fulfillment_status=fulfillment_status or status,
        )


class ReturnPolicyEngine:
    """
    Provider-neutral return eligibility, driven by merchant settings in company_rules
    (return_window_days, max_auto_refund_amount). Providers only supply canonical orders
    and refund history; the rules live here so every provider behaves identically.
    """

    RETURNABLE_STATUSES = {"FULFILLED", "DELIVERED", "COMPLETED"}
    NON_RETURNABLE_STATUSES = {"CANCELLED", "REFUNDED", "VOIDED", "FAILED"}

    @staticmethod
    def _parse_date(value: Optional[str]) -> Optional[datetime.datetime]:
        if not value:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)

    @classmethod
    def evaluate(
        cls,
        order: Dict[str, Any],
        rules: Optional[Dict[str, Any]],
        requested_amount: Optional[float] = None,
        already_refunded: float = 0.0,
        now: Optional[datetime.datetime] = None,
    ) -> ReturnEligibility:
        rules = rules or {}
        window = int(rules.get("return_window_days") or 30)
        auto_limit = float(rules.get("max_auto_refund_amount") or 0.0)
        order_id = str(order.get("id") or "")
        total = float(order.get("total") or 0.0)
        max_refundable = max(round(total - float(already_refunded or 0.0), 2), 0.0)

        def result(eligible: bool, reason: str, **extra) -> ReturnEligibility:
            return ReturnEligibility(
                order_id=order_id,
                eligible=eligible,
                reason=reason,
                return_window_days=window,
                order_total=total,
                already_refunded=float(already_refunded or 0.0),
                max_refundable=max_refundable,
                requested_amount=requested_amount,
                auto_approval_limit=auto_limit,
                **extra,
            )

        statuses = {
            str(order.get(k) or "").upper()
            for k in ("status", "fulfillment_status", "financial_status")
        }
        if statuses & cls.NON_RETURNABLE_STATUSES:
            return result(False, f"Order {order_id} is cancelled or already refunded, so it cannot be returned.")
        if not statuses & cls.RETURNABLE_STATUSES:
            return result(
                False,
                f"Order {order_id} has not been fulfilled/delivered yet, so it cannot be returned. "
                "An unfulfilled order may be eligible for cancellation instead.",
            )

        reference = cls._parse_date(order.get("completed_at")) or cls._parse_date(order.get("created_at"))
        extra = {}
        if reference:
            now = now or datetime.datetime.now(datetime.timezone.utc)
            days = (now - reference).days
            extra = {"days_since_reference": days, "reference_date": reference.date().isoformat()}
            if days > window:
                return result(False, f"The {window}-day return window for order {order_id} has passed ({days} days).", **extra)

        if max_refundable <= 0:
            return result(False, f"Order {order_id} has already been fully refunded.", **extra)
        if requested_amount is not None:
            if requested_amount <= 0:
                return result(False, "The requested refund amount must be greater than zero.", **extra)
            if requested_amount > max_refundable:
                return result(
                    False,
                    f"Requested amount ${requested_amount:.2f} exceeds the refundable balance ${max_refundable:.2f}.",
                    **extra,
                )

        needs_review = requested_amount is not None and requested_amount > auto_limit
        return result(
            True,
            "Order is within the return window."
            + (f" Amounts above ${auto_limit:.2f} require merchant review." if needs_review else ""),
            requires_human_review=needs_review,
            **extra,
        )
