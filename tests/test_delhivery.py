"""
Mocked Delhivery phase-1 (read-only tracking) tests. No Delhivery account is needed:
FakeDelhivery records every request (method, host, path, waybill, auth header) so tests can
prove tenant isolation, GET-only behaviour and that no AWB or status is ever invented.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

import agent
from database import execute_db, init_db, query_db
from integrations import credential_store
from integrations.errors import TenantConfigurationError
from integrations.gateway import IntegrationGateway, gateway
from integrations.models import CustomerIdentity
from integrations.providers.delhivery import DelhiveryConnector
from integrations.providers.shippo import _EndpointBudget
from integrations.providers.shopify import ShopifyConnector

DLV_A, DLV_B, DLV_ONLY = "COMP-TEST-DLV-A", "COMP-TEST-DLV-B", "COMP-TEST-DLV-ONLY"
SHIPPO_T, STORE_T, ENV_T = "COMP-TEST-DLV-SHIPPO", "COMP-TEST-DLV-STORE", "COMP-TEST-DLV-ENV"
TOKEN_A, TOKEN_B, TOKEN_ENV = "dlv_token_tenant_a_0001", "dlv_token_tenant_b_0002", "dlv_token_env_0003"
AWB = "1234567890123"
EMAIL = "buyer@example.com"


def shipment(awb=AWB, status="In Transit", status_type="UD", location="Delhi_Hub (Delhi)", reference="#1005",
             expected="2026-09-30T00:00:00", nsl=None, instructions="Shipment in transit", scans=None):
    s = {"AWB": awb, "ReferenceNo": reference, "PickUpDate": "2026-09-27T09:00:00",
         "Status": {"Status": status, "StatusType": status_type, "StatusLocation": location,
                    "StatusDateTime": "2026-09-28T10:00:00", "Instructions": instructions},
         "Scans": scans if scans is not None else [
             {"ScanDetail": {"Scan": "Manifested", "ScanDateTime": "2026-09-27T08:00:00", "ScanType": "UD",
                             "ScannedLocation": "Mumbai_Hub (Maharashtra)", "Instructions": "Manifest uploaded"}},
             {"ScanDetail": {"Scan": status, "ScanDateTime": "2026-09-28T10:00:00", "ScanType": status_type,
                             "ScannedLocation": location, "Instructions": instructions}}]}
    if expected:
        s["ExpectedDeliveryDate"] = expected
    if nsl:
        s["NSLCode"] = nsl
    return {"ShipmentData": [{"Shipment": s}]}


def _resp(status=200, body=None, headers=None, bad_json=False):
    r = MagicMock()
    r.status_code, r.headers = status, headers or {}
    if bad_json:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = body
    return r


class FakeDelhivery:
    def __init__(self):
        self.calls, self.queue = [], []

    @staticmethod
    def _normalized_headers(headers):
        """
        Normalize persistent httpx client headers for predictable test assertions.
        HTTPX may internally use lowercase keys.
        """
        return {
            "-".join(part.capitalize() for part in str(key).split("-")): value
            for key, value in dict(headers or {}).items()
        }

    def __call__(
        self,
        client,
        method=None,
        url=None,
        params=None,
        headers=None,
        **_,
    ):
        host, path = url.split("://", 1)[1].split("/", 1)

        effective_headers = headers or client.headers

        self.calls.append(
            {
                "method": method,
                "host": host,
                "path": path,
                "waybill": (params or {}).get("waybill"),
                "auth": self._normalized_headers(effective_headers),
            }
        )

        if self.queue:
            item = self.queue.pop(0)

            if isinstance(item, Exception):
                raise item

            return item

        return _resp(
            200,
            shipment(
                awb=(params or {}).get("waybill"),
            ),
        )

class DelhiveryFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.env = patch.dict(os.environ, {credential_store.KEY_ENV: Fernet.generate_key().decode()})
        cls.env.start()
        init_db()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):

        # pytest/Jupyter on Windows can expose an invalid sys.__stdout__ handle.
        # Gateway/provider logging writes to sys.__stdout__, so redirect it to
        # the active valid stdout stream during each test.
        stdout_patch = patch.object(sys, "__stdout__", sys.stdout)
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)
        for table in ("carrier_integrations", "shipping_integrations"):
            execute_db(f"DELETE FROM {table}")
        IntegrationGateway._delhivery_env_invalid.clear()
        _EndpointBudget._buckets.clear()
        for tenant in (DLV_A, DLV_B, DLV_ONLY, SHIPPO_T, STORE_T, ENV_T):
            execute_db("INSERT OR IGNORE INTO companies (id, name, tier) VALUES (?, ?, 'STANDARD')", (tenant, tenant))
        for tenant in (DLV_A, SHIPPO_T, STORE_T):
            execute_db("INSERT OR REPLACE INTO company_integrations (company_id, provider_type, shop_domain, access_token) "
                       "VALUES (?, 'SHOPIFY', 'test.myshopify.com', 'shpat_test')", (tenant,))
        for tenant, token, base in ((DLV_A, TOKEN_A, None), (DLV_B, TOKEN_B, "https://staging-express.delhivery.com"),
                                    (DLV_ONLY, TOKEN_A, None)):
            execute_db("INSERT INTO carrier_integrations (company_id, provider_type, base_url, api_token) VALUES (?, 'DELHIVERY', ?, ?)",
                       (tenant, base, credential_store.encrypt(token)))
        execute_db("INSERT INTO shipping_integrations (company_id, provider_type, environment, api_token) VALUES (?, 'SHIPPO', 'test', ?)",
                   (SHIPPO_T, credential_store.encrypt("shippo_test_dlvsuite_token")))
        self.fake = FakeDelhivery()
        self.shippo_calls = []
        fake = self.fake

        def transport(client, method, url, *args, **kwargs):
            if "api.goshippo.com" in str(url):
                self.shippo_calls.append(str(url))
                r = _resp(200, {"carrier": "ups", "tracking_number": "1Z999AA10123456784", "test": True,
                                "tracking_status": {"status": "TRANSIT", "object_updated": "2026-09-28T10:00:00Z"},
                                "tracking_history": []})
                return r
            if "delhivery.com" in str(url):
                return fake(
                    client,
                    method=method,
                    url=str(url),
                    **kwargs,
                )
            return original(client, method, url, *args, **kwargs)

        original = httpx.Client.request
        self.order = {"id": "#1005", "status": "FULFILLED", "financial_status": "PAID", "fulfillment_status": "FULFILLED",
                      "total": 10.0, "customer_email": EMAIL,
                      "shipment": {"id": "gid://shopify/Fulfillment/1", "order_id": "#1005", "courier": "Delhivery",
                                   "tracking_number": AWB, "status": "SUCCESS", "tracking_url": None}}
        self.order_reads = MagicMock(side_effect=lambda order_id, company_id: json.loads(json.dumps(self.order))
                                     if order_id in ("#1005", "1005") else None)
        for target in (patch.object(httpx.Client, "request", transport),
                       patch("integrations.providers.delhivery.time.sleep"),
                       patch.object(ShopifyConnector, "get_order_details", self.order_reads)):
            target.start()
            self.addCleanup(target.stop)

    def direct(self, awb=AWB, tenant=DLV_A, history=False):
        return gateway.shipping_get_tracking(tenant, "Delhivery", awb, include_history=history)


class TrackingTest(DelhiveryFixture):

    def test_01_valid_tracking_lookup(self):
        result = self.direct()
        t = result["tracking"]
        self.assertEqual((t["provider"], t["awb"], t["status"], t["provider_status"], t["status_location"]),
                         ("delhivery", AWB, "IN_TRANSIT", "In Transit", "Delhi_Hub (Delhi)"))
        self.assertEqual((t["expected_delivery"], t["order_reference"], t["status_timestamp"]),
                         ("2026-09-30T00:00:00", "#1005", "2026-09-28T10:00:00"))
        call = self.fake.calls[0]
        self.assertEqual((call["method"], call["host"], call["path"], call["waybill"]),
                         ("GET", "track.delhivery.com", "api/v1/packages/json/", AWB))
        self.assertEqual(call["auth"]["Authorization"], f"Token {TOKEN_A}")


    def test_http_client_reused_and_closed(self):
        connector = DelhiveryConnector(
            api_token=TOKEN_A,
            base_url="https://track.delhivery.com",
            timeout=2.0,
            retry_backoff=0.001,
        )

        first_client = connector._get_client()
        second_client = connector._get_client()

        self.assertIs(first_client, second_client)
        self.assertFalse(first_client.is_closed)

        connector.close()

        self.assertIsNone(connector._client)
        self.assertTrue(first_client.is_closed)
        
    def test_02_tracking_history(self):
        history = self.direct(history=True)["tracking"]["tracking_history"]
        self.assertEqual([(h["provider_status"], h["status"]) for h in history], [("Manifested", "PRE_TRANSIT"), ("In Transit", "IN_TRANSIT")])
        self.assertEqual(history[0]["location"]["label"], "Mumbai_Hub (Maharashtra)")
        self.assertNotIn("tracking_history", self.direct()["tracking"])


    def test_large_tracking_history_and_instruction_text_are_truncated(self):
        long_instruction = "X" * 2000

        scans = [
            {
                "ScanDetail": {
                    "Scan": "In Transit",
                    "ScanType": "UD",
                    "ScannedLocation": "Transit Hub",
                    "ScanDateTime": "2026-09-28T10:00:00",
                    "Instructions": long_instruction,
                }
            }
            for _ in range(150)
        ]

        raw_shipment = shipment(
            scans=scans,
            instructions=long_instruction,
        )["ShipmentData"][0]["Shipment"]

        normalized = DelhiveryConnector.normalize_shipment(
            raw_shipment,
        )

        self.assertEqual(
            len(normalized["tracking_history"]),
            100,
        )

        self.assertLessEqual(
            len(normalized["status_details"]),
            550,
        )

        first_scan = normalized["tracking_history"][0]

        self.assertLessEqual(
            len(first_scan["status_details"]),
            550,
        )

        self.assertIn(
            "[TRUNCATED FOR TOKEN LIMITS]",
            first_scan["status_details"],
        )
        
    def test_03_normalized_statuses(self):
        cases = {"Pending": ("PRE_TRANSIT", "PENDING"), "Ready to Ship": ("PRE_TRANSIT", "READY_TO_SHIP"),
                 "Ready for Pickup": ("PRE_TRANSIT", "READY_FOR_PICKUP"), "In Transit": ("IN_TRANSIT", "IN_TRANSIT"),
                 "Out for Delivery": ("OUT_FOR_DELIVERY", "OUT_FOR_DELIVERY"), "Delivered": ("DELIVERED", "DELIVERED"),
                 "Cancelled": ("CANCELLED", "CANCELLED"), "RTO-Intransit": ("RETURNED", "RETURNING"),
                 "RTO-Returned": ("RETURNED", "RETURNED"), "Lost": ("EXCEPTION", "LOST")}
        for provider_status, (status, detail) in cases.items():
            with self.subTest(provider_status=provider_status):
                t = DelhiveryConnector.normalize_shipment(shipment(status=provider_status)["ShipmentData"][0]["Shipment"])
                self.assertEqual((t["status"], t["substatus"], t["provider_status"]), (status, detail, provider_status))

    def test_03b_ndr_is_exception_only_on_explicit_signal(self):
        t = DelhiveryConnector.normalize_shipment(shipment(status="Pending", nsl="EOD-74", instructions="Consignee refused")["ShipmentData"][0]["Shipment"])
        self.assertEqual((t["status"], t["substatus"], t["action_required"]), ("EXCEPTION", "NDR", True))
        self.assertEqual(t["ndr_state"], {"detected": True, "code": "EOD-74", "reason": "Consignee refused"})
        self.assertIsNone(DelhiveryConnector.normalize_shipment(shipment()["ShipmentData"][0]["Shipment"])["ndr_state"])

    def test_04_unknown_status_is_not_guessed(self):
        t = DelhiveryConnector.normalize_shipment(shipment(status="Dispatched")["ShipmentData"][0]["Shipment"])
        self.assertEqual((t["status"], t["substatus"], t["provider_status"]), ("UNKNOWN", None, "Dispatched"))

    def test_04b_missing_fields_are_null(self):
        t = DelhiveryConnector.normalize_shipment({"AWB": AWB, "Status": {"Status": "In Transit"}})
        self.assertEqual((t["expected_delivery"], t["order_reference"], t["current_location"], t["status_location"],
                          t["tracking_history"]), (None, None, None, None, []))

    def test_05_missing_tracking_number(self):
        self.assertEqual(self.direct(awb="")["code"], "INVALID_TRACKING_NUMBER")
        self.order["shipment"] = None
        result = gateway.shipping_track_order(CustomerIdentity(email=EMAIL), "#1005", DLV_A)
        self.assertEqual((result["tracking_available"], result["identifiers"]["tracking_number"]), (False, None))
        self.assertIn("not available", result["reason"])
        self.assertEqual(self.fake.calls, [])

    def test_06_shipment_not_found(self):
        self.fake.queue = [_resp(200, {"Error": "No such waybill or Order Id found"}),
                           _resp(200, {"ShipmentData": []})]
        self.assertEqual(self.direct()["code"], "NOT_FOUND")
        self.assertEqual(self.direct()["code"], "NOT_FOUND")


class ErrorTest(DelhiveryFixture):

    def test_07_401_marks_invalid_and_requires_reconnect(self):
        self.fake.queue = [_resp(401, {"error": "invalid token"})]
        result = self.direct()
        self.assertEqual((result["code"], result["reconnect_required"]), ("PROVIDER_AUTH_FAILED", True))
        row = query_db("SELECT credential_status FROM carrier_integrations WHERE company_id = ?", (DLV_A,), one=True)
        self.assertEqual(row["credential_status"], "INVALID")
        with self.assertRaises(TenantConfigurationError):
            gateway.get_delhivery_connector(DLV_A)

    def test_08_403_is_entitlement_not_invalidation(self):
        self.fake.queue = [_resp(403, {})]
        self.assertEqual(self.direct()["code"], "PROVIDER_FORBIDDEN")
        self.assertEqual(query_db("SELECT credential_status FROM carrier_integrations WHERE company_id = ?", (DLV_A,), one=True)["credential_status"], "ACTIVE")

    def test_09_404_shipment_vs_route(self):
        self.fake.queue = [_resp(404, {"Error": "not found"}), _resp(404, bad_json=True)]
        self.assertEqual(self.direct()["code"], "NOT_FOUND")
        self.assertEqual(self.direct()["code"], "PROVIDER_API_UNAVAILABLE")

    def test_09b_400_and_409_not_retried(self):
        self.fake.queue = [_resp(400, {}), _resp(409, {})]
        self.assertEqual(self.direct()["code"], "PROVIDER_VALIDATION_FAILED")
        self.assertEqual(self.direct()["code"], "PROVIDER_CONFLICT")
        self.assertEqual(len(self.fake.calls), 2)

    def test_10_429_retry_after_then_success_and_bounded(self):
        self.fake.queue = [_resp(429, {}, headers={"Retry-After": "2"})]
        with patch("integrations.providers.delhivery.time.sleep") as sleep:
            self.assertEqual(self.direct()["tracking"]["status"], "IN_TRANSIT")
        sleep.assert_called_once_with(2.0)
        self.fake.queue = [_resp(429, {}, headers={"Retry-After": "900"})] * 3
        with patch("integrations.providers.delhivery.time.sleep") as sleep:
            self.assertEqual(self.direct()["code"], "RATE_LIMITED")
        self.assertTrue(all(c.args[0] <= DelhiveryConnector.MAX_RETRY_AFTER_SECONDS for c in sleep.call_args_list))

    def test_11_5xx_retried_with_backoff(self):
        self.fake.queue = [_resp(503, {"trace": "internal"})]
        self.assertEqual(self.direct()["tracking"]["status"], "IN_TRANSIT")
        self.fake.queue = [_resp(502, {}), _resp(500, {}), _resp(504, {})]
        result = self.direct()
        self.assertEqual(result["code"], "PROVIDER_UNAVAILABLE")
        self.assertNotIn("trace", json.dumps(result))
        self.assertTrue(all(c["method"] == "GET" for c in self.fake.calls))

    def test_12_timeout_and_network(self):
        self.fake.queue = [httpx.ReadTimeout("slow")] * 3
        self.assertEqual(self.direct()["code"], "PROVIDER_TIMEOUT")
        self.assertEqual(len(self.fake.calls), 3)
        self.fake.queue = [httpx.ConnectError("down")] * 3
        self.assertEqual(self.direct()["code"], "PROVIDER_UNREACHABLE")

    def test_13_malformed_responses(self):
        for response in (_resp(200, bad_json=True), _resp(200, {"ShipmentData": "nope"}), _resp(200, ["x"]),
                         _resp(200, {"unexpected": True}),
                         _resp(200, {"ShipmentData": [{"Shipment": {"AWB": AWB, "Status": "oops"}}]}),
                         _resp(200, {"ShipmentData": [{"Shipment": {"AWB": "OTHER-AWB-1"}}]})):
            with self.subTest(body=str(response.json)[:40]):
                self.fake.queue = [response]
                self.assertEqual(self.direct()["code"], "PROVIDER_INVALID_RESPONSE")

    def test_14_credential_never_in_logs_or_results(self):
        self.fake.queue = [RuntimeError(f"exploded with {TOKEN_A}")]
        buffer = io.StringIO()
        with patch("sys.__stdout__", buffer), redirect_stdout(buffer), self.assertLogs("uvicorn.error", level="WARNING") as logs:
            result = self.direct()
            ok = self.direct()
        blob = buffer.getvalue() + json.dumps(result) + json.dumps(ok) + "".join(logs.output)
        self.assertNotIn(TOKEN_A, blob)
        self.assertNotIn(TOKEN_A, json.dumps(query_db("SELECT * FROM carrier_integrations")))
        self.assertNotIn(TOKEN_A, json.dumps(query_db("SELECT * FROM action_audit_logs")))


class RoutingAndIsolationTest(DelhiveryFixture):

    def test_15_tenant_isolation(self):
        self.direct(tenant=DLV_A)
        self.direct(tenant=DLV_B)
        self.assertEqual([(c["host"], c["auth"]["Authorization"]) for c in self.fake.calls],
                         [("track.delhivery.com", f"Token {TOKEN_A}"), ("staging-express.delhivery.com", f"Token {TOKEN_B}")])

    def test_15b_env_token_only_for_its_named_tenant(self):
        with patch.dict(os.environ, {"DELHIVERY_API_TOKEN": TOKEN_ENV, "DELHIVERY_ENV_TENANT": ""}):
            self.assertFalse(gateway.has_delhivery(ENV_T))
            with self.assertRaises(TenantConfigurationError):
                gateway.get_delhivery_connector(ENV_T)
        with patch.dict(os.environ, {"DELHIVERY_API_TOKEN": TOKEN_ENV, "DELHIVERY_ENV_TENANT": ENV_T,
                                     "DELHIVERY_AUTH_HEADER": "X-Api-Key", "DELHIVERY_AUTH_SCHEME": ""}):
            self.assertTrue(gateway.has_delhivery(ENV_T))
            self.assertFalse(gateway.has_delhivery(STORE_T))
            self.direct(tenant=ENV_T)
            self.direct(tenant=DLV_A)  # DB tenant keeps its own token even when env is set
        env_headers = self.fake.calls[0]["auth"]

        self.assertEqual(
            env_headers["X-Api-Key"],
            TOKEN_ENV,
        )

        self.assertEqual(
            env_headers["Accept"],
            "application/json",
        )

        self.assertEqual(
            env_headers["User-Agent"],
            "Luintix-Delhivery-Connector/1.0",
        )
        self.assertEqual(self.fake.calls[1]["auth"]["Authorization"], f"Token {TOKEN_A}")

    def test_15c_non_delhivery_base_url_is_refused_without_request(self):
        execute_db("UPDATE carrier_integrations SET base_url = 'https://evil.example.com' WHERE company_id = ?", (DLV_A,))
        with self.assertRaises(TenantConfigurationError):
            gateway.get_delhivery_connector(DLV_A)
        self.assertEqual(self.fake.calls, [])

    def test_16_delhivery_tenant_routes_to_delhivery_connector(self):
        self.assertIsInstance(gateway.get_delhivery_connector(DLV_A), DelhiveryConnector)
        self.assertTrue(gateway.has_delhivery(DLV_A))
        self.assertFalse(gateway.has_shipping(DLV_A))
        with self.assertRaises(TenantConfigurationError):
            gateway.get_connector(DLV_ONLY)  # no store row -> never LocalDB
        self.assertEqual(gateway.shipping_track_order(CustomerIdentity(email=EMAIL), "#1005", DLV_ONLY)["code"], "STORE_NOT_CONFIGURED")

    def test_17_shopify_only_tenant_unchanged(self):
        self.assertFalse(gateway.has_delhivery(STORE_T) or gateway.has_shipping(STORE_T))
        with self.assertRaises(TenantConfigurationError):
            gateway.shipping_track_order(CustomerIdentity(email=EMAIL), "#1005", STORE_T)
        self.assertEqual(self.fake.calls, [])

    def test_18_shippo_tenant_still_uses_shippo(self):
        self.order["shipment"] = {**self.order["shipment"], "courier": "UPS", "tracking_number": "1Z999AA10123456784"}
        result = gateway.shipping_track_order(CustomerIdentity(email=EMAIL), "#1005", SHIPPO_T)
        self.assertEqual((result["tracking_available"], result["tracking"]["provider"]), (True, "shippo"))
        self.assertEqual(len(self.shippo_calls), 1)
        self.assertEqual(self.fake.calls, [])
        self.order["shipment"] = {**self.order["shipment"], "courier": "Delhivery", "tracking_number": AWB}
        no_dlv = gateway.shipping_track_order(CustomerIdentity(email=EMAIL), "#1005", SHIPPO_T)
        self.assertFalse(no_dlv["tracking_available"])
        self.assertIn("Delhivery tracking is not configured", no_dlv["reason"])
        self.assertEqual((len(self.shippo_calls), self.fake.calls), (1, []))


class ShopifyDelhiveryFlowTest(DelhiveryFixture):

    def test_19_shopify_order_to_awb_to_delhivery(self):
        result = gateway.shipping_track_order(CustomerIdentity(email=EMAIL), "#1005", DLV_A)
        ids = result["identifiers"]
        self.assertEqual((result["tracking_available"], ids["order_number"], ids["awb"], ids["carrier_slug"], ids["tracking_provider"]),
                         (True, "#1005", AWB, "delhivery", "DELHIVERY"))
        self.assertEqual(result["tracking"]["status"], "IN_TRANSIT")
        self.assertEqual(self.fake.calls[0]["waybill"], AWB)
        self.order_reads.assert_called_once_with("#1005", DLV_A)
        self.assertTrue(result["ownership_verified"])

    def test_19b_ownership_checked_in_store_not_carrier(self):
        self.order["customer_email"] = "someone.else@example.com"
        self.assertEqual(gateway.shipping_track_order(CustomerIdentity(email=EMAIL), "#1005", DLV_A)["code"], "ORDER_OWNERSHIP_NOT_VERIFIED")
        self.assertEqual(self.fake.calls, [])

    def run_chat(self, script_fn, tenant=DLV_A):
        import server
        logs = io.StringIO()
        with patch.object(type(agent.llm), "invoke", script_fn), patch("sys.__stdout__", logs), redirect_stdout(logs):
            response = TestClient(server.app).post("/api/chat", json={"user_input": "Where is my order #1005?"},
                                                   headers={"X-Company-ID": tenant})
        return response.json()["agent_response"], logs.getvalue()

    def test_19c_api_chat_runtime_shopify_then_delhivery(self):
        def model(self_, messages, config=None, **kwargs):
            bound = [t["function"]["name"] for t in kwargs.get("tools", [])]
            results = [m for m in messages if isinstance(m, ToolMessage)]
            if not results:
                assert "register_tracking" not in bound  # phase 1: no Delhivery writes offered
                name = "get_tracking_status" if "get_tracking_status" in bound else "get_shipment_status"
                return AIMessage(content="", tool_calls=[{"name": name, "args": {"order_id": "#1005", "email": EMAIL}, "id": "c1"}])
            status = (json.loads(results[-1].content).get("tracking") or {}).get("status")
            return AIMessage(content=f"Order #1005 Delhivery status: {status}.")
        answer, logs = self.run_chat(model)
        self.assertIn("Tool: get_tracking_status", logs)
        self.assertIn("[PROVIDER] Provider: DELHIVERY | Operation: GET api/v1/packages/json/", logs)
        self.assertEqual(answer, "Order #1005 Delhivery status: IN_TRANSIT.")
        self.assertNotIn(TOKEN_A, logs)

    def test_20_no_invented_awb(self):
        def model(self_, messages, config=None, **kwargs):
            if not [m for m in messages if isinstance(m, ToolMessage)]:
                return AIMessage(content="", tool_calls=[{"name": "get_tracking_status",
                                                          "args": {"carrier": "Delhivery", "tracking_number": "9876543210987"}, "id": "c1"}])
            return AIMessage(content="Could you share your AWB number?")
        answer, logs = self.run_chat(model)
        self.assertIn("UNVERIFIED_TRACKING_NUMBER", logs)
        self.assertEqual(self.fake.calls, [])

    def test_21_no_invented_tracking_status(self):
        self.fake.queue = [_resp(200, shipment(status="Manifested"))]
        replies = iter(["Your order #1005 is in transit with Delhivery.",
                        "Your order #1005 has been manifested with Delhivery and is awaiting pickup."])

        def model(self_, messages, config=None, **kwargs):
            if not [m for m in messages if isinstance(m, ToolMessage)]:
                return AIMessage(content="", tool_calls=[{"name": "get_tracking_status", "args": {"order_id": "#1005", "email": EMAIL}, "id": "c1"}])
            return AIMessage(content=next(replies))
        answer, logs = self.run_chat(model)
        self.assertIn("[CLAIM GUARD]", logs)
        self.assertNotIn("in transit", answer)
        self.assertIn("manifested", answer)


if __name__ == "__main__":
    unittest.main()
