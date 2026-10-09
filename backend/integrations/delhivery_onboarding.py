"""
Connect (or rotate) a tenant's Delhivery API token - phase 1, read-only tracking.

  python -m integrations.delhivery_onboarding --company-id COMP-SHOPIFY --awb <one of your AWBs> --verify-only
  python -m integrations.delhivery_onboarding --company-id COMP-SHOPIFY --awb <one of your AWBs>
      [--base-url https://track.delhivery.com] [--environment production|staging]
      [--auth-header Authorization] [--auth-scheme Token]

Verification makes ONE GET tracking request for an AWB the merchant supplies (Delhivery
publishes no read-only credential-check endpoint in the spec). A "no such waybill" answer
still proves the token was accepted. Only after that is the token encrypted and stored.
Re-running replaces the token (rotation) and re-activates an INVALID credential.
The token is read from a hidden prompt and never printed.
"""
import argparse
import getpass
import sys
from typing import Any, Dict, Optional

from backend.database import execute_db, init_db, query_db
from backend.integrations import credential_store
from backend.integrations.audit import AuditLogger
from backend.integrations.errors import TenantConfigurationError
from backend.integrations.providers.delhivery import DelhiveryConnector

ACCEPTED_CODES = {None, "NOT_FOUND"}  # token accepted; shipment may or may not exist


def verify_delhivery(api_token: str, awb: str, base_url: Optional[str] = None, environment: Optional[str] = None,
                     auth_header: Optional[str] = None, auth_scheme: Optional[str] = None) -> Dict[str, Any]:
    connector = DelhiveryConnector(api_token, base_url=base_url, environment=environment,
                                   auth_header=auth_header, auth_scheme=auth_scheme)
    config_error = connector.configuration_error()
    if config_error:
        return {"ok": False, **config_error}
    result = connector.get_tracking(awb)
    code = result.get("code") if "error" in result else None
    return {
        "ok": code in ACCEPTED_CODES,
        "base_url": connector.base_url,
        "environment": connector.environment,
        "auth_format": f"{connector.auth_header}: {connector.auth_scheme + ' ' if connector.auth_scheme else ''}<token>",
        "verification_awb_found": code is None,
        **({"code": code, "error": result.get("error")} if code else {"provider_status": result.get("provider_status")}),
    }


def connect_delhivery(company_id: str, api_token: str, awb: str, base_url: Optional[str] = None,
                      environment: str = "production", auth_header: Optional[str] = None,
                      auth_scheme: Optional[str] = None) -> Dict[str, Any]:
    if not credential_store.encryption_available():
        raise TenantConfigurationError(
            f"Set {credential_store.KEY_ENV} before connecting Delhivery; tokens are never stored in plaintext."
        )
    init_db()
    if not query_db("SELECT 1 FROM companies WHERE id = ?", (company_id,), one=True):
        raise TenantConfigurationError(f"Tenant '{company_id}' does not exist.")
    verification = verify_delhivery(api_token, awb, base_url, environment, auth_header, auth_scheme)
    if not verification["ok"]:
        return {"connected": False, **verification}
    existing = query_db("SELECT 1 FROM carrier_integrations WHERE company_id = ? AND provider_type = 'DELHIVERY'",
                        (company_id,), one=True)
    execute_db(
        "INSERT INTO carrier_integrations (company_id, provider_type, environment, base_url, api_token, auth_header, "
        "auth_scheme, credential_status, last_verified_at) VALUES (?, 'DELHIVERY', ?, ?, ?, ?, ?, 'ACTIVE', CURRENT_TIMESTAMP) "
        "ON CONFLICT(company_id, provider_type) DO UPDATE SET environment = excluded.environment, "
        "base_url = excluded.base_url, api_token = excluded.api_token, auth_header = excluded.auth_header, "
        "auth_scheme = excluded.auth_scheme, credential_status = 'ACTIVE', last_verified_at = CURRENT_TIMESTAMP",
        (company_id, verification["environment"], verification["base_url"], credential_store.encrypt(api_token),
         auth_header, auth_scheme),
    )
    AuditLogger.log_action(
        session_id="onboarding", company_id=company_id, order_id="",
        action_type="ROTATE_CARRIER_CREDENTIAL" if existing else "CONNECT_CARRIER", provider="DELHIVERY",
        status="CONNECTED", details={"environment": verification["environment"], "base_url": verification["base_url"]},
    )
    return {"connected": True, "company_id": company_id, "rotated": bool(existing), **verification}


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Connect or rotate a tenant's Delhivery API token (read-only tracking).")
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--awb", required=True, help="An AWB from your Delhivery account, used for a read-only check.")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--environment", default="production")
    parser.add_argument("--auth-header", default=None)
    parser.add_argument("--auth-scheme", default=None, help='Default "Token"; pass "" for a raw token header value.')
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()

    import env_config  # noqa: F401  (loads LUINTIX_CREDENTIALS_KEY)

    token = getpass.getpass("Delhivery API token: ").strip()
    if args.verify_only:
        result = verify_delhivery(token, args.awb, args.base_url, args.environment, args.auth_header, args.auth_scheme)
        print(result)
        return 0 if result["ok"] else 1
    result = connect_delhivery(args.company_id, token, args.awb, args.base_url, args.environment,
                               args.auth_header, args.auth_scheme)
    print(result)
    return 0 if result.get("connected") else 1


if __name__ == "__main__":
    sys.exit(_cli())
