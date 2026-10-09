"""
READ-ONLY live verification: Shopify order <-> Freshdesk ticket for one tenant.

    venv/bin/python scratch/verify_freshdesk_shopify_live.py [ORDER] [TICKET] [COMPANY_ID]
    (defaults: "#1003" 4 COMP-SHOPIFY)

Write protection (enforced before any request is sent):
  - Freshdesk: only GET requests are allowed.
  - Shopify: only the OAuth token exchange and GraphQL *queries* are allowed; any mutation is blocked.
Never prints API keys/tokens; emails are masked.
"""
import sys

sys.path.insert(0, ".")
from backend import env_config  # noqa: E402,F401
import httpx  # noqa: E402

BLOCKED = []
_request, _post = httpx.Client.request, httpx.Client.post


def _allowed(method, url, payload):
    method, url = str(method).upper(), str(url).split("?")[0]
    if method == "GET":
        return True
    if method == "POST" and url.endswith("/admin/oauth/access_token"):
        return True
    query = str((payload or {}).get("query", "")).lower()
    return method == "POST" and url.endswith("/graphql.json") and "mutation" not in query


def _guard_request(self, method, url, *args, **kwargs):
    if not _allowed(method, url, kwargs.get("json")):
        BLOCKED.append(f"{method} {str(url).split('?')[0]}")
        raise RuntimeError("BLOCKED write request")
    return _request(self, method, url, *args, **kwargs)


def _guard_post(self, url, *args, **kwargs):
    if not _allowed("POST", url, kwargs.get("json")):
        BLOCKED.append(f"POST {url}")
        raise RuntimeError("BLOCKED Shopify mutation/unknown POST")
    return _post(self, url, *args, **kwargs)


httpx.Client.request, httpx.Client.post = _guard_request, _guard_post

from backend.integrations.gateway import gateway  # noqa: E402
from backend.integrations.models import CustomerIdentity  # noqa: E402
from backend.integrations.support_workflow import extract_order_reference, ticket_intent  # noqa: E402

ORDER = sys.argv[1] if len(sys.argv) > 1 else "#1003"
TICKET = int(sys.argv[2]) if len(sys.argv) > 2 else 4
TENANT = sys.argv[3] if len(sys.argv) > 3 else "COMP-SHOPIFY"


def mask(v):
    v = str(v or "")
    if "@" not in v:
        return v[:3] + "***" if v else "(none)"
    user, dom = v.split("@", 1)
    return f"{user[:2]}***@{dom[:1]}***.{dom.rsplit('.', 1)[-1]}"


print(f"\n## 1. Shopify order {ORDER} (fresh read, tenant {TENANT})")
order = gateway.get_order_details(ORDER, TENANT)
if not order or "error" in order:
    print("  ", order)
else:
    print(f"  id={order['id']} status={order['status']} financial={order['financial_status']} "
          f"fulfillment={order['fulfillment_status']} total={order['total']} customer={mask(order.get('customer_email'))}")

fd = gateway.get_helpdesk_connector(TENANT)
print(f"\n## 2. Freshdesk ticket #{TICKET} (fresh read)")
raw = fd.get_ticket_conversations(TICKET)
if "error" in raw:
    print("  ", raw)
    sys.exit(1)
public = [c for c in raw["conversations"] if not c["private"]]
print(f"  ticket_reference=#{raw['ticket_id']} subject={raw['subject']!r} status={raw['status']} priority={raw['priority']}")
print(f"  created={raw['created_at']} updated={raw['updated_at']} tags={raw['tags']}")

print("\n## 3. Order link on the ticket")
order_field = fd.get_order_field()
ref, source = extract_order_reference(raw, order_field)
print(f"  merchant order field: {order_field or 'NOT FOUND'} | value on ticket: {(raw['custom_fields'] or {}).get(order_field)!r}")
print(f"  linked order reference: {ref!r} (source: {source}) | complaint intent: {ticket_intent(raw)}")

print("\n## 4. Conversations")
print(f"  total={len(raw['conversations'])} public={len(public)} private={len(raw['conversations']) - len(public)}")
if public:
    last = public[-1]
    print(f"  latest public message from {'customer' if last['from_customer'] else 'support'} at {last['created_at']}")

print("\n## 5. Customer ownership")
contact = fd.get_contact_by_id(raw["requester_id"]).get("contact") or {}
print(f"  ticket requester: contact {contact.get('contact_id')} email={mask(contact.get('email'))}")
if order and "error" not in order:
    same = (order.get("customer_email") or "").casefold() == (contact.get("email") or "").casefold()
    print(f"  Shopify order {ORDER} customer email matches ticket requester: {same}")

if contact.get("email"):
    identity = CustomerIdentity(email=contact["email"])
    print("\n## 6. Luintix combined complaint status (as the ticket's own customer)")
    status = gateway.helpdesk_complaint_status(identity, TENANT, ticket_id=TICKET)
    if "error" in status:
        print("  ", status)
    else:
        print(f"  ticket {status['ticket']['ticket_reference']} = {status['ticket']['status']} | order: {status['order']}")
    print("\n## 7. Reconciliation DRY RUN (apply=False, no writes)")
    sync = gateway.helpdesk_sync_ticket_with_order(identity, TENANT, ticket_id=TICKET, apply=False)
    for t in sync.get("tickets", []):
        print(f"  {t['ticket_reference']}: intent={t['intent']} decision={t['decision']} - {t['reason']}")
    print("\n## 8. Duplicate check for a new escalation on this order (read only)")
    existing = gateway._open_ticket_for_order(fd, contact, ORDER)
    print(f"  an escalation for {ORDER} would reuse: "
          f"{'#' + existing['ticket']['ticket_id'] if existing.get('ticket') else 'nothing (a new ticket would be created)'}")

print(f"\nWrite requests attempted and blocked: {BLOCKED or 'none'}")
