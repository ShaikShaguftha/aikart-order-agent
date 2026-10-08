import sys
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from integrations.models import CustomerIdentity


def log_provider_call(provider: str, operation: str, http_status: Any) -> None:
    """Safe diagnostic line for every outbound provider API call (never logs credentials)."""
    try:
        stream = sys.stdout or sys.__stdout__
        if stream is not None:
            stream.write(
                f"[PROVIDER] Provider: {provider} | Operation: {operation} | "
                f"API request executed: YES | Provider HTTP status: {http_status}\n"
            )
            stream.flush()
    except Exception:
        pass


def log_provider_warning(provider: str, operation: str, message: str) -> None:
    """Safe diagnostic line for provider responses that were only partially usable."""
    sys.__stdout__.write(f"[PROVIDER WARNING] Provider: {provider} | Operation: {operation} | {message}\n")
    sys.__stdout__.flush()


class BaseConnector(ABC):
    """
    Abstract Base Class for E-commerce Provider Connectors.
    All external connectors (ShopifyConnector, WooCommerceConnector, LocalDBConnector)
    must implement these provider-agnostic domain capability methods.
    """

    provider_name = "UNKNOWN"
    # Whether the provider's cancel action also refunds a paid order.
    cancel_refunds_automatically = True
    # True when the provider has no native returns/helpdesk API and Luintix records
    # return requests / support tickets in its shared stores (integrations/support.py).
    managed_returns = False
    managed_tickets = False

    def credentials_configured(self) -> bool:
        """Whether the provider credentials needed for API calls are present."""
        return True

    def domain_configured(self) -> bool:
        """Whether the provider store domain/site URL is configured."""
        return True

    @abstractmethod
    def get_shop(self, company_id: str) -> Dict[str, Any]:
        """Retrieve merchant shop details."""
        pass

    @abstractmethod
    def get_orders(self, company_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve list of orders for tenant."""
        pass

    @abstractmethod
    def get_customer_details(
        self, identifier: str, company_id: str
    ) -> List[Dict[str, Any]]:
        """Retrieve customer record and associated order history by ID/email/order ID."""
        pass

    @abstractmethod
    def get_order_details(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve status, line items, total, and timestamps for a specific order."""
        pass

    @abstractmethod
    def get_shipment_status(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        """Retrieve shipping status, tracking number, and courier details."""
        pass

    @abstractmethod
    def cancel_order(
        self, order_id: str, company_id: str, reason: Optional[str] = "CUSTOMER"
    ) -> Dict[str, Any]:
        """Attempt to cancel an order based on policy rules."""
        pass

    @abstractmethod
    def request_return(
        self, order_id: str, refund_amount: float, company_id: str
    ) -> Dict[str, Any]:
        """Process a return or refund request against dynamic auto-refund rules."""
        pass

    @abstractmethod
    def check_proactive_notifications(
        self, customer_id: str, company_id: str
    ) -> Optional[str]:
        """Check for active carrier delays or proactive alerts for a customer account."""
        pass

    @abstractmethod
    def escalate_to_human(
        self, order_id: str, reason: str, company_id: str
    ) -> Dict[str, Any]:
        """Create a human escalation ticket in CRM/Helpdesk system."""
        pass

    @abstractmethod
    def get_ticket_details(
        self, ticket_id: str, company_id: str
    ) -> Dict[str, Any]:
        """Retrieve status and audit details for a human support ticket."""
        pass

    def get_customer(
        self, identifier: str, company_id: str
    ) -> Dict[str, Any]:
        """Retrieve customer record by ID, email, or name."""
        records = self.get_customer_details(identifier, company_id)
        if records:
            return records[0]
        return {"error": f"Customer '{identifier}' not found."}

    def find_customer(
        self, identity: CustomerIdentity, company_id: str
    ) -> Dict[str, Any]:
        """
        Resolve a customer from identity.primary() (customer_id / email / phone).
        Returns {"customer": {"customer_id", "email", "phone", "name", ...}},
        {"customer": None} when no exact match exists, or {"error": ...}.
        """
        return {"error": f"Customer lookup is not supported for provider {self.provider_name}."}

    def get_customer_orders(
        self, customer: Dict[str, Any], company_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Canonical orders for a resolved customer (from find_customer), newest first."""
        return [{"error": f"Customer order lookup is not supported for provider {self.provider_name}."}]

    def get_product(
        self, product_id: str, company_id: str
    ) -> Dict[str, Any]:
        """Retrieve product catalog entry by ID or slug."""
        return {"error": f"Product capability not supported for this provider."}

    def get_order_status(
        self, order_id: str, company_id: str
    ) -> Dict[str, Any]:
        """Retrieve high-level status for a specific order."""
        details = self.get_order_details(order_id, company_id)
        if not details or "error" in details:
            return details or {"error": f"Order '{order_id}' not found."}
        return {
            "order_id": order_id,
            "status": details.get("status"),
            "financial_status": details.get("financial_status"),
            "fulfillment_status": details.get("fulfillment_status"),
        }

    def get_inventory(
        self, product_query: str, company_id: str
    ) -> Dict[str, Any]:
        """Stock levels for a product (and its variants)."""
        return {"error": f"Inventory lookup is not supported for provider {self.provider_name}.", "code": "NOT_SUPPORTED"}

    def update_order(
        self, order_id: str, company_id: str, **kwargs
    ) -> Dict[str, Any]:
        """Update order fields or status on provider API."""
        return {"error": "Update order capability not supported for this provider."}

    def get_order_transactions(
        self, order_id: str, company_id: str
    ) -> Dict[str, Any]:
        """
        Payment transactions recorded on an order:
        {"order_id", "transactions": [OrderTransaction dicts], "count", "provider"} or {"error", "code"}.
        """
        return {"error": f"Order payment transactions are not supported for provider {self.provider_name}.",
                "code": "NOT_SUPPORTED"}

    def get_refunds(
        self, order_id: str, company_id: str
    ) -> Dict[str, Any]:
        """Retrieve refund status and historical refund records for an order."""
        return {"error": "Refund read operations not supported for this provider."}

