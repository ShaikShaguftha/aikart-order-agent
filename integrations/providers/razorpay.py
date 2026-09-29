"""
Razorpay connector - PHASE 1: READ-ONLY payments provider (payment and refund lookups).

Official Razorpay API, fixed base https://api.razorpay.com/v1:
    GET /v1/payments/{payment_id}
    GET /v1/payments/{payment_id}/refunds
    GET /v1/refunds/{refund_id}

Authentication: HTTP Basic, username = RAZORPAY_KEY_ID, password = RAZORPAY_KEY_SECRET.
Test Mode keys start with rzp_test_, Live Mode keys with rzp_live_. Phase 1 is intended for Test
Mode; because every request is a GET, a live key can only read.

Every request is a GET; any other method is refused before sending. There is no refund /
capture / cancel code path (refund creation is a separate, later phase). Amounts arrive in the
currency's smallest unit (e.g. paise) and are normalized into PaymentRecord / RefundRecord with
both the major-unit amount and the original minor-unit integer.
"""
import base64
import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from integrations.base import BaseConnector, log_provider_call
from integrations.models import PaymentRecord, RefundRecord

logger = logging.getLogger("uvicorn.error")

BASE_URL = "https://api.razorpay.com/v1"  # fixed: never taken from config or the LLM

KEY_ID_PATTERN = re.compile(r"^rzp_(test|live)_[A-Za-z0-9]{6,40}$")
PAYMENT_ID_PATTERN = re.compile(r"^pay_[A-Za-z0-9]{8,40}$")
REFUND_ID_PATTERN = re.compile(r"^rfnd_[A-Za-z0-9]{8,40}$")
REFUND_PAGE_SIZE = 100  # Razorpay maximum per page

# Razorpay amounts are in the smallest currency unit; exponent 2 unless listed here.
CURRENCY_EXPONENTS = {"JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "PYG": 0, "ISK": 0, "UGX": 0, "XAF": 0, "XOF": 0,
                      "KWD": 3, "BHD": 3, "OMR": 3, "JOD": 3, "TND": 3}


class RazorpayConnector(BaseConnector):
    provider_name = "RAZORPAY"
    MAX_RETRY_AFTER_SECONDS = 5.0
    MAX_BACKOFF_SECONDS = 8.0

    ERROR_MESSAGES = {
        "PROVIDER_AUTH_FAILED": "Razorpay authentication failed. Check the API key. Reconnect required.",
        "PROVIDER_FORBIDDEN": "Razorpay access denied for this account.",
        "NOT_FOUND": "No Razorpay record was found for that ID.",
        "PROVIDER_API_UNAVAILABLE": "The Razorpay API endpoint was not found.",
        "PROVIDER_VALIDATION_FAILED": "Razorpay rejected the request as invalid.",
        "RATE_LIMITED": "Razorpay is rate limiting requests right now. Please try again shortly.",
        "PROVIDER_UNAVAILABLE": "Razorpay is temporarily unavailable.",
        "PROVIDER_TIMEOUT": "Razorpay did not respond in time.",
        "PROVIDER_UNREACHABLE": "Razorpay could not be reached.",
        "PROVIDER_INVALID_RESPONSE": "Razorpay returned an unexpected response.",
        "PROVIDER_ERROR": "Razorpay returned an error.",
        "NOT_SUPPORTED": "This capability is not available for a Razorpay tenant (read-only payments, no store orders).",
    }

    def __init__(self, key_id: Optional[str], key_secret: Optional[str], timeout: float = 10.0,
                 retry_backoff: float = 0.5):
        self.key_id = (key_id or "").strip()
        self.key_secret = (key_secret or "").strip()
        self.timeout = timeout
        self.retry_backoff = retry_backoff

    # ---- configuration & secrets -----------------------------------------------------

    @property
    def mode(self) -> Optional[str]:
        """'test' or 'live' from the key ID prefix (the key ID is not a secret); None if unknown."""
        match = KEY_ID_PATTERN.match(self.key_id)
        return match.group(1) if match else None

    def credentials_configured(self) -> bool:
        return bool(self.key_id and self.key_secret)

    def domain_configured(self) -> bool:
        return True  # fixed official base URL

    def configuration_error(self) -> Optional[Dict[str, str]]:
        if not self.credentials_configured():
            return {"error": "Razorpay credentials are not configured for this tenant (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET).",
                    "code": "PROVIDER_NOT_CONFIGURED"}
        if not self.mode:
            return {"error": "Razorpay key ID must start with rzp_test_ or rzp_live_.", "code": "PROVIDER_NOT_CONFIGURED"}
        return None

    def _basic_token(self) -> str:
        return base64.b64encode(f"{self.key_id}:{self.key_secret}".encode()).decode()

    def _sanitize_error(self, message: Any) -> str:
        text = str(message or "")
        for secret in (self.key_secret, self._basic_token() if self.key_secret else ""):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def _error(self, code: str, status: Any = None) -> Dict[str, Any]:
        err = {"error": self.ERROR_MESSAGES[code], "code": code}
        if code == "PROVIDER_AUTH_FAILED":
            err["reconnect_required"] = True
        if status is not None:
            err["http_status"] = status
        return err

    @staticmethod
    def _is_error(body: Any) -> bool:
        return isinstance(body, dict) and "error" in body and "code" in body

    def _backoff(self, attempt: int, response: Optional[httpx.Response]) -> float:
        if response is not None and response.headers.get("Retry-After"):
            try:
                return min(float(response.headers["Retry-After"]), self.MAX_RETRY_AFTER_SECONDS)
            except (TypeError, ValueError):
                pass
        base = self.retry_backoff * (2 ** (attempt - 1))
        return min(base + random.uniform(0, self.retry_backoff), self.MAX_BACKOFF_SECONDS)

    # ---- transport ---------------------------------------------------------------------

    @staticmethod
    def _error_description(response: httpx.Response) -> Optional[str]:
        """Razorpay errors are {"error": {"code", "description", ...}}; None when not that shape."""
        try:
            body = response.json()
        except ValueError:
            return None
        error = body.get("error") if isinstance(body, dict) else None
        return str(error.get("description") or "").lower() if isinstance(error, dict) else None

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 operation: str = "", max_retries: int = 3) -> Any:
        """GET only (safe to retry on 429 / 5xx / timeouts: nothing is modified)."""
        method = method.upper()
        if method != "GET":
            return {"error": "Only read operations are allowed for Razorpay.", "code": "NOT_SUPPORTED"}
        config_error = self.configuration_error()
        if config_error:
            return config_error
        operation = f"{method} {operation or path}"
        last_error: Dict[str, Any] = self._error("PROVIDER_ERROR")
        for attempt in range(1, max_retries + 1):
            response = None
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    response = client.request(
                        method=method, url=f"{BASE_URL}/{path}", params=params,
                        headers={"Authorization": f"Basic {self._basic_token()}", "Accept": "application/json",
                                 "User-Agent": "Luintix-Razorpay-Connector/1.0"},
                    )
                status = response.status_code
                log_provider_call(self.provider_name, operation, status)
                if status == 200:
                    try:
                        return response.json()
                    except ValueError:
                        return self._error("PROVIDER_INVALID_RESPONSE", status)
                description = self._error_description(response)
                if status == 401:
                    return self._error("PROVIDER_AUTH_FAILED", status)
                if status == 403:
                    return self._error("PROVIDER_FORBIDDEN", status)
                if status in (400, 404):
                    # Razorpay reports an unknown ID as 400 "The id provided does not exist" and an
                    # unknown route as "The requested URL was not found on the server".
                    if description is None:
                        return self._error("PROVIDER_API_UNAVAILABLE" if status == 404 else "PROVIDER_VALIDATION_FAILED", status)
                    if "url was not found" in description:
                        return self._error("PROVIDER_API_UNAVAILABLE", status)
                    if status == 404 or "does not exist" in description:
                        return self._error("NOT_FOUND", status)
                    return self._error("PROVIDER_VALIDATION_FAILED", status)
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
                logger.warning(f"[RAZORPAY] request failed: {self._sanitize_error(type(e).__name__)}")
                return self._error("PROVIDER_ERROR")
            if attempt < max_retries:
                time.sleep(self._backoff(attempt, response))
        return last_error

    # ---- normalization -----------------------------------------------------------------

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        return str(value) if value not in (None, "") and not isinstance(value, (dict, list, bool)) else None

    @staticmethod
    def _minor(value: Any) -> Optional[int]:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    @staticmethod
    def _major(minor: Optional[int], currency: Optional[str]) -> Optional[float]:
        if minor is None:
            return None
        return round(minor / (10 ** CURRENCY_EXPONENTS.get((currency or "").upper(), 2)), 3)

    @staticmethod
    def _timestamp(value: Any) -> Optional[str]:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _entity(body: Any, entity: str, record_id: Optional[str] = None) -> bool:
        return (isinstance(body, dict) and body.get("entity") == entity and isinstance(body.get("id"), str)
                and bool(body["id"]) and (record_id is None or body["id"] == record_id))

    def _payment(self, p: Dict[str, Any]) -> Dict[str, Any]:
        currency = self._text(p.get("currency"))
        amount, refunded = self._minor(p.get("amount")), self._minor(p.get("amount_refunded"))
        return PaymentRecord(
            id=p["id"], amount=self._major(amount, currency), amount_minor=amount, currency=currency,
            status=self._text(p.get("status")), order_id=self._text(p.get("order_id")),
            method=self._text(p.get("method")), email=self._text(p.get("email")), contact=self._text(p.get("contact")),
            captured=p.get("captured") if isinstance(p.get("captured"), bool) else None,
            amount_refunded=self._major(refunded, currency), refund_status=self._text(p.get("refund_status")),
            created_at=self._timestamp(p.get("created_at")), provider=self.provider_name,
        ).model_dump()

    def _refund(self, r: Dict[str, Any]) -> Dict[str, Any]:
        currency = self._text(r.get("currency"))
        amount = self._minor(r.get("amount"))
        return RefundRecord(
            id=r["id"], payment_id=self._text(r.get("payment_id")), amount=self._major(amount, currency),
            amount_minor=amount, currency=currency, status=self._text(r.get("status")),
            speed=self._text(r.get("speed_processed") or r.get("speed_requested") or r.get("speed")),
            created_at=self._timestamp(r.get("created_at")), provider=self.provider_name,
        ).model_dump()

    @staticmethod
    def _valid_id(pattern: "re.Pattern", value: Any) -> Optional[str]:
        rid = str(value or "").strip()
        return rid if pattern.match(rid) else None

    # ---- READ capabilities ---------------------------------------------------------------

    def get_payment(self, payment_id: str) -> Dict[str, Any]:
        pid = self._valid_id(PAYMENT_ID_PATTERN, payment_id)
        if not pid:
            return {"error": "That is not a valid Razorpay payment ID (it looks like pay_XXXXXXXXXXXXXX).",
                    "code": "INVALID_RECORD_ID"}
        body = self._request("GET", f"payments/{pid}", operation="payments/{payment_id}")
        if self._is_error(body):
            return body
        if not self._entity(body, "payment", pid):
            return self._error("PROVIDER_INVALID_RESPONSE")
        return self._payment(body)

    def get_payment_refunds(self, payment_id: str) -> Dict[str, Any]:
        """All refunds of one payment: {"payment_id", "refunds": [...], "count", "more_records"}."""
        pid = self._valid_id(PAYMENT_ID_PATTERN, payment_id)
        if not pid:
            return {"error": "That is not a valid Razorpay payment ID (it looks like pay_XXXXXXXXXXXXXX).",
                    "code": "INVALID_RECORD_ID"}
        body = self._request("GET", f"payments/{pid}/refunds", {"count": REFUND_PAGE_SIZE},
                             operation="payments/{payment_id}/refunds")
        if self._is_error(body):
            return body
        items = body.get("items") if isinstance(body, dict) and body.get("entity") == "collection" else None
        if not isinstance(items, list):
            return self._error("PROVIDER_INVALID_RESPONSE")
        refunds = [r for r in items if self._entity(r, "refund")]
        if any(r.get("payment_id") != pid for r in refunds):
            return self._error("PROVIDER_INVALID_RESPONSE")  # never attribute another payment's refund
        return {"payment_id": pid, "refunds": [self._refund(r) for r in refunds], "count": len(refunds),
                "more_records": len(items) >= REFUND_PAGE_SIZE, "provider": self.provider_name}

    def get_refund(self, refund_id: str) -> Dict[str, Any]:
        rid = self._valid_id(REFUND_ID_PATTERN, refund_id)
        if not rid:
            return {"error": "That is not a valid Razorpay refund ID (it looks like rfnd_XXXXXXXXXXXXXX).",
                    "code": "INVALID_RECORD_ID"}
        body = self._request("GET", f"refunds/{rid}", operation="refunds/{refund_id}")
        if self._is_error(body):
            return body
        if not self._entity(body, "refund", rid):
            return self._error("PROVIDER_INVALID_RESPONSE")
        return self._refund(body)

    # ---- BaseConnector commerce capabilities: not applicable to a payments-only tenant ----

    def _unsupported(self) -> Dict[str, Any]:
        return self._error("NOT_SUPPORTED")

    def get_shop(self, company_id: str) -> Dict[str, Any]:
        return {"name": "Razorpay", "domain": BASE_URL, "provider": self.provider_name}

    def get_orders(self, company_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        return [self._unsupported()]

    def get_customer_details(self, identifier: str, company_id: str) -> List[Dict[str, Any]]:
        return []

    def get_order_details(self, order_id: str, company_id: str) -> Optional[Dict[str, Any]]:
        return self._unsupported()

    def get_shipment_status(self, order_id: str, company_id: str) -> Optional[Dict[str, Any]]:
        return self._unsupported()

    def cancel_order(self, order_id: str, company_id: str, reason: Optional[str] = "CUSTOMER") -> Dict[str, Any]:
        return {**self._unsupported(), "success": False, "status": "FAILED", "order_id": order_id}

    def request_return(self, order_id: str, refund_amount: float, company_id: str) -> Dict[str, Any]:
        return {**self._unsupported(), "status": "BLOCKED"}  # no refund can be created in phase 1

    def check_proactive_notifications(self, customer_id: str, company_id: str) -> Optional[str]:
        return None

    def update_order(self, order_id: str, company_id: str, **kwargs) -> Dict[str, Any]:
        return self._unsupported()

    def escalate_to_human(self, order_id: str, reason: str, company_id: str) -> Dict[str, Any]:
        return {**self._unsupported(), "status": "ERROR"}  # never invents a ticket reference

    def get_ticket_details(self, ticket_id: str, company_id: str) -> Dict[str, Any]:
        return self._unsupported()
