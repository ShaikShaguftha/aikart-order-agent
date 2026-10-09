"""
Shopify order <-> Freshdesk ticket workflow tests (mocked; see conftest for isolation).

FreshdeskState is a stateful fake of the Freshdesk v2 API: it assigns real-looking IDs,
stores custom fields/notes, and lets a simulated human agent change tickets between calls.
Shopify order state is mocked at the connector level so every fresh read is observable.
"""
import itertools
import json
import os
import re
import unittest
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

import agent
from backend.database import execute_db, init_db
from backend.integrations import credential_store
from backend.integrations.gateway import gateway
from backend.integrations.models import CustomerIdentity
from backend.integrations.providers.freshdesk import FreshdeskConnector
from backend.integrations.providers.shopify import ShopifyConnector
from backend.integrations.providers.woocommerce import WooCommerceConnector

SHOP_TENANT, WOO_TENANT = "COMP-TEST-SF", "COMP-TEST-WF"
EMAIL, OTHER_EMAIL = "vishal@example.com", "someone.else@example.com"
ME = CustomerIdentity(email=EMAIL)
_clock = itertools.count(1)


def _stamp():
    return f"2026-09-26T10:{next(_clock) % 60:02d}:{next(_clock) % 60:02d}Z"


def _resp(status=200, body=None):
    response = MagicMock()
    response.status_code = status
    response.headers = {}
    response.json.return_value = body
    return response


class FreshdeskState:
    """Minimal stateful Freshdesk v2 fake."""

    def __init__(self, order_field=True):
        self.contacts = {501: {"id": 501, "name": "Vishal S", "email": EMAIL},
                         777: {"id": 777, "name": "Someone Else", "email": OTHER_EMAIL}}
        self.fields = [{"name": "cf_order_id", "label": "Order ID", "type": "custom_text", "default": False}] if order_field else []
        self.tickets, self.conversations, self.calls, self.fail = {}, {}, [], {}
        self.next_id = 7

    def add_ticket(self, tid, subject, requester=501, status=2, order_field_value=None, tags=None, description=""):
        self.tickets[tid] = {"id": tid, "subject": subject, "status": status, "priority": 2, "requester_id": requester,
                             "created_at": _stamp(), "updated_at": _stamp(), "tags": tags or [],
                             "custom_fields": {"cf_order_id": order_field_value, "cf_reference_number": None},
                             "description_text": description or subject}
        self.conversations[tid] = []

    def human_sets_status(self, tid, status):
        self.tickets[tid].update(status=status, updated_at=_stamp())

    def writes(self, method=None, path=None):
        return [c for c in self.calls if c[0] != "GET" and (method is None or c[0] == method) and (path is None or c[1] == path)]

    def __call__(self, method=None, url=None, params=None, json=None, **_):
        path, params = url.split("/api/v2/", 1)[1], dict(params or {})
        self.calls.append((method, path, params, json))
        for (m, prefix), status in self.fail.items():
            if m == method and path.startswith(prefix):
                return _resp(status, {"description": "internal failure detail"})
        if method == "GET" and path == "ticket_fields":
            return _resp(200, self.fields)
        if method == "GET" and path == "contacts":
            return _resp(200, [c for c in self.contacts.values() if c["email"] == params.get("email")])
        if method == "GET" and path == "tickets":
            mine = [t for t in self.tickets.values() if str(t["requester_id"]) == str(params.get("requester_id"))]
            return _resp(200, sorted(mine, key=lambda t: t["created_at"], reverse=True))
        if method == "POST" and path == "tickets":
            tid, self.next_id = self.next_id, self.next_id + 1
            requester = json.get("requester_id") or next(
                (c["id"] for c in self.contacts.values() if c["email"] == json.get("email")), 900)
            self.add_ticket(tid, json["subject"], requester=requester, tags=json.get("tags"),
                            order_field_value=(json.get("custom_fields") or {}).get("cf_order_id"),
                            description=json["description"])
            self.tickets[tid]["priority"] = json["priority"]
            return _resp(201, self.tickets[tid])
        match = re.fullmatch(r"tickets/(\d+)(/notes)?", path)
        if match and int(match.group(1)) in self.tickets:
            ticket = self.tickets[int(match.group(1))]
            if method == "POST" and match.group(2):
                note = {"id": 9000 + len(self.calls), "body_text": json["body"], "private": json["private"],
                        "incoming": False, "created_at": _stamp()}
                self.conversations[ticket["id"]].append(note)
                return _resp(201, note)
            if method == "PUT":
                for key in ("status", "priority"):
                    if key in json:
                        ticket[key] = json[key]
                ticket["custom_fields"].update(json.get("custom_fields") or {})
                ticket["updated_at"] = _stamp()
                return _resp(200, ticket)
            if method == "GET":
                body = dict(ticket)
                if params.get("include") == "conversations":
                    body["conversations"] = self.conversations[ticket["id"]]
                return _resp(200, body)
        return _resp(404, {})


class ShopifyOrders:
    """Canonical Shopify order state, read through ShopifyConnector.get_order_details."""

    def __init__(self):
        self.orders = {"#1003": {"id": "#1003", "status": "CANCELLED", "financial_status": "VOIDED",
                                 "fulfillment_status": "CANCELLED", "total": 49.95, "customer_email": EMAIL}}
        self.reads = 0

    def __call__(self, order_id, company_id):
        self.reads += 1
        key = order_id if order_id.startswith("#") else f"#{order_id}"
        return dict(self.orders[key]) if key in self.orders else None


class WorkflowFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {credential_store.KEY_ENV: Fernet.generate_key().decode()})
        cls.env.start()
        init_db()
        for tenant, provider in ((SHOP_TENANT, "SHOPIFY"), (WOO_TENANT, "WOOCOMMERCE")):
            execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES (?, ?, 'STANDARD')", (tenant, tenant))
            execute_db("INSERT OR REPLACE INTO helpdesk_integrations (company_id, provider_type, domain, api_key) VALUES (?, 'FRESHDESK', 'aikart', ?)",
                       (tenant, credential_store.encrypt("fd_test_key_123")))
        execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type, shop_domain, access_token) VALUES (?, 'SHOPIFY', 'test.myshopify.com', 'shpat_test')", (SHOP_TENANT,))
        execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type, shop_domain, consumer_key, consumer_secret, api_version) VALUES (?, 'WOOCOMMERCE', 'https://w.example.com', ?, ?, 'v3')",
                   (WOO_TENANT, credential_store.encrypt("ck_w"), credential_store.encrypt("cs_w")))

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        FreshdeskConnector._order_field_cache.clear()
        self.fd = FreshdeskState()
        self.shop = ShopifyOrders()
        for target in (patch("httpx.Client.request", side_effect=self.fd),
                       patch("integrations.providers.freshdesk.time.sleep"),
                       patch.object(ShopifyConnector, "get_order_details", side_effect=self.shop)):
            target.start()
            self.addCleanup(target.stop)

    def escalate(self, description="My order #1003 was delayed and I need help.", tenant=SHOP_TENANT, identity=ME):
        return gateway.helpdesk_create_ticket(identity, "Help with order #1003", description, "medium", tenant, order_id="#1003")


class TicketIdAndEscalationTest(WorkflowFixture):

    def test_A_existing_ticket_lookup(self):
        self.fd.add_ticket(4, "Order #1003 delayed")
        status = gateway.helpdesk_complaint_status(ME, SHOP_TENANT)
        self.assertEqual((status["ticket"]["ticket_reference"], status["ticket"]["order_reference"],
                          status["ticket"]["order_reference_source"]), ("#4", "#1003", "subject"))

    def test_B_D_G_escalation_creates_real_ticket_with_order_field(self):
        result = self.escalate()
        self.assertEqual((result["ticket_id"], result["ticket_reference"], result["reused"]), ("7", "#7", False))
        method, path, _, body = self.fd.writes("POST", "tickets")[0]
        self.assertEqual(body["custom_fields"], {"cf_order_id": "#1003"})  # exact, not ORD-1003/1003
        self.assertIn("luintix-intent-delivery_issue", body["tags"])
        self.assertIn("status CANCELLED", body["description"])  # fresh Shopify state at escalation
        self.assertTrue(result["order_field_stored"])
        self.assertEqual(self.shop.reads, 1)

    def test_C_no_fake_tick_reference_anywhere(self):
        result = self.escalate()
        self.assertNotIn("TICK", json.dumps(result))
        self.assertNotIn("ORD-1003", json.dumps(result))

    def test_E_reuses_existing_open_ticket_and_links_order(self):
        self.fd.add_ticket(4, "Order #1003 delayed")
        result = self.escalate("Still waiting on #1003, please help")
        self.assertEqual((result["ticket_reference"], result["reused"], result["note_added"], result["order_field_linked"]),
                         ("#4", True, True, True))
        self.assertEqual(self.fd.writes("POST", "tickets"), [])
        self.assertEqual(self.fd.tickets[4]["custom_fields"]["cf_order_id"], "#1003")
        self.assertTrue(self.fd.conversations[4][0]["private"])

    def test_F_repeated_escalation_never_duplicates(self):
        first, second, third = self.escalate(), self.escalate(), self.escalate()
        self.assertEqual({first["ticket_reference"], second["ticket_reference"], third["ticket_reference"]}, {"#7"})
        self.assertEqual(len(self.fd.writes("POST", "tickets")), 1)
        self.assertEqual((second["reused"], third["reused"]), (True, True))

    def test_resolved_ticket_is_not_reused(self):
        self.fd.add_ticket(4, "Order #1003 delayed", status=4)
        self.assertEqual(self.escalate()["ticket_reference"], "#7")

    def test_G_missing_order_field_is_reported_not_created(self):
        self.fd.fields = []
        result = self.escalate()
        self.assertTrue(result["order_field_missing"])
        body = self.fd.writes("POST", "tickets")[0][3]
        self.assertNotIn("custom_fields", body)
        self.assertIn("Related order: #1003", body["description"])
        self.assertFalse([c for c in self.fd.calls if c[1] == "ticket_fields" and c[0] != "GET"])

    def test_woocommerce_tenant_with_freshdesk_reads_order_from_woocommerce(self):
        with patch.object(WooCommerceConnector, "get_order_details",
                          return_value={"id": "1003", "status": "PROCESSING", "financial_status": "PAID",
                                        "fulfillment_status": "UNFULFILLED", "total": 10.0, "customer_email": EMAIL}) as woo:
            result = gateway.helpdesk_create_ticket(ME, "Where is 1003", "Where is my order 1003?", "low", WOO_TENANT, order_id="1003")
        self.assertEqual(result["order"]["provider"], "WOOCOMMERCE")
        self.assertEqual(self.fd.writes("POST", "tickets")[0][3]["custom_fields"], {"cf_order_id": "1003"})
        woo.assert_called_once()
        self.assertEqual(self.shop.reads, 0)


class ReconciliationTest(WorkflowFixture):

    def sync(self, tid):
        return gateway.helpdesk_sync_ticket_with_order(ME, SHOP_TENANT, ticket_id=tid, session_id="s1")["tickets"][0]

    def test_H_cancelled_order_with_open_ticket_is_reported_separately(self):
        self.fd.add_ticket(4, "Order #1003 delayed")
        status = gateway.helpdesk_complaint_status(ME, SHOP_TENANT, ticket_id=4)
        self.assertEqual((status["ticket"]["status"], status["order"]["status"]), ("OPEN", "CANCELLED"))
        self.assertEqual(self.fd.writes(), [])

    def test_I_cancellation_request_ticket_is_resolved_once_cancelled(self):
        self.fd.add_ticket(5, "Please cancel my order #1003")
        outcome = self.sync(5)
        self.assertEqual((outcome["intent"], outcome["decision"], outcome["actions_applied"]),
                         ("CANCEL_REQUEST", "RESOLVE", ["NOTE_ADDED", "RESOLVED"]))
        self.assertEqual(self.fd.tickets[5]["status"], 4)
        note = self.fd.conversations[5][0]
        self.assertTrue(note["private"])
        self.assertIn("#1003", note["body_text"])
        writes = [(m, p) for m, p, _, _ in self.fd.writes()]
        self.assertEqual(writes, [("POST", "tickets/5/notes"), ("PUT", "tickets/5")])  # note first, then status

    def test_I_cancellation_request_stays_open_if_order_not_cancelled(self):
        self.shop.orders["#1003"].update(status="UNFULFILLED", fulfillment_status="UNFULFILLED")
        self.fd.add_ticket(5, "Please cancel my order #1003")
        self.assertEqual(self.sync(5)["decision"], "KEEP_OPEN")
        self.assertEqual(self.fd.writes(), [])

    def test_J_K_D_non_cancellation_complaints_stay_open(self):
        for tid, subject, intent in ((10, "My order #1003 was cancelled. When will I get my refund?", "REFUND_STATUS"),
                                     (11, "Why was my order #1003 cancelled?", "CANCELLATION_REASON"),
                                     (12, "My order #1003 is cancelled but I still need the product.", "STILL_NEEDS_PRODUCT")):
            self.fd.add_ticket(tid, subject)
            outcome = self.sync(tid)
            self.assertEqual((outcome["intent"], outcome["decision"], outcome["actions_applied"]), (intent, "KEEP_OPEN", []))
        self.assertEqual(self.fd.writes(), [])

    def test_L_human_sets_pending_luintix_reads_pending(self):
        self.fd.add_ticket(4, "Order #1003 delayed")
        self.assertEqual(gateway.helpdesk_complaint_status(ME, SHOP_TENANT)["ticket"]["status"], "OPEN")
        self.fd.human_sets_status(4, 3)
        self.assertEqual(gateway.helpdesk_complaint_status(ME, SHOP_TENANT)["ticket"]["status"], "PENDING")

    def test_M_human_resolves_luintix_reads_resolved_and_never_overrides(self):
        self.fd.add_ticket(5, "Please cancel my order #1003")
        self.fd.human_sets_status(5, 4)
        self.assertEqual(gateway.helpdesk_complaint_status(ME, SHOP_TENANT, ticket_id=5)["ticket"]["status"], "RESOLVED")
        self.assertEqual(self.sync(5)["decision"], "NO_ACTION")
        self.fd.human_sets_status(5, 5)
        self.assertEqual(self.sync(5)["decision"], "NO_ACTION")
        self.assertEqual(self.fd.writes(), [])

    def test_O_P_fresh_reads_every_time(self):
        self.fd.add_ticket(4, "Order #1003 delayed")
        self.shop.orders["#1003"].update(status="UNFULFILLED", fulfillment_status="UNFULFILLED")
        first = gateway.helpdesk_complaint_status(ME, SHOP_TENANT, ticket_id=4)
        self.shop.orders["#1003"].update(status="CANCELLED", fulfillment_status="CANCELLED")
        self.fd.human_sets_status(4, 3)
        second = gateway.helpdesk_complaint_status(ME, SHOP_TENANT, ticket_id=4)
        self.assertEqual((first["order"]["status"], second["order"]["status"]), ("UNFULFILLED", "CANCELLED"))
        self.assertEqual((first["ticket"]["status"], second["ticket"]["status"]), ("OPEN", "PENDING"))
        self.assertEqual(self.shop.reads, 2)
        self.assertEqual(len([c for c in self.fd.calls if c[0] == "GET" and c[1] == "tickets/4"]), 2)

    def test_latest_public_message_excludes_private_notes(self):
        self.fd.add_ticket(4, "Order #1003 delayed")
        self.fd.conversations[4] = [
            {"id": 1, "body_text": "We are checking with the courier.", "private": False, "incoming": False, "created_at": _stamp()},
            {"id": 2, "body_text": "internal: courier lost it", "private": True, "incoming": False, "created_at": _stamp()},
        ]
        status = gateway.helpdesk_complaint_status(ME, SHOP_TENANT, ticket_id=4)
        self.assertEqual((status["latest_public_message"]["from"], status["public_message_count"]), ("support", 1))
        self.assertNotIn("lost it", json.dumps(status))


class FailureAndSecurityTest(WorkflowFixture):

    def test_N_failed_create_is_reported_as_failure(self):
        self.fd.fail[("POST", "tickets")] = 500
        result = self.escalate()
        self.assertEqual(result["code"], "PROVIDER_UNAVAILABLE")
        self.assertNotIn("ticket_reference", result)
        self.assertNotIn("internal failure detail", json.dumps(result))
        self.assertEqual(len(self.fd.writes("POST", "tickets")), 1)  # never retried (no duplicates)

    def test_N_failed_note_means_no_resolution(self):
        self.fd.add_ticket(5, "Please cancel my order #1003")
        self.fd.fail[("POST", "tickets/5/notes")] = 500
        outcome = gateway.helpdesk_sync_ticket_with_order(ME, SHOP_TENANT, ticket_id=5)["tickets"][0]
        self.assertEqual((outcome["actions_applied"], outcome["errors"][0]["action"]), ([], "ADD_INTERNAL_NOTE"))
        self.assertEqual(self.fd.tickets[5]["status"], 2)

    def test_Q_other_customers_ticket_is_not_found(self):
        self.fd.add_ticket(20, "Order #2000 broken", requester=777)
        for call in (lambda: gateway.helpdesk_complaint_status(ME, SHOP_TENANT, ticket_id=20),
                     lambda: gateway.helpdesk_sync_ticket_with_order(ME, SHOP_TENANT, ticket_id=20),
                     lambda: gateway.helpdesk_get_ticket(ME, 20, SHOP_TENANT)):
            result = call()
            self.assertEqual(result["code"], "NOT_FOUND")
            self.assertNotIn("#2000", json.dumps(result))
        self.assertEqual(self.fd.writes(), [])

    def test_Q_other_customers_order_is_withheld_and_not_linked(self):
        self.shop.orders["#1003"]["customer_email"] = OTHER_EMAIL
        self.fd.add_ticket(4, "Order #1003 delayed")
        status = gateway.helpdesk_complaint_status(ME, SHOP_TENANT, ticket_id=4)
        self.assertTrue(status["order"]["withheld"])
        self.assertNotIn("CANCELLED", json.dumps(status["order"]))
        self.assertEqual(self.escalate()["code"], "ORDER_OWNERSHIP_MISMATCH")
        self.assertEqual(self.fd.writes(), [])


class AgentWorkflowTest(WorkflowFixture):

    def run_agent(self, script, user_input, session_identity=None):
        scripted = MagicMock()
        scripted.invoke.side_effect = script
        with patch.object(agent, "llm_with_helpdesk_tools", scripted):
            answer = agent.process_query(user_input, company_id=SHOP_TENANT, session_identity=session_identity or {"email": EMAIL})
        return answer, scripted

    def test_helpdesk_tenant_does_not_offer_legacy_local_ticket_tools(self):
        bound = {t["function"]["name"] for t in agent.llm_with_helpdesk_tools.kwargs["tools"]}
        self.assertFalse(bound & agent.HELPDESK_REPLACED_TOOLS)
        self.assertIn("freshdesk_create_ticket", bound)

    def test_C_legacy_escalation_redirects_to_real_ticket_and_fake_id_is_corrected(self):
        script = [
            AIMessage(content="", tool_calls=[{"name": "escalate_to_human_tool",
                                               "args": {"order_id": "#1003", "reason": "Customer is upset about the delay"}, "id": "c1"}]),
            AIMessage(content="I've escalated this. Your ticket number is TICK-1003."),
            AIMessage(content="I've escalated this to our support team. Your support ticket is #7."),
        ]
        answer, scripted = self.run_agent(script, "This is unacceptable, get me a human for order #1003")
        self.assertEqual(answer, "I've escalated this to our support team. Your support ticket is #7.")
        messages = scripted.invoke.call_args_list[-1][0][0]
        tool_result = json.loads(next(m for m in messages if isinstance(m, ToolMessage)).content)
        self.assertEqual((tool_result["ticket_reference"], tool_result["order_reference"]), ("#7", "#1003"))
        self.assertTrue(any(isinstance(m, SystemMessage) and "TICK-1003" in m.content for m in messages))
        self.assertEqual(len(self.fd.writes("POST", "tickets")), 1)

    def test_N_claim_without_successful_action_is_never_returned(self):
        self.fd.fail[("POST", "tickets")] = 503
        script = [
            AIMessage(content="", tool_calls=[{"name": "freshdesk_create_ticket",
                                               "args": {"subject": "Delay on #1003", "description": "Order #1003 delayed", "order_id": "#1003"}, "id": "c1"}]),
            AIMessage(content="I've created a support ticket for you: ticket #9."),
            AIMessage(content="I have opened ticket #9 for you."),
        ]
        answer, _ = self.run_agent(script, "please open a ticket for order #1003")
        self.assertEqual(answer, agent.CLAIM_FALLBACK)

    def test_no_tool_fake_promise_is_corrected(self):
        script = [AIMessage(content="I'll flag this for the fulfillment team right away."),
                  AIMessage(content="Would you like me to open a support ticket so our team can look into it?")]
        answer, _ = self.run_agent(script, "my order is late")
        self.assertTrue(answer.startswith("Would you like me to open"))

    def test_I_successful_cancellation_syncs_cancel_request_ticket(self):
        self.fd.add_ticket(5, "Please cancel my order #1003")
        cancelled = {"success": True, "status": "CANCELLED", "order_id": "#1003", "refund_issued": False, "verification_status": "VERIFIED"}
        script = [
            AIMessage(content="", tool_calls=[{"name": "cancel_order_tool", "args": {"order_id": "#1003", "confirmed": True}, "id": "c1"}]),
            AIMessage(content="Your order #1003 has been cancelled and your support ticket #5 is now resolved."),
        ]
        with patch.object(gateway, "cancel_order", return_value=cancelled):
            answer, scripted = self.run_agent(script, "YES cancel #1003")
        tool_result = json.loads(scripted.invoke.call_args_list[1][0][0][-1].content)
        self.assertEqual(tool_result["support_ticket_sync"]["tickets"][0]["actions_applied"], ["NOTE_ADDED", "RESOLVED"])
        self.assertIn("#5", answer)
        self.assertEqual(self.fd.tickets[5]["status"], 4)

    def test_complaint_status_answer_uses_fresh_tool_result(self):
        self.fd.add_ticket(4, "Order #1003 delayed")
        script = [
            AIMessage(content="", tool_calls=[{"name": "freshdesk_get_complaint_status", "args": {}, "id": "c1"}]),
            AIMessage(content="Your support ticket #4 is still open. It is related to order #1003, which is currently cancelled."),
        ]
        answer, scripted = self.run_agent(script, "Maine already complaint ki thi, uska kya hua?")
        self.assertIn("#4", answer)
        tool_result = json.loads(scripted.invoke.call_args_list[1][0][0][-1].content)
        self.assertEqual((tool_result["ticket"]["status"], tool_result["order"]["status"]), ("OPEN", "CANCELLED"))


if __name__ == "__main__":
    unittest.main()
