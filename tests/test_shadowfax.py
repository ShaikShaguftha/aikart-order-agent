"""
Unit and integration tests for Shadowfax Logistics Connector.
Mocked HTTP responses - no real API calls.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import httpx

from integrations.gateway import IntegrationGateway, gateway
from integrations.models import (
    ShadowfaxReversePickup,
    ShadowfaxServiceability,
    ShadowfaxShipment,
    ShadowfaxTrackingEvent,
)
from integrations.providers.shadowfax import ShadowfaxConnector, normalize_base_url
from tools import (
    shadowfax_check_serviceability,
    shadowfax_track_reverse_pickup,
    shadowfax_track_shipment,
    shadowfax_tracking_history,
)


class TestShadowfaxConnector(unittest.TestCase):

    def setUp(self):
        # pytest/Jupyter on Windows can expose an invalid sys.__stdout__ handle.
        # Gateway/provider logging writes to sys.__stdout__, so redirect it to
        # the active valid stdout stream while tests run.
        stdout_patch = patch.object(sys, "__stdout__", sys.stdout)
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)

        self.api_token = "sf_test_token_12345"
        self.client_id = "sf_client_id_67890"
        self.client_secret = "sf_client_secret_abcde"
        self.base_url = "https://api.shadowfax.in"

        self.connector = ShadowfaxConnector(
            api_token=self.api_token,
            client_id=self.client_id,
            client_secret=self.client_secret,
            base_url=self.base_url,
            timeout=2.0,
            retry_backoff=0.001,
        )
    # 1. Connector initialization
    def test_connector_initialization(self):
        connector = ShadowfaxConnector(
            api_token="token123",
            client_id="client123",
            client_secret="secret123",
            base_url="https://api.shadowfax.in",
        )
        self.assertEqual(connector.api_token, "token123")
        self.assertEqual(connector.client_id, "client123")
        self.assertEqual(connector.client_secret, "secret123")
        self.assertEqual(connector.base_url, "https://api.shadowfax.in")
        self.assertEqual(connector.provider_name, "SHADOWFAX")
        self.assertTrue(connector.credentials_configured())
        self.assertIsNone(connector.configuration_error())



    def test_http_client_reused_and_closed(self):
        first_client = self.connector._get_client()
        second_client = self.connector._get_client()

        self.assertIs(first_client, second_client)
        self.assertFalse(first_client.is_closed)

        self.connector.close()

        self.assertIsNone(self.connector._client)
        self.assertTrue(first_client.is_closed)

    # 2. Missing credentials
    def test_missing_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            connector = ShadowfaxConnector(api_token="", client_id="", client_secret="")
            self.assertFalse(connector.credentials_configured())
            err = connector.configuration_error()
            self.assertIsNotNone(err)
            self.assertEqual(err["code"], "MISSING_CREDENTIALS")

    # 3. Authentication / Header generation
    def test_auth_header_generation(self):
        headers = self.connector._auth_headers()
        self.assertEqual(headers.get("Authorization"), f"Token {self.api_token}")
        self.assertEqual(headers.get("X-Client-ID"), self.client_id)
        self.assertEqual(headers.get("X-Client-Secret"), self.client_secret)

    # 4. Track shipment success
    @patch("httpx.Client.request")
    def test_track_shipment_success(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": 200,
            "data": {
                "awb": "SF123456",
                "status": "in_transit",
                "promised_delivery_date": "2026-10-01",
                "pickup_date": "2026-09-27",
                "delivered_date": None,
                "origin": "400001",
                "destination": "560001",
                "tracking_details": [
                    {
                        "timestamp": "2026-09-27T10:00:00Z",
                        "status": "Picked Up",
                        "location": "Mumbai Hub",
                        "description": "Shipment picked up from merchant",
                    },
                    {
                        "timestamp": "2026-09-28T08:00:00Z",
                        "status": "In Transit",
                        "location": "Pune Hub",
                        "description": "Shipment in transit to destination hub",
                    },
                ],
            },
        }
        mock_request.return_value = mock_resp

        result = self.connector.track_shipment("SF123456")
        self.assertNotIn("error", result)
        self.assertEqual(result["awb"], "SF123456")
        self.assertEqual(result["status"], "IN_TRANSIT")
        self.assertEqual(result["courier"], "SHADOWFAX")
        self.assertEqual(result["estimated_delivery"], "2026-10-01")
        self.assertEqual(result["pickup_date"], "2026-09-27")
        self.assertEqual(result["origin"], "400001")
        self.assertEqual(result["destination"], "560001")
        self.assertIsNotNone(result["latest_event"])
        self.assertEqual(result["latest_event"]["status"], "In Transit")
        self.assertEqual(len(result["tracking_history"]), 2)

    # 5. Shipment not found (404 JSON)
    @patch("httpx.Client.request")
    def test_shipment_not_found(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"error": "Order not found", "code": "NOT_FOUND"}
        mock_request.return_value = mock_resp

        result = self.connector.track_shipment("SF999999")
        self.assertIn("error", result)
        self.assertEqual(result["code"], "NOT_FOUND")

    # 6. Tracking history success
    @patch("httpx.Client.request")
    def test_tracking_history_success(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "awb": "SF123456",
                "status": "delivered",
                "delivered_date": "2026-09-28",
                "tracking_details": [
                    {
                        "timestamp": "2026-09-28T12:00:00Z",
                        "status": "Delivered",
                        "location": "Bangalore",
                        "description": "Package delivered to customer",
                    }
                ],
            }
        }
        mock_request.return_value = mock_resp

        history = self.connector.get_tracking_history("SF123456")
        self.assertNotIn("error", history)
        self.assertEqual(history["awb"], "SF123456")
        self.assertEqual(len(history["tracking_history"]), 1)
        self.assertEqual(history["tracking_history"][0]["status"], "Delivered")
        self.assertEqual(history["tracking_history"][0]["location"], "Bangalore")

    # 7. Serviceability success
    @patch("httpx.Client.request")
    def test_serviceability_success(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Documented v1 response: a list of {"code": <pincode>, "services": [...]} per request.
        pickup = MagicMock(status_code=200)
        pickup.json.return_value = [{"code": 400001, "services": ["Regular"]}]
        delivery = MagicMock(status_code=200)
        delivery.json.return_value = [{"code": 560001, "services": ["Regular", "Surface"]}]
        mock_request.side_effect = [pickup, delivery]

        result = self.connector.check_serviceability("400001", "560001")
        self.assertNotIn("error", result)
        self.assertEqual(result["pickup_pincode"], "400001")
        self.assertEqual(result["delivery_pincode"], "560001")
        self.assertTrue(result["serviceable"])
        self.assertEqual(result["service_info"]["pickup"],
                         {"pincode": "400001", "service": "seller_pickup", "serviceable": True, "service_types": ["Regular"]})
        self.assertEqual(result["service_info"]["delivery"]["service_types"], ["Regular", "Surface"])

    # 8. Serviceability false
    @patch("httpx.Client.request")
    def test_serviceability_false(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # A pincode absent from the documented list response is not serviceable for that service.
        pickup = MagicMock(status_code=200)
        pickup.json.return_value = [{"code": 400001, "services": ["Regular"]}]
        delivery = MagicMock(status_code=200)
        delivery.json.return_value = []
        mock_request.side_effect = [pickup, delivery]

        result = self.connector.check_serviceability("400001", "999999")
        self.assertNotIn("error", result)
        self.assertFalse(result["serviceable"])
        self.assertTrue(result["service_info"]["pickup"]["serviceable"])
        self.assertFalse(result["service_info"]["delivery"]["serviceable"])

    # 9. Reverse pickup success
    @patch("httpx.Client.request")
    def test_reverse_pickup_success(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": {
                "reverse_awb": "SF-REV-1001",
                "pickup_status": "picked_up",
                "scheduled_date": "2026-09-27",
                "pickup_date": "2026-09-28",
                "tracking_details": [
                    {
                        "timestamp": "2026-09-28T11:00:00Z",
                        "status": "Picked Up",
                        "location": "Customer Residence",
                        "description": "Item picked up from customer",
                    }
                ],
            }
        }
        mock_request.return_value = mock_resp

        result = self.connector.track_reverse_pickup("SF-REV-1001")
        self.assertNotIn("error", result)
        self.assertEqual(result["reverse_awb"], "SF-REV-1001")
        self.assertEqual(result["pickup_status"], "IN_TRANSIT")
        self.assertEqual(result["pickup_date"], "2026-09-28")
        self.assertEqual(len(result["tracking_history"]), 1)

    # 10. Provider 401
    @patch("httpx.Client.request")
    def test_provider_401(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"error": "Unauthorized"}
        mock_request.return_value = mock_resp

        result = self.connector.track_shipment("SF123456")
        self.assertIn("error", result)
        self.assertEqual(result["code"], "PROVIDER_AUTH_FAILED")
        self.assertTrue(result.get("reconnect_required"))


    @patch("httpx.Client.request")
    def test_provider_403(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.headers = {}
        mock_resp.json.return_value = {
            "error": "Permission denied",
        }

        mock_request.return_value = mock_resp

        result = self.connector.track_shipment("SF123456")

        self.assertIn("error", result)
        self.assertEqual(result["code"], "PROVIDER_FORBIDDEN")
        self.assertEqual(result["http_status"], 403)

    # 11. Provider 404 (non-JSON body / API route missing)
    @patch("httpx.Client.request")
    def test_provider_404_non_json(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.side_effect = ValueError("Not JSON")
        mock_request.return_value = mock_resp

        result = self.connector.track_shipment("SF123456")
        self.assertIn("error", result)
        self.assertEqual(result["code"], "PROVIDER_API_UNAVAILABLE")

    # 12. Provider 429 (Rate limited)
    @patch("httpx.Client.request")
    def test_provider_429(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"Retry-After": "1"}
        mock_request.return_value = mock_resp

        result = self.connector.track_shipment("SF123456")
        self.assertIn("error", result)
        self.assertEqual(result["code"], "RATE_LIMITED")



    @patch("integrations.providers.shadowfax.time.sleep")
    @patch("httpx.Client.request")
    def test_429_retries_then_succeeds(self, mock_request, mock_sleep):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {
            "Retry-After": "0",
        }
        rate_limited.json.return_value = {
            "message": "Too many requests",
        }

        success = MagicMock()
        success.status_code = 200
        success.headers = {}
        success.json.return_value = {
            "order_details": {
                "awb_number": "SF123456",
                "status": "in_transit",
            },
            "tracking_details": [],
        }

        mock_request.side_effect = [
            rate_limited,
            success,
        ]

        result = self.connector.track_shipment("SF123456")

        self.assertEqual(result["awb"], "SF123456")
        self.assertEqual(result["status"], "IN_TRANSIT")
        self.assertEqual(mock_request.call_count, 2)
        mock_sleep.assert_called_once_with(0.0)

    # 13. Provider 500 (Server error)
    @patch("httpx.Client.request")
    def test_provider_500(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_request.return_value = mock_resp

        result = self.connector.track_shipment("SF123456")
        self.assertIn("error", result)
        self.assertEqual(result["code"], "PROVIDER_UNAVAILABLE")

    # 14. Timeout
    @patch("httpx.Client.request")
    def test_provider_timeout(self, mock_request):
        mock_request.side_effect = httpx.TimeoutException("Connection timed out")

        result = self.connector.track_shipment("SF123456")
        self.assertIn("error", result)
        self.assertEqual(result["code"], "PROVIDER_TIMEOUT")

    # 15. Malformed response
    @patch("httpx.Client.request")
    def test_malformed_response(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("Invalid JSON body")
        mock_request.return_value = mock_resp

        result = self.connector.track_shipment("SF123456")
        self.assertIn("error", result)
        self.assertEqual(result["code"], "PROVIDER_INVALID_RESPONSE")


    @patch("httpx.Client.request")
    def test_empty_200_response_is_invalid(self, mock_request):
        empty_response = MagicMock()
        empty_response.status_code = 200
        empty_response.headers = {}
        empty_response.json.side_effect = ValueError("empty response body")

        mock_request.return_value = empty_response

        result = self.connector.track_shipment("SF123456")

        self.assertEqual(
            result["code"],
            "PROVIDER_INVALID_RESPONSE",
        )

        self.assertEqual(
            result["http_status"],
            200,
        )


    @patch("httpx.Client.request")
    def test_large_tracking_history_and_text_are_truncated(self, mock_request):
        long_description = "X" * 2000

        tracking_details = [
            {
                "created": f"2026-09-{(index % 28) + 1:02d}T10:00:00Z",
                "status": "in_transit",
                "location": "Transit Hub",
                "remarks": long_description,
            }
            for index in range(150)
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {
            "order_details": {
                "awb_number": "SF123456",
                "status": "in_transit",
            },
            "tracking_details": tracking_details,
        }

        mock_request.return_value = mock_response

        result = self.connector.track_shipment("SF123456")

        self.assertEqual(
            len(result["tracking_history"]),
            100,
        )

        first_event = result["tracking_history"][0]

        self.assertLessEqual(
            len(first_event["description"]),
            550,
        )

        self.assertIn(
            "[TRUNCATED FOR TOKEN LIMITS]",
            first_event["description"],
        )

    # 16. Gateway routing
    def test_gateway_routing(self):
        gw = IntegrationGateway()
        self.assertTrue(gw.has_shadowfax("COMP-SHADOWFAX"))
        with patch.dict(os.environ, {"SHADOWFAX_API_TOKEN": "token_test_123"}):
            conn = gw.get_shadowfax_connector("COMP-SHADOWFAX")
            self.assertIsInstance(conn, ShadowfaxConnector)
            self.assertEqual(conn.provider_name, "SHADOWFAX")

    # 17. Tenant isolation
    def test_tenant_isolation(self):
        gw = IntegrationGateway()
        # COMP-SHOPIFY should not register as a Shadowfax tenant
        with patch.dict(os.environ, {"SHADOWFAX_ENV_TENANT": "COMP-SHADOWFAX"}):
            self.assertFalse(gw.has_shadowfax("COMP-SHOPIFY"))

    # 18. Tool delegation
    @patch("httpx.Client.request")
    def test_tool_delegation(self, mock_request):
        tracking = {"data": {"awb": "SF123456", "status": "delivered", "reverse_awb": "REV-123", "pickup_status": "delivered"}}

        def respond(method=None, url=None, params=None, **_):
            resp = MagicMock(status_code=200)
            if "/v1/clients/serviceability/" in url:
                resp.json.return_value = [{"code": int(params["pincodes"]), "services": ["Regular"]}]
            else:
                resp.json.return_value = tracking
            return resp

        mock_request.side_effect = respond

        with patch.dict(os.environ, {"SHADOWFAX_API_TOKEN": "token_test_123"}):
            res_track = shadowfax_track_shipment.invoke({"awb": "SF123456", "company_id": "COMP-SHADOWFAX"})
            self.assertIn("SF123456", res_track)

            res_history = shadowfax_tracking_history.invoke({"awb": "SF123456", "company_id": "COMP-SHADOWFAX"})
            self.assertIn("SF123456", res_history)

            res_svc = shadowfax_check_serviceability.invoke({"pickup_pincode": "400001", "delivery_pincode": "560001", "company_id": "COMP-SHADOWFAX"})
            self.assertIn("serviceable", res_svc)

            res_rev = shadowfax_track_reverse_pickup.invoke({"reverse_awb": "REV-123", "company_id": "COMP-SHADOWFAX"})
            self.assertIn("REV-123", res_rev)

    # 19. Normalized Pydantic models
    def test_normalized_pydantic_models(self):
        event = ShadowfaxTrackingEvent(
            timestamp="2026-09-28T10:00:00Z",
            status="In Transit",
            location="Hub Delhi",
            description="Arrived at hub",
        )
        shipment = ShadowfaxShipment(
            awb="SF1001",
            status="IN_TRANSIT",
            courier="SHADOWFAX",
            estimated_delivery="2026-09-30",
            latest_event=event,
            tracking_history=[event],
        )
        svc = ShadowfaxServiceability(
            pickup_pincode="110001",
            delivery_pincode="400001",
            serviceable=True,
            service_info={"express": True},
        )
        pickup = ShadowfaxReversePickup(
            reverse_awb="REV1001",
            pickup_status="IN_TRANSIT",
            scheduled_date="2026-09-28",
            latest_event=event,
            tracking_history=[event],
        )

        self.assertEqual(shipment.awb, "SF1001")
        self.assertEqual(shipment.courier, "SHADOWFAX")
        self.assertEqual(svc.pickup_pincode, "110001")
        self.assertEqual(pickup.reverse_awb, "REV1001")

    # 20. No secret leakage
    def test_no_secret_leakage(self):
        secret_token = "secret_token_super_private_99"
        secret_key = "secret_client_key_777"
        connector = ShadowfaxConnector(api_token=secret_token, client_id="cid", client_secret=secret_key)
        
        sanitized = connector._sanitize_error(f"Error connecting with token {secret_token} and secret {secret_key}")
        self.assertNotIn(secret_token, sanitized)
        self.assertNotIn(secret_key, sanitized)
        self.assertIn("[REDACTED_TOKEN]", sanitized)
        self.assertIn("[REDACTED_SECRET]", sanitized)


class TestShadowfaxDocumentedContract(unittest.TestCase):
    """Request construction and parsing pinned to the official Shadowfax Unified API."""

    STAGING = "https://dale.staging.shadowfax.in/api"

    def setUp(self):
        # pytest/Jupyter on Windows can expose an invalid sys.__stdout__ handle.
        # Provider logging writes to sys.__stdout__, so redirect it to the
        # active valid stdout stream during contract tests.
        stdout_patch = patch.object(sys, "__stdout__", sys.stdout)
        stdout_patch.start()
        self.addCleanup(stdout_patch.stop)

        self.connector = ShadowfaxConnector(
            api_token="sf_contract_token",
            base_url=self.STAGING,
            timeout=2.0,
            retry_backoff=0.001,
        )

        sleep = patch(
            "integrations.providers.shadowfax.time.sleep",
        )
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_base_url_keeps_api_path_and_never_duplicates_it(self):
        self.assertEqual(normalize_base_url(self.STAGING), self.STAGING)
        self.assertEqual(normalize_base_url(self.STAGING + "/"), self.STAGING)
        self.assertEqual(normalize_base_url("https://dale.shadowfax.in/api"), "https://dale.shadowfax.in/api")
        self.assertIsNone(normalize_base_url("http://dale.shadowfax.in/api"))

    @patch("httpx.Client.request")
    def test_serviceability_request_construction(self, mock_request):
        pickup = MagicMock(status_code=200)
        pickup.json.return_value = [{"code": 400001, "services": ["Regular"]}]
        delivery = MagicMock(status_code=200)
        delivery.json.return_value = [{"code": 560001, "services": ["Regular"]}]
        mock_request.side_effect = [pickup, delivery]

        self.connector.check_serviceability("400001", "560001")

        calls = [call.kwargs for call in mock_request.call_args_list]

        self.assertEqual(
            [call["url"] for call in calls],
            [f"{self.STAGING}/v1/clients/serviceability/"] * 2,
        )

        self.assertNotIn(
            "/api/api",
            calls[0]["url"],
        )

        self.assertEqual(
            [call["method"] for call in calls],
            ["GET", "GET"],
        )

        self.assertEqual(
            calls[0]["params"],
            {
                "service": "seller_pickup",
                "pincodes": "400001",
                "page": 1,
                "count": 10,
            },
        )

        self.assertEqual(
            calls[1]["params"],
            {
                "service": "customer_delivery",
                "pincodes": "560001",
                "page": 1,
                "count": 10,
            },
        )

        client_headers = self.connector._get_client().headers

        self.assertEqual(
            client_headers["Authorization"],
            "Token sf_contract_token",
        )

    @patch("httpx.Client.request")
    def test_pickup_service_is_configurable_and_validated(self, mock_request):
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"code": 400001, "services": ["Regular"]}]
        mock_request.return_value = resp
        with patch.dict(os.environ, {"SHADOWFAX_PICKUP_SERVICE": "warehouse_pickup"}):
            self.connector.check_serviceability("400001", "400001")
        self.assertEqual(mock_request.call_args_list[0].kwargs["params"]["service"], "warehouse_pickup")
        self.assertEqual(self.connector.check_serviceability("400001", "560001", pickup_service="teleport")["code"], "INVALID_ARGUMENT")

    @patch("httpx.Client.request")
    def test_serviceability_unexpected_shape_is_invalid_response(self, mock_request):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"message": "something else"}
        mock_request.return_value = resp
        self.assertEqual(self.connector.check_serviceability("400001", "560001")["code"], "PROVIDER_INVALID_RESPONSE")

    @patch("httpx.Client.request")
    def test_tracking_uses_documented_v4_endpoint_and_fields(self, mock_request):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "message": "Success",
            "order_details": {
                "awb_number": "SF222344412TST", "client_order_id": "11002", "order_date": "2022-02-28",
                "promised_delivery_date": None, "status_display": "Out For Delivery", "status": "ofd",
                "pickup_details": {"name": "HSR Telecom", "contact": "9999999999", "address_line_1": "8th main",
                                   "city": "Bengaluru", "state": "Karnataka", "pincode": 560102},
                "delivery_details": {"name": "Deepak", "contact": "1234567890", "address_line_1": "Flat 38",
                                     "city": "New Delhi", "state": "Delhi", "pincode": 110085},
            },
            "tracking_details": [
                {"created": "2022-02-28T09:00:15Z", "location": "SFX TestHub", "status_id": "recd_at_rev_hub",
                 "status": "Received at Reverse Hub", "remarks": "Received at hub Successfully", "awb_number": "SF222344412TST"},
                {"created": "2022-02-28T09:03:54Z", "location": "TestHub2", "status_id": "ofd",
                 "status": "Out For Delivery", "remarks": "Item OFD at TestHub2", "awb_number": "SF222344412TST"},
            ],
        }
        mock_request.return_value = resp

        result = self.connector.track_shipment("SF222344412TST")

        self.assertEqual(mock_request.call_args.kwargs["url"], f"{self.STAGING}/v4/clients/orders/SF222344412TST/track/")
        self.assertEqual((result["awb"], result["status"]), ("SF222344412TST", "OUT_FOR_DELIVERY"))
        self.assertIsNone(result["estimated_delivery"])  # promised_delivery_date is null: not invented
        self.assertIsNone(result["delivered_date"])
        self.assertEqual(result["pickup_date"], "2022-02-28T09:00:15Z")
        self.assertEqual((result["origin"], result["destination"]), ("Bengaluru, Karnataka, 560102", "New Delhi, Delhi, 110085"))
        self.assertEqual(result["latest_event"], {"timestamp": "2022-02-28T09:03:54Z", "status": "Out For Delivery",
                                                  "location": "TestHub2", "description": "Item OFD at TestHub2"})
        blob = str(result)
        for pii in ("HSR Telecom", "9999999999", "Deepak", "1234567890", "Flat 38", "8th main"):
            self.assertNotIn(pii, blob)

    @patch("httpx.Client.request")
    def test_documented_invalid_awb_400_is_not_found(self, mock_request):
        resp = MagicMock(status_code=400)
        resp.json.return_value = {"message": "Invalid AWB number"}
        mock_request.return_value = resp
        self.assertEqual(self.connector.track_shipment("SF000000000TST")["code"], "NOT_FOUND")
        resp.json.return_value = {"message": "Invalid Request"}
        self.assertEqual(self.connector.check_serviceability("400001", "560001")["code"], "PROVIDER_VALIDATION_FAILED")

    @patch("httpx.Client.request")
    def test_reverse_pickup_uses_documented_tracking_endpoint(self, mock_request):
        resp = MagicMock(status_code=200)
        resp.json.return_value = {"message": "Success", "order_details": {"awb_number": "SFREV123", "status": "recd_at_rev_hub"},
                                  "tracking_details": []}
        mock_request.return_value = resp
        result = self.connector.track_reverse_pickup("SFREV123")
        self.assertEqual(mock_request.call_args.kwargs["url"], f"{self.STAGING}/v4/clients/orders/SFREV123/track/")
        self.assertEqual((result["reverse_awb"], result["pickup_status"]), ("SFREV123", "IN_TRANSIT"))


if __name__ == "__main__":
    unittest.main()


