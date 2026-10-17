"""
READ-ONLY live Delhivery tracking verification.

    venv/bin/python scratch/verify_delhivery_live.py --awb <AWB from your Delhivery account>
    venv/bin/python scratch/verify_delhivery_live.py --tenant COMP-SHOPIFY --awb <AWB>   # via the tenant's gateway config

Credentials: with --tenant, whatever the Luintix gateway resolves for that tenant (encrypted DB
row or its DELHIVERY_ENV_TENANT binding); otherwise DELHIVERY_API_TOKEN / DELHIVERY_BASE_URL /
DELHIVERY_ENVIRONMENT / DELHIVERY_AUTH_HEADER / DELHIVERY_AUTH_SCHEME from .env.
No AWB is hardcoded (also read from DELHIVERY_VERIFY_AWB). Only GET requests can leave the
process; the token is never printed (masked).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, ".")
from backend import env_config  # noqa: E402,F401
import httpx  # noqa: E402

STATUSES = []
_request = httpx.Client.request


def _get_only(self, method, url, *args, **kwargs):
    if str(method).upper() != "GET":
        raise RuntimeError(f"BLOCKED non-GET request: {method}")
    response = _request(self, method, url, *args, **kwargs)
    STATUSES.append(response.status_code)
    return response


httpx.Client.request = _get_only

from backend.integrations.providers.delhivery import DelhiveryConnector  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--awb", default=os.getenv("DELHIVERY_VERIFY_AWB"))
parser.add_argument("--tenant", default=None)
args = parser.parse_args()

if not args.awb:
    print("STOP: provide an AWB from your Delhivery account with --awb (or DELHIVERY_VERIFY_AWB). None is hardcoded.")
    sys.exit(1)

if args.tenant:
    from integrations.errors import TenantConfigurationError  # noqa: E402
    from integrations.gateway import gateway  # noqa: E402
    try:
        connector = gateway.get_delhivery_connector(args.tenant)
    except TenantConfigurationError as e:
        print(f"STOP: {e.message}")
        sys.exit(1)
else:
    token = os.getenv("DELHIVERY_API_TOKEN")
    if not token:
        print("STOP: DELHIVERY_API_TOKEN is not set (and no --tenant given). Nothing was sent.")
        sys.exit(1)
    connector = DelhiveryConnector(token, base_url=os.getenv("DELHIVERY_BASE_URL"),
                                   environment=os.getenv("DELHIVERY_ENVIRONMENT"),
                                   auth_header=os.getenv("DELHIVERY_AUTH_HEADER"),
                                   auth_scheme=os.getenv("DELHIVERY_AUTH_SCHEME"))

config_error = connector.configuration_error()
masked = (connector.api_token[:3] + "***" + connector.api_token[-2:]) if len(connector.api_token) > 6 else "***"
print(f"base_url={connector.base_url} environment={connector.environment} "
      f"auth='{connector.auth_header}: {(connector.auth_scheme + ' ') if connector.auth_scheme else ''}{masked}'")
if config_error:
    print(f"STOP: {config_error}")
    sys.exit(1)

result = connector.get_tracking(args.awb)
print(f"HTTP status(es): {STATUSES or 'no request sent'}")
if "error" in result:
    print(f"RESULT: {result['code']} - {result['error']}")
    print("Live verification: FAILED" if result["code"] != "NOT_FOUND"
          else "Live verification: token ACCEPTED, but this AWB was not found for the account")
    sys.exit(1)
print("RESULT (normalized):")
print(json.dumps({k: v for k, v in result.items() if k != "tracking_history"}, indent=2))
print(f"tracking_history: {len(result['tracking_history'])} scans")
for scan in result["tracking_history"][-5:]:
    print(f"  {scan['status_date']}  {scan['provider_status']!s:<20} -> {scan['status']:<16} {(scan['location'] or {}).get('label')}")
print("Live verification: PASSED (real Delhivery API response normalized)")
