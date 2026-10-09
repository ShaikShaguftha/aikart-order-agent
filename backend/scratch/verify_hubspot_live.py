"""
READ-ONLY live HubSpot CRM verification for tenant COMP-HUBSPOT.

    PYTHONPATH=. venv/bin/python scratch/verify_hubspot_live.py --email someone@yourcompany.com
    PYTHONPATH=. venv/bin/python scratch/verify_hubspot_live.py --name Rahul
    PYTHONPATH=. venv/bin/python scratch/verify_hubspot_live.py --contact-id 12345 [--deal-id ..] [--ticket-id ..]

Setup (HubSpot > Development > Legacy apps / Private apps): create a private app with ONLY these
read scopes: crm.objects.contacts.read, crm.objects.companies.read, crm.objects.deals.read,
crm.objects.tickets.read. Put its access token in .env as HUBSPOT_ACCESS_TOKEN=... and keep
HUBSPOT_ENV_TENANT=COMP-HUBSPOT.

Only GET requests (and the read-only contacts search POST) can leave the process. The access
token is never printed. No IDs are hardcoded.
"""
import argparse
import os
import sys

sys.path.insert(0, ".")
from backend import env_config # noqa: E402
import httpx  # noqa: E402

from backend.integrations.providers.hubspot import SEARCH_PATH  # noqa: E402

TRACE = []
_request = httpx.Client.request


def _read_only(self, method, url, *args, **kwargs):
    method = str(method).upper()
    if method != "GET" and not (method == "POST" and str(url).endswith("/" + SEARCH_PATH)):
        raise RuntimeError(f"BLOCKED non-read request: {method}")
    response = _request(self, method, url, *args, **kwargs)
    TRACE.append((method, str(url).split("?")[0].split("api.hubapi.com/", 1)[-1], response.status_code))
    return response


httpx.Client.request = _read_only

from backend.database import init_db  # noqa: E402
from backend.integrations.errors import TenantConfigurationError  # noqa: E402
from backend.integrations.gateway import gateway  # noqa: E402


def show(title, record, fields):
    print(f"\n[{title}]")
    for label, key in fields:
        print(f"  {label}: {record.get(key) if record.get(key) not in (None, '') else '(not recorded)'}")


parser = argparse.ArgumentParser()
parser.add_argument("--email")
parser.add_argument("--phone")
parser.add_argument("--name")
parser.add_argument("--contact-id")
parser.add_argument("--deal-id")
parser.add_argument("--ticket-id")
args = parser.parse_args()
tenant = os.getenv("HUBSPOT_ENV_TENANT") or "COMP-HUBSPOT"

print("[CONFIG CHECK]")
print(f"  .env loaded from: {env_config.ENV_FILE or 'NOT FOUND'}")
print(f"  HUBSPOT_ACCESS_TOKEN configured: {'YES' if os.getenv('HUBSPOT_ACCESS_TOKEN') else 'NO'}")
print(f"  HUBSPOT_ENV_TENANT configured: {'YES' if os.getenv('HUBSPOT_ENV_TENANT') else 'NO (default COMP-HUBSPOT)'}")
if not os.getenv("HUBSPOT_ACCESS_TOKEN"):
    print("\nCREDENTIALS NOT CONFIGURED - set HUBSPOT_ACCESS_TOKEN in .env. Nothing was sent.")
    print("Live verification: NOT RUN")
    sys.exit(2)
if not any((args.email, args.phone, args.name, args.contact_id, args.deal_id, args.ticket_id)):
    print("\nSTOP: give --email/--phone/--name or a record ID to look up (nothing is hardcoded).")
    sys.exit(2)

init_db()  # idempotent: seeds the COMP-HUBSPOT tenant row exactly as server startup does
try:
    connector = gateway.get_connector(tenant)
except TenantConfigurationError as e:
    print(f"\nSTOP: {e.message}")
    print("Live verification: FAILED")
    sys.exit(1)
print(f"\n[TENANT]: {tenant}\n[PROVIDER]: {connector.provider_name}\n[CREDENTIAL SOURCE]: ENV")

results = []
if args.email or args.phone or args.name:
    found = gateway.hubspot_search_contacts(tenant, email=args.email, phone=args.phone, name=args.name)
    results.append(found)
    if "error" not in found:
        print(f"\n[SEARCH] {found['count']} contact(s) by {found['searched_by']}")
        for c in found["contacts"]:
            print(f"  ID: {c['id']} | {c.get('first_name') or ''} {c.get('last_name') or ''} | {c.get('email') or '(no email)'}")
        if found["contacts"] and not args.contact_id:
            args.contact_id = found["contacts"][0]["id"]
if args.contact_id:
    profile = gateway.hubspot_get_customer_profile(tenant, args.contact_id)
    results.append(profile)
    if "error" not in profile:
        show("CONTACT", profile["contact"], (("ID", "id"), ("First name", "first_name"), ("Last name", "last_name"),
                                             ("Email", "email"), ("Phone", "phone"), ("Company", "company")))
        if profile.get("company") and "error" not in profile["company"]:
            show("COMPANY", profile["company"], (("ID", "id"), ("Name", "name"), ("Website", "website")))
if args.deal_id:
    deal = gateway.hubspot_get_deal(tenant, args.deal_id)
    results.append(deal)
    if "error" not in deal:
        show("DEAL", deal, (("ID", "id"), ("Name", "name"), ("Amount", "amount"), ("Stage", "stage"), ("Close date", "closing_date")))
if args.ticket_id:
    ticket = gateway.hubspot_get_ticket(tenant, args.ticket_id)
    results.append(ticket)
    if "error" not in ticket:
        show("TICKET", ticket, (("ID", "id"), ("Subject", "subject"), ("Stage", "stage"), ("Priority", "priority")))

print("\n[PROVIDER HTTP]")
for method, path, status in TRACE:
    print(f"  {method} {path} | HTTP status: {status}")
errors = [r for r in results if "error" in r]
for err in errors:
    print(f"\nERROR: {err['code']} - {err['error']}")
token = os.getenv("HUBSPOT_ACCESS_TOKEN")
assert token not in str(results), "token leaked into results"
passed = bool(TRACE) and not errors and all(status == 200 for _, _, status in TRACE)
print(f"\nLive verification: {'PASSED' if passed else 'FAILED'}")
sys.exit(0 if passed else 1)
