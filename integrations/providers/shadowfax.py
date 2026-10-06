"""
Shadowfax logistics connector - PHASE 1: read-only tracking & serviceability.

Supported capabilities (Read-Only):
1. track_shipment(awb)
2. get_tracking_history(awb)
3. check_serviceability(pickup_pincode, delivery_pincode)
4. track_reverse_pickup(reverse_awb)

Auth precedence:
- Credentials passed explicitly at construction or in carrier_integrations
- Environment variables: SHADOWFAX_API_TOKEN, SHADOWFAX_CLIENT_ID, SHADOWFAX_CLIENT_SECRET
- Base URL configurable via SHADOWFAX_API_BASE_URL (default https://dale.shadowfax.in/api)

Endpoints (official Shadowfax Unified API, sfxunifiedapi.docs.apiary.io; auth "Authorization: Token <key>"):
- Base URLs: staging https://dale.staging.shadowfax.in/api, production https://dale.shadowfax.in/api
- Tracking (v4):      GET {base}/v4/clients/orders/{awb_number}/track/
- Serviceability (v1): GET {base}/v1/clients/serviceability/?service=&pincodes=&page=&count=
Endpoint paths are relative to the base URL (which already ends in /api), so never "/api/api".

Output uses normalized Pydantic models (ShadowfaxShipment, ShadowfaxTrackingEvent,
ShadowfaxServiceability, ShadowfaxReversePickup) converted to dicts.
Raw provider-specific structures are never exposed directly to the agent.
Missing values are set to None - never invented.
"""
import logging
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx

from integrations.base import log_provider_call
from integrations.models import (
    ShadowfaxReversePickup,
    ShadowfaxServiceability,
    ShadowfaxShipment,
    ShadowfaxTrackingEvent,
)

logger = logging.getLogger("uvicorn.error")

DEFAULT_BASE_URL = "https://dale.shadowfax.in/api"  # documented production base
TRACK_PATH = "v4/clients/orders/{awb}/track/"
SERVICEABILITY_PATH = "v1/clients/serviceability/"
# Documented serviceability "service" values; the pickup side depends on the merchant model
# (marketplace seller pickup vs warehouse pickup) and is configurable.
PICKUP_SERVICES = {"seller_pickup", "warehouse_pickup", "customer_pickup"}
DEFAULT_PICKUP_SERVICE = "seller_pickup"
DELIVERY_SERVICE = "customer_delivery"
DEFAULT_AUTH_HEADER = "Authorization"
DEFAULT_AUTH_SCHEME = "Token"
AWB_PATTERN = re.compile(r"^[A-Za-z0-9-]{4,64}$")
PINCODE_PATTERN = re.compile(r"^\d{6}$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Status text mapping -> unified status
STATUS_MAP = {
    "manifested": "PRE_TRANSIT",
    "pending": "PRE_TRANSIT",
    "created": "PRE_TRANSIT",
    "pickup_pending": "PRE_TRANSIT",
    "ready_for_pickup": "PRE_TRANSIT",
    "out_for_pickup": "PRE_TRANSIT",
    "pickup_scheduled": "PRE_TRANSIT",
    "picked_up": "IN_TRANSIT",
    "in_transit": "IN_TRANSIT",
    "in transit": "IN_TRANSIT",
    "hub_arrived": "IN_TRANSIT",
    "hub_dispatched": "IN_TRANSIT",
    "reach_destination_hub": "IN_TRANSIT",
    "out_for_delivery": "OUT_FOR_DELIVERY",
    "out for delivery": "OUT_FOR_DELIVERY",
    "delivered": "DELIVERED",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
    "rto": "RETURNED",
    "rto_in_transit": "RETURNED",
    "rto_delivered": "RETURNED",
    "returned": "RETURNED",
    "undelivered": "EXCEPTION",
    "failed": "EXCEPTION",
    "delivery_failed": "EXCEPTION",
    "lost": "EXCEPTION",
    "damaged": "EXCEPTION",
    "ndr": "EXCEPTION",
    # Documented Shadowfax v4 status_id values
    "new": "PRE_TRANSIT",
    "assigned_for_seller_pickup": "PRE_TRANSIT",
    "ofp": "PRE_TRANSIT",
    "picked": "IN_TRANSIT",
    "recd_at_rev_hub": "IN_TRANSIT",
    "received_from_client_warehouse": "IN_TRANSIT",
    "item_manifested": "IN_TRANSIT",
    "bag_in_transit": "IN_TRANSIT",
    "bag_received": "IN_TRANSIT",
    "bag_received_at_via": "IN_TRANSIT",
    "recd_at_fwd_hub": "IN_TRANSIT",
    "recd_at_fwd_dc": "IN_TRANSIT",
    "assigned_for_delivery": "IN_TRANSIT",
    "pincode_updated": "IN_TRANSIT",
    "ofd": "OUT_FOR_DELIVERY",
    "cancelled_by_seller": "CANCELLED",
    "cancelled_by_customer": "CANCELLED",
    "cid": "EXCEPTION",
    "seller_initiated_delay": "EXCEPTION",
    "seller_not_contactable": "EXCEPTION",
    "nc": "EXCEPTION",
    "pickup_not_attempted": "EXCEPTION",
    "na": "EXCEPTION",
    "on_hold": "EXCEPTION",
    "pickup_on_hold": "EXCEPTION",
    "reopen_ndr": "EXCEPTION",
    "item_misrouted": "EXCEPTION",
    "rts": "RETURNED",
    "rts_in_process": "RETURNED",
    "rts_ofd": "RETURNED",
    "rts_d": "RETURNED",
    "rts_nd": "EXCEPTION",
    "in_transit_return": "RETURNED",
    "recd_at_dc_rts": "RETURNED",
    "rto_d": "RETURNED",
}
PICKUP_DONE_STATUS_IDS = ("picked", "recd_at_rev_hub", "received_from_client_warehouse")


def valid_awb(awb: Optional[str]) -> bool:
    return bool(awb) and bool(AWB_PATTERN.match(str(awb).strip()))


def valid_pincode(pincode: Optional[str]) -> bool:
    return bool(pincode) and bool(PINCODE_PATTERN.match(str(pincode).strip()))


def normalize_base_url(base_url: Optional[str]) -> Optional[str]:
    """
    HTTPS only (http allowed for localhost) to protect the token. The base URL's PATH is
    kept: Shadowfax's documented base is ".../api", and endpoint paths are relative to it.
    """
    value = (base_url or os.getenv("SHADOWFAX_API_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host in LOCAL_HOSTS:
        return value
    if parsed.scheme != "https" or not host or parsed.query or parsed.fragment:
        return None
    return f"https://{host}" + (f":{parsed.port}" if parsed.port else "") + parsed.path.rstrip("/")


def build_url(base_url: str, path: str) -> str:
    """Joins the base URL and a relative endpoint path with exactly one slash."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


class ShadowfaxConnector:
    provider_name = "SHADOWFAX"
    cancel_refunds_automatically = False
    managed_returns = False
    managed_tickets = False

    MAX_RETRY_AFTER_SECONDS = 5.0
    MAX_BACKOFF_SECONDS = 8.0
    MAX_TRACKING_EVENTS = 100
    MAX_EVENT_TEXT_CHARS = 500

    ERROR_MESSAGES = {
        "MISSING_CREDENTIALS": "Shadowfax API credentials missing. SHADOWFAX_API_TOKEN or SHADOWFAX_CLIENT_ID/SHADOWFAX_CLIENT_SECRET is required.",
        "PROVIDER_CONFIG_INSECURE": "Shadowfax base URL must be an https:// address.",
        "PROVIDER_AUTH_FAILED": "Shadowfax rejected the API credentials. Please check your API token or client credentials.",
        "PROVIDER_FORBIDDEN": "Shadowfax refused access to this resource.",
        "NOT_FOUND": "Shadowfax could not find a shipment or resource with that identifier.",
        "PROVIDER_API_UNAVAILABLE": "The Shadowfax API endpoint was not found at the configured base URL.",
        "RATE_LIMITED": "Shadowfax is rate limiting requests right now. Please try again shortly.",
        "PROVIDER_UNAVAILABLE": "Shadowfax is temporarily unavailable.",
        "PROVIDER_TIMEOUT": "Shadowfax did not respond in time.",
        "PROVIDER_UNREACHABLE": "Shadowfax could not be reached.",
        "PROVIDER_INVALID_RESPONSE": "Shadowfax returned an unexpected response format.",
        "PROVIDER_VALIDATION_FAILED": "Shadowfax rejected the request as invalid.",
        "PROVIDER_ERROR": "Shadowfax returned an error.",
    }

    def __init__(
        self,
        api_token: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        environment: Optional[str] = None,
        auth_header: Optional[str] = None,
        auth_scheme: Optional[str] = None,
        timeout: float = 10.0,
        retry_backoff: float = 0.5,
    ):
        self.api_token = (api_token or os.getenv("SHADOWFAX_API_TOKEN") or "").strip()
        self.client_id = (client_id or os.getenv("SHADOWFAX_CLIENT_ID") or "").strip()
        self.client_secret = (client_secret or os.getenv("SHADOWFAX_CLIENT_SECRET") or "").strip()
        self.base_url = normalize_base_url(base_url)
        self.environment = (environment or "production").strip().lower()
        self.auth_header = (auth_header or DEFAULT_AUTH_HEADER).strip()
        self.auth_scheme = DEFAULT_AUTH_SCHEME if auth_scheme is None else auth_scheme.strip()
        self.timeout = timeout
        self.retry_backoff = retry_backoff
        self.credential_source = "ENV"
        self._client: Optional[httpx.Client] = None

    def credentials_configured(self) -> bool:
        return bool(self.api_token or (self.client_id and self.client_secret))

    def configuration_error(self) -> Optional[Dict[str, str]]:
        if not self.credentials_configured():
            return {"error": self.ERROR_MESSAGES["MISSING_CREDENTIALS"], "code": "MISSING_CREDENTIALS"}
        if not self.base_url:
            return {"error": self.ERROR_MESSAGES["PROVIDER_CONFIG_INSECURE"], "code": "PROVIDER_CONFIG_INSECURE"}
        return None

    def _auth_headers(self) -> Dict[str, str]:
        """Construct secure authorization headers without exposing them in logs."""
        headers = {}
        if self.api_token:
            value = f"{self.auth_scheme} {self.api_token}" if self.auth_scheme else self.api_token
            headers[self.auth_header] = value
        if self.client_id:
            headers["X-Client-ID"] = self.client_id
        if self.client_secret:
            headers["X-Client-Secret"] = self.client_secret
        return headers


    def _get_client(self) -> httpx.Client:
        """Create and reuse one HTTP connection pool for this connector instance."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                headers={
                    **self._auth_headers(),
                    "Accept": "application/json",
                    "User-Agent": "Luintix-Shadowfax-Connector/1.0",
                },
            )
        return self._client

    def close(self) -> None:
        """Close the reusable HTTP connection pool."""
        if self._client is not None and not self._client.is_closed:
            self._client.close()
        self._client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _sanitize_error(self, message: Any) -> str:
        """Redact tokens and client secrets from diagnostic messages."""
        text = str(message or "")
        if self.api_token:
            text = text.replace(self.api_token, "[REDACTED_TOKEN]")
        if self.client_secret:
            text = text.replace(self.client_secret, "[REDACTED_SECRET]")
        if self.client_id:
            text = text.replace(self.client_id, "[REDACTED_CLIENT_ID]")
        return text

    def _error(self, code: str, status: Any = None, **extra) -> Dict[str, Any]:
        msg = self.ERROR_MESSAGES.get(code, "Shadowfax returned an error.")
        err = {"error": msg, "code": code, **extra}
        if code == "PROVIDER_AUTH_FAILED":
            err["reconnect_required"] = True
        if status is not None:
            err["http_status"] = status
        return err

    def _backoff(self, attempt: int, response: Optional[httpx.Response]) -> float:
        if response is not None and response.headers.get("Retry-After"):
            try:
                return min(float(response.headers["Retry-After"]), self.MAX_RETRY_AFTER_SECONDS)
            except (TypeError, ValueError):
                pass
        base = self.retry_backoff * (2 ** (attempt - 1))
        return min(base + random.uniform(0, self.retry_backoff), self.MAX_BACKOFF_SECONDS)

    def _get(self, path: str, params: Dict[str, Any], max_retries: int = 3) -> Any:
        config_error = self.configuration_error()
        if config_error:
            return config_error

        last_error: Dict[str, Any] = self._error("PROVIDER_ERROR")
        for attempt in range(1, max_retries + 1):
            response = None
            try:
                url = build_url(self.base_url, path)
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    url = build_url(self.base_url, path)
                    client = self._get_client()
                    
                    response = client.request(
                        method="GET",
                        url=url,
                        params=params,
                    )
                    
                status = response.status_code
                log_provider_call(self.provider_name, f"GET {path}", status)
                if status == 200:
                    try:
                        return response.json()
                    except ValueError:
                        return self._error("PROVIDER_INVALID_RESPONSE", status)
                if status == 400:
                    try:
                        message = str((response.json() or {}).get("message") or "")
                    except (ValueError, AttributeError):
                        message = ""
                    # Documented tracking error: 400 {"message": "Invalid AWB number"}.
                    if "invalid awb" in message.casefold():
                        return self._error("NOT_FOUND", status)
                    return self._error("PROVIDER_VALIDATION_FAILED", status)
                if status == 401:
                    return self._error("PROVIDER_AUTH_FAILED", status)
                if status == 403:
                    return self._error("PROVIDER_FORBIDDEN", status)
                if status == 404:
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    return self._error("NOT_FOUND" if isinstance(body, dict) else "PROVIDER_API_UNAVAILABLE", status)
                if status == 429:
                    last_error = self._error("RATE_LIMITED", status)
                elif status >= 500:
                    last_error = self._error("PROVIDER_UNAVAILABLE", status)
                else:
                    return self._error("PROVIDER_ERROR", status)
            except httpx.TimeoutException:
                last_error = self._error("PROVIDER_TIMEOUT")
            except httpx.TransportError:
                last_error = self._error("PROVIDER_UNREACHABLE")
            except Exception as e:
                logger.warning(f"[SHADOWFAX] request failed: {self._sanitize_error(type(e).__name__)}")
                return self._error("PROVIDER_ERROR")
            if attempt < max_retries:
                time.sleep(self._backoff(attempt, response))
        return last_error

    @staticmethod
    def map_status(provider_status: Optional[str]) -> str:
        """Map raw provider status string to unified domain status."""
        return STATUS_MAP.get(str(provider_status or "").strip().casefold(), "UNKNOWN")

    @classmethod
    def _parse_event(cls, raw: Dict[str, Any]) -> ShadowfaxTrackingEvent:
        # Documented v4 event: {"created", "location", "status_id", "status", "remarks", "awb_number"}
        status_text = raw.get("status") or raw.get("event_name") or raw.get("scan") or raw.get("status_id") or "UNKNOWN"
        return ShadowfaxTrackingEvent(
            timestamp=raw.get("created") or raw.get("timestamp") or raw.get("time") or raw.get("date") or raw.get("scan_date_time"),
            status=str(status_text),
            location=raw.get("location") or raw.get("city") or raw.get("hub") or raw.get("scanned_location"),
            description=cls._truncate_text(
                raw.get("remarks")
                or raw.get("description")
                or raw.get("message")
                or raw.get("remark")
                or raw.get("instructions"),
                cls.MAX_EVENT_TEXT_CHARS,
            ),
        )

    @staticmethod
    def _place(details: Any) -> Optional[str]:
        """City/state/pincode only - names, phone numbers and street addresses are never returned."""
        if not isinstance(details, dict):
            return None
        parts = [str(details[k]) for k in ("city", "state", "pincode") if details.get(k) not in (None, "")]
        return ", ".join(parts) or None

    @staticmethod
    def _first_event_time(scans: List[Dict[str, Any]], status_ids) -> Optional[str]:
        for scan in scans:
            if isinstance(scan, dict) and scan.get("status_id") in status_ids:
                return scan.get("created")
        return None

    @staticmethod
    def _truncate_text(value: Any, max_chars: int = 500) -> Optional[str]:
        """Limit large provider text fields before returning them to the agent."""
        if value in (None, ""):
            return None

        text = str(value).strip()

        if len(text) > max_chars:
            return text[:max_chars] + "\n...[TRUNCATED FOR TOKEN LIMITS]"

        return text

    def _tracking_payload(self, awb: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """GET the documented v4 tracking endpoint -> (order/data dict, scans list) or error."""
        raw = self._get(TRACK_PATH.format(awb=quote(awb.strip(), safe="")), {})
        if isinstance(raw, dict) and "error" in raw and "code" in raw:
            return None, raw
        if not isinstance(raw, dict):
            return None, self._error("PROVIDER_INVALID_RESPONSE")
        if isinstance(raw.get("order_details"), dict):  # documented v4 shape
            return raw["order_details"], raw.get("tracking_details") if isinstance(raw.get("tracking_details"), list) else []
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        scans = data.get("tracking_details") or data.get("scans") or data.get("tracking_events") or data.get("history") or []
        return data, scans if isinstance(scans, list) else []

    # ---- Capability Methods (Read-Only) ---------------------------------------------

    def track_shipment(self, awb: str) -> Dict[str, Any]:
        """
        Track a shipment on Shadowfax using AWB or shipment identifier.
        Returns normalized ShadowfaxShipment dict or error dict.
        """
        if not valid_awb(awb):
            return {"error": f"Invalid AWB identifier '{awb}'.", "code": "INVALID_ARGUMENT"}

        data, scans = self._tracking_payload(awb)
        if data is None:
            return scans  # error dict

        raw_status = data.get("status") or data.get("current_status") or "UNKNOWN"
        norm_status = self.map_status(raw_status)

        tracking_history = [
            self._parse_event(scan)
            for scan in scans[-self.MAX_TRACKING_EVENTS:]
            if isinstance(scan, dict)
        ]

        latest_event = None
        if tracking_history:
            latest_event = tracking_history[-1]
        elif data.get("latest_event") and isinstance(data.get("latest_event"), dict):
            latest_event = self._parse_event(data["latest_event"])

        shipment = ShadowfaxShipment(
            awb=str(data.get("awb_number") or data.get("awb") or data.get("order_number") or awb),
            status=norm_status,
            courier="SHADOWFAX",
            estimated_delivery=data.get("promised_delivery_date") or data.get("edd") or data.get("estimated_delivery"),
            pickup_date=data.get("pickup_date") or data.get("actual_pickup_date")
            or self._first_event_time(scans, PICKUP_DONE_STATUS_IDS),
            delivered_date=data.get("delivered_date") or data.get("delivery_date")
            or self._first_event_time(scans, ("delivered",)),
            origin=data.get("origin") or data.get("origin_pincode") or data.get("origin_city")
            or self._place(data.get("pickup_details")),
            destination=data.get("destination") or data.get("destination_pincode") or data.get("destination_city")
            or self._place(data.get("delivery_details")),
            latest_event=latest_event,
            tracking_history=tracking_history,
        )
        return shipment.model_dump()

    def get_tracking_history(self, awb: str) -> Dict[str, Any]:
        """
        Retrieve full normalized scan events for a Shadowfax shipment.
        """
        shipment = self.track_shipment(awb)
        if "error" in shipment:
            return shipment
        return {
            "awb": shipment["awb"],
            "status": shipment["status"],
            "latest_event": shipment.get("latest_event"),
            "tracking_history": shipment.get("tracking_history", []),
        }

    def check_serviceability(
        self, pickup_pincode: str, delivery_pincode: str, pickup_service: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Pickup -> delivery serviceability using the documented v1 endpoint, which answers
        per pincode and per service:
            GET {base}/v1/clients/serviceability/?service=<service>&pincodes=<pin>&page=1&count=10
            -> [{"code": 560017, "services": ["Regular", "Surface"]}, ...]
        Two read-only lookups: the pickup pincode for the pickup service (SHADOWFAX_PICKUP_SERVICE,
        default seller_pickup) and the delivery pincode for customer_delivery. A pincode absent
        from the response is not serviceable for that service.
        """
        if not valid_pincode(pickup_pincode) or not valid_pincode(delivery_pincode):
            return {"error": "Pickup pincode and delivery pincode must be valid 6-digit pincodes.", "code": "INVALID_ARGUMENT"}
        pickup_service = (pickup_service or os.getenv("SHADOWFAX_PICKUP_SERVICE") or DEFAULT_PICKUP_SERVICE).strip()
        if pickup_service not in PICKUP_SERVICES:
            return {"error": f"Unsupported Shadowfax pickup service '{pickup_service}'.", "code": "INVALID_ARGUMENT"}

        legs = {}
        for leg, pincode, service in (("pickup", pickup_pincode.strip(), pickup_service),
                                      ("delivery", delivery_pincode.strip(), DELIVERY_SERVICE)):
            raw = self._get(SERVICEABILITY_PATH, {"service": service, "pincodes": pincode, "page": 1, "count": 10})
            if isinstance(raw, dict) and "error" in raw and "code" in raw:
                return raw
            rows = raw if isinstance(raw, list) else next(
                (raw[k] for k in ("results", "data") if isinstance(raw, dict) and isinstance(raw.get(k), list)), None)
            if rows is None:
                return self._error("PROVIDER_INVALID_RESPONSE")
            match = next((r for r in rows if isinstance(r, dict) and str(r.get("code")) == pincode), None)
            legs[leg] = {
                "pincode": pincode,
                "service": service,
                "serviceable": match is not None,
                "service_types": match.get("services") if match and isinstance(match.get("services"), list) else [],
            }

        return ShadowfaxServiceability(
            pickup_pincode=pickup_pincode.strip(),
            delivery_pincode=delivery_pincode.strip(),
            serviceable=legs["pickup"]["serviceable"] and legs["delivery"]["serviceable"],
            service_info=legs,
        ).model_dump()

    def track_reverse_pickup(self, reverse_awb: str) -> Dict[str, Any]:
        """
        Track a reverse pickup on Shadowfax using reverse AWB / identifier.
        Returns normalized ShadowfaxReversePickup dict or error dict (Read-Only).
        """
        if not valid_awb(reverse_awb):
            return {"error": f"Invalid reverse pickup identifier '{reverse_awb}'.", "code": "INVALID_ARGUMENT"}

        # The official Unified API documents no separate reverse-tracking endpoint; reverse legs
        # are tracked by AWB on the documented v4 tracking endpoint (e.g. recd_at_rev_hub events).
        data, scans = self._tracking_payload(reverse_awb)
        if data is None:
            return scans  # error dict

        raw_status = data.get("pickup_status") or data.get("status") or "UNKNOWN"
        norm_status = self.map_status(raw_status)

        tracking_history = [
            self._parse_event(scan)
            for scan in scans[-self.MAX_TRACKING_EVENTS:]
            if isinstance(scan, dict)
        ]

        latest_event = None
        if tracking_history:
            latest_event = tracking_history[-1]
        elif data.get("latest_event") and isinstance(data.get("latest_event"), dict):
            latest_event = self._parse_event(data["latest_event"])

        pickup = ShadowfaxReversePickup(
            reverse_awb=str(data.get("reverse_awb") or data.get("awb_number") or data.get("awb") or reverse_awb),
            pickup_status=norm_status,
            courier="SHADOWFAX",
            scheduled_date=data.get("scheduled_date") or data.get("scheduled_pickup_date"),
            pickup_date=data.get("pickup_date") or data.get("actual_pickup_date")
            or self._first_event_time(scans, PICKUP_DONE_STATUS_IDS),
            delivered_date=data.get("delivered_date") or data.get("vendor_delivered_date"),
            latest_event=latest_event,
            tracking_history=tracking_history,
        )
        return pickup.model_dump()
