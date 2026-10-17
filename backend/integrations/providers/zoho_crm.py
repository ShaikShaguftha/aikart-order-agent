"""
Zoho CRM connector - PHASE 1: READ-ONLY CRM provider (Zoho CRM REST API v8).

    GET {api_domain}/crm/v8/Contacts/{id}
    GET {api_domain}/crm/v8/Contacts/search   (email | phone | criteria)
    GET {api_domain}/crm/v8/Accounts/{id}
    GET {api_domain}/crm/v8/Deals/{id}
    Authorization: Zoho-oauthtoken <access_token>

OAuth 2.0: the access token (1 hour) is refreshed with the refresh token at
{accounts_url}/oauth/v2/token (grant_type=refresh_token) when missing or rejected with
INVALID_TOKEN; secrets travel in the POST form body, never in URLs or logs.

Required Zoho scopes: ZohoCRM.modules.contacts.READ, ZohoCRM.modules.accounts.READ,
ZohoCRM.modules.deals.READ and ZohoSearch.securesearch.READ (search).

Only GET requests are made to the CRM (the single POST is the token refresh). There is no
create/update/delete code path. As a BaseConnector for a CRM-only tenant, the commerce
capabilities (orders, cancellations, returns, tickets) explicitly return NOT_SUPPORTED.
"""
import hashlib
import logging
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx

from backend.integrations.base import BaseConnector, log_provider_call
from backend.integrations.models import CRMAccount, CRMContact, CRMDeal

logger = logging.getLogger("uvicorn.error")

# Documented Zoho data centers: API domain -> accounts (OAuth) server.
DATA_CENTERS = {
    "www.zohoapis.com": "https://accounts.zoho.com",
    "www.zohoapis.eu": "https://accounts.zoho.eu",
    "www.zohoapis.in": "https://accounts.zoho.in",
    "www.zohoapis.com.au": "https://accounts.zoho.com.au",
    "www.zohoapis.jp": "https://accounts.zoho.jp",
    "www.zohoapis.ca": "https://accounts.zohocloud.ca",
    "www.zohoapis.com.cn": "https://accounts.zoho.com.cn",
}
DEFAULT_API_DOMAIN = "https://www.zohoapis.com"
API_VERSION = "v8"
CONTACT_FIELDS = "First_Name,Last_Name,Email,Phone,Mobile,Account_Name,Created_Time"
ACCOUNT_FIELDS = "Account_Name,Website,Phone"
DEAL_FIELDS = "Deal_Name,Amount,Stage,Closing_Date"
RECORD_ID_PATTERN = re.compile(r"^\d{5,25}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NAME_PATTERN = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ .'-]){1,59}$", re.UNICODE)
SEARCH_LIMIT = 10
# Characters Zoho requires to be backslash-escaped inside criteria values (API v8 docs).
CRITERIA_SPECIAL = set("{}[]^:-/!?*_@ ")


def normalize_api_domain(api_domain: Optional[str]) -> Optional[str]:
    """Only documented https://www.zohoapis.<dc> domains; the token is never sent elsewhere."""
    value = (api_domain or DEFAULT_API_DOMAIN).strip().rstrip("/")
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in DATA_CENTERS or parsed.path not in ("", "/"):
        return None
    return f"https://{host}"


def escape_criteria(value: str) -> str:
    return "".join(f"\\{c}" if c in CRITERIA_SPECIAL or c in "()," else c for c in value)


class ZohoCRMConnector(BaseConnector):
    provider_name = "ZOHO_CRM"
    MAX_RETRY_AFTER_SECONDS = 5.0
    MAX_BACKOFF_SECONDS = 8.0
    # Zoho caps how many access tokens a refresh token can mint in a short window; avoid
    # refreshing again within this many seconds of a successful refresh.
    MIN_REFRESH_INTERVAL_SECONDS = 30.0

    # {credential_key: {"token": str, "expires_at": float, "refreshed_at": float}}
    _token_cache: Dict[str, Dict[str, Any]] = {}
    _refresh_lock = threading.Lock()

    ERROR_MESSAGES = {
        "CRM_AUTH_FAILED": "Zoho CRM rejected the credentials. The merchant must reconnect Zoho CRM.",
        "CRM_SCOPE_MISSING": "The Zoho CRM connection is missing a required read permission (OAuth scope).",
        "PROVIDER_FORBIDDEN": "Zoho CRM refused access: the connected user cannot read that record or module.",
        "NOT_FOUND": "No Zoho CRM record was found for that ID.",
        "PROVIDER_API_UNAVAILABLE": "The Zoho CRM API was not found at the configured domain.",
        "PROVIDER_VALIDATION_FAILED": "Zoho CRM rejected the request as invalid.",
        "RATE_LIMITED": "Zoho CRM is rate limiting requests right now. Please try again shortly.",
        "PROVIDER_UNAVAILABLE": "Zoho CRM is temporarily unavailable.",
        "PROVIDER_TIMEOUT": "Zoho CRM did not respond in time.",
        "PROVIDER_UNREACHABLE": "Zoho CRM could not be reached.",
        "PROVIDER_INVALID_RESPONSE": "Zoho CRM returned an unexpected response.",
        "PROVIDER_ERROR": "Zoho CRM returned an error.",
        "NOT_SUPPORTED": "This capability is not available for a Zoho CRM tenant (read-only CRM, no store orders).",
    }

    def __init__(
        self,
        client_id: Optional[str],
        client_secret: Optional[str],
        refresh_token: Optional[str],
        access_token: Optional[str] = None,
        api_domain: Optional[str] = None,
        timeout: float = 10.0,
        retry_backoff: float = 0.5,
    ):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        self.api_domain = normalize_api_domain(api_domain)
        self.accounts_url = DATA_CENTERS.get(urlparse(self.api_domain).hostname) if self.api_domain else None
        self.timeout = timeout
        self.retry_backoff = retry_backoff
        self._cache_key = hashlib.sha256(
            f"{self.client_id}|{self.refresh_token}|{self.api_domain}".encode()
        ).hexdigest()[:24]
        if access_token and self._cache_key not in self._token_cache:
            # Seed with the configured token; its expiry is unknown, so it is used until rejected.
            self._token_cache[self._cache_key] = {"token": access_token.strip(), "expires_at": float("inf"),
                                                  "refreshed_at": 0.0}
        self._client: Optional[httpx.Client] = None

    # ---- configuration & secrets -----------------------------------------------------

    def credentials_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def domain_configured(self) -> bool:
        return bool(self.api_domain)

    def configuration_error(self) -> Optional[Dict[str, str]]:
        if not self.api_domain:
            return {"error": "ZOHO_API_DOMAIN must be a documented https://www.zohoapis.<region> domain.",
                    "code": "PROVIDER_CONFIG_INSECURE"}
        if not self.credentials_configured():
            return {"error": "Zoho CRM OAuth credentials are not configured for this tenant.",
                    "code": "PROVIDER_NOT_CONFIGURED"}
        return None

    def _secrets(self) -> List[str]:
        cached = self._token_cache.get(self._cache_key, {}).get("token")
        return [s for s in (self.client_secret, self.refresh_token, cached, self.client_id) if s]

    def _sanitize_error(self, message: str) -> str:
        clean = str(message)
        for secret in self._secrets():
            clean = clean.replace(secret, "[REDACTED]")
        return clean

    def _error(self, code: str, status: Any = None, **extra) -> Dict[str, Any]:
        err = {"error": self.ERROR_MESSAGES[code], "code": code, **extra}
        if code == "CRM_AUTH_FAILED":
            err["reconnect_required"] = True
        if status is not None:
            err["http_status"] = status
        return err


    def _get_client(self) -> httpx.Client:
        """Create and reuse an HTTP connection pool for CRM and OAuth requests."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                timeout=self.timeout,
                follow_redirects=False,
                headers={
                    "User-Agent": "Luintix-ZohoCRM-Connector/1.0",
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

    # ---- OAuth 2.0 access-token management -------------------------------------------

    def _current_token(self) -> Optional[str]:
        entry = self._token_cache.get(self._cache_key)
        if entry and entry["expires_at"] > time.time():
            return entry["token"]
        return None

    def refresh_access_token(self, rejected_token: Optional[str] = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """
        Exchanges the refresh token for a new access token (grant_type=refresh_token).
        Serialized per credential; if another caller already refreshed, its token is reused.
        Returns (token, None) or (None, error).
        """
        with self._refresh_lock:
            entry = self._token_cache.get(self._cache_key)
            if entry and entry["token"] != rejected_token and entry["expires_at"] > time.time():
                return entry["token"], None
            if entry and time.time() - entry.get("refreshed_at", 0) < self.MIN_REFRESH_INTERVAL_SECONDS \
                    and entry["token"] == rejected_token:
                return None, self._error("CRM_AUTH_FAILED")  # a freshly minted token was rejected
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.request(
                        method="POST",
                        url=f"{self.accounts_url}/oauth/v2/token",
                        data={"grant_type": "refresh_token", "refresh_token": self.refresh_token,
                              "client_id": self.client_id, "client_secret": self.client_secret},
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                log_provider_call(self.provider_name, "POST oauth/v2/token (refresh)", response.status_code)
                try:
                    body = response.json()
                except ValueError:
                    body = {}
            except httpx.TimeoutException:
                return None, self._error("PROVIDER_TIMEOUT")
            except httpx.TransportError:
                return None, self._error("PROVIDER_UNREACHABLE")
            except Exception as e:
                logger.warning(f"[ZOHO CRM] token refresh failed: {self._sanitize_error(type(e).__name__)}")
                return None, self._error("PROVIDER_ERROR")
            token = body.get("access_token") if isinstance(body, dict) else None
            if response.status_code != 200 or not token:
                # Zoho reports bad client/refresh tokens as {"error": "invalid_client" | "invalid_code"}.
                if response.status_code >= 500:
                    return None, self._error("PROVIDER_UNAVAILABLE", response.status_code)
                return None, self._error("CRM_AUTH_FAILED", response.status_code)
            expires_in = float(body.get("expires_in") or 3600)
            self._token_cache[self._cache_key] = {"token": token, "expires_at": time.time() + max(expires_in - 300, 60),
                                                  "refreshed_at": time.time()}
            return token, None

    # ---- transport (GET only) ----------------------------------------------------------

    def _backoff(self, attempt: int, response: Optional[httpx.Response]) -> float:
        if response is not None and response.headers.get("Retry-After"):
            try:
                return min(float(response.headers["Retry-After"]), self.MAX_RETRY_AFTER_SECONDS)
            except (TypeError, ValueError):
                pass
        base = self.retry_backoff * (2 ** (attempt - 1))
        return min(base + random.uniform(0, self.retry_backoff), self.MAX_BACKOFF_SECONDS)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> Any:
        """GET {api_domain}/crm/v8/{path}. Returns parsed JSON, {} for 204, or an error dict."""
        config_error = self.configuration_error()
        if config_error:
            return config_error
        token = self._current_token()
        if not token:
            token, error = self.refresh_access_token()
            if error:
                return error
        refreshed_after_rejection = False
        last_error: Dict[str, Any] = self._error("PROVIDER_ERROR")
        attempt = 0
        while attempt < max_retries:
            attempt += 1
            response = None
            try:
                client = self._get_client()
                response = client.request(
                    method="GET",
                    url=f"{self.api_domain}/crm/{API_VERSION}/{path}",
                    params=params,
                    headers={
                        "Authorization": f"Zoho-oauthtoken {token}",
                        "Accept": "application/json",
                    },
                )
                status = response.status_code
                log_provider_call(self.provider_name, "GET " + re.sub(r"/\d{5,}", "/{id}", path), status)
                if status == 204:
                    return {}
                try:
                    body = response.json()
                except ValueError:
                    body = None
                code = (body or {}).get("code") if isinstance(body, dict) else None
                if status == 200:
                    return body if isinstance(body, (dict, list)) else self._error("PROVIDER_INVALID_RESPONSE", status)
                if status == 401:
                    if code == "OAUTH_SCOPE_MISMATCH":
                        return self._error("CRM_SCOPE_MISSING", status)
                    if code == "INVALID_TOKEN" and not refreshed_after_rejection:
                        token, error = self.refresh_access_token(rejected_token=token)
                        if error:
                            return error
                        refreshed_after_rejection = True
                        attempt -= 1  # the refreshed retry does not consume a retry slot
                        continue
                    return self._error("CRM_AUTH_FAILED", status)
                if status == 403:
                    return self._error("PROVIDER_FORBIDDEN", status)
                if status == 404:
                    return self._error("PROVIDER_API_UNAVAILABLE" if code in (None, "INVALID_URL_PATTERN") else "NOT_FOUND", status)
                if status == 400:
                    if code == "INVALID_DATA":
                        return self._error("NOT_FOUND", status)  # e.g. malformed / unknown record ID
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
                logger.warning(f"[ZOHO CRM] request failed: {self._sanitize_error(type(e).__name__)}")
                return self._error("PROVIDER_ERROR")
            if attempt < max_retries:
                time.sleep(self._backoff(attempt, response))
        return last_error

    # ---- normalization -----------------------------------------------------------------

    def _records(self, body: Any) -> Optional[List[Dict[str, Any]]]:
        if body == {}:
            return []
        if not isinstance(body, dict) or not isinstance(body.get("data", []), list):
            return None
        return [r for r in body.get("data", []) if isinstance(r, dict) and r.get("id")]

    def _contact(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        account = raw.get("Account_Name") if isinstance(raw.get("Account_Name"), dict) else {}
        return CRMContact(
            id=str(raw["id"]), first_name=raw.get("First_Name"), last_name=raw.get("Last_Name"),
            email=raw.get("Email"), phone=raw.get("Phone") or raw.get("Mobile"),
            company=account.get("name"), account_id=str(account["id"]) if account.get("id") else None,
            created_at=raw.get("Created_Time"), provider=self.provider_name,
        ).model_dump()

    def _single(self, module: str, record_id: str, fields: str, build) -> Dict[str, Any]:
        rid = str(record_id or "").strip()
        if not RECORD_ID_PATTERN.match(rid):
            return {"error": "That is not a valid Zoho CRM record ID.", "code": "INVALID_RECORD_ID"}
        body = self._get(f"{module}/{quote(rid)}", {"fields": fields})
        if isinstance(body, dict) and "code" in body and "error" in body:
            return body
        records = self._records(body)
        if records is None:
            return self._error("PROVIDER_INVALID_RESPONSE")
        match = next((r for r in records if str(r["id"]) == rid), None)
        return build(match) if match else self._error("NOT_FOUND")

    # ---- READ capabilities ---------------------------------------------------------------

    def get_contact(self, company_id: str, contact_id: str) -> Dict[str, Any]:
        return self._single("Contacts", contact_id, CONTACT_FIELDS, self._contact)

    def search_contacts(
        self, company_id: str, email: Optional[str] = None, phone: Optional[str] = None, name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Contacts matching an exact email, a phone, or a name (first/last name starts_with).
        Returns {"contacts": [...], "count": n, "searched_by": ...} (at most SEARCH_LIMIT).
        """
        email, phone, name = (email or "").strip(), (phone or "").strip(), " ".join((name or "").split())
        if email:
            if not EMAIL_PATTERN.match(email):
                return {"error": "That is not a valid email address.", "code": "INVALID_ARGUMENT"}
            params, searched_by = {"email": email}, "email"
        elif phone:
            if len(re.sub(r"\D", "", phone)) < 7:
                return {"error": "That is not a valid phone number.", "code": "INVALID_ARGUMENT"}
            params, searched_by = {"phone": phone}, "phone"
        elif name:
            if not NAME_PATTERN.match(name):
                return {"error": "Please give a customer name (letters only, at least 2 characters).", "code": "INVALID_ARGUMENT"}
            parts = [escape_criteria(p) for p in name.split()]
            if len(parts) >= 2:
                criteria = f"((First_Name:starts_with:{parts[0]})and(Last_Name:starts_with:{parts[-1]}))"
            else:
                criteria = f"((First_Name:starts_with:{parts[0]})or(Last_Name:starts_with:{parts[0]}))"
            params, searched_by = {"criteria": criteria}, "name"
        else:
            return {"error": "Provide an email address, phone number or name to search.", "code": "INVALID_ARGUMENT"}
        body = self._get("Contacts/search", {**params, "per_page": SEARCH_LIMIT})
        if isinstance(body, dict) and "code" in body and "error" in body:
            return body
        records = self._records(body)
        if records is None:
            return self._error("PROVIDER_INVALID_RESPONSE")
        contacts = [self._contact(r) for r in records[:SEARCH_LIMIT]]
        more = bool(isinstance(body, dict) and (body.get("info") or {}).get("more_records"))
        return {"contacts": contacts, "count": len(contacts), "searched_by": searched_by, "more_records": more}

    def get_account(self, company_id: str, account_id: str) -> Dict[str, Any]:
        return self._single("Accounts", account_id, ACCOUNT_FIELDS, lambda r: CRMAccount(
            id=str(r["id"]), name=r.get("Account_Name"), website=r.get("Website"), phone=r.get("Phone"),
            provider=self.provider_name).model_dump())

    def get_deal(self, company_id: str, deal_id: str) -> Dict[str, Any]:
        def build(r):
            try:
                amount = float(r["Amount"]) if r.get("Amount") is not None else None
            except (TypeError, ValueError):
                amount = None
            return CRMDeal(id=str(r["id"]), name=r.get("Deal_Name"), amount=amount, stage=r.get("Stage"),
                           closing_date=r.get("Closing_Date"), provider=self.provider_name).model_dump()
        return self._single("Deals", deal_id, DEAL_FIELDS, build)

    # ---- BaseConnector commerce capabilities: not applicable to a CRM-only tenant --------

    def _unsupported(self) -> Dict[str, Any]:
        return self._error("NOT_SUPPORTED")

    def get_shop(self, company_id: str) -> Dict[str, Any]:
        return {"name": "Zoho CRM", "domain": self.api_domain, "provider": self.provider_name}

    def get_orders(self, company_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        return []

    def get_customer_details(self, identifier: str, company_id: str) -> List[Dict[str, Any]]:
        return []

    def get_order_details(self, order_id: str, company_id: str) -> Optional[Dict[str, Any]]:
        return self._unsupported()

    def get_shipment_status(self, order_id: str, company_id: str) -> Optional[Dict[str, Any]]:
        return self._unsupported()

    def cancel_order(self, order_id: str, company_id: str, reason: Optional[str] = "CUSTOMER") -> Dict[str, Any]:
        return {**self._unsupported(), "success": False, "status": "FAILED", "order_id": order_id}

    def request_return(self, order_id: str, refund_amount: float, company_id: str) -> Dict[str, Any]:
        return {**self._unsupported(), "status": "BLOCKED"}

    def check_proactive_notifications(self, customer_id: str, company_id: str) -> Optional[str]:
        return None

    def update_order(self, order_id: str, company_id: str, **kwargs) -> Dict[str, Any]:
        return self._unsupported()  # read-only: explicit, never reaches Zoho

    def escalate_to_human(self, order_id: str, reason: str, company_id: str) -> Dict[str, Any]:
        # Never invent a ticket reference: this tenant has no ticketing system.
        return {**self._unsupported(), "status": "ERROR"}

    def get_ticket_details(self, ticket_id: str, company_id: str) -> Dict[str, Any]:
        return self._unsupported()
