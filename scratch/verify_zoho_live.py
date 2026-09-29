"""
READ-ONLY live Zoho CRM verification (tenant COMP-ZOHO, credentials from .env ZOHO_*).

    venv/bin/python scratch/verify_zoho_live.py --email someone@yourcompany.com
    venv/bin/python scratch/verify_zoho_live.py --name Rahul

Only GET requests reach the CRM; the single allowed POST is the OAuth token refresh to the
Zoho accounts server. Secrets are never printed (masked).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, ".")
import env_config  # noqa: E402,F401
import httpx  # noqa: E402

_request = httpx.Client.request


def _guard(self, method, url, *args, **kwargs):
    if str(method).upper() != "GET" and not str(url).endswith("/oauth/v2/token"):
        raise RuntimeError(f"BLOCKED {method} request")
    return _request(self, method, url, *args, **kwargs)


httpx.Client.request = _guard

from integrations.errors import TenantConfigurationError  # noqa: E402
from integrations.gateway import gateway  # noqa: E402


def mask(value):
    value = value or ""
    return (value[:4] + "***" + value[-2:]) if len(value) > 8 else ("***" if value else "(missing)")


parser = argparse.ArgumentParser()
parser.add_argument("--email")
parser.add_argument("--phone")
parser.add_argument("--name")
args = parser.parse_args()
tenant = os.getenv("ZOHO_ENV_TENANT", "COMP-ZOHO")

for key in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_ACCESS_TOKEN"):
    print(f"{key}: {mask(os.getenv(key))}")
print(f"ZOHO_API_DOMAIN: {os.getenv('ZOHO_API_DOMAIN') or 'https://www.zohoapis.com (default)'} | tenant: {tenant}")
if not (args.email or args.phone or args.name):
    print("STOP: give --email, --phone or --name to search (nothing is hardcoded).")
    sys.exit(1)
from database import init_db  # noqa: E402

init_db()  # idempotent: seeds the COMP-ZOHO tenant row exactly as server startup does
try:
    connector = gateway.get_connector(tenant)
except TenantConfigurationError as e:
    print(f"STOP: {e.message}")
    sys.exit(1)

result = gateway.crm_search_contacts(tenant, email=args.email, phone=args.phone, name=args.name)
if "error" in result:
    print(f"RESULT: {result['code']} - {result['error']}")
    print("Live verification: FAILED")
    sys.exit(1)
print(f"search_contacts: {result['count']} contact(s) (searched by {result['searched_by']})")
for contact in result["contacts"]:
    print("  ", json.dumps({k: contact[k] for k in ("id", "first_name", "last_name", "company", "created_at")}))
if result["contacts"]:
    profile = gateway.crm_get_customer_profile(tenant, result["contacts"][0]["id"])
    print("customer profile (contact + account):", "ERROR " + profile["code"] if "error" in profile else "OK")
print("Live verification: PASSED (real Zoho CRM API responses normalized)")
