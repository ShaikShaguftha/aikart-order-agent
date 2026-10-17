"""
Mocked Freshdesk tests. No real Freshdesk account is contacted: all HTTP goes through
FakeFreshdesk, which records calls (method, host, path, params, body, auth).
"""
import base64
import io
import json
import os
import sys 
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage

import agent
from backend.database import execute_db, init_db, query_db
from backend.integrations import credential_store
from backend.integrations.errors import TenantConfigurationError
from backend.integrations.freshdesk_onboarding import connect_freshdesk
from backend.integrations.gateway import gateway
from backend.integrations.models import CustomerIdentity
from backend.integrations.providers.freshdesk import FreshdeskConnector
from backend.integrations.providers.local_db import LocalDBConnector
from backend.integrations.providers.shopify import ShopifyConnector
from backend.integrations.providers.woocommerce import WooCommerceConnector
from backend.tools import freshdesk_get_my_tickets, freshdesk_tools, tools

TENANT_A, TENANT_B, TENANT_NO_HD = "COMP-TEST-FD-A", "COMP-TEST-FD-B", "COMP-TEST-FD-NOHD"
KEY_A, KEY_B = "fdkeyAAAA1111secretA", "fdkeyBBBB2222secretB"
EMAIL = "alice@example.com"
CONTACT = {"id": 501, "name": "Alice Wonder", "email": EMAIL, "phone": None, "mobile": "+1 555 000 1234"}
OTHER_CONTACT_ID = 777


def _resp(status=200, body=None, headers=None):
    response = MagicMock()
    response.status_code = status
    response.headers = headers or {}
    if isinstance(body, Exception):
        response.json.side_effect = body
    else:
        response.json.return_value = body
    return response


def ticket(tid, requester_id=501, status=2, priority=2, **extra):
    return {"id": tid, "subject": f"Issue {tid}", "status": status, "priority": priority, "requester_id": requester_id,
            "created_at": "2026-09-20T10:00:00Z", "updated_at": "2026-09-21T10:00:00Z", "tags": [],
            "description_text": "My order arrived damaged.", **extra}


class FakeFreshdesk:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    @staticmethod
    def _client_auth(client, request_auth):
        """
        Freshdesk auth may be supplied per request or configured on the
        persistent httpx.Client. Normalize it to (api_key, 'X') for tests.
        """
        if request_auth is not None:
            return request_auth

        auth = getattr(client, "auth", None)

        if auth is None:
            return None

        # httpx.BasicAuth stores a prebuilt Basic authorization header.
        auth_header = getattr(auth, "_auth_header", b"")

        if isinstance(auth_header, bytes):
            auth_header = auth_header.decode()

        if isinstance(auth_header, str) and auth_header.startswith("Basic "):
            encoded = auth_header.split(" ", 1)[1]
            decoded = base64.b64decode(encoded).decode()
            username, password = decoded.split(":", 1)
            return (username, password)

        return auth

    def __call__(
        self,
        client,
        method=None,
        url=None,
        params=None,
        json=None,
        auth=None,
        **_,
    ):
        host, path = url.split("://", 1)[1].split("/api/v2/", 1)

        effective_auth = self._client_auth(
            client,
            auth,
        )

        self.calls.append(
            {
                "method": method,
                "host": host,
                "path": path,
                "params": dict(params or {}),
                "json": json,
                "auth": effective_auth,
            }
        )

        for route_method, route_path, predicate, response in self.routes:
            if (
                route_method == method
                and route_path == path
                and (predicate is None or predicate(params or {}))
            ):
                result = (
                    response()
                    if isinstance(response, types.FunctionType)
                    else response
                )

                if isinstance(result, Exception):
                    raise result

                return result

        return _resp(
            404,
            {
                "code": "not_found",
            },
        )

    def by_method(self, method):
        return [
            call
            for call in self.calls
            if call["method"] == method
        ]

class FreshdeskFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.env_patch = patch.dict(os.environ, {credential_store.KEY_ENV: Fernet.generate_key().decode()})
        cls.env_patch.start()
        init_db()
        for tenant in (TENANT_A, TENANT_B, TENANT_NO_HD):
            execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES (?, ?, 'STANDARD')", (tenant, tenant))
        for tenant, domain, key in ((TENANT_A, "acme", KEY_A), (TENANT_B, "globex.freshdesk.com", KEY_B)):
            execute_db("INSERT OR REPLACE INTO helpdesk_integrations (company_id, provider_type, domain, api_key) VALUES (?, 'FRESHDESK', ?, ?)",
                       (tenant, domain, credential_store.encrypt(key)))

    @classmethod
    def tearDownClass(cls):
        for table, column in (("helpdesk_integrations", "company_id"), ("action_audit_logs", "company_id"), ("companies", "id")):
            for tenant in (TENANT_A, TENANT_B, TENANT_NO_HD):
                execute_db(f"DELETE FROM {table} WHERE {column} = ?", (tenant,))
        cls.env_patch.stop()



    def setUp(self):
        # pytest/Jupyter on Windows can expose an invalid sys.__stdout__ handle.
        # Gateway/provider logging writes to sys.__stdout__, so redirect it to
        # the valid active stdout stream for each test.
        stdout_patch = patch.object(sys, "__stdout__", sys.stdout)
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)

    def fake(self, routes):
        fake = FakeFreshdesk(routes)
        original_request = httpx.Client.request

        def transport(client, method, url, *args, **kwargs):
            if ".freshdesk.com/api/v2/" in str(url):
                return fake(
                    client,
                    method=method,
                    url=str(url),
                    **kwargs,
                )

            return original_request(
                client,
                method,
                url,
                *args,
                **kwargs,
            )

        for target in (
            patch.object(httpx.Client, "request", transport),
            patch("integrations.providers.freshdesk.time.sleep"),
        ):
            target.start()
            self.addCleanup(target.stop)

        return fake
    CONTACT_ROUTE = ("GET", "contacts", lambda p: p.get("email") == EMAIL, _resp(200, [CONTACT]))
    ME = CustomerIdentity(email=EMAIL)


def direct(retry_backoff=0):
    return FreshdeskConnector("acme", KEY_A, retry_backoff=retry_backoff)


class ReadCapabilityTest(FreshdeskFixture):

    def test_01_credential_verification(self):
        fake = self.fake([("GET", "ticket_fields", None, _resp(200, [{"name": "subject"}, {"name": "status"}]))])
        self.assertEqual(gateway.helpdesk_verify_credentials(TENANT_A), {"ok": True, "ticket_field_count": 2})
        self.assertEqual((fake.calls[0]["host"], fake.calls[0]["auth"]), ("acme.freshdesk.com", (KEY_A, "X")))


    def test_http_client_reused_and_closed(self):
        connector = direct()

        first_client = connector._get_client()
        second_client = connector._get_client()

        self.assertIs(first_client, second_client)
        self.assertFalse(first_client.is_closed)

        connector.close()

        self.assertIsNone(connector._client)
        self.assertTrue(first_client.is_closed)

    def test_02_get_contact_exact_match_only(self):
        self.fake([("GET", "contacts", None, _resp(200, [{**CONTACT, "email": "alice.other@example.com"}]))])
        self.assertEqual(gateway.helpdesk_get_contact(self.ME, TENANT_A)["code"], "CUSTOMER_NOT_FOUND")
        self.fake([self.CONTACT_ROUTE])
        self.assertEqual(gateway.helpdesk_get_contact(self.ME, TENANT_A)["contact"]["contact_id"], "501")

    def test_02b_contact_by_phone_falls_back_to_mobile(self):
        self.fake([("GET", "contacts", lambda p: "phone" in p, _resp(200, [])),
                   ("GET", "contacts", lambda p: "mobile" in p, _resp(200, [CONTACT]))])
        result = gateway.helpdesk_get_contact(CustomerIdentity(phone="15550001234"), TENANT_A)
        self.assertEqual(result["contact"]["contact_id"], "501")

    def test_02c_name_alone_is_not_identity(self):
        fake = self.fake([])
        self.assertEqual(gateway.helpdesk_get_contact(CustomerIdentity(name="Alice Wonder"), TENANT_A)["code"], "IDENTITY_REQUIRED")
        self.assertEqual(fake.calls, [])

    def test_03_customer_tickets(self):
        fake = self.fake([self.CONTACT_ROUTE, ("GET", "tickets", None, _resp(200, [ticket(12, status=3), ticket(9, status=4, priority=4)]))])
        result = gateway.helpdesk_get_customer_tickets(self.ME, TENANT_A, limit=5)
        self.assertEqual([(t["ticket_id"], t["status"], t["priority"]) for t in result["tickets"]],
                         [("12", "PENDING", "MEDIUM"), ("9", "RESOLVED", "URGENT")])
        params = fake.by_method("GET")[-1]["params"]
        self.assertEqual((params["requester_id"], params["order_by"], params["order_type"], params["per_page"]), ("501", "created_at", "desc", 5))
        self.assertIn("updated_since", params)

    def test_04_ticket_details_owned_and_not_owned(self):
        self.fake([self.CONTACT_ROUTE, ("GET", "tickets/12", None, _resp(200, ticket(12))),
                   ("GET", "tickets/13", None, _resp(200, ticket(13, requester_id=OTHER_CONTACT_ID)))])
        self.assertEqual(gateway.helpdesk_get_ticket(self.ME, 12, TENANT_A)["description_text"], "My order arrived damaged.")
        other = gateway.helpdesk_get_ticket(self.ME, 13, TENANT_A)
        self.assertEqual(other["code"], "NOT_FOUND")
        self.assertNotIn("Issue 13", json.dumps(other))

    def test_05_conversations_exclude_private_notes(self):
        convs = [{"id": 1, "body_text": "Please help", "private": False, "incoming": True, "from_email": EMAIL},
                 {"id": 2, "body_text": "internal: customer is VIP", "private": True, "incoming": False}]
        fake = self.fake([self.CONTACT_ROUTE, ("GET", "tickets/12", lambda p: p.get("include") == "conversations",
                                               _resp(200, ticket(12, conversations=convs)))])
        result = gateway.helpdesk_get_ticket(self.ME, 12, TENANT_A, include_conversations=True)
        self.assertEqual([c["conversation_id"] for c in result["conversations"]], ["1"])
        self.assertNotIn("VIP", json.dumps(result))
        self.assertNotIn("from_email", json.dumps(result))
        self.assertEqual(fake.calls[-1]["params"], {"include": "conversations"})



    def test_large_ticket_conversations_are_truncated_and_limited(self):
        long_description = "A" * 5000
        long_message = "B" * 3000

        conversations = [
            {
                "id": index,
                "body_text": f"Conversation {index}: {long_message}",
                "private": False,
                "incoming": True,
                "created_at": "2026-09-20T10:00:00Z",
            }
            for index in range(15)
        ]

        fake = self.fake(
            [
                (
                    "GET",
                    "tickets/12",
                    lambda params: params.get("include") == "conversations",
                    _resp(
                        200,
                        ticket(
                            12,
                            description_text=long_description,
                            conversations=conversations,
                        ),
                    ),
                )
            ]
        )

        result = direct().get_ticket_conversations(
            12,
            max_messages=999,
        )

        self.assertEqual(
            len(result["conversations"]),
            10,
        )

        self.assertLessEqual(
            len(result["description_text"]),
            2050,
        )

        for conversation in result["conversations"]:
            self.assertLessEqual(
                len(conversation["body_text"]),
                2050,
            )

        self.assertEqual(
            fake.calls[0]["params"],
            {"include": "conversations"},
        )

    def test_ticket_fields(self):
        self.fake([("GET", "ticket_fields", None, _resp(200, [{"name": "priority", "label": "Priority", "type": "default_priority",
                                                                  "choices": {"Low": 1, "High": 3}}]))])
        self.assertEqual(gateway.helpdesk_get_ticket_fields(TENANT_A)[0]["choices"], {"Low": 1, "High": 3})


class WriteCapabilityTest(FreshdeskFixture):

    def _audit(self, action):
        return query_db("SELECT status, details_json FROM action_audit_logs WHERE company_id = ? AND action_type = ? ORDER BY id DESC",
                        (TENANT_A, f"FRESHDESK_{action}"), one=True)

    def test_06_create_ticket_for_known_contact(self):
        # Escalation now checks the customer's open tickets for this order first (idempotency).
        fake = self.fake([self.CONTACT_ROUTE, ("GET", "tickets", None, _resp(200, [])),
                          ("GET", "ticket_fields", None, _resp(200, [])),
                          ("POST", "tickets", None, _resp(201, ticket(40, priority=3)))])
        result = gateway.helpdesk_create_ticket(self.ME, "Damaged item", "Box was crushed.", "high", TENANT_A, order_id="#1003", session_id="s1")
        self.assertEqual((result["ticket_id"], result["priority"]), ("40", "HIGH"))
        body = fake.by_method("POST")[0]["json"]
        self.assertEqual((body["requester_id"], body["priority"], body["status"], body["source"]), (501, 3, 2, 7))
        self.assertIn("Related order: #1003", body["description"])
        audit = self._audit("CREATE_TICKET")
        self.assertEqual(audit["status"], "SUCCESS")
        self.assertNotIn(EMAIL, audit["details_json"])

    def test_06b_create_ticket_for_new_contact_uses_email(self):
        fake = self.fake([("GET", "contacts", None, _resp(200, [])), ("POST", "tickets", None, _resp(201, ticket(41)))])
        gateway.helpdesk_create_ticket(self.ME, "Question", "Where is my refund?", "medium", TENANT_A)
        self.assertEqual(fake.by_method("POST")[0]["json"]["email"], EMAIL)

    def test_07_update_status(self):
        fake = self.fake([self.CONTACT_ROUTE, ("GET", "tickets/12", None, _resp(200, ticket(12))),
                          ("PUT", "tickets/12", None, _resp(200, ticket(12, status=4)))])
        result = gateway.helpdesk_update_ticket_status(self.ME, 12, "resolved", TENANT_A, session_id="s1")
        self.assertEqual(result["status"], "RESOLVED")
        self.assertEqual(fake.by_method("PUT")[0]["json"], {"status": 4})
        self.assertEqual(self._audit("UPDATE_STATUS")["status"], "SUCCESS")

    def test_07b_cannot_update_another_customers_ticket(self):
        fake = self.fake([self.CONTACT_ROUTE, ("GET", "tickets/13", None, _resp(200, ticket(13, requester_id=OTHER_CONTACT_ID)))])
        result = gateway.helpdesk_update_ticket_status(self.ME, 13, "closed", TENANT_A)
        self.assertEqual(result["code"], "NOT_FOUND")
        self.assertEqual(fake.by_method("PUT"), [])
        self.assertEqual(self._audit("UPDATE_STATUS")["status"], "FAILED")

    def test_08_update_priority(self):
        fake = self.fake([self.CONTACT_ROUTE, ("GET", "tickets/12", None, _resp(200, ticket(12))),
                          ("PUT", "tickets/12", None, _resp(200, ticket(12, priority=4)))])
        self.assertEqual(gateway.helpdesk_update_ticket_priority(self.ME, 12, "urgent", TENANT_A)["priority"], "URGENT")
        self.assertEqual(fake.by_method("PUT")[0]["json"], {"priority": 4})

    def test_09_add_internal_note_is_private(self):
        fake = self.fake([self.CONTACT_ROUTE, ("GET", "tickets/12", None, _resp(200, ticket(12))),
                          ("POST", "tickets/12/notes", None, _resp(201, {"id": 900, "private": True, "created_at": "2026-09-26T09:00:00Z"}))])
        result = gateway.helpdesk_add_internal_note(self.ME, 12, "Verified order #1003 delivered damaged.", TENANT_A)
        self.assertEqual((result["note_id"], result["private"]), ("900", True))
        self.assertEqual(fake.by_method("POST")[0]["json"], {"body": "Verified order #1003 delivered damaged.", "private": True})
        self.assertNotIn("Verified order", self._audit("ADD_INTERNAL_NOTE")["details_json"])

    def test_invalid_status_and_priority_rejected_before_any_call(self):
        fake = self.fake([])
        self.assertEqual(direct().update_ticket_status(12, "escalated")["code"], "INVALID_ARGUMENT")
        self.assertEqual(direct().update_ticket_priority(12, "critical")["code"], "INVALID_ARGUMENT")
        self.assertEqual(direct().get_ticket_details("abc")["code"], "INVALID_TICKET_ID")
        self.assertEqual(fake.calls, [])


class ErrorHandlingTest(FreshdeskFixture):

    def _get(self, response):
        fake = self.fake([("GET", "tickets/12", None, response)])
        return direct().get_ticket_details(12), fake

    def test_10_401(self):
        self.assertEqual(self._get(_resp(401, {"message": f"bad key {KEY_A}"}))[0]["code"], "PROVIDER_AUTH_FAILED")

    def test_11_403(self):
        self.assertEqual(self._get(_resp(403, {}))[0]["code"], "PROVIDER_FORBIDDEN")

    def test_12_404(self):
        self.assertEqual(self._get(_resp(404, {}))[0]["code"], "NOT_FOUND")

    def test_13_429_respects_retry_after_and_is_bounded(self):
        responses = iter([_resp(429, {}, headers={"Retry-After": "3"}), _resp(200, ticket(12))])
        fake = self.fake([("GET", "tickets/12", None, lambda: next(responses))])
        with patch("integrations.providers.freshdesk.time.sleep") as sleep:
            self.assertEqual(direct().get_ticket_details(12)["ticket_id"], "12")
        sleep.assert_called_once_with(3.0)
        self.fake([("GET", "tickets/12", None, _resp(429, {}, headers={"Retry-After": "120"}))])
        with patch("integrations.providers.freshdesk.time.sleep") as sleep:
            self.assertEqual(direct().get_ticket_details(12)["code"], "RATE_LIMITED")
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(c.args[0] <= FreshdeskConnector.MAX_RETRY_AFTER_SECONDS for c in sleep.call_args_list))

    def test_13b_429_on_post_is_retried(self):
        responses = iter([_resp(429, {}, headers={"Retry-After": "1"}), _resp(201, ticket(50))])
        fake = self.fake([("POST", "tickets", None, lambda: next(responses))])
        self.assertEqual(direct().create_ticket("Subj", "Desc body", "low", {"email": EMAIL})["ticket_id"], "50")
        self.assertEqual(len(fake.by_method("POST")), 2)

    def test_14_5xx_retried_for_get_not_for_post(self):
        responses = iter([_resp(503, {}), _resp(200, ticket(12))])
        self.fake([("GET", "tickets/12", None, lambda: next(responses))])
        self.assertEqual(direct().get_ticket_details(12)["ticket_id"], "12")
        fake = self.fake([("POST", "tickets", None, _resp(502, {"trace": "internal"}))])
        result = direct().create_ticket("Subj", "Desc body", "low", {"email": EMAIL})
        self.assertEqual(result["code"], "PROVIDER_UNAVAILABLE")
        self.assertEqual(len(fake.by_method("POST")), 1)
        self.assertNotIn("trace", json.dumps(result))

    def test_15_timeout_retried_only_when_safe(self):
        fake = self.fake([("GET", "tickets/12", None, httpx.ReadTimeout("slow"))])
        self.assertEqual(direct().get_ticket_details(12)["code"], "PROVIDER_TIMEOUT")
        self.assertEqual(len(fake.calls), 3)
        fake = self.fake([("POST", "tickets/12/notes", None, httpx.ReadTimeout("slow"))])
        self.assertEqual(direct().add_internal_note(12, "note text")["code"], "PROVIDER_TIMEOUT")
        self.assertEqual(len(fake.calls), 1)

    def test_malformed_or_empty_200_response_is_invalid(self):
        malformed = MagicMock()
        malformed.status_code = 200
        malformed.headers = {}
        malformed.json.side_effect = ValueError("not valid json")

        result, _ = self._get(malformed)

        self.assertEqual(
            result["code"],
            "PROVIDER_INVALID_RESPONSE",
        )

        self.assertEqual(
            result["http_status"],
            200,
        )

    def test_validation_error_returns_field_names_only(self):
        self.fake([("POST", "tickets", None, _resp(400, {"errors": [{"field": "email", "message": "It should be a valid email", "code": "invalid_value"}]}))])
        result = direct().create_ticket("Subj", "Desc body", "low", {"email": "x@y.z"})
        self.assertEqual((result["code"], result["fields"]), ("PROVIDER_VALIDATION_FAILED", ["email"]))


class IsolationAndRoutingTest(FreshdeskFixture):

    def test_16_tenant_isolation(self):
        fake = self.fake([("GET", "ticket_fields", None, _resp(200, []))])
        gateway.helpdesk_verify_credentials(TENANT_A)
        gateway.helpdesk_verify_credentials(TENANT_B)
        self.assertEqual([(c["host"], c["auth"]) for c in fake.calls],
                         [("acme.freshdesk.com", (KEY_A, "X")), ("globex.freshdesk.com", (KEY_B, "X"))])

    def test_16b_no_fallback_for_unconfigured_or_broken_tenants(self):
        with patch.dict(os.environ, {"FRESHDESK_API_KEY": "should-never-be-used", "FRESHDESK_DOMAIN": "acme"}):
            for tenant in (TENANT_NO_HD, "COMP-SHOPIFY", "COMP-DOES-NOT-EXIST", ""):
                with self.assertRaises(TenantConfigurationError):
                    gateway.get_helpdesk_connector(tenant)
        execute_db("INSERT OR REPLACE INTO helpdesk_integrations (company_id, provider_type, domain, api_key) VALUES (?, 'FRESHDESK', 'evil.example.com', ?)",
                   (TENANT_NO_HD, credential_store.encrypt("k")))
        with self.assertRaises(TenantConfigurationError):
            gateway.get_helpdesk_connector(TENANT_NO_HD)
        execute_db("UPDATE helpdesk_integrations SET provider_type = 'ZENDESK', domain = 'acme' WHERE company_id = ?", (TENANT_NO_HD,))
        with self.assertRaises(TenantConfigurationError):
            gateway.get_helpdesk_connector(TENANT_NO_HD)
        execute_db("DELETE FROM helpdesk_integrations WHERE company_id = ?", (TENANT_NO_HD,))

    def test_16c_tool_returns_safe_error_for_tenant_without_helpdesk(self):
        result = json.loads(freshdesk_get_my_tickets.invoke({"email": EMAIL, "company_id": TENANT_NO_HD}))
        self.assertEqual(result["code"], "TENANT_CONFIGURATION_ERROR")

    def test_17_api_key_masking(self):
        encoded = base64.b64encode(f"{KEY_A}:X".encode()).decode()
        self.assertEqual(direct()._sanitize_error(f"{KEY_A} / {encoded}"), "[REDACTED_API_KEY] / [REDACTED_AUTH]")
        row = query_db("SELECT api_key FROM helpdesk_integrations WHERE company_id = ?", (TENANT_A,), one=True)
        self.assertTrue(row["api_key"].startswith("enc:v1:"))
        self.fake([("GET", "tickets/12", None, RuntimeError(f"exploded with {KEY_A}"))])
        buffer = io.StringIO()
        with patch("sys.__stdout__", buffer), redirect_stdout(buffer), self.assertLogs("uvicorn.error", level="WARNING") as logs:
            result = direct().get_ticket_details(12)
        blob = buffer.getvalue() + json.dumps(result) + "".join(logs.output)
        self.assertNotIn(KEY_A, blob)
        audits = query_db("SELECT details_json, reason FROM action_audit_logs WHERE company_id IN (?, ?)", (TENANT_A, TENANT_B))
        self.assertNotIn(KEY_A, json.dumps(audits))

    def test_18_gateway_routing(self):
        self.assertIsInstance(gateway.get_helpdesk_connector(TENANT_A), FreshdeskConnector)
        self.assertIsInstance(gateway.get_connector("COMP-SHOPIFY"), ShopifyConnector)
        self.assertIsInstance(gateway.get_connector("COMP-WOOCOMMERCE"), WooCommerceConnector)
        self.assertIsInstance(gateway.get_connector("COMP-ALPHA"), LocalDBConnector)
        self.assertTrue(gateway.has_helpdesk(TENANT_A))
        for tenant in ("COMP-SHOPIFY", "COMP-WOOCOMMERCE", "COMP-ALPHA"):
            self.assertFalse(gateway.has_helpdesk(tenant))

    def test_domain_allowlist(self):
        for bad in ("evil.com", "acme.freshdesk.com.evil.com", "http://10.0.0.1", "a_b"):
            self.assertFalse(FreshdeskConnector(bad, KEY_A).domain_configured())

    def test_onboarding_verifies_then_stores_encrypted(self):
        self.fake([("GET", "ticket_fields", None, _resp(401, {}))])
        self.assertFalse(connect_freshdesk(TENANT_NO_HD, "initech", "badkey")["connected"])
        self.assertFalse(gateway.has_helpdesk(TENANT_NO_HD))
        self.fake([("GET", "ticket_fields", None, _resp(200, []))])
        result = connect_freshdesk(TENANT_NO_HD, "https://initech.freshdesk.com", "goodkey123")
        self.assertTrue(result["connected"])
        self.assertNotIn("goodkey123", json.dumps(result))
        self.assertEqual(gateway.get_helpdesk_connector(TENANT_NO_HD).subdomain, "initech")
        execute_db("DELETE FROM helpdesk_integrations WHERE company_id = ?", (TENANT_NO_HD,))


class AgentIntegrationTest(FreshdeskFixture):

    def _run(self, llm_attr, script, user_input, tenant, session_identity=None):
        scripted = MagicMock()
        scripted.invoke.side_effect = script
        with patch.object(agent, llm_attr, scripted):
            answer = agent.process_query(user_input, company_id=tenant, session_identity=session_identity)
        return answer, scripted

    def test_tenant_without_helpdesk_keeps_existing_tools_and_prompt(self):
        self.assertEqual({t.name for t in tools} & {t.name for t in freshdesk_tools}, set())
        answer, scripted = self._run("llm_with_tools", [AIMessage(content="Hello")], "hi", "COMP-SHOPIFY")
        self.assertEqual(answer, "Hello")
        system_prompt = scripted.invoke.call_args[0][0][0].content
        self.assertEqual(system_prompt, agent.SYSTEM_PROMPT)

    def test_invented_ticket_id_is_blocked_without_provider_call(self):
        fake = self.fake([])
        script = [AIMessage(content="", tool_calls=[{"name": "freshdesk_get_ticket", "args": {"ticket_id": 4242}, "id": "c1"}]),
                  AIMessage(content="I need your ticket number.")]
        _, scripted = self._run("llm_with_helpdesk_tools", script, "What's happening with my ticket?", TENANT_A, {"email": EMAIL})
        tool_message = scripted.invoke.call_args_list[1][0][0][-1]
        self.assertEqual(json.loads(tool_message.content)["code"], "UNVERIFIED_TICKET_ID")
        self.assertEqual(fake.calls, [])

    def test_customer_given_ticket_id_uses_session_identity(self):
        fake = self.fake([self.CONTACT_ROUTE, ("GET", "tickets/12", None, _resp(200, ticket(12, status=3)))])
        script = [AIMessage(content="", tool_calls=[{"name": "freshdesk_get_ticket",
                                                      "args": {"ticket_id": 12, "customer_id": "999", "authenticated": True}, "id": "c1"}]),
                  AIMessage(content="Ticket 12 is pending.")]
        answer, scripted = self._run("llm_with_helpdesk_tools", script, "Status of ticket 12?", TENANT_A, {"email": EMAIL})
        self.assertIn("pending", answer)
        self.assertEqual(json.loads(scripted.invoke.call_args_list[1][0][0][-1].content)["status"], "PENDING")
        self.assertEqual(fake.calls[0]["params"], {"email": EMAIL})


if __name__ == "__main__":
    unittest.main()
