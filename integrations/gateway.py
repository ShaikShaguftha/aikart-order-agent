import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
import env_config  # noqa: F401  (connectors read provider config at construction)
import sqlite3
from database import execute_db, get_company_rules, get_db_connection, query_db
from integrations.base import BaseConnector
from integrations.providers.local_db import LocalDBConnector
from integrations.providers.shopify import ShopifyConnector
from integrations.providers.woocommerce import WooCommerceConnector
from integrations.providers.zoho_crm import ZohoCRMConnector
from integrations.providers.hubspot import HubSpotConnector
from integrations.providers.salesforce import SalesforceConnector
from integrations.providers.razorpay import RazorpayConnector
from integrations.providers.freshdesk import FreshdeskConnector
from integrations.providers.delhivery import DelhiveryConnector, is_delhivery_carrier
from integrations.providers.shadowfax import ShadowfaxConnector
from integrations.providers.shippo import (
    TEST_CARRIER,
    TEST_TRACKING_NUMBERS,
    ShippoConnector,
    carrier_slug_for,
    valid_tracking_number,
)
from integrations.policy import CancellationPolicyEngine, ReturnPolicyEngine
from integrations.support import ReturnRequestStore, SupportTicketStore
from integrations.confirmation import confirmation_manager
from integrations.audit import AuditLogger
from integrations import credential_store
from integrations.errors import TenantConfigurationError
from integrations.models import CustomerIdentity
from integrations.payment_mapping import MAPPED
from integrations.support_workflow import (
    OPEN_STATUSES as SUPPORT_OPEN_STATUSES,
    classify_complaint,
    decide_reconciliation,
    extract_order_reference,
    intent_tag,
    normalize_order_ref,
    ticket_intent,
    ticket_matches_order,
)


def log_gateway(msg: str):
    sys.__stdout__.write(f"{msg}\n")
    sys.__stdout__.flush()


class IntegrationGateway:
    """
    Central Integration Gateway layer.
    Routes provider-agnostic capability requests from tools to the appropriate
    configured provider connector (LocalDBConnector, ShopifyConnector, WooCommerceConnector).
    """

    def __init__(self):
        self._local_db_connector = LocalDBConnector()
        self._shopify_connector = ShopifyConnector()
        self._woocommerce_connector = WooCommerceConnector()
        self._connectors: Dict[str, BaseConnector] = {
            "LOCAL_DB": self._local_db_connector,
            "SHOPIFY": self._shopify_connector,
            "WOOCOMMERCE": self._woocommerce_connector,
        }

    def _resolve_connector(self, company_id: str) -> Tuple[BaseConnector, str]:
        """
        Resolves the connector for a tenant from company_integrations.

        Credential precedence (documented, intentional): per-tenant credentials stored
        in company_integrations are used only when they are set; otherwise the connector
        uses the .env credentials. Returns (connector, credential_source).

        There is NO silent LocalDB fallback: a tenant without an integration row, or
        with an unknown provider_type, raises TenantConfigurationError.
        """
        if not company_id:
            raise TenantConfigurationError("No tenant (X-Company-ID) supplied for this request.")

        integration = query_db(
            "SELECT * FROM company_integrations WHERE company_id = ?",
            (company_id,),
            one=True,
        )
        if not integration:
            raise TenantConfigurationError(
                f"Tenant '{company_id}' has no row in company_integrations."
            )

        provider_type = (integration.get("provider_type") or "").upper()
        if provider_type == "LOCAL_DB":
            return self._local_db_connector, "LOCAL_DB"

        if provider_type == "SHOPIFY":
            shop_domain = integration.get("shop_domain")
            access_token = integration.get("access_token")
            if shop_domain or access_token:
                return (
                    ShopifyConnector(
                        shop_domain=shop_domain,
                        access_token=access_token,
                        api_version=integration.get("api_version"),
                    ),
                    "DATABASE",
                )
            return self._shopify_connector, "ENV"

        if provider_type == "WOOCOMMERCE":
            return self._resolve_woocommerce(company_id, integration)

        if provider_type == "ZOHO_CRM":
            return self._resolve_zoho_crm(company_id)

        if provider_type == "HUBSPOT":
            return self._resolve_hubspot(company_id)

        if provider_type == "SALESFORCE":
            return self._resolve_salesforce(company_id)

        if provider_type == "RAZORPAY":
            return self._resolve_razorpay(company_id)

        raise TenantConfigurationError(
            f"Tenant '{company_id}' has unsupported provider_type '{provider_type}'."
        )

    def _resolve_woocommerce(
        self, company_id: str, integration: Dict[str, Any]
    ) -> Tuple[BaseConnector, str]:
        """
        Merchant-specific WooCommerce configuration.

        - A tenant with stored credentials must have URL, key AND secret stored; they are
          decrypted and never mixed with .env values (no cross-merchant credential leak).
        - .env credentials (WOOCOMMERCE_BASE_URL/KEY/SECRET) belong to exactly one tenant:
          WOOCOMMERCE_ENV_TENANT (default COMP-WOOCOMMERCE). Any other WooCommerce tenant
          without stored credentials is a configuration error, never another store's data.
        """
        stored = {
            "site_url": integration.get("shop_domain"),
            "consumer_key": integration.get("consumer_key"),
            "consumer_secret": integration.get("consumer_secret"),
        }
        if any(stored.values()):
            missing = [k for k, v in stored.items() if not v]
            if missing:
                raise TenantConfigurationError(
                    f"Tenant '{company_id}' has incomplete stored WooCommerce credentials (missing: {', '.join(missing)})."
                )
            api_version = integration.get("api_version") or "v3"
            if not re.fullmatch(r"(wc/)?v\d+", api_version):
                api_version = "v3"
            return (
                WooCommerceConnector(
                    site_url=stored["site_url"],
                    consumer_key=credential_store.decrypt(stored["consumer_key"]),
                    consumer_secret=credential_store.decrypt(stored["consumer_secret"]),
                    api_version=api_version,
                    use_env_fallback=False,
                ),
                "DATABASE",
            )

        env_tenant = os.getenv("WOOCOMMERCE_ENV_TENANT", "COMP-WOOCOMMERCE")
        if company_id != env_tenant:
            raise TenantConfigurationError(
                f"Tenant '{company_id}' has no stored WooCommerce credentials. Connect the merchant's store first."
            )
        return self._woocommerce_connector, "ENV"

    def _resolve_zoho_crm(self, company_id: str) -> Tuple[BaseConnector, str]:
        """
        Zoho CRM (read-only CRM tenant). Phase 1 credentials come from the ZOHO_* environment
        variables and belong to exactly one tenant: ZOHO_ENV_TENANT (default COMP-ZOHO).
        Any other Zoho tenant is a configuration error - never another tenant's CRM.
        """
        env_tenant = os.getenv("ZOHO_ENV_TENANT", "COMP-ZOHO")
        if company_id != env_tenant:
            raise TenantConfigurationError(f"Tenant '{company_id}' has no Zoho CRM credentials configured.")
        connector = ZohoCRMConnector(
            client_id=os.getenv("ZOHO_CLIENT_ID"), client_secret=os.getenv("ZOHO_CLIENT_SECRET"),
            refresh_token=os.getenv("ZOHO_REFRESH_TOKEN"), access_token=os.getenv("ZOHO_ACCESS_TOKEN"),
            api_domain=os.getenv("ZOHO_API_DOMAIN"),
        )
        config_error = connector.configuration_error()
        if config_error:
            raise TenantConfigurationError(f"Tenant '{company_id}': {config_error['error']}")
        return connector, "ENV"

    # Tenants whose registered integration (company_integrations.provider_type) is a standalone
    # logistics provider rather than a store. They have no store connector; their provider is
    # resolved through its own credential-checked resolver.
    STANDALONE_TENANT_PROVIDERS = {"SHADOWFAX"}

    def _resolve_hubspot(self, company_id: str) -> Tuple[BaseConnector, str]:
        """
        HubSpot CRM (read-only CRM tenant). Phase 1: private-app token from HUBSPOT_ACCESS_TOKEN,
        which belongs to exactly one tenant: HUBSPOT_ENV_TENANT (default COMP-HUBSPOT).
        """
        if company_id != os.getenv("HUBSPOT_ENV_TENANT", "COMP-HUBSPOT"):
            raise TenantConfigurationError(f"Tenant '{company_id}' has no HubSpot credentials configured.")
        connector = HubSpotConnector(os.getenv("HUBSPOT_ACCESS_TOKEN"))
        config_error = connector.configuration_error()
        if config_error:
            raise TenantConfigurationError(f"Tenant '{company_id}': {config_error['error']}")
        return connector, "ENV"

    def _resolve_razorpay(self, company_id: str) -> Tuple[BaseConnector, str]:
        """
        Razorpay (read-only payments tenant). Phase 1: API key from RAZORPAY_KEY_ID /
        RAZORPAY_KEY_SECRET (or the older names RAZORPAY_API_KEY / RAZORPAY_SECRET), which belong
        to exactly one tenant: RAZORPAY_ENV_TENANT (default COMP-RAZORPAY).
        """
        if company_id != os.getenv("RAZORPAY_ENV_TENANT", "COMP-RAZORPAY"):
            raise TenantConfigurationError(f"Tenant '{company_id}' has no Razorpay credentials configured.")
        connector = self._razorpay_env_connector()
        config_error = connector.configuration_error()
        if config_error:
            raise TenantConfigurationError(f"Tenant '{company_id}': {config_error['error']}")
        return connector, "ENV"

    def _resolve_salesforce(self, company_id: str) -> Tuple[BaseConnector, str]:
        """
        Salesforce CRM (read-only CRM tenant). Phase 1: access token + instance URL (+ optional API
        version) from SALESFORCE_*, which belong to exactly one tenant: SALESFORCE_ENV_TENANT
        (default COMP-SALESFORCE).
        """
        if company_id != os.getenv("SALESFORCE_ENV_TENANT", "COMP-SALESFORCE"):
            raise TenantConfigurationError(f"Tenant '{company_id}' has no Salesforce credentials configured.")
        connector = SalesforceConnector(os.getenv("SALESFORCE_ACCESS_TOKEN"), os.getenv("SALESFORCE_INSTANCE_URL"),
                                        os.getenv("SALESFORCE_API_VERSION"))
        config_error = connector.configuration_error()
        if config_error:
            raise TenantConfigurationError(f"Tenant '{company_id}': {config_error['error']}")
        return connector, "ENV"

    def get_tenant_primary_connector(self, company_id: str) -> Any:
        """
        Validates that a tenant is configured, as /api/chat requires. For store tenants this is
        exactly get_connector(); for a standalone Shadowfax tenant it resolves the Shadowfax
        connector (which raises TenantConfigurationError if its credentials are missing).
        """
        row = query_db("SELECT provider_type FROM company_integrations WHERE company_id = ?", (company_id,), one=True) \
            if company_id else None
        if row and (row.get("provider_type") or "").upper() == "SHADOWFAX":
            return self.get_shadowfax_connector(company_id)
        return self.get_connector(company_id)

    def get_connector(self, company_id: str) -> BaseConnector:
        """Returns the active connector for a tenant and logs safe routing diagnostics."""
        connector, source = self._resolve_connector(company_id)
        log_gateway(
            f"[GATEWAY] Tenant: {company_id} | Connector: {connector.__class__.__name__} | "
            f"Provider: {connector.provider_name} | Credential source: {source} | "
            f"Credential configured: {'YES' if connector.credentials_configured() else 'NO'}"
        )
        return connector

    def describe_tenants(self) -> List[str]:
        """Safe, secret-free summary of every configured tenant (for startup logs)."""
        lines = []
        for row in query_db("SELECT company_id FROM company_integrations ORDER BY company_id"):
            tenant = row["company_id"]
            try:
                primary = self.get_tenant_primary_connector(tenant) if tenant else None
                if primary is not None and primary.provider_name == "SHADOWFAX":
                    lines.append(
                        f"tenant={tenant} provider=SHADOWFAX (standalone logistics) "
                        f"base_url={primary.base_url} credential configured={'YES' if primary.credentials_configured() else 'NO'} "
                        f"credential source={primary.credential_source}"
                    )
                    continue
                connector, source = self._resolve_connector(tenant)
                lines.append(
                    f"tenant={tenant} provider={connector.provider_name} "
                    f"domain configured={'YES' if connector.domain_configured() else 'NO'} "
                    f"credential configured={'YES' if connector.credentials_configured() else 'NO'} "
                    f"credential source={source}"
                )
            except TenantConfigurationError as e:
                lines.append(f"tenant={tenant} CONFIG ERROR: {e.message}")
        return lines

    def get_shop(self, company_id: str) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        return connector.get_shop(company_id)

    def get_orders(self, company_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        connector = self.get_connector(company_id)
        return connector.get_orders(company_id, limit)

    def get_customer_details(
        self, identifier: str, company_id: str
    ) -> List[Dict[str, Any]]:
        connector = self.get_connector(company_id)
        return connector.get_customer_details(identifier, company_id)

    def get_order_details(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        connector = self.get_connector(company_id)
        return connector.get_order_details(order_id, company_id)

    def get_shipment_status(
        self, order_id: str, company_id: str
    ) -> Optional[Dict[str, Any]]:
        connector = self.get_connector(company_id)
        return connector.get_shipment_status(order_id, company_id)

    # ---- Order payment transactions -> payment provider (read-only) ----------------
    # The provider and payment reference come ONLY from the store's transaction record,
    # mapped by integrations.payment_mapping. Nothing here accepts a payment ID from chat.

    PAYMENT_TRANSACTION_KINDS = {"SALE", "CAPTURE", "AUTHORIZATION"}
    # Provider payment statuses that confirm a store transaction of that kind.
    SETTLED_PAYMENT_STATUSES = {"RAZORPAY": {"SALE": {"captured"}, "CAPTURE": {"captured"},
                                             "AUTHORIZATION": {"authorized", "captured"}}}

    def get_order_transactions(self, order_id: str, company_id: str) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        return connector.get_order_transactions(order_id, company_id)

    @staticmethod
    def _razorpay_env_connector() -> RazorpayConnector:
        return RazorpayConnector(os.getenv("RAZORPAY_KEY_ID") or os.getenv("RAZORPAY_API_KEY"),
                                 os.getenv("RAZORPAY_KEY_SECRET") or os.getenv("RAZORPAY_SECRET"))

    def payment_provider_connector(self, company_id: str, provider: str) -> Tuple[Optional[BaseConnector], Optional[str]]:
        """
        The payment-provider connector a store tenant is EXPLICITLY linked to, or (None, reason).
        Razorpay env credentials serve RAZORPAY_ENV_TENANT and the store tenants listed in
        RAZORPAY_STORE_TENANTS (comma-separated). No link -> PAYMENT_PROVIDER_NOT_CONNECTED.
        """
        if (provider or "").upper() != "RAZORPAY":
            return None, "PAYMENT_PROVIDER_NOT_SUPPORTED"
        linked = {t.strip() for t in os.getenv("RAZORPAY_STORE_TENANTS", "").split(",") if t.strip()}
        if company_id != os.getenv("RAZORPAY_ENV_TENANT", "COMP-RAZORPAY") and company_id not in linked:
            return None, "PAYMENT_PROVIDER_NOT_CONNECTED"
        connector = self._razorpay_env_connector()
        if connector.configuration_error():
            return None, "PAYMENT_PROVIDER_NOT_CONFIGURED"
        return connector, None

    def _verify_payment_transaction(self, company_id: str, t: Dict[str, Any]) -> Dict[str, Any]:
        result = {k: t.get(k) for k in ("id", "kind", "gateway", "amount", "currency", "payment_provider",
                                        "payment_reference", "payment_reference_source", "mapping_status")}
        result["transaction_id"] = result.pop("id")
        if t.get("mapping_status") != MAPPED:
            return {**result, "verification_status": "MAPPING_UNAVAILABLE"}
        connector, problem = self.payment_provider_connector(company_id, t["payment_provider"])
        if problem:
            return {**result, "verification_status": problem}
        payment = connector.get_payment(t["payment_reference"])
        if "error" in payment:
            return {**result, "verification_status": "PROVIDER_ERROR",
                    "provider_error": {"code": payment.get("code"), "error": payment.get("error")}}
        same_currency = bool(payment.get("currency") and t.get("currency")) and \
            payment["currency"].upper() == t["currency"].upper()
        expected = self.SETTLED_PAYMENT_STATUSES.get(t["payment_provider"], {}).get((t.get("kind") or "").upper(), set())
        checks = {
            "payment_found": payment.get("id") == t["payment_reference"],
            "status_matches": (payment.get("status") or "").lower() in expected,
            "currency_matches": same_currency if payment.get("currency") and t.get("currency") else None,
            "amount_matches": (abs(payment["amount"] - t["amount"]) < 0.005)
            if same_currency and payment.get("amount") is not None and t.get("amount") is not None else None,
        }
        status = "VERIFIED" if all(v is not False for v in checks.values()) and checks["payment_found"] else "MISMATCH"
        provider_payment = {k: payment.get(k) for k in ("id", "amount", "currency", "status", "method",
                                                         "amount_refunded", "refund_status", "created_at")}
        return {**result, "verification_status": status, "checks": checks, "provider_payment": provider_payment}

    def verify_order_payment(self, order_id: str, company_id: str) -> Dict[str, Any]:
        """
        Verifies each successful payment transaction of an order with the payment provider that
        processed it (read-only). Provider + payment ID come only from the transaction record.
        """
        tx = self.get_order_transactions(order_id, company_id)
        if "error" in tx:
            return tx
        payments = [t for t in tx["transactions"] if (t.get("kind") or "").upper() in self.PAYMENT_TRANSACTION_KINDS
                    and (t.get("status") or "").upper() == "SUCCESS"]
        if not payments:
            return {"order_id": tx["order_id"], "status": "NO_PAYMENT_TRANSACTION", "transactions_found": tx["count"],
                    "message": "The order has no successful payment transaction, so there is no payment to verify."}
        results = [self._verify_payment_transaction(company_id, t) for t in payments]
        statuses = {r["verification_status"] for r in results}
        if statuses == {"VERIFIED"}:
            overall = "VERIFIED"
        elif "MISMATCH" in statuses:
            overall = "MISMATCH"
        elif statuses <= {"MAPPING_UNAVAILABLE"}:
            overall = "MAPPING_UNAVAILABLE"
        else:
            overall = "NOT_VERIFIED"
        return {"order_id": tx["order_id"], "status": overall, "payments": results, "read_only": True}

    # ---- Identity-first customer capabilities -------------------------------------

    def _resolve_customer(
        self, connector: BaseConnector, identity: CustomerIdentity, company_id: str
    ) -> Dict[str, Any]:
        """Validates identity, resolves the customer on the tenant's provider, cross-checks name."""
        error = identity.validation_error()
        if error:
            return {"error": error, "code": "IDENTITY_REQUIRED"}

        kind, _ = identity.primary()
        result = connector.find_customer(identity, company_id)
        if "error" in result:
            return result
        customer = result.get("customer")
        if not customer:
            return {
                "error": f"No customer found for the provided {kind.replace('_', ' ')}.",
                "code": "CUSTOMER_NOT_FOUND",
            }
        if identity.name and customer.get("name") and (
            identity.name.strip().casefold() != customer["name"].strip().casefold()
        ):
            return {
                "error": "The details provided do not match our customer records.",
                "code": "IDENTITY_MISMATCH",
            }
        log_gateway(
            f"[IDENTITY] Tenant: {company_id} | Identified by: {kind} | "
            f"Authenticated: {'YES' if identity.authenticated else 'NO'} | Customer found: YES"
        )
        return {"customer": customer, "identified_by": kind}

    def get_customer(self, identity: CustomerIdentity, company_id: str) -> Dict[str, Any]:
        """Resolve the customer for this tenant from their identity."""
        connector = self.get_connector(company_id)
        return self._resolve_customer(connector, identity, company_id)

    def get_customer_orders(
        self, identity: CustomerIdentity, company_id: str, limit: int = 10
    ) -> Dict[str, Any]:
        """The identified customer's orders, newest first (fresh provider call)."""
        connector = self.get_connector(company_id)
        resolved = self._resolve_customer(connector, identity, company_id)
        if "error" in resolved:
            return resolved

        orders = connector.get_customer_orders(resolved["customer"], company_id, limit)
        errors = [o for o in orders if isinstance(o, dict) and "error" in o]
        if errors:
            return errors[0]
        orders = sorted(orders, key=lambda o: o.get("created_at") or "", reverse=True)[:limit]
        return {
            "customer": resolved["customer"],
            "identified_by": resolved["identified_by"],
            "orders": orders,
            "count": len(orders),
        }

    def get_recent_orders(
        self, identity: CustomerIdentity, company_id: str, limit: int = 5
    ) -> Dict[str, Any]:
        """The identified customer's most recent orders."""
        return self.get_customer_orders(identity, company_id, limit)

    def get_latest_order(self, identity: CustomerIdentity, company_id: str) -> Dict[str, Any]:
        """
        The identified customer's most recent order, re-read from the provider so
        status/payment/shipping answers always reflect current provider state.
        """
        result = self.get_customer_orders(identity, company_id, limit=5)
        if "error" in result:
            return result
        if not result["orders"]:
            return {"customer": result["customer"], "order": None, "message": "This customer has no orders."}

        latest_id = result["orders"][0]["id"]
        fresh = self.get_order_details(latest_id, company_id)
        if not fresh:
            return {"error": f"Latest order {latest_id} could not be re-read from the provider."}
        if "error" in fresh:
            return fresh
        return {"customer": result["customer"], "order": fresh, "fresh_provider_lookup": True}

    def get_product(self, product_id: str, company_id: str) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        return connector.get_product(product_id, company_id)

    def get_order_status(self, order_id: str, company_id: str) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        return connector.get_order_status(order_id, company_id)

    def update_order(self, order_id: str, company_id: str, **kwargs) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        return connector.update_order(order_id, company_id, **kwargs)

    def get_refunds(self, order_id: str, company_id: str) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        return connector.get_refunds(order_id, company_id)


    def prepare_order_cancellation(
        self,
        session_id: str,
        company_id: str,
        order_id: str,
        reason: Optional[str] = "CUSTOMER",
    ) -> Dict[str, Any]:
        """
        Stage 1 of Safe Action Framework:
        Pre-checks policy eligibility and stages a pending confirmation action.
        """
        connector = self.get_connector(company_id)
        provider_name = connector.__class__.__name__.replace("Connector", "").upper()

        order = connector.get_order_details(order_id, company_id)
        if not order or "error" in order:
            err_msg = order.get("error") if isinstance(order, dict) else f"Order '{order_id}' not found."
            AuditLogger.log_action(
                session_id=session_id,
                company_id=company_id,
                order_id=order_id,
                action_type="CANCEL_ORDER",
                provider=provider_name,
                status="ORDER_NOT_FOUND",
                reason=err_msg,
                refund_issued=False,
                details={"error": err_msg},
            )
            return {
                "status": "FAILED",
                "cancellable": False,
                "reason": err_msg,
                "order_id": order_id,
                "message": f"Could not prepare cancellation: {err_msg}",
            }

        # Policy check
        policy_decision = CancellationPolicyEngine.evaluate(
            order, reason, auto_refund_on_cancel=connector.cancel_refunds_automatically
        )

        if policy_decision.requires_human_review:
            AuditLogger.log_action(
                session_id=session_id,
                company_id=company_id,
                order_id=order_id,
                action_type="CANCEL_ORDER",
                provider=provider_name,
                status="REQUIRES_HUMAN_REVIEW",
                reason=policy_decision.refusal_reason,
                refund_issued=False,
                details=policy_decision.model_dump(),
            )
            return {
                "status": "REQUIRES_HUMAN_REVIEW",
                "cancellable": False,
                "reason": policy_decision.refusal_reason,
                "order_id": order_id,
                "financial_status": policy_decision.financial_status,
                "fulfillment_status": policy_decision.fulfillment_status,
                "message": policy_decision.refusal_reason,
            }

        if not policy_decision.is_cancellable:
            AuditLogger.log_action(
                session_id=session_id,
                company_id=company_id,
                order_id=order_id,
                action_type="CANCEL_ORDER",
                provider=provider_name,
                status="REJECTED_BY_POLICY",
                reason=policy_decision.refusal_reason,
                refund_issued=False,
                details=policy_decision.model_dump(),
            )
            return {
                "status": "REJECTED",
                "cancellable": False,
                "reason": policy_decision.refusal_reason,
                "order_id": order_id,
                "financial_status": policy_decision.financial_status,
                "fulfillment_status": policy_decision.fulfillment_status,
                "message": policy_decision.refusal_reason,
            }

        # Staging pending action
        pending = confirmation_manager.register_pending_action(
            session_id=session_id,
            company_id=company_id,
            order_id=order_id,
            reason=reason or "CUSTOMER",
            policy_decision=policy_decision,
        )

        AuditLogger.log_action(
            session_id=session_id,
            company_id=company_id,
            order_id=order_id,
            action_type="CANCEL_ORDER",
            provider=provider_name,
            status="STAGED_FOR_CONFIRMATION",
            reason=reason,
            refund_issued=False,
            details=pending.model_dump(),
        )

        return {
            "status": "REQUIRES_CONFIRMATION",
            "cancellable": True,
            "action_id": pending.action_id,
            "order_id": order_id,
            "order_total": order.get("total"),
            "financial_status": policy_decision.financial_status,
            "fulfillment_status": policy_decision.fulfillment_status,
            "refund_recommended": policy_decision.refund_recommended,
            "refund_note": policy_decision.refund_note,
            "message": (
                f"Cancellation policy check passed for order '{order_id}'. "
                f"{policy_decision.refund_note} "
                "Please confirm with the customer before proceeding."
            ),
        }

    def confirm_order_cancellation(
        self,
        session_id: str,
        company_id: str,
        order_id: str,
        confirmed: bool = True,
        reason: Optional[str] = "CUSTOMER",
    ) -> Dict[str, Any]:
        """
        Stage 2 of Safe Action Framework:
        Executes staged cancellation upon user confirmation, verifies state change, and records audit log.
        """
        connector = self.get_connector(company_id)
        provider_name = connector.__class__.__name__.replace("Connector", "").upper()

        if not confirmed:
            confirmation_manager.clear_pending_action(session_id, company_id, order_id)
            AuditLogger.log_action(
                session_id=session_id,
                company_id=company_id,
                order_id=order_id,
                action_type="CANCEL_ORDER",
                provider=provider_name,
                status="CANCELLED_BY_USER",
                reason="User declined confirmation",
                refund_issued=False,
            )
            return {
                "success": False,
                "status": "CANCELLED_BY_USER",
                "order_id": order_id,
                "message": f"Order cancellation for '{order_id}' was aborted by the user.",
            }

        pending = confirmation_manager.get_pending_action(session_id, company_id, order_id)

        # If no pending action staged, execute preparation pre-check on the fly
        if not pending:
            prep = self.prepare_order_cancellation(session_id, company_id, order_id, reason)
            # Only a policy-approved cancellation may execute. REJECTED, FAILED and
            # REQUIRES_HUMAN_REVIEW must never fall through to the provider write.
            if prep.get("status") != "REQUIRES_CONFIRMATION":
                return prep
            pending = confirmation_manager.get_pending_action(session_id, company_id, order_id)

        actual_reason = pending.reason if pending else (reason or "CUSTOMER")

        # Execute cancellation on connector
        cancel_result = connector.cancel_order(order_id, company_id, reason=actual_reason)

        # Clean up staged pending action
        confirmation_manager.clear_pending_action(session_id, company_id, order_id)

        # Post-action state verification
        fresh_order = connector.get_order_details(order_id, company_id)
        verification_status = cancel_result.get("verification_status", "UNVERIFIED")
        if fresh_order and "error" not in fresh_order:
            f_status = str(fresh_order.get("fulfillment_status") or "").upper()
            o_status = str(fresh_order.get("status") or "").upper()
            fin_status = str(fresh_order.get("financial_status") or "").upper()
            if f_status == "CANCELLED" or o_status == "CANCELLED" or fin_status in ["REFUNDED", "VOIDED"]:
                verification_status = "VERIFIED"

        cancel_result["verification_status"] = verification_status

        # Record audit log
        AuditLogger.log_action(
            session_id=session_id,
            company_id=company_id,
            order_id=order_id,
            action_type="CANCEL_ORDER",
            provider=provider_name,
            status=cancel_result.get("status", "COMPLETED"),
            reason=cancel_result.get("reason"),
            refund_issued=cancel_result.get("refund_issued", False),
            details=cancel_result,
        )

        return cancel_result

    def cancel_order(
        self,
        order_id: str,
        company_id: str,
        reason: Optional[str] = "CUSTOMER",
        session_id: Optional[str] = "default",
        confirmed: bool = False,
    ) -> Dict[str, Any]:
        """
        Unified gateway entrypoint for order cancellation.
        Supports both single-step (confirmed=True) and 2-step confirmation flows.
        """
        if not confirmed:
            return self.prepare_order_cancellation(
                session_id=session_id or "default",
                company_id=company_id,
                order_id=order_id,
                reason=reason,
            )
        else:
            return self.confirm_order_cancellation(
                session_id=session_id or "default",
                company_id=company_id,
                order_id=order_id,
                confirmed=True,
                reason=reason,
            )

    def request_return(
        self,
        order_id: str,
        refund_amount: float,
        company_id: str,
        reason: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        if connector.managed_returns:
            return self._create_managed_return(connector, order_id, refund_amount, company_id, reason, session_id)
        return connector.request_return(order_id, refund_amount, company_id)

    # ---- Returns, refunds and support (provider-neutral) --------------------------

    def get_return_policy(self, company_id: str) -> Dict[str, Any]:
        """The merchant's return policy as configured in Luintix (company_rules)."""
        connector = self.get_connector(company_id)
        rules = get_company_rules(company_id) or {}
        return {
            "return_window_days": int(rules.get("return_window_days") or 30),
            "auto_approval_limit": float(rules.get("max_auto_refund_amount") or 0.0),
            "conditions": [
                "Only fulfilled/delivered orders can be returned.",
                "Returns must be requested within the return window, counted from completion (or order date).",
                "Refunds cannot exceed the amount paid minus refunds already issued.",
                "Requests above the auto-approval limit are reviewed by the merchant.",
            ],
            "refund_processing": (
                "Refunds are issued by the merchant after the return request is reviewed."
                if connector.managed_returns else "Refunds are processed according to the store's own settings."
            ),
            "source": "LUINTIX_MERCHANT_SETTINGS",
        }

    def _eligibility(
        self, connector: BaseConnector, order_id: str, company_id: str, amount: Optional[float]
    ) -> Dict[str, Any]:
        order = connector.get_order_details(order_id, company_id)
        if not order:
            return {"error": f"Order '{order_id}' not found.", "code": "NOT_FOUND"}
        if "error" in order:
            return order
        refunds = connector.get_refunds(order_id, company_id)
        already_refunded = float(refunds.get("total_refunded") or 0.0) if "error" not in refunds else 0.0
        decision = ReturnPolicyEngine.evaluate(order, get_company_rules(company_id), amount, already_refunded)
        return {"order": order, "decision": decision}

    def check_return_eligibility(
        self, order_id: str, company_id: str, refund_amount: Optional[float] = None
    ) -> Dict[str, Any]:
        """Read-only: evaluates an order against the merchant's return policy."""
        connector = self.get_connector(company_id)
        result = self._eligibility(connector, order_id, company_id, refund_amount)
        if "error" in result:
            return result
        return result["decision"].model_dump()

    def _create_managed_return(
        self,
        connector: BaseConnector,
        order_id: str,
        refund_amount: float,
        company_id: str,
        reason: Optional[str],
        session_id: Optional[str],
    ) -> Dict[str, Any]:
        """
        Policy-gated return request for providers without a native returns API.
        Records the request for the merchant; never moves money itself.
        """
        provider = connector.provider_name
        result = self._eligibility(connector, order_id, company_id, refund_amount)
        if "error" in result:
            return {"status": "ERROR", "order_id": order_id, "message": result["error"], "code": result.get("code")}
        decision = result["decision"]

        def audit(status: str, details: Dict[str, Any]) -> None:
            AuditLogger.log_action(
                session_id=session_id, company_id=company_id, order_id=order_id,
                action_type="RETURN_REQUEST", provider=provider, status=status,
                reason=reason or decision.reason, refund_issued=False, details=details,
            )

        if not decision.eligible:
            audit("REJECTED_BY_POLICY", decision.model_dump())
            return {"status": "REJECTED", "order_id": order_id, "reason": decision.reason, "eligibility": decision.model_dump()}

        existing = ReturnRequestStore.open_for_order(company_id, order_id)
        if existing:
            return {
                "status": "ALREADY_REQUESTED",
                "order_id": order_id,
                "return_id": existing["return_id"],
                "message": f"A return request ({existing['return_id']}) is already open for order {order_id}.",
            }

        ticket = None
        if decision.requires_human_review:
            ticket = SupportTicketStore.create(
                company_id, order_id,
                f"Return request of ${refund_amount:.2f} exceeds auto-approval limit ${decision.auto_approval_limit:.2f}.",
                session_id=session_id, source="RETURN_POLICY",
            )
            status = "REQUIRES_HUMAN_REVIEW"
        else:
            status = "SUBMITTED"

        record = ReturnRequestStore.create(
            company_id, order_id, refund_amount, reason, status,
            ticket_id=ticket["ticket_id"] if ticket else None, session_id=session_id,
        )
        audit(status, {"return_id": record["return_id"], "ticket_id": record["ticket_id"], "amount": refund_amount})
        return {
            "status": status,
            "order_id": order_id,
            "return_id": record["return_id"],
            "ticket_id": record["ticket_id"],
            "refund_issued": False,
            "message": (
                f"Return request {record['return_id']} recorded for order {order_id}. "
                + ("It exceeds the auto-approval limit and has been sent to the merchant for review. "
                   if ticket else "The merchant will process the refund. ")
                + "No money has been refunded yet."
            ),
        }

    def get_refund_status(self, order_id: str, company_id: str) -> Dict[str, Any]:
        """Refunds issued on the provider plus any open Luintix return request."""
        connector = self.get_connector(company_id)
        refunds = connector.get_refunds(order_id, company_id)
        if "error" in refunds:
            return refunds
        open_return = ReturnRequestStore.open_for_order(company_id, order_id)
        return {**refunds, "open_return_request": open_return}

    def get_inventory(self, product_query: str, company_id: str) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        return connector.get_inventory(product_query, company_id)

    def create_support_ticket(
        self, order_id: Optional[str], reason: str, company_id: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Creates a real, tenant-scoped support ticket in the shared store and audits it."""
        connector = self.get_connector(company_id)
        ticket = SupportTicketStore.create(company_id, order_id, reason, session_id=session_id)
        AuditLogger.log_action(
            session_id=session_id, company_id=company_id, order_id=order_id or "",
            action_type="SUPPORT_TICKET", provider=connector.provider_name, status="OPEN",
            reason=reason, details={"ticket_id": ticket["ticket_id"]},
        )
        return {
            "status": "ESCALATED",
            "ticket_id": ticket["ticket_id"],
            "message": f"Support ticket {ticket['ticket_id']} created for human review. Reason: {reason}",
        }

    def check_proactive_notifications(
        self, customer_id: str, company_id: str
    ) -> Optional[str]:
        connector = self.get_connector(company_id)
        return connector.check_proactive_notifications(customer_id, company_id)

    def escalate_to_human(
        self, order_id: str, reason: str, company_id: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        if connector.managed_tickets:
            return self.create_support_ticket(order_id, reason, company_id, session_id)
        return connector.escalate_to_human(order_id, reason, company_id)

    def get_ticket_details(
        self, ticket_id: str, company_id: str
    ) -> Dict[str, Any]:
        connector = self.get_connector(company_id)
        if connector.managed_tickets:
            ticket = SupportTicketStore.get(company_id, ticket_id)
            return ticket or {"error": f"Ticket {ticket_id} not found.", "code": "NOT_FOUND"}
        return connector.get_ticket_details(ticket_id, company_id)


    # ---- Helpdesk (Freshdesk) --------------------------------------------------------
    # Resolved from helpdesk_integrations, independently of the store provider in
    # company_integrations. Credentials are per tenant and encrypted; there is no
    # environment or cross-tenant fallback.

    HELPDESK_PROVIDERS = {"FRESHDESK"}

    def _resolve_helpdesk(self, company_id: str) -> FreshdeskConnector:
        if not company_id:
            raise TenantConfigurationError("No tenant (X-Company-ID) supplied for this request.")
        row = query_db("SELECT * FROM helpdesk_integrations WHERE company_id = ?", (company_id,), one=True)
        if not row:
            raise TenantConfigurationError(f"Tenant '{company_id}' has no helpdesk integration.")
        provider_type = (row.get("provider_type") or "").upper()
        if provider_type not in self.HELPDESK_PROVIDERS:
            raise TenantConfigurationError(
                f"Tenant '{company_id}' has unsupported helpdesk provider '{provider_type}'."
            )
        if not row.get("domain") or not row.get("api_key"):
            raise TenantConfigurationError(f"Tenant '{company_id}' has incomplete Freshdesk configuration.")
        connector = FreshdeskConnector(row["domain"], credential_store.decrypt(row["api_key"]))
        if not connector.domain_configured():
            raise TenantConfigurationError(f"Tenant '{company_id}' has an invalid Freshdesk domain.")
        return connector

    def get_helpdesk_connector(self, company_id: str) -> FreshdeskConnector:
        connector = self._resolve_helpdesk(company_id)
        log_gateway(
            f"[GATEWAY] Tenant: {company_id} | Helpdesk Connector: {connector.__class__.__name__} | "
            f"Provider: {connector.provider_name} | Credential source: DATABASE | "
            f"Credential configured: {'YES' if connector.credentials_configured() else 'NO'}"
        )
        return connector

    def has_helpdesk(self, company_id: str) -> bool:
        """Whether this tenant has a (supported) helpdesk configured. Never raises."""
        try:
            row = query_db(
                "SELECT provider_type FROM helpdesk_integrations WHERE company_id = ?", (company_id,), one=True
            )
        except Exception:
            return False
        return bool(row) and (row.get("provider_type") or "").upper() in self.HELPDESK_PROVIDERS

    @staticmethod
    def _helpdesk_identifier(identity: CustomerIdentity) -> Optional[Tuple[str, str]]:
        """Helpdesk contacts are matched by email, else phone (never by name alone)."""
        email, phone = (identity.email or "").strip(), (identity.phone or "").strip()
        if email and CustomerIdentity(email=email).primary():
            return ("email", email)
        if phone and CustomerIdentity(phone=phone).primary():
            return ("phone", phone)
        return None

    def _helpdesk_contact(
        self, connector: FreshdeskConnector, identity: CustomerIdentity, company_id: str
    ) -> Dict[str, Any]:
        identifier = self._helpdesk_identifier(identity)
        if not identifier:
            error = identity.validation_error() if not (identity.email or identity.phone) else (
                "The email address or phone number provided is not valid."
            )
            if identity.authenticated and identity.customer_id and not (identity.email or identity.phone):
                error = "Support tickets are matched by email or phone; please provide the one used with support."
            return {"error": error, "code": "IDENTITY_REQUIRED"}
        kind, value = identifier
        result = connector.get_contact(**{kind: value})
        if "error" in result:
            return result
        contact = result.get("contact")
        if not contact:
            return {"error": f"No support contact found for the provided {kind}.", "code": "CUSTOMER_NOT_FOUND"}
        if identity.name and contact.get("name") and identity.name.strip().casefold() != contact["name"].strip().casefold():
            return {"error": "The details provided do not match our support records.", "code": "IDENTITY_MISMATCH"}
        log_gateway(f"[IDENTITY] Tenant: {company_id} | Helpdesk identified by: {kind} | Contact found: YES")
        return {"contact": contact, "identified_by": kind}

    def _owned_ticket(
        self,
        connector: FreshdeskConnector,
        identity: CustomerIdentity,
        ticket_id: Any,
        company_id: str,
        include_conversations: bool = False,
    ) -> Dict[str, Any]:
        """Fetches a ticket only if it belongs to the identified customer."""
        resolved = self._helpdesk_contact(connector, identity, company_id)
        if "error" in resolved:
            return resolved
        ticket = (
            connector.get_ticket_conversations(ticket_id)
            if include_conversations else connector.get_ticket_details(ticket_id)
        )
        if "error" in ticket:
            return ticket
        if ticket.get("requester_id") != resolved["contact"]["contact_id"]:
            # Same response as a missing ticket: never confirm another customer's ticket exists.
            return {"error": FreshdeskConnector.ERROR_MESSAGES["NOT_FOUND"], "code": "NOT_FOUND"}
        return {"ticket": ticket, "contact": resolved["contact"]}

    def _audit_helpdesk(
        self, action: str, company_id: str, session_id: Optional[str], result: Dict[str, Any],
        ticket_id: Optional[str] = None, order_id: Optional[str] = None, details: Optional[Dict[str, Any]] = None,
    ) -> None:
        AuditLogger.log_action(
            session_id=session_id,
            company_id=company_id,
            order_id=order_id or "",
            action_type=f"FRESHDESK_{action}",
            provider="FRESHDESK",
            status="FAILED" if "error" in result else "SUCCESS",
            reason=result.get("code") if "error" in result else None,
            details={"ticket_id": ticket_id or result.get("ticket_id"), **(details or {})},
        )

    def helpdesk_verify_credentials(self, company_id: str) -> Dict[str, Any]:
        return self.get_helpdesk_connector(company_id).verify_credentials()

    def helpdesk_get_ticket_fields(self, company_id: str) -> Any:
        return self.get_helpdesk_connector(company_id).get_ticket_fields()

    def helpdesk_get_contact(self, identity: CustomerIdentity, company_id: str) -> Dict[str, Any]:
        return self._helpdesk_contact(self.get_helpdesk_connector(company_id), identity, company_id)

    # ---- Presentation & order linking ----------------------------------------------

    @staticmethod
    def _order_field_for(connector: FreshdeskConnector, tickets: List[Dict[str, Any]]) -> Optional[str]:
        """Only looks up the order field when a ticket actually has custom field values."""
        if any(v not in (None, "") for t in tickets for v in (t.get("custom_fields") or {}).values()):
            return connector.get_order_field()
        return None

    @staticmethod
    def _present_ticket(ticket: Dict[str, Any], order_field: Optional[str]) -> Dict[str, Any]:
        """Customer-safe ticket: real Freshdesk reference + linked order; raw custom fields removed."""
        presented = {k: v for k, v in ticket.items() if k != "custom_fields"}
        order_ref, source = extract_order_reference(ticket, order_field)
        presented["ticket_reference"] = f"#{ticket['ticket_id']}"
        presented["order_reference"] = order_ref
        presented["order_reference_source"] = source
        return presented

    def _order_snapshot(
        self, company_id: str, order_ref: str, identity: CustomerIdentity, contact: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Fresh read of the order from the tenant's STORE provider (never from the helpdesk).
        Order details are withheld when the order demonstrably belongs to someone else.
        """
        try:
            store = self.get_connector(company_id)
        except TenantConfigurationError:
            return {"order_reference": order_ref, "available": False, "reason": "No store integration for this tenant."}
        order = store.get_order_details(order_ref, company_id)
        if not order:
            return {"order_reference": order_ref, "found": False}
        if "error" in order:
            return {"order_reference": order_ref, "error": order["error"], "code": order.get("code")}
        emails = {e.strip().casefold() for e in (identity.email, (contact or {}).get("email")) if e}
        owner = (order.get("customer_email") or "").strip().casefold()
        if owner and emails and owner not in emails:
            return {"order_reference": order_ref, "withheld": True, "reason": "ORDER_OWNERSHIP_NOT_VERIFIED"}
        return {
            "order_reference": order["id"],
            "provider": store.provider_name,
            "status": order.get("status"),
            "financial_status": order.get("financial_status"),
            "fulfillment_status": order.get("fulfillment_status"),
            "total": order.get("total"),
            "ownership_verified": bool(owner) and owner in emails,
            "fresh_provider_read": True,
        }

    def _cancelled_by_luintix(self, company_id: str, order_ref: str) -> bool:
        rows = query_db(
            "SELECT order_id FROM action_audit_logs WHERE company_id = ? AND action_type = 'CANCEL_ORDER' AND status = 'CANCELLED'",
            (company_id,),
        )
        return any(normalize_order_ref(r["order_id"]) == normalize_order_ref(order_ref) for r in rows)

    def _customer_tickets_raw(self, connector: FreshdeskConnector, contact: Dict[str, Any]) -> Any:
        return connector.get_tickets_by_customer(contact["contact_id"], 100)

    def _open_ticket_for_order(
        self, connector: FreshdeskConnector, contact: Dict[str, Any], order_ref: str
    ) -> Dict[str, Any]:
        """Newest OPEN/PENDING ticket of this customer linked to this order (idempotency check)."""
        tickets = self._customer_tickets_raw(connector, contact)
        if isinstance(tickets, dict):
            return tickets
        order_field = self._order_field_for(connector, tickets)
        matches = [
            t for t in tickets
            if t["status"] in SUPPORT_OPEN_STATUSES and ticket_matches_order(t, order_field, order_ref)
        ]
        matches.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
        return {"ticket": matches[0] if matches else None, "order_field": order_field}

    # ---- Customer ticket reads -------------------------------------------------------

    def helpdesk_get_customer_tickets(
        self, identity: CustomerIdentity, company_id: str, limit: int = 10
    ) -> Dict[str, Any]:
        connector = self.get_helpdesk_connector(company_id)
        resolved = self._helpdesk_contact(connector, identity, company_id)
        if "error" in resolved:
            return resolved
        tickets = connector.get_tickets_by_customer(resolved["contact"]["contact_id"], limit)
        if isinstance(tickets, dict):
            return tickets
        order_field = self._order_field_for(connector, tickets)
        tickets = [self._present_ticket(t, order_field) for t in tickets]
        return {"tickets": tickets, "count": len(tickets), "identified_by": resolved["identified_by"]}

    def helpdesk_get_ticket(
        self, identity: CustomerIdentity, ticket_id: Any, company_id: str, include_conversations: bool = False
    ) -> Dict[str, Any]:
        connector = self.get_helpdesk_connector(company_id)
        owned = self._owned_ticket(connector, identity, ticket_id, company_id, include_conversations)
        if "error" in owned:
            return owned
        ticket = owned["ticket"]
        if include_conversations:
            # Private (agent-only) notes are never shown in the customer conversation.
            ticket["conversations"] = [c for c in ticket.get("conversations", []) if not c["private"]]
        return self._present_ticket(ticket, self._order_field_for(connector, [ticket]))

    # ---- Escalation: create or reuse a REAL helpdesk ticket ---------------------------

    def helpdesk_create_ticket(
        self,
        identity: CustomerIdentity,
        subject: str,
        description: str,
        priority: str,
        company_id: str,
        order_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Human escalation into the helpdesk. With an order:
          1. fresh store read of the order (ownership checked),
          2. reuse the customer's OPEN/PENDING ticket for that order (follow-up private note,
             order field back-filled) instead of creating a duplicate,
          3. otherwise create a ticket with the order reference in the merchant's order field.
        Always returns the REAL ticket ID from the helpdesk API.
        """
        connector = self.get_helpdesk_connector(company_id)
        identifier = self._helpdesk_identifier(identity)
        if not identifier:
            return {"error": "Please provide the email address or phone number to open a support ticket.", "code": "IDENTITY_REQUIRED"}
        kind, value = identifier
        lookup = connector.get_contact(**{kind: value})
        if "error" in lookup:
            return lookup
        contact = lookup.get("contact")
        if contact:
            if identity.name and contact.get("name") and identity.name.strip().casefold() != contact["name"].strip().casefold():
                return {"error": "The details provided do not match our support records.", "code": "IDENTITY_MISMATCH"}
            requester = {"requester_id": contact["contact_id"]}
        elif kind == "email":
            requester = {"email": value}
        elif identity.name:
            requester = {"phone": value, "name": identity.name}
        else:
            return {"error": "Please provide your email address (or your name with your phone number) to open a ticket.", "code": "IDENTITY_REQUIRED"}

        intent = classify_complaint(f"{subject}. {description}")
        order = None
        if order_id:
            order = self._order_snapshot(company_id, order_id, identity, contact)
            if order.get("withheld"):
                return {"error": "That order could not be verified for this customer.", "code": "ORDER_OWNERSHIP_MISMATCH"}
            if order.get("found") is False:
                return {"error": f"Order {order_id} was not found in the store.", "code": "ORDER_NOT_FOUND"}

        order_line = ""
        if order_id:
            state = (
                f"status {order.get('status')}, payment {order.get('financial_status')}, "
                f"fulfillment {order.get('fulfillment_status')} (read live from {order.get('provider')})"
                if order.get("fresh_provider_read") else "current state unavailable"
            )
            order_line = f"Related order: {order_id} ({state})."

        if order_id and contact:
            existing = self._open_ticket_for_order(connector, contact, order_id)
            if "error" in existing:
                return existing
            if existing["ticket"]:
                return self._reuse_ticket(connector, existing, description, order_line, order_id, order, company_id, session_id)

        order_field = connector.get_order_field() if order_id else None
        result = connector.create_ticket(
            subject,
            f"{description}\n\n{order_line}".strip(),
            priority,
            requester,
            tags=["luintix", intent_tag(intent)],
            custom_fields={order_field: order_id} if order_field else None,
        )
        self._audit_helpdesk("CREATE_TICKET", company_id, session_id, result, order_id=order_id,
                             details={"priority": priority, "identified_by": kind, "intent": intent,
                                      "order_field_stored": bool(order_field)})
        if "error" in result:
            return result
        return {
            **self._present_ticket(result, order_field),
            "order_reference": order_id or self._present_ticket(result, order_field)["order_reference"],
            "reused": False,
            "intent": intent,
            "order_field_stored": bool(order_field),
            **({"order_field_missing": True} if order_id and not order_field else {}),
            "order": order,
        }

    def _reuse_ticket(
        self, connector: FreshdeskConnector, existing: Dict[str, Any], description: str, order_line: str,
        order_id: str, order: Optional[Dict[str, Any]], company_id: str, session_id: Optional[str],
    ) -> Dict[str, Any]:
        ticket, order_field = existing["ticket"], existing["order_field"] or connector.get_order_field()
        note = connector.add_internal_note(
            ticket["ticket_id"], f"Customer followed up via Luintix: {description}\n{order_line}".strip()
        )
        linked = False
        if order_field and not (ticket.get("custom_fields") or {}).get(order_field):
            link = connector.set_custom_field(ticket["ticket_id"], order_field, order_id)
            linked = "error" not in link
        result = {
            **self._present_ticket(ticket, order_field),
            "order_reference": order_id,
            "reused": True,
            "note_added": "error" not in note,
            **({"note_error": note.get("code")} if "error" in note else {}),
            "order_field_linked": linked,
            "order": order,
        }
        self._audit_helpdesk("REUSE_TICKET", company_id, session_id, {"ticket_id": ticket["ticket_id"]},
                             ticket_id=ticket["ticket_id"], order_id=order_id,
                             details={"note_added": result["note_added"], "order_field_linked": linked})
        return result

    # ---- Complaint status: helpdesk state + store state, both read fresh ---------------

    def helpdesk_complaint_status(
        self, identity: CustomerIdentity, company_id: str, ticket_id: Any = None, order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        connector = self.get_helpdesk_connector(company_id)
        resolved = self._helpdesk_contact(connector, identity, company_id)
        if "error" in resolved:
            return resolved
        contact = resolved["contact"]

        if ticket_id:
            selected_id = ticket_id
            others: List[Dict[str, Any]] = []
        else:
            tickets = self._customer_tickets_raw(connector, contact)
            if isinstance(tickets, dict):
                return tickets
            order_field = self._order_field_for(connector, tickets)
            pool = [t for t in tickets if not order_id or ticket_matches_order(t, order_field, order_id)]
            if not pool:
                return {"ticket": None, "message": "No support tickets were found for this customer"
                        + (f" and order {order_id}." if order_id else "."), "fresh_provider_read": True}
            pool.sort(key=lambda t: (t["status"] in SUPPORT_OPEN_STATUSES, t.get("updated_at") or ""), reverse=True)
            selected_id = pool[0]["ticket_id"]
            others = [
                {"ticket_reference": f"#{t['ticket_id']}", "status": t["status"], "subject": t.get("subject")}
                for t in pool[1:] if t["status"] in SUPPORT_OPEN_STATUSES
            ]

        owned = self._owned_ticket(connector, identity, selected_id, company_id, include_conversations=True)
        if "error" in owned:
            return owned
        raw = owned["ticket"]
        public = [c for c in raw.get("conversations", []) if not c["private"]]
        ticket = self._present_ticket({k: v for k, v in raw.items() if k != "conversations"},
                                      self._order_field_for(connector, [raw]))
        order = (
            self._order_snapshot(company_id, ticket["order_reference"], identity, contact)
            if ticket["order_reference"] else None
        )
        latest = public[-1] if public else None
        return {
            "ticket": ticket,
            "order": order,
            "latest_public_message": (
                {"from": "customer" if latest["from_customer"] else "support", "created_at": latest["created_at"],
                 "excerpt": (latest.get("body_text") or "")[:300]} if latest else None
            ),
            "public_message_count": len(public),
            "other_open_tickets": others,
            "note": "Ticket state comes from the helpdesk and order state from the store; they are independent.",
            "fresh_provider_read": True,
        }

    # ---- Order <-> ticket reconciliation ------------------------------------------------

    def helpdesk_sync_ticket_with_order(
        self,
        identity: CustomerIdentity,
        company_id: str,
        ticket_id: Any = None,
        order_id: Optional[str] = None,
        session_id: Optional[str] = None,
        apply: bool = True,
    ) -> Dict[str, Any]:
        """
        Re-reads ticket(s) and the order, then applies the only automatic transition allowed:
        a cancellation-request ticket is resolved once the store confirms the cancellation
        (private note first, then status). Everything else is left to human support.
        """
        connector = self.get_helpdesk_connector(company_id)
        resolved = self._helpdesk_contact(connector, identity, company_id)
        if "error" in resolved:
            return resolved
        contact = resolved["contact"]

        if ticket_id:
            owned = self._owned_ticket(connector, identity, ticket_id, company_id)
            if "error" in owned:
                return owned
            candidates = [owned["ticket"]]
        elif order_id:
            tickets = self._customer_tickets_raw(connector, contact)
            if isinstance(tickets, dict):
                return tickets
            order_field = self._order_field_for(connector, tickets)
            candidates = [t for t in tickets if t["status"] in SUPPORT_OPEN_STATUSES and ticket_matches_order(t, order_field, order_id)]
        else:
            return {"error": "A ticket number or order number is required.", "code": "INVALID_ARGUMENT"}

        order_field = self._order_field_for(connector, candidates)
        outcomes = []
        for raw in candidates:
            ticket = self._present_ticket(raw, order_field)
            order_ref = ticket["order_reference"] or order_id
            order = self._order_snapshot(company_id, order_ref, identity, contact) if order_ref else None
            intent = ticket_intent(raw)
            decision = decide_reconciliation(ticket["status"], intent, order)
            outcome = {
                "ticket_reference": ticket["ticket_reference"], "ticket_status_before": ticket["status"],
                "order_reference": order_ref, "order": order, "intent": intent,
                "decision": decision["action"], "reason": decision["reason"],
                "actions_applied": [], "errors": [],
            }
            if apply and decision["action"] == "RESOLVE":
                by_luintix = self._cancelled_by_luintix(company_id, order_ref)
                note_text = (
                    f"Order {order_ref} was successfully cancelled by Luintix." if by_luintix
                    else f"Order {order_ref} is confirmed CANCELLED in the store (verified by Luintix)."
                ) + " The customer's cancellation request is fulfilled; ticket resolved by Luintix."
                note = connector.add_internal_note(raw["ticket_id"], note_text)
                self._audit_helpdesk("ADD_INTERNAL_NOTE", company_id, session_id, note, ticket_id=raw["ticket_id"],
                                     order_id=order_ref, details={"reason": "order_ticket_sync"})
                if "error" in note:
                    outcome["errors"].append({"action": "ADD_INTERNAL_NOTE", "code": note.get("code")})
                else:
                    outcome["actions_applied"].append("NOTE_ADDED")
                    update = connector.update_ticket_status(raw["ticket_id"], "resolved")
                    self._audit_helpdesk("UPDATE_STATUS", company_id, session_id, update, ticket_id=raw["ticket_id"],
                                         order_id=order_ref, details={"status": "resolved", "reason": "order_ticket_sync"})
                    if "error" in update:
                        outcome["errors"].append({"action": "UPDATE_STATUS", "code": update.get("code")})
                    else:
                        outcome["actions_applied"].append("RESOLVED")
                        outcome["ticket_status_after"] = update["status"]
            outcomes.append(outcome)
        return {"tickets": outcomes, "evaluated": len(outcomes), "fresh_provider_read": True}

    def _helpdesk_owned_write(
        self, action: str, identity: CustomerIdentity, ticket_id: Any, company_id: str,
        session_id: Optional[str], write, details: Dict[str, Any],
    ) -> Dict[str, Any]:
        connector = self.get_helpdesk_connector(company_id)
        owned = self._owned_ticket(connector, identity, ticket_id, company_id)
        result = owned if "error" in owned else write(connector)
        self._audit_helpdesk(action, company_id, session_id, result, ticket_id=str(ticket_id), details=details)
        if "error" not in result and "custom_fields" in result:
            result = self._present_ticket(result, self._order_field_for(connector, [result]))
        return result

    def helpdesk_update_ticket_status(
        self, identity: CustomerIdentity, ticket_id: Any, status: str, company_id: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._helpdesk_owned_write(
            "UPDATE_STATUS", identity, ticket_id, company_id, session_id,
            lambda c: c.update_ticket_status(ticket_id, status), {"status": status},
        )

    def helpdesk_update_ticket_priority(
        self, identity: CustomerIdentity, ticket_id: Any, priority: str, company_id: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._helpdesk_owned_write(
            "UPDATE_PRIORITY", identity, ticket_id, company_id, session_id,
            lambda c: c.update_ticket_priority(ticket_id, priority), {"priority": priority},
        )

    def helpdesk_add_internal_note(
        self, identity: CustomerIdentity, ticket_id: Any, note: str, company_id: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._helpdesk_owned_write(
            "ADD_INTERNAL_NOTE", identity, ticket_id, company_id, session_id,
            lambda c: c.add_internal_note(ticket_id, note), {"note_length": len(note or "")},
        )

    # ---- Shipping / tracking (Shippo) ------------------------------------------------
    # Resolved from shipping_integrations, independently of store and helpdesk. Store
    # providers remain the source of truth for orders and ownership; Shippo only answers
    # "where is this shipment" for a carrier + tracking number the store recorded.

    SHIPPING_PROVIDERS = {"SHIPPO"}
    REGISTRATION_IN_FLIGHT_SECONDS = 120

    def _resolve_shipping(self, company_id: str) -> Tuple[ShippoConnector, Dict[str, Any]]:
        if not company_id:
            raise TenantConfigurationError("No tenant (X-Company-ID) supplied for this request.")
        row = query_db("SELECT * FROM shipping_integrations WHERE company_id = ?", (company_id,), one=True)
        if not row:
            raise TenantConfigurationError(f"Tenant '{company_id}' has no shipping integration.")
        if (row.get("provider_type") or "").upper() not in self.SHIPPING_PROVIDERS:
            raise TenantConfigurationError(f"Tenant '{company_id}' has unsupported shipping provider '{row.get('provider_type')}'.")
        if not row.get("api_token"):
            raise TenantConfigurationError(f"Tenant '{company_id}' has incomplete Shippo configuration.")
        if (row.get("credential_status") or "ACTIVE").upper() != "ACTIVE":
            raise TenantConfigurationError(
                f"Tenant '{company_id}' Shippo credential is {row['credential_status']}; the merchant must reconnect Shippo."
            )
        connector = ShippoConnector(credential_store.decrypt(row["api_token"]), environment=row.get("environment"))
        return connector, row

    def get_shipping_connector(self, company_id: str) -> ShippoConnector:
        connector, row = self._resolve_shipping(company_id)
        log_gateway(
            f"[GATEWAY] Tenant: {company_id} | Shipping Connector: {connector.__class__.__name__} | "
            f"Provider: {connector.provider_name} | Environment: {row.get('environment')} | "
            f"Credential source: DATABASE | Credential configured: {'YES' if connector.credentials_configured() else 'NO'}"
        )
        return connector

    def has_shipping(self, company_id: str) -> bool:
        """Whether this tenant has an ACTIVE supported shipping integration. Never raises."""
        try:
            row = query_db(
                "SELECT provider_type, credential_status FROM shipping_integrations WHERE company_id = ?",
                (company_id,), one=True,
            )
        except Exception:
            return False
        return bool(row) and (row.get("provider_type") or "").upper() in self.SHIPPING_PROVIDERS

    def _shipping_result(self, company_id: str, result: Any) -> Any:
        """401 from Shippo -> credential marked INVALID (merchant must reconnect)."""
        if isinstance(result, dict) and result.get("code") == "PROVIDER_AUTH_FAILED":
            execute_db(
                "UPDATE shipping_integrations SET credential_status = 'INVALID' WHERE company_id = ?", (company_id,)
            )
            log_gateway(f"[GATEWAY] Tenant: {company_id} | Shippo credential marked INVALID (reconnect required)")
        return result

    def shipping_verify_credentials(self, company_id: str) -> Dict[str, Any]:
        result = self._shipping_result(company_id, self.get_shipping_connector(company_id).verify_credentials())
        if result.get("ok"):
            execute_db("UPDATE shipping_integrations SET last_verified_at = CURRENT_TIMESTAMP WHERE company_id = ?", (company_id,))
        return result

    def shipping_get_carrier_accounts(self, company_id: str) -> Any:
        return self._shipping_result(company_id, self.get_shipping_connector(company_id).get_carrier_accounts())

    @staticmethod
    def _stored_tracking_state(company_id: str, carrier_slug: str, tracking_number: str) -> Optional[Dict[str, Any]]:
        row = query_db(
            "SELECT status, substatus, action_required, estimated_delivery, source_updated_at, alert, updated_at "
            "FROM shipment_tracking_state WHERE company_id = ? AND provider = 'SHIPPO' AND carrier_slug = ? AND tracking_number = ?",
            (company_id, carrier_slug, tracking_number), one=True,
        )
        if row:
            row["action_required"] = bool(row["action_required"])
        return row

    def _shippo_track(
        self, connector: ShippoConnector, company_id: str, carrier_slug: str, tracking_number: str, include_history: bool
    ) -> Dict[str, Any]:
        tracking = self._shipping_result(company_id, connector.get_tracking(carrier_slug, tracking_number))
        if "error" in tracking:
            return tracking
        if not include_history:
            tracking = {**tracking, "tracking_history_count": len(tracking["tracking_history"])}
            tracking.pop("tracking_history")
        tracking["latest_webhook_update"] = self._stored_tracking_state(company_id, carrier_slug, tracking_number)
        tracking["fresh_provider_read"] = True
        return tracking

    def shipping_get_tracking(
        self, company_id: str, carrier: str, tracking_number: str, include_history: bool = False
    ) -> Dict[str, Any]:
        """Direct lookup for a carrier + tracking number the customer supplied (this tenant's token only)."""
        if is_delhivery_carrier(carrier):
            if not self.has_delhivery(company_id):
                return {"error": "Delhivery tracking is not configured for this store.", "code": "INVALID_CARRIER"}
            tracking = self._delhivery_track(self.get_delhivery_connector(company_id), company_id,
                                             str(tracking_number or "").strip(), include_history)
            if "error" in tracking:
                return tracking
            return {
                "tracking_available": True,
                "identifiers": {"tracking_number": tracking["tracking_number"], "awb": tracking["awb"],
                                "carrier_slug": "delhivery", "order_reference": tracking.get("order_reference")},
                "tracking": tracking,
            }
        connector = self.get_shipping_connector(company_id)
        slug = carrier_slug_for(carrier)
        if not slug:
            return {"error": f"Carrier '{carrier}' is not supported for tracking lookup.", "code": "INVALID_CARRIER"}
        tracking = self._shippo_track(connector, company_id, slug, str(tracking_number or "").strip(), include_history)
        if "error" in tracking:
            return tracking
        return {
            "tracking_available": True,
            "identifiers": {"tracking_number": tracking["tracking_number"], "carrier_slug": slug,
                            "provider_reference": tracking.get("provider_reference")},
            "tracking": tracking,
        }

    def shipping_track_order(
        self, identity: CustomerIdentity, order_id: str, company_id: str, include_history: bool = False
    ) -> Dict[str, Any]:
        """
        Store order -> fulfillment tracking number -> tracking provider chosen by the order's
        carrier (Delhivery for Delhivery AWBs, otherwise Shippo). Ownership is verified against
        the STORE order (never the tracking provider); tracking numbers/AWBs are taken only
        from the store's fulfillment data, never invented.
        """
        shippo_enabled, delhivery_enabled = self.has_shipping(company_id), self.has_delhivery(company_id)
        if not (shippo_enabled or delhivery_enabled):
            raise TenantConfigurationError(f"Tenant '{company_id}' has no shipping integration.")
        email = (identity.email or "").strip()
        try:
            store = self.get_connector(company_id)
        except TenantConfigurationError:
            return {"error": "No store integration is configured for this tenant.", "code": "STORE_NOT_CONFIGURED"}
        order = store.get_order_details(order_id, company_id)
        if not order:
            return {"error": f"Order {order_id} was not found.", "code": "ORDER_NOT_FOUND"}
        if "error" in order:
            return order
        owner = (order.get("customer_email") or "").strip().casefold()
        if owner:
            # The order belongs to a known customer: the requester must prove it is them.
            if not (email and CustomerIdentity(email=email).primary()):
                return {"error": "Please provide the email address used for the order so it can be verified.",
                        "code": "IDENTITY_REQUIRED"}
            if owner != email.casefold():
                return {"error": "That order could not be verified for this customer.", "code": "ORDER_OWNERSHIP_NOT_VERIFIED"}
        # Orders without a customer email (e.g. guest/draft) cannot be verified; they get the
        # same access as the existing order-number lookup, clearly flagged as unverified.
        ownership_verified = bool(owner)

        shipment = order.get("shipment") or {}
        identifiers = {
            "order_number": order["id"],
            "order_provider": store.provider_name,
            "shipment_id": shipment.get("id"),
            "tracking_number": shipment.get("tracking_number"),
            "carrier": shipment.get("courier"),
            "carrier_slug": None,
            "provider_reference": None,
        }
        ownership = {"ownership_verified": ownership_verified}
        order_state = {k: order.get(k) for k in ("status", "financial_status", "fulfillment_status")}
        if not shipment.get("tracking_number"):
            return {"tracking_available": False, "identifiers": identifiers, "order": order_state, **ownership,
                    "reason": "No tracking number has been added to this order yet, so tracking information is not available."}
        if is_delhivery_carrier(shipment.get("courier")):
            if not delhivery_enabled:
                return {"tracking_available": False, "identifiers": identifiers, "order": order_state, **ownership,
                        "reason": "This order ships with Delhivery, but Delhivery tracking is not configured for this store."}
            identifiers.update(carrier_slug="delhivery", awb=shipment["tracking_number"], tracking_provider="DELHIVERY")
            tracking = self._delhivery_track(self.get_delhivery_connector(company_id), company_id,
                                             shipment["tracking_number"], include_history)
            if "error" in tracking:
                return {"tracking_available": False, "identifiers": identifiers, "order": order_state, **ownership,
                        "error": tracking["error"], "code": tracking.get("code")}
            return {"tracking_available": True, "identifiers": identifiers, "order": order_state, **ownership,
                    "tracking": tracking}
        if not shippo_enabled:
            return {"tracking_available": False, "identifiers": identifiers, "order": order_state, **ownership,
                    "reason": f"The carrier recorded on this order ('{shipment.get('courier') or 'unknown'}') is not supported for detailed tracking."}
        connector = self.get_shipping_connector(company_id)
        slug = carrier_slug_for(shipment.get("courier"))
        if not slug and connector.environment == "test" and shipment["tracking_number"] in TEST_TRACKING_NUMBERS:
            # Shippo's documented mock numbers only exist for the test carrier "shippo".
            slug = TEST_CARRIER
            identifiers["carrier_slug_source"] = "shippo_test_tracking_number"
        if not slug:
            return {"tracking_available": False, "identifiers": identifiers, "order": order_state, **ownership,
                    "reason": f"The carrier recorded on this order ('{shipment.get('courier') or 'unknown'}') is not supported for detailed tracking."}
        identifiers["carrier_slug"] = slug
        tracking = self._shippo_track(connector, company_id, slug, shipment["tracking_number"], include_history)
        if "error" in tracking:
            return {"tracking_available": False, "identifiers": identifiers, "order": order_state, **ownership,
                    "error": tracking["error"], "code": tracking.get("code")}
        identifiers["provider_reference"] = tracking.get("provider_reference")
        return {"tracking_available": True, "identifiers": identifiers, "order": order_state, **ownership,
                "tracking": tracking}

    # ---- Tracking registration (idempotent write) ------------------------------------

    _REGISTRATION_RETRYABLE = {"RATE_LIMITED", "PROVIDER_AUTH_FAILED", "PROVIDER_FORBIDDEN", "PROVIDER_NOT_CONFIGURED"}
    _REGISTRATION_PERMANENT = {"PROVIDER_VALIDATION_FAILED", "INVALID_CARRIER", "INVALID_TRACKING_NUMBER", "NOT_FOUND"}

    @staticmethod
    def _claim_registration(key: tuple, order_reference: Optional[str]) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        Atomically claims the (tenant, provider, environment, carrier, tracking number) record.
        Returns ("CLAIMED", row) when this caller may call the provider, else (state, row).
        """
        conn = get_db_connection()
        try:
            try:
                conn.execute(
                    "INSERT INTO shipment_tracking_registrations (company_id, provider, environment, carrier_slug, "
                    "tracking_number, status, attempts, order_reference) VALUES (?, ?, ?, ?, ?, 'PENDING', 1, ?)",
                    (*key, order_reference),
                )
                conn.commit()
                return "CLAIMED", None
            except sqlite3.IntegrityError:
                conn.rollback()
            where = "company_id = ? AND provider = ? AND environment = ? AND carrier_slug = ? AND tracking_number = ?"
            row = dict(conn.execute(f"SELECT * FROM shipment_tracking_registrations WHERE {where}", key).fetchone())
            if row["status"] == "FAILED_RETRYABLE":
                updated = conn.execute(
                    f"UPDATE shipment_tracking_registrations SET status = 'PENDING', attempts = attempts + 1, "
                    f"updated_at = CURRENT_TIMESTAMP WHERE {where} AND status = 'FAILED_RETRYABLE'", key,
                ).rowcount
                conn.commit()
                return ("CLAIMED", row) if updated else ("PENDING", row)
            if row["status"] == "PENDING":
                age = conn.execute(
                    f"SELECT (julianday('now') - julianday(updated_at)) * 86400 FROM shipment_tracking_registrations WHERE {where}", key,
                ).fetchone()[0]
                if age > IntegrationGateway.REGISTRATION_IN_FLIGHT_SECONDS:
                    conn.execute(f"UPDATE shipment_tracking_registrations SET status = 'UNCONFIRMED', "
                                 f"updated_at = CURRENT_TIMESTAMP WHERE {where} AND status = 'PENDING'", key)
                    conn.commit()
                    row["status"] = "UNCONFIRMED"
            return row["status"], row
        finally:
            conn.close()

    def shipping_register_tracking(
        self, company_id: str, carrier: str, tracking_number: str,
        order_reference: Optional[str] = None, session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Registers a tracking number for track_updated webhooks at most once per
        tenant + environment + carrier + tracking number. The record is written before
        the provider is called; an outcome that may have reached Shippo (timeout/5xx)
        is recorded as UNCONFIRMED and never re-sent automatically.
        """
        connector, row = self._resolve_shipping(company_id)
        slug = carrier_slug_for(carrier)
        number = str(tracking_number or "").strip()
        if not slug:
            return {"error": f"Carrier '{carrier}' is not supported for tracking.", "code": "INVALID_CARRIER"}
        if not valid_tracking_number(number):
            return {"error": "That is not a valid tracking number.", "code": "INVALID_TRACKING_NUMBER"}
        environment = row.get("environment") or connector.environment
        if environment == "live" and not row.get("allow_tracking_registration"):
            return {"error": "Registering tracking for updates needs the merchant's approval (Shippo may charge for "
                             "tracking shipments not created in Shippo).", "code": "MERCHANT_APPROVAL_REQUIRED"}

        key = (company_id, "SHIPPO", environment, slug, number)
        state, existing = self._claim_registration(key, order_reference)
        identifiers = {"tracking_number": number, "carrier_slug": slug, "order_number": order_reference}
        if state != "CLAIMED":
            messages = {
                "REGISTERED": "This tracking number is already registered for updates; it was not registered again.",
                "PENDING": "A registration for this tracking number is already in progress.",
                "UNCONFIRMED": "A previous registration attempt may have reached Shippo; it is not re-sent to avoid duplicate notifications.",
                "FAILED_PERMANENT": "Shippo rejected this tracking number earlier; it will not be registered.",
            }
            return {"status": f"ALREADY_{state}" if state == "REGISTERED" else state, "registered_now": False,
                    "identifiers": identifiers, "message": messages.get(state, state)}

        result = self._shipping_result(company_id, connector.register_tracking(slug, number, metadata=f"luintix:{company_id}"))
        code = result.get("code") if "error" in result else None
        if code is None or (code == "PROVIDER_INVALID_RESPONSE" and result.get("http_status") in (None, 200, 201)):
            status = "REGISTERED"  # 2xx from Shippo: registered even if the body could not be normalized
        elif code in self._REGISTRATION_RETRYABLE:
            status = "FAILED_RETRYABLE"
        elif code in self._REGISTRATION_PERMANENT:
            status = "FAILED_PERMANENT"
        else:
            status = "UNCONFIRMED"
        execute_db(
            "UPDATE shipment_tracking_registrations SET status = ?, last_error_code = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE company_id = ? AND provider = ? AND environment = ? AND carrier_slug = ? AND tracking_number = ?",
            (status, code, *key),
        )
        AuditLogger.log_action(
            session_id=session_id, company_id=company_id, order_id=order_reference or "",
            action_type="SHIPPO_REGISTER_TRACKING", provider="SHIPPO", status=status, reason=code,
            details={"carrier_slug": slug, "environment": environment},
        )
        if status == "REGISTERED":
            return {"status": "REGISTERED", "registered_now": True, "identifiers": identifiers,
                    "tracking": result if "error" not in result else None}
        return {"status": status, "registered_now": False, "identifiers": identifiers,
                "error": result["error"], "code": code}

    def shipping_register_order_tracking(
        self, identity: CustomerIdentity, order_id: str, company_id: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Registers the tracking number recorded on a verified store order (never an invented one)."""
        located = self.shipping_track_order(identity, order_id, company_id)
        if "error" in located and not located.get("identifiers"):
            return located
        ids = located.get("identifiers") or {}
        if not ids.get("tracking_number") or not ids.get("carrier_slug"):
            return {"status": "NOT_REGISTERED", "registered_now": False, "identifiers": ids,
                    "reason": located.get("reason") or "No trackable shipment on this order."}
        return self.shipping_register_tracking(company_id, ids["carrier_slug"], ids["tracking_number"],
                                               order_reference=ids.get("order_number"), session_id=session_id)

    # ---- Direct carrier: Delhivery (phase 1, read-only tracking) ----------------------
    # Credentials: carrier_integrations (encrypted, per tenant) or, for exactly one tenant named
    # by DELHIVERY_ENV_TENANT, the DELHIVERY_* environment variables. No global fallback.

    _delhivery_env_invalid: set = set()

    def _resolve_delhivery(self, company_id: str) -> DelhiveryConnector:
        if not company_id:
            raise TenantConfigurationError("No tenant (X-Company-ID) supplied for this request.")
        row = query_db(
            "SELECT * FROM carrier_integrations WHERE company_id = ? AND UPPER(provider_type) = 'DELHIVERY'",
            (company_id,), one=True,
        )
        if row:
            if not row.get("api_token"):
                raise TenantConfigurationError(f"Tenant '{company_id}' has incomplete Delhivery configuration.")
            if (row.get("credential_status") or "ACTIVE").upper() != "ACTIVE":
                raise TenantConfigurationError(
                    f"Tenant '{company_id}' Delhivery credential is {row['credential_status']}; the merchant must reconnect Delhivery."
                )
            connector = DelhiveryConnector(
                credential_store.decrypt(row["api_token"]), base_url=row.get("base_url"),
                environment=row.get("environment"), auth_header=row.get("auth_header"), auth_scheme=row.get("auth_scheme"),
            )
            connector.credential_source = "DATABASE"
        elif os.getenv("DELHIVERY_API_TOKEN") and company_id == os.getenv("DELHIVERY_ENV_TENANT"):
            if company_id in self._delhivery_env_invalid:
                raise TenantConfigurationError(
                    f"Tenant '{company_id}' Delhivery token was rejected; update DELHIVERY_API_TOKEN and restart."
                )
            connector = DelhiveryConnector(
                os.getenv("DELHIVERY_API_TOKEN"), base_url=os.getenv("DELHIVERY_BASE_URL"),
                environment=os.getenv("DELHIVERY_ENVIRONMENT"), auth_header=os.getenv("DELHIVERY_AUTH_HEADER"),
                auth_scheme=os.getenv("DELHIVERY_AUTH_SCHEME"),
            )
            connector.credential_source = "ENV"
        else:
            raise TenantConfigurationError(f"Tenant '{company_id}' has no Delhivery integration.")
        config_error = connector.configuration_error()
        if config_error:
            raise TenantConfigurationError(f"Tenant '{company_id}': {config_error['error']}")
        return connector

    def get_delhivery_connector(self, company_id: str) -> DelhiveryConnector:
        connector = self._resolve_delhivery(company_id)
        log_gateway(
            f"[GATEWAY] Tenant: {company_id} | Carrier Connector: {connector.__class__.__name__} | "
            f"Provider: {connector.provider_name} | Environment: {connector.environment} | "
            f"Credential source: {connector.credential_source} | Credential configured: YES"
        )
        return connector

    def has_delhivery(self, company_id: str) -> bool:
        """Whether this tenant has Delhivery configured (DB row or its env binding). Never raises."""
        try:
            row = query_db(
                "SELECT 1 FROM carrier_integrations WHERE company_id = ? AND UPPER(provider_type) = 'DELHIVERY'",
                (company_id,), one=True,
            )
        except Exception:
            return False
        return bool(row) or bool(os.getenv("DELHIVERY_API_TOKEN") and company_id and company_id == os.getenv("DELHIVERY_ENV_TENANT"))

    def _delhivery_result(self, company_id: str, connector: DelhiveryConnector, result: Dict[str, Any]) -> Dict[str, Any]:
        """401 -> credential marked invalid (merchant must reconnect)."""
        if result.get("code") == "PROVIDER_AUTH_FAILED":
            if connector.credential_source == "DATABASE":
                execute_db("UPDATE carrier_integrations SET credential_status = 'INVALID' WHERE company_id = ? "
                           "AND UPPER(provider_type) = 'DELHIVERY'", (company_id,))
            else:
                self._delhivery_env_invalid.add(company_id)
            log_gateway(f"[GATEWAY] Tenant: {company_id} | Delhivery credential marked INVALID (reconnect required)")
        return result

    def _delhivery_track(
        self, connector: DelhiveryConnector, company_id: str, awb: str, include_history: bool
    ) -> Dict[str, Any]:
        tracking = self._delhivery_result(company_id, connector, connector.get_tracking(awb))
        if "error" in tracking:
            return tracking
        if not include_history:
            tracking = {**tracking, "tracking_history_count": len(tracking["tracking_history"])}
            tracking.pop("tracking_history")
        tracking["fresh_provider_read"] = True
        return tracking

    def delhivery_get_tracking(self, company_id: str, awb: str, include_history: bool = False) -> Dict[str, Any]:
        return self._delhivery_track(self.get_delhivery_connector(company_id), company_id, awb, include_history)

    # ---- CRM (Zoho CRM, read-only) -----------------------------------------------------

    def has_crm(self, company_id: str) -> bool:
        """Whether the tenant's integration is a CRM provider. Never raises."""
        try:
            row = query_db("SELECT provider_type FROM company_integrations WHERE company_id = ?", (company_id,), one=True)
        except Exception:
            return False
        return bool(row) and (row.get("provider_type") or "").upper() == "ZOHO_CRM"

    def _crm_connector(self, company_id: str) -> ZohoCRMConnector:
        connector = self.get_connector(company_id)
        if not isinstance(connector, ZohoCRMConnector):
            raise TenantConfigurationError(f"Tenant '{company_id}' has no CRM integration.")
        return connector

    def crm_search_contacts(self, company_id: str, email: Optional[str] = None, phone: Optional[str] = None,
                            name: Optional[str] = None) -> Dict[str, Any]:
        return self._crm_connector(company_id).search_contacts(company_id, email=email, phone=phone, name=name)

    def crm_get_contact(self, company_id: str, contact_id: str) -> Dict[str, Any]:
        return self._crm_connector(company_id).get_contact(company_id, contact_id)

    def crm_get_account(self, company_id: str, account_id: str) -> Dict[str, Any]:
        return self._crm_connector(company_id).get_account(company_id, account_id)

    def crm_get_deal(self, company_id: str, deal_id: str) -> Dict[str, Any]:
        return self._crm_connector(company_id).get_deal(company_id, deal_id)

    def crm_get_customer_profile(self, company_id: str, contact_id: str) -> Dict[str, Any]:
        """Contact plus its linked account (both fresh reads). Deals need a deal ID."""
        connector = self._crm_connector(company_id)
        contact = connector.get_contact(company_id, contact_id)
        if "error" in contact:
            return contact
        account = connector.get_account(company_id, contact["account_id"]) if contact.get("account_id") else None
        return {"contact": contact, "account": account, "provider": connector.provider_name}

    # ---- Direct carrier: Shadowfax (phase 1, read-only tracking) ----------------------

    _shadowfax_env_invalid: set = set()

    def _resolve_shadowfax(self, company_id: str) -> ShadowfaxConnector:
        if not company_id:
            raise TenantConfigurationError("No tenant (X-Company-ID) supplied for this request.")
        row = query_db(
            "SELECT * FROM carrier_integrations WHERE company_id = ? AND UPPER(provider_type) = 'SHADOWFAX'",
            (company_id,), one=True,
        )
        env_tenant = os.getenv("SHADOWFAX_ENV_TENANT") or "COMP-SHADOWFAX"
        has_env_creds = bool(os.getenv("SHADOWFAX_API_TOKEN") or (os.getenv("SHADOWFAX_CLIENT_ID") and os.getenv("SHADOWFAX_CLIENT_SECRET")))

        has_db_creds = False
        if row:
            token = credential_store.decrypt(row["api_token"]) if row.get("api_token") else None
            client_id = row.get("client_id")
            client_secret = row.get("client_secret")
            if token or (client_id and client_secret):
                has_db_creds = True

        if row and has_db_creds:
            if (row.get("credential_status") or "ACTIVE").upper() != "ACTIVE":
                raise TenantConfigurationError(
                    f"Tenant '{company_id}' Shadowfax credential is {row['credential_status']}; the merchant must reconnect Shadowfax."
                )
            connector = ShadowfaxConnector(
                api_token=credential_store.decrypt(row["api_token"]) if row.get("api_token") else None,
                client_id=row.get("client_id"),
                client_secret=row.get("client_secret"),
                base_url=row.get("base_url"),
                environment=row.get("environment"),
                auth_header=row.get("auth_header"),
                auth_scheme=row.get("auth_scheme"),
            )
            connector.credential_source = "DATABASE"
        elif (row or company_id == env_tenant) and (has_env_creds or company_id == env_tenant):
            if company_id in self._shadowfax_env_invalid:
                raise TenantConfigurationError(
                    f"Tenant '{company_id}' Shadowfax credentials were rejected; update SHADOWFAX credentials and restart."
                )
            connector = ShadowfaxConnector(
                api_token=os.getenv("SHADOWFAX_API_TOKEN"),
                client_id=os.getenv("SHADOWFAX_CLIENT_ID"),
                client_secret=os.getenv("SHADOWFAX_CLIENT_SECRET"),
                base_url=os.getenv("SHADOWFAX_API_BASE_URL"),
            )
            connector.credential_source = "ENV"
        else:
            raise TenantConfigurationError(f"Tenant '{company_id}' has no Shadowfax integration.")

        config_error = connector.configuration_error()
        if config_error:
            raise TenantConfigurationError(f"Tenant '{company_id}': {config_error['error']}")
        return connector

    def get_shadowfax_connector(self, company_id: str) -> ShadowfaxConnector:
        connector = self._resolve_shadowfax(company_id)
        log_gateway(
            f"[GATEWAY] Tenant: {company_id} | Carrier Connector: {connector.__class__.__name__} | "
            f"Provider: {connector.provider_name} | Environment: {connector.environment} | "
            f"Credential source: {connector.credential_source} | Credential configured: YES"
        )
        return connector

    def has_shadowfax(self, company_id: str) -> bool:
        """Whether this tenant has Shadowfax configured (DB row or tenant match). Never raises."""
        try:
            row = query_db(
                "SELECT 1 FROM carrier_integrations WHERE company_id = ? AND UPPER(provider_type) = 'SHADOWFAX'",
                (company_id,), one=True,
            )
        except Exception:
            return False
        env_tenant = os.getenv("SHADOWFAX_ENV_TENANT", "COMP-SHADOWFAX")
        return bool(row) or (bool(company_id) and (company_id == env_tenant or company_id == "COMP-SHADOWFAX"))

    def _shadowfax_result(self, company_id: str, connector: ShadowfaxConnector, result: Dict[str, Any]) -> Dict[str, Any]:
        if result.get("code") == "PROVIDER_AUTH_FAILED":
            if connector.credential_source == "DATABASE":
                execute_db("UPDATE carrier_integrations SET credential_status = 'INVALID' WHERE company_id = ? "
                           "AND UPPER(provider_type) = 'SHADOWFAX'", (company_id,))
            else:
                self._shadowfax_env_invalid.add(company_id)
            log_gateway(f"[GATEWAY] Tenant: {company_id} | Shadowfax credential marked INVALID (reconnect required)")
        return result

    def shadowfax_track_shipment(self, company_id: str, awb: str) -> Dict[str, Any]:
        connector = self.get_shadowfax_connector(company_id)
        return self._shadowfax_result(company_id, connector, connector.track_shipment(awb))

    def shadowfax_get_tracking_history(self, company_id: str, awb: str) -> Dict[str, Any]:
        connector = self.get_shadowfax_connector(company_id)
        return self._shadowfax_result(company_id, connector, connector.get_tracking_history(awb))

    def shadowfax_check_serviceability(self, company_id: str, pickup_pincode: str, delivery_pincode: str) -> Dict[str, Any]:
        connector = self.get_shadowfax_connector(company_id)
        return self._shadowfax_result(company_id, connector, connector.check_serviceability(pickup_pincode, delivery_pincode))

    def shadowfax_track_reverse_pickup(self, company_id: str, reverse_awb: str) -> Dict[str, Any]:
        connector = self.get_shadowfax_connector(company_id)
        return self._shadowfax_result(company_id, connector, connector.track_reverse_pickup(reverse_awb))


    # ---- HubSpot CRM (read-only) -------------------------------------------------------

    def has_hubspot(self, company_id: str) -> bool:
        """Whether the tenant's registered integration is HubSpot. Never raises."""
        try:
            row = query_db("SELECT provider_type FROM company_integrations WHERE company_id = ?", (company_id,), one=True)
        except Exception:
            return False
        return bool(row) and (row.get("provider_type") or "").upper() == "HUBSPOT"

    def _hubspot_connector(self, company_id: str) -> HubSpotConnector:
        connector = self.get_connector(company_id)
        if not isinstance(connector, HubSpotConnector):
            raise TenantConfigurationError(f"Tenant '{company_id}' has no HubSpot integration.")
        return connector

    def hubspot_search_contacts(self, company_id: str, email: Optional[str] = None, phone: Optional[str] = None,
                                name: Optional[str] = None) -> Dict[str, Any]:
        return self._hubspot_connector(company_id).search_contacts(email=email, phone=phone, name=name)

    def hubspot_get_contact(self, company_id: str, contact_id: str) -> Dict[str, Any]:
        return self._hubspot_connector(company_id).get_contact(contact_id)

    def hubspot_get_customer_profile(self, company_id: str, contact_id: str) -> Dict[str, Any]:
        """Contact plus its primary company (associatedcompanyid), both fresh reads."""
        connector = self._hubspot_connector(company_id)
        contact = connector.get_contact(contact_id)
        if "error" in contact:
            return contact
        company = connector.get_company(contact["account_id"]) if contact.get("account_id") else None
        return {"contact": contact, "company": company, "provider": connector.provider_name}

    def hubspot_get_company(self, company_id: str, company_record_id: str) -> Dict[str, Any]:
        return self._hubspot_connector(company_id).get_company(company_record_id)

    def hubspot_get_deal(self, company_id: str, deal_id: str) -> Dict[str, Any]:
        return self._hubspot_connector(company_id).get_deal(deal_id)

    def hubspot_get_ticket(self, company_id: str, ticket_id: str) -> Dict[str, Any]:
        return self._hubspot_connector(company_id).get_ticket(ticket_id)

    # ---- Salesforce CRM (read-only) ----------------------------------------------------

    def has_salesforce(self, company_id: str) -> bool:
        """Whether the tenant's registered integration is Salesforce. Never raises."""
        try:
            row = query_db("SELECT provider_type FROM company_integrations WHERE company_id = ?", (company_id,), one=True)
        except Exception:
            return False
        return bool(row) and (row.get("provider_type") or "").upper() == "SALESFORCE"

    def _salesforce_connector(self, company_id: str) -> SalesforceConnector:
        connector = self.get_connector(company_id)
        if not isinstance(connector, SalesforceConnector):
            raise TenantConfigurationError(f"Tenant '{company_id}' has no Salesforce integration.")
        return connector

    def salesforce_search_contacts(self, company_id: str, email: Optional[str] = None, phone: Optional[str] = None,
                                   name: Optional[str] = None) -> Dict[str, Any]:
        return self._salesforce_connector(company_id).search_contacts(email=email, phone=phone, name=name)

    def salesforce_get_contact(self, company_id: str, contact_id: str) -> Dict[str, Any]:
        return self._salesforce_connector(company_id).get_contact(contact_id)

    def salesforce_get_customer_profile(self, company_id: str, contact_id: str) -> Dict[str, Any]:
        """Contact plus its Account (AccountId), both fresh reads."""
        connector = self._salesforce_connector(company_id)
        contact = connector.get_contact(contact_id)
        if "error" in contact:
            return contact
        account = connector.get_account(contact["account_id"]) if contact.get("account_id") else None
        return {"contact": contact, "account": account, "provider": connector.provider_name}

    def salesforce_get_account(self, company_id: str, account_id: str) -> Dict[str, Any]:
        return self._salesforce_connector(company_id).get_account(account_id)

    def salesforce_get_opportunity(self, company_id: str, opportunity_id: str) -> Dict[str, Any]:
        return self._salesforce_connector(company_id).get_opportunity(opportunity_id)

    def salesforce_get_case(self, company_id: str, case_id: str) -> Dict[str, Any]:
        return self._salesforce_connector(company_id).get_case(case_id)

    # ---- Razorpay payments (read-only) -------------------------------------------------

    def has_razorpay(self, company_id: str) -> bool:
        """Whether the tenant's registered integration is Razorpay. Never raises."""
        try:
            row = query_db("SELECT provider_type FROM company_integrations WHERE company_id = ?", (company_id,), one=True)
        except Exception:
            return False
        return bool(row) and (row.get("provider_type") or "").upper() == "RAZORPAY"

    def _razorpay_connector(self, company_id: str) -> RazorpayConnector:
        connector = self.get_connector(company_id)
        if not isinstance(connector, RazorpayConnector):
            raise TenantConfigurationError(f"Tenant '{company_id}' has no Razorpay integration.")
        return connector

    def razorpay_get_payment(self, company_id: str, payment_id: str) -> Dict[str, Any]:
        return self._razorpay_connector(company_id).get_payment(payment_id)

    def razorpay_get_payment_refunds(self, company_id: str, payment_id: str) -> Dict[str, Any]:
        return self._razorpay_connector(company_id).get_payment_refunds(payment_id)

    def razorpay_get_refund(self, company_id: str, refund_id: str) -> Dict[str, Any]:
        return self._razorpay_connector(company_id).get_refund(refund_id)


# Global gateway instance
gateway = IntegrationGateway()
