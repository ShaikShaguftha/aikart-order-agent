"""
Merchant onboarding for WooCommerce:

  Merchant -> provide WooCommerce REST API key (Read, or Read/Write for cancellations)
           -> validate_woocommerce_connection()  (GET-only checks)
           -> connect_woocommerce()              (encrypted storage + tenant integration)
           -> Luintix tools work for that tenant via IntegrationGateway

CLI (prompts for key/secret so they never appear in shell history):
  python -m integrations.onboarding woocommerce --company-id COMP-ACME \\
      --name "Acme Store" --base-url https://shop.acme.com

There is intentionally no HTTP endpoint for this yet: storing merchant credentials must
sit behind merchant/admin authentication, which Luintix does not have today.
"""
import argparse
import getpass
import sys
from typing import Any, Dict, Optional

from database import execute_db, init_db, query_db
from integrations import credential_store
from integrations.audit import AuditLogger
from integrations.errors import TenantConfigurationError
from integrations.providers.woocommerce import WooCommerceConnector

REQUIRED_CHECKS = {
    "orders": ("orders", {"per_page": 1}),
    "products": ("products", {"per_page": 1}),
    "customers": ("customers", {"per_page": 1}),
}


def validate_woocommerce_connection(base_url: str, consumer_key: str, consumer_secret: str) -> Dict[str, Any]:
    """Read-only connection check. Never returns or logs the credentials."""
    connector = WooCommerceConnector(
        site_url=base_url, consumer_key=consumer_key, consumer_secret=consumer_secret, use_env_fallback=False,
    )
    if not base_url.lower().startswith("https://"):
        return {"ok": False, "base_url": connector.site_url, "error": "Store URL must use https://.", "checks": {}}

    checks = {}
    for name, (endpoint, params) in REQUIRED_CHECKS.items():
        res = connector._execute_request("GET", endpoint, params=params, max_retries=1)
        checks[name] = "OK" if isinstance(res, list) else res.get("code", "PROVIDER_ERROR")

    tracking = connector._execute_request(
        "GET", "", max_retries=1, api_base=f"{connector.site_url}/wp-json/wc-shipment-tracking/v3"
    )
    optional = {"shipment_tracking_api": "AVAILABLE" if "error" not in tracking else "NOT_INSTALLED"}

    ok = all(v == "OK" for v in checks.values())
    return {
        "ok": ok,
        "base_url": connector.site_url,
        "checks": checks,
        "optional_capabilities": optional,
        "note": "Write permission (needed for cancellations) cannot be verified without a write call.",
        **({} if ok else {"error": "WooCommerce connection validation failed."}),
    }


def connect_woocommerce(
    company_id: str,
    company_name: str,
    base_url: str,
    consumer_key: str,
    consumer_secret: str,
    webhook_secret: Optional[str] = None,
) -> Dict[str, Any]:
    """Validates the store, then stores the merchant's credentials encrypted for this tenant."""
    if not credential_store.encryption_available():
        raise TenantConfigurationError(
            f"Set {credential_store.KEY_ENV} before connecting a store; credentials are never stored in plaintext."
        )
    validation = validate_woocommerce_connection(base_url, consumer_key, consumer_secret)
    if not validation["ok"]:
        return {"connected": False, **validation}

    init_db()
    existing = query_db("SELECT provider_type FROM company_integrations WHERE company_id = ?", (company_id,), one=True)
    if existing and existing["provider_type"].upper() != "WOOCOMMERCE":
        raise TenantConfigurationError(
            f"Tenant '{company_id}' is already connected to {existing['provider_type']}."
        )

    execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES (?, ?, 'STANDARD')", (company_id, company_name))
    if not query_db("SELECT 1 FROM company_rules WHERE company_id = ?", (company_id,), one=True):
        execute_db(
            "INSERT INTO company_rules (company_id, max_auto_refund_amount, return_window_days) VALUES (?, 100.0, 30)",
            (company_id,),
        )
    execute_db(
        "INSERT INTO company_integrations (company_id, provider_type, shop_domain, api_version, consumer_key, consumer_secret, webhook_secret) "
        "VALUES (?, 'WOOCOMMERCE', ?, 'v3', ?, ?, ?) "
        "ON CONFLICT(company_id) DO UPDATE SET provider_type = 'WOOCOMMERCE', shop_domain = excluded.shop_domain, "
        "api_version = 'v3', consumer_key = excluded.consumer_key, consumer_secret = excluded.consumer_secret, "
        "webhook_secret = excluded.webhook_secret",
        (
            company_id,
            validation["base_url"],
            credential_store.encrypt(consumer_key),
            credential_store.encrypt(consumer_secret),
            credential_store.encrypt(webhook_secret) if webhook_secret else None,
        ),
    )
    AuditLogger.log_action(
        session_id="onboarding", company_id=company_id, order_id="", action_type="CONNECT_INTEGRATION",
        provider="WOOCOMMERCE", status="CONNECTED",
        details={"base_url": validation["base_url"], "checks": validation["checks"], "webhooks": bool(webhook_secret)},
    )
    return {"connected": True, "company_id": company_id, **validation}


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Connect a merchant's WooCommerce store to Luintix.")
    sub = parser.add_subparsers(dest="provider", required=True)
    woo = sub.add_parser("woocommerce")
    woo.add_argument("--company-id", required=True)
    woo.add_argument("--name", required=True)
    woo.add_argument("--base-url", required=True)
    woo.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    import env_config  # noqa: F401  (loads LUINTIX_CREDENTIALS_KEY)

    key = getpass.getpass("WooCommerce consumer key: ")
    secret = getpass.getpass("WooCommerce consumer secret: ")
    if args.validate_only:
        result = validate_woocommerce_connection(args.base_url, key, secret)
    else:
        webhook_secret = getpass.getpass("Webhook secret (optional, Enter to skip): ") or None
        result = connect_woocommerce(args.company_id, args.name, args.base_url, key, secret, webhook_secret)
    print({k: v for k, v in result.items()})
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_cli())
