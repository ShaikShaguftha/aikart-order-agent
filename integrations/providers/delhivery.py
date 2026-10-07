"""
Delhivery tracking connector - PHASE 1: read-only tracking.

Spec (section 4): GET {base_url}/api/v1/packages/json/?waybill={awb}
  - base URL configurable per tenant/environment (default https://track.delhivery.com)
  - default auth "Authorization: Token <token>"; the spec marks this as a Luintix default that
    must be validated against the merchant's Delhivery One developer-portal contract, so the
    header name and scheme are configurable and built in ONE place (_auth_headers).

Not implemented (later phases): NDR actions, address updates, reattempts, RTO, cancellation,
shipment creation, webhooks, customer notifications. This connector only issues GET requests.

Output uses the same normalized tracking shape as the Shippo connector (status,
provider_status, substatus, estimated_delivery, current_location, tracking_history, ...)
plus Delhivery fields (provider_status_type, status_location, order_reference, ndr_state).
Fields Delhivery does not return are None - never guessed.
"""
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from integrations.base import log_provider_call

logger = logging.getLogger("uvicorn.error")

DEFAULT_BASE_URL = "https://track.delhivery.com"
DEFAULT_AUTH_HEADER = "Authorization"
DEFAULT_AUTH_SCHEME = "Token"
TRACKING_PATH = "api/v1/packages/json/"
AWB_PATTERN = re.compile(r"^[A-Za-z0-9-]{6,32}$")
HEADER_NAME_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,64}$")
SCHEME_PATTERN = re.compile(r"^[A-Za-z]{0,20}$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Delhivery status text (casefolded) -> (existing Luintix unified status, spec 4.11 detail).
# Only statuses named in the spec (plus "Manifested", from the spec's own payload example).
# Anything else -> UNKNOWN; the raw value is always kept in provider_status.
STATUS_MAP = {
    "manifested": ("PRE_TRANSIT", "MANIFESTED"),
    "pending": ("PRE_TRANSIT", "PENDING"),
    "ready to ship": ("PRE_TRANSIT", "READY_TO_SHIP"),
    "ready for pickup": ("PRE_TRANSIT", "READY_FOR_PICKUP"),
    "in transit": ("IN_TRANSIT", "IN_TRANSIT"),
    "out for delivery": ("OUT_FOR_DELIVERY", "OUT_FOR_DELIVERY"),
    "delivered": ("DELIVERED", "DELIVERED"),
    "cancelled": ("CANCELLED", "CANCELLED"),
    "canceled": ("CANCELLED", "CANCELLED"),
    "rto-intransit": ("RETURNED", "RETURNING"),
    "rto in transit": ("RETURNED", "RETURNING"),
    "rto-returned": ("RETURNED", "RETURNED"),
    "lost": ("EXCEPTION", "LOST"),
}
# Explicit NDR / failed-delivery signals. Only these mark ndr_state; otherwise it stays None.
NDR_STATUS_WORDS = ("ndr", "undelivered", "failed delivery", "delivery failed", "not delivered")
NDR_CODE_PREFIX = "EOD-"


def valid_awb(awb: Optional[str]) -> bool:
    return bool(awb) and bool(AWB_PATTERN.match(str(awb)))


def is_delhivery_carrier(carrier: Optional[str]) -> bool:
    return "delhivery" in (carrier or "").casefold()


def normalize_base_url(base_url: Optional[str]) -> Optional[str]:
    """HTTPS *.delhivery.com only (http allowed for localhost), so the token cannot leak elsewhere."""
    value = (base_url or DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "http" and host in LOCAL_HOSTS:
        return value
    if parsed.scheme != "https" or not (host == "delhivery.com" or host.endswith(".delhivery.com")):
        return None
    return f"https://{host}" + (f":{parsed.port}" if parsed.port else "")


class DelhiveryConnector:
    provider_name = "DELHIVERY"
    MAX_RETRY_AFTER_SECONDS = 5.0
    MAX_BACKOFF_SECONDS = 8.0

    ERROR_MESSAGES = {
        "PROVIDER_AUTH_FAILED": "Delhivery rejected the API token. The merchant must reconnect Delhivery.",
        "PROVIDER_FORBIDDEN": "Delhivery refused access: this account is not entitled to that API or shipment.",
        "NOT_FOUND": "Delhivery could not find a shipment with that AWB.",
        "PROVIDER_API_UNAVAILABLE": "The Delhivery tracking API was not found at the configured base URL.",
        "PROVIDER_CONFLICT": "Delhivery reported a conflict for this request.",
        "RATE_LIMITED": "Delhivery is rate limiting requests right now. Please try again shortly.",
        "PROVIDER_UNAVAILABLE": "Delhivery is temporarily unavailable.",
        "PROVIDER_TIMEOUT": "Delhivery did not respond in time.",
        "PROVIDER_UNREACHABLE": "Delhivery could not be reached.",
        "PROVIDER_INVALID_RESPONSE": "Delhivery returned an unexpected response.",
        "PROVIDER_VALIDATION_FAILED": "Delhivery rejected the request as invalid.",
        "PROVIDER_ERROR": "Delhivery returned an error.",
    }

    def __init__(
        self,
        api_token: Optional[str],
        base_url: Optional[str] = None,
        environment: Optional[str] = None,
        auth_header: Optional[str] = None,
        auth_scheme: Optional[str] = None,
        timeout: float = 10.0,
        retry_backoff: float = 0.5,
    ):
        self.api_token = (api_token or "").strip()
        self.base_url = normalize_base_url(base_url)
        self.environment = (environment or "production").strip().lower()
        header = (auth_header or DEFAULT_AUTH_HEADER).strip()
        scheme = DEFAULT_AUTH_SCHEME if auth_scheme is None else auth_scheme.strip()
        self.auth_header = header if HEADER_NAME_PATTERN.match(header) else None
        self.auth_scheme = scheme if SCHEME_PATTERN.match(scheme) else None
        self.timeout = timeout
        self.retry_backoff = retry_backoff

    def credentials_configured(self) -> bool:
        return bool(self.api_token)

    def configuration_error(self) -> Optional[Dict[str, str]]:
        if not self.api_token:
            return {"error": "Delhivery is not configured for this tenant.", "code": "PROVIDER_NOT_CONFIGURED"}
        if not self.base_url:
            return {"error": "Delhivery base URL must be an https://*.delhivery.com address.", "code": "PROVIDER_CONFIG_INSECURE"}
        if not self.auth_header or self.auth_scheme is None:
            return {"error": "Delhivery auth header configuration is invalid.", "code": "PROVIDER_NOT_CONFIGURED"}
        return None

    def _auth_headers(self) -> Dict[str, str]:
        """The single place the Delhivery credential format is defined (configurable per tenant)."""
        value = f"{self.auth_scheme} {self.api_token}" if self.auth_scheme else self.api_token
        return {self.auth_header: value}

    def _sanitize_error(self, message: str) -> str:
        return str(message).replace(self.api_token, "[REDACTED_TOKEN]") if self.api_token else str(message)

    def _error(self, code: str, status: Any = None, **extra) -> Dict[str, Any]:
        err = {"error": self.ERROR_MESSAGES[code], "code": code, **extra}
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

    # ---- transport (GET only in phase 1) -------------------------------------------

    def _get(self, path: str, params: Dict[str, Any], max_retries: int = 3) -> Any:
        config_error = self.configuration_error()
        if config_error:
            return config_error
        last_error: Dict[str, Any] = self._error("PROVIDER_ERROR")
        for attempt in range(1, max_retries + 1):
            response = None
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    response = client.request(
                        method="GET",
                        url=f"{self.base_url}/{path}",
                        params=params,
                        headers={**self._auth_headers(), "Accept": "application/json",
                                 "User-Agent": "Luintix-Delhivery-Connector/1.0"},
                    )
                status = response.status_code
                log_provider_call(self.provider_name, f"GET {path}", status)
                if status == 200:
                    try:
                        return response.json()
                    except ValueError:
                        return self._error("PROVIDER_INVALID_RESPONSE", status)
                if status == 400:
                    return self._error("PROVIDER_VALIDATION_FAILED", status)
                if status == 401:
                    return self._error("PROVIDER_AUTH_FAILED", status)
                if status == 403:
                    return self._error("PROVIDER_FORBIDDEN", status)
                if status == 404:
                    # A JSON body means the API answered "no such shipment"; anything else
                    # (HTML page, empty body) means the route itself does not exist.
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    return self._error("NOT_FOUND" if isinstance(body, dict) else "PROVIDER_API_UNAVAILABLE", status)
                if status == 409:
                    return self._error("PROVIDER_CONFLICT", status)
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
                logger.warning(f"[DELHIVERY] request failed: {self._sanitize_error(type(e).__name__)}")
                return self._error("PROVIDER_ERROR")
            if attempt < max_retries:
                time.sleep(self._backoff(attempt, response))
        return last_error

    # ---- normalization -------------------------------------------------------------

    @staticmethod
    def map_status(provider_status: Optional[str]) -> tuple:
        """(unified status, detail); unknown -> ("UNKNOWN", None)."""
        return STATUS_MAP.get(str(provider_status or "").strip().casefold(), ("UNKNOWN", None))

    @staticmethod
    def _ndr_signal(status_text: Optional[str], code: Optional[str], instructions: Optional[str]) -> Optional[Dict[str, Any]]:
        text = str(status_text or "").casefold()
        code = str(code or "")
        if code.upper().startswith(NDR_CODE_PREFIX) or any(w in text for w in NDR_STATUS_WORDS):
            return {"detected": True, "code": code or None, "reason": instructions or None}
        return None

    @classmethod
    def _scan(cls, raw: Dict[str, Any]) -> Dict[str, Any]:
        detail = raw.get("ScanDetail") if isinstance(raw.get("ScanDetail"), dict) else raw
        provider_status = detail.get("Scan") or detail.get("Status")
        status, sub = cls.map_status(provider_status)
        ndr = cls._ndr_signal(provider_status, detail.get("StatusCode") or detail.get("NSLCode"), detail.get("Instructions"))
        if ndr and status != "DELIVERED":
            status, sub = "EXCEPTION", "NDR"
        location = detail.get("ScannedLocation") or detail.get("StatusLocation")
        return {
            "status": status,
            "provider_status": provider_status,
            "provider_status_type": detail.get("ScanType") or detail.get("StatusType"),
            "substatus": sub,
            "substatus_text": None,
            "action_required": bool(ndr),
            "status_details": detail.get("Instructions"),
            "status_date": detail.get("ScanDateTime") or detail.get("StatusDateTime"),
            "location": {"label": location, "city": None, "region": None, "country": None} if location else None,
            "provider_reference": None,
        }

    @classmethod
    def normalize_shipment(cls, shipment: Any) -> Dict[str, Any]:
        """Delhivery Shipment object -> Luintix normalized tracking (Shippo-compatible keys)."""
        if not isinstance(shipment, dict) or not shipment.get("AWB"):
            return {"error": cls.ERROR_MESSAGES["PROVIDER_INVALID_RESPONSE"], "code": "PROVIDER_INVALID_RESPONSE"}
        current = shipment.get("Status")
        if current is not None and not isinstance(current, dict):
            return {"error": cls.ERROR_MESSAGES["PROVIDER_INVALID_RESPONSE"], "code": "PROVIDER_INVALID_RESPONSE"}
        scans = shipment.get("Scans") or []
        if not isinstance(scans, list):
            return {"error": cls.ERROR_MESSAGES["PROVIDER_INVALID_RESPONSE"], "code": "PROVIDER_INVALID_RESPONSE"}
        current = current or {}
        provider_status = current.get("Status")
        status, sub = cls.map_status(provider_status)
        ndr = cls._ndr_signal(provider_status, shipment.get("NSLCode") or current.get("NSLCode"), current.get("Instructions"))
        if ndr and status != "DELIVERED":
            status, sub = "EXCEPTION", "NDR"
        location = current.get("StatusLocation")
        expected = shipment.get("ExpectedDeliveryDate") or shipment.get("PromisedDeliveryDate")
        return {
            "provider": "delhivery",
            "carrier": "delhivery",
            "carrier_slug": "delhivery",
            "service_level": None,
            "tracking_number": shipment.get("AWB"),
            "awb": shipment.get("AWB"),
            "status": status,
            "provider_status": provider_status,
            "provider_status_type": current.get("StatusType"),
            "substatus": sub,
            "substatus_text": None,
            "status_details": current.get("Instructions"),
            "action_required": bool(ndr),
            "ndr_state": ndr,
            "status_location": location,
            "status_timestamp": current.get("StatusDateTime"),
            "estimated_delivery": expected or None,
            "expected_delivery": expected or None,
            "current_location": {"label": location, "city": None, "region": None, "country": None} if location else None,
            "order_reference": shipment.get("ReferenceNo") or None,
            "pickup_date": shipment.get("PickUpDate") or None,
            "tracking_history": [cls._scan(s) for s in scans if isinstance(s, dict)],
            "source_updated_at": current.get("StatusDateTime"),
            "provider_reference": None,
            "test_mode": None,
        }

    # ---- READ capability -----------------------------------------------------------

    def get_tracking(self, awb: str) -> Dict[str, Any]:
        """GET /api/v1/packages/json/?waybill={awb} -> normalized tracking (with full scan history)."""
        if not valid_awb(awb):
            return {"error": "That is not a valid AWB / waybill number.", "code": "INVALID_TRACKING_NUMBER"}
        res = self._get(TRACKING_PATH, {"waybill": awb})
        if isinstance(res, dict) and "error" in res and "code" in res:
            return res
        if not isinstance(res, dict):
            return self._error("PROVIDER_INVALID_RESPONSE")
        data = res.get("ShipmentData")
        if data is None:
            # Delhivery reports unknown waybills in a 200 body, e.g. {"Error": "..."}.
            if res.get("Error") or res.get("error"):
                return self._error("NOT_FOUND")
            return self._error("PROVIDER_INVALID_RESPONSE")
        if not isinstance(data, list):
            return self._error("PROVIDER_INVALID_RESPONSE")
        shipments: List[Dict[str, Any]] = [d.get("Shipment") for d in data if isinstance(d, dict)]
        match = next((s for s in shipments if isinstance(s, dict) and str(s.get("AWB")) == str(awb)), None)
        if match is None:
            return self._error("NOT_FOUND") if not shipments else self._error("PROVIDER_INVALID_RESPONSE")
        return self.normalize_shipment(match)
