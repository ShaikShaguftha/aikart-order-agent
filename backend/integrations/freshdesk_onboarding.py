"""
Connect a tenant's Freshdesk helpdesk (in addition to its store provider).

  python -m integrations.freshdesk_onboarding --company-id COMP-SHOPIFY --domain acme
      (prompts for the API key; add --verify-only to test without storing)

Flow: verify credentials (GET /api/v2/ticket_fields) -> encrypt API key ->
store in helpdesk_integrations for that tenant only -> audit log.
There is intentionally no HTTP endpoint: storing credentials needs admin authentication.
"""
import argparse
import getpass
import sys
from typing import Any, Dict

from backend.database import execute_db, init_db, query_db
from backend.integrations import credential_store
from backend.integrations.audit import AuditLogger
from backend.integrations.errors import TenantConfigurationError
from backend.integrations.providers.freshdesk import FreshdeskConnector


def verify_freshdesk(domain: str, api_key: str) -> Dict[str, Any]:
    connector = FreshdeskConnector(domain, api_key)
    if not connector.domain_configured():
        return {"ok": False, "error": "Freshdesk domain must be <subdomain> or <subdomain>.freshdesk.com.", "code": "INVALID_DOMAIN"}
    return {"subdomain": connector.subdomain, **connector.verify_credentials()}


def connect_freshdesk(company_id: str, domain: str, api_key: str) -> Dict[str, Any]:
    if not credential_store.encryption_available():
        raise TenantConfigurationError(
            f"Set {credential_store.KEY_ENV} before connecting Freshdesk; API keys are never stored in plaintext."
        )
    init_db()
    if not query_db("SELECT 1 FROM companies WHERE id = ?", (company_id,), one=True):
        raise TenantConfigurationError(f"Tenant '{company_id}' does not exist.")

    verification = verify_freshdesk(domain, api_key)
    if not verification.get("ok"):
        return {"connected": False, **verification}

    execute_db(
        "INSERT INTO helpdesk_integrations (company_id, provider_type, domain, api_key) VALUES (?, 'FRESHDESK', ?, ?) "
        "ON CONFLICT(company_id) DO UPDATE SET provider_type = 'FRESHDESK', domain = excluded.domain, api_key = excluded.api_key",
        (company_id, verification["subdomain"], credential_store.encrypt(api_key)),
    )
    AuditLogger.log_action(
        session_id="onboarding", company_id=company_id, order_id="", action_type="CONNECT_HELPDESK",
        provider="FRESHDESK", status="CONNECTED", details={"subdomain": verification["subdomain"]},
    )
    return {"connected": True, "company_id": company_id, **verification}


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Connect a tenant's Freshdesk helpdesk to Luintix.")
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--domain", required=True, help="acme or acme.freshdesk.com")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    import env_config  # noqa: F401  (loads LUINTIX_CREDENTIALS_KEY)

    api_key = getpass.getpass("Freshdesk API key: ")
    result = verify_freshdesk(args.domain, api_key) if args.verify_only else connect_freshdesk(args.company_id, args.domain, api_key)
    print(result)
    return 0 if (result.get("connected") or (args.verify_only and result.get("ok"))) else 1


if __name__ == "__main__":
    sys.exit(_cli())
