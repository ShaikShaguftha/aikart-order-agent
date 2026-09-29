"""
READ-ONLY live Salesforce CRM verification for tenant COMP-SALESFORCE.

    PYTHONPATH=. venv/bin/python scratch/verify_salesforce_live.py --email someone@yourcompany.com
    PYTHONPATH=. venv/bin/python scratch/verify_salesforce_live.py --name Rahul
    PYTHONPATH=. venv/bin/python scratch/verify_salesforce_live.py --contact-id 003... [--account-id 001..]
        [--opportunity-id 006..] [--case-id 500.. | --case-id 00001026]

Setup: in .env set SALESFORCE_ACCESS_TOKEN (an access token for a user with API Enabled and read
access to Contact, Account, Opportunity and Case), SALESFORCE_INSTANCE_URL
(https://<domain>.my.salesforce.com), SALESFORCE_ENV_TENANT=COMP-SALESFORCE and optionally
SALESFORCE_API_VERSION (e.g. v62.0; empty = newest version the org reports).

Only GET requests can leave the process (anything else is blocked before sending). The access
token is never printed. No record IDs or customer data are hardcoded.
"""
import argparse
import os
import sys

sys.path.insert(0, ".")
import env_config  # noqa: E402
import httpx  # noqa: E402

TRACE = []
_request = httpx.Client.request


def _read_only(self, method, url, *args, **kwargs):
    method = str(method).upper()
    if method != "GET":
        raise RuntimeError(f"BLOCKED non-read request: {method}")
    response = _request(self, method, url, *args, **kwargs)
    path = str(url).split("?")[0].split(".com/", 1)[-1]  # the SOQL (customer data) is not printed
    TRACE.append((method, path, response.status_code))
    return response


httpx.Client.request = _read_only

from database import init_db  # noqa: E402
from integrations.errors import TenantConfigurationError  # noqa: E402
from integrations.gateway import gateway  # noqa: E402


def show(title, record, fields):
    print(f"\n[{title}]")
    for label, key in fields:
        print(f"  {label}: {record.get(key) if record.get(key) not in (None, '') else '(not recorded)'}")


parser = argparse.ArgumentParser()
parser.add_argument("--email")
parser.add_argument("--phone")
parser.add_argument("--name")
parser.add_argument("--contact-id")
parser.add_argument("--account-id")
parser.add_argument("--opportunity-id")
parser.add_argument("--case-id")
args = parser.parse_args()
tenant = os.getenv("SALESFORCE_ENV_TENANT") or "COMP-SALESFORCE"

print("[CONFIG CHECK]")
print(f"  .env loaded from: {env_config.ENV_FILE or 'NOT FOUND'}")
for key in ("SALESFORCE_ACCESS_TOKEN", "SALESFORCE_INSTANCE_URL"):
    print(f"  {key} configured: {'YES' if os.getenv(key) else 'NO'}")
print(f"  SALESFORCE_API_VERSION: {os.getenv('SALESFORCE_API_VERSION') or '(not set - newest org version is used)'}")
print(f"  SALESFORCE_ENV_TENANT configured: {'YES' if os.getenv('SALESFORCE_ENV_TENANT') else 'NO (default COMP-SALESFORCE)'}")
if not (os.getenv("SALESFORCE_ACCESS_TOKEN") and os.getenv("SALESFORCE_INSTANCE_URL")):
    print("\nCREDENTIALS NOT CONFIGURED - set SALESFORCE_ACCESS_TOKEN and SALESFORCE_INSTANCE_URL in .env. Nothing was sent.")
    print("Live verification: NOT RUN")
    sys.exit(2)
if not any((args.email, args.phone, args.name, args.contact_id, args.account_id, args.opportunity_id, args.case_id)):
    print("\nSTOP: give --email/--phone/--name or a record ID to look up (nothing is hardcoded).")
    sys.exit(2)

init_db()  # idempotent: seeds the COMP-SALESFORCE tenant row exactly as server startup does
try:
    connector = gateway.get_connector(tenant)
except TenantConfigurationError as e:
    print(f"\nSTOP: {e.message}")
    print("Live verification: FAILED")
    sys.exit(1)
print(f"\n[TENANT]: {tenant}\n[PROVIDER]: {connector.provider_name}\n[INSTANCE]: {connector.instance_url}"
      f"\n[CREDENTIAL SOURCE]: ENV")

results = []
if args.email or args.phone or args.name:
    found = gateway.salesforce_search_contacts(tenant, email=args.email, phone=args.phone, name=args.name)
    results.append(found)
    if "error" not in found:
        print(f"\n[SEARCH] {found['count']} contact(s) by {found['searched_by']}")
        for c in found["contacts"]:
            print(f"  ID: {c['id']} | {c.get('first_name') or ''} {c.get('last_name') or ''} | {c.get('email') or '(no email)'}")
        if found["contacts"] and not args.contact_id:
            args.contact_id = found["contacts"][0]["id"]
if args.contact_id:
    profile = gateway.salesforce_get_customer_profile(tenant, args.contact_id)
    results.append(profile)
    if "error" not in profile:
        show("CONTACT", profile["contact"], (("ID", "id"), ("First name", "first_name"), ("Last name", "last_name"),
                                             ("Email", "email"), ("Phone", "phone"), ("Account", "company")))
        if profile.get("account"):
            results.append(profile["account"])
            if "error" not in profile["account"]:
                show("ACCOUNT", profile["account"], (("ID", "id"), ("Name", "name"), ("Website", "website")))
if args.account_id:
    account = gateway.salesforce_get_account(tenant, args.account_id)
    results.append(account)
    if "error" not in account:
        show("ACCOUNT", account, (("ID", "id"), ("Name", "name"), ("Website", "website"), ("Phone", "phone")))
if args.opportunity_id:
    opp = gateway.salesforce_get_opportunity(tenant, args.opportunity_id)
    results.append(opp)
    if "error" not in opp:
        show("OPPORTUNITY", opp, (("ID", "id"), ("Name", "name"), ("Amount", "amount"), ("Stage", "stage"),
                                  ("Close date", "closing_date")))
if args.case_id:
    case = gateway.salesforce_get_case(tenant, args.case_id)
    results.append(case)
    if "error" not in case:
        show("CASE", case, (("ID", "id"), ("Case number", "number"), ("Subject", "subject"), ("Status", "stage"),
                            ("Priority", "priority")))

print("\n[PROVIDER HTTP]")
for method, path, status in TRACE:
    print(f"  {method} {path} | HTTP status: {status}")
errors = [r for r in results if "error" in r]
for err in errors:
    print(f"\nERROR: {err['code']} - {err['error']}")
token = os.getenv("SALESFORCE_ACCESS_TOKEN")
assert token not in str(results), "token leaked into results"
passed = bool(TRACE) and not errors and all(status == 200 for _, _, status in TRACE)
print(f"\nLive verification: {'PASSED' if passed else 'FAILED'}")
sys.exit(0 if passed else 1)
