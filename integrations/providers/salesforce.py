"""
Salesforce CRM connector - PHASE 1: READ-ONLY CRM provider.

Salesforce REST API, org-specific instance (SALESFORCE_INSTANCE_URL, e.g. https://acme.my.salesforce.com):
    GET /services/data/                          (lists the org's supported API versions; used only
                                                  when SALESFORCE_API_VERSION is not configured)
    GET /services/data/{version}/query?q=<SOQL>  (all reads: Contact, Account, Opportunity, Case)

Authentication: "Authorization: Bearer <access token>" (SALESFORCE_ACCESS_TOKEN). OAuth setup and
token refresh are out of scope for phase 1; the token is read from the environment.

Every request is a GET; there is no create/update/delete code path. SOQL is only built from:
  * fixed field / object names defined in this module, and
  * values that passed format validation AND were escaped with soql_literal() (quotes,
    backslashes, control characters; % and _ inside LIKE patterns).
Responses are normalized into CRMContact / CRMAccount / CRMDeal (opportunity) / CRMTicket (case).
"""
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from integrations.base import BaseConnector, log_provider_call
from integrations.models import CRMAccount, CRMContact, CRMDeal, CRMTicket

logger = logging.getLogger("uvicorn.error")

VERSIONS_PATH = "services/data/"
API_VERSION_PATTERN = re.compile(r"^v?(\d{2,3})\.0$")
ALLOWED_HOST_SUFFIXES = (".salesforce.com", ".force.com")

CONTACT_FIELDS = "Id, FirstName, LastName, Email, Phone, MobilePhone, AccountId, Account.Name, CreatedDate"
ACCOUNT_FIELDS = "Id, Name, Website, Phone"
OPPORTUNITY_FIELDS = "Id, Name, Amount, StageName, CloseDate"
CASE_FIELDS = "Id, CaseNumber, Subject, Status, Priority, Description, CreatedDate, LastModifiedDate"

# 15-character (case-sensitive) or 18-character (case-safe) record IDs; the first 3 characters
# are the object's key prefix.
RECORD_ID_PATTERN = re.compile(r"^[a-zA-Z0-9]{15}(?:[a-zA-Z0-9]{3})?$")
KEY_PREFIXES = {"Contact": "003", "Account": "001", "Opportunity": "006", "Case": "500"}
CASE_NUMBER_PATTERN = re.compile(r"^\d{1,30}$")
CASE_NUMBER_WIDTH = 8  # default Case auto-number format {00000000}
EMAIL_PATTERN = re.compile(r"^[^@\s/]+@[^@\s/]+\.[^@\s/]+$")
NAME_PATTERN = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ .'-]){1,59}$", re.UNICODE)
PHONE_PATTERN = re.compile(r"^\+?[\d\s().-]{7,30}$")
SEARCH_LIMIT = 10
DESCRIPTION_LIMIT = 500

_SOQL_ESCAPES = {"\\": "\\\\", "'": "\\'", '"': '\\"', "\n": "\\n", "\r": "\\r",
                 "\t": "\\t", "\b": "\\b", "\f": "\\f"}

# Discovered API versions per instance (only used when SALESFORCE_API_VERSION is not set).
_DISCOVERED_VERSIONS: Dict[str, str] = {}


def soql_literal(value: Any, like: bool = False) -> str:
    """
    Returns a quoted, escaped SOQL string literal. Escapes every character SOQL treats specially
    inside quotes; with like=True also escapes the LIKE wildcards % and _. Other control
    characters are rejected (ValueError) rather than passed through.
    """
    text = str(value)
    if any(ord(ch) < 32 and ch not in _SOQL_ESCAPES for ch in text) or "\x7f" in text:
        raise ValueError("control characters are not allowed in SOQL values")
    escaped = "".join(_SOQL_ESCAPES.get(ch, ch) for ch in text)
    if like:
        escaped = escaped.replace("%", "\\%").replace("_", "\\_")
    return f"'{escaped}'"


def soql_prefix(value: Any) -> str:
    """Escaped LIKE literal matching values that start with `value` (its own % and _ are literal)."""
    return soql_literal(value, like=True)[:-1] + "%'"


def normalize_instance_url(url: Optional[str]) -> Optional[str]:
    """https://<host> for a Salesforce-owned host; None when missing or not allowed."""
    raw = (url or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443)
            or parts.query or parts.fragment or parts.path.strip("/")
            or not host.endswith(ALLOWED_HOST_SUFFIXES)):
        return None
    return f"https://{host}"


class SalesforceConnector(BaseConnector):
    provider_name = "SALESFORCE"
    MAX_RETRY_AFTER_SECONDS = 5.0
    MAX_BACKOFF_SECONDS = 8.0

    ERROR_MESSAGES = {
        "PROVIDER_AUTH_FAILED": "Salesforce authentication failed. Reconnect required.",
        "PROVIDER_FORBIDDEN": "Salesforce access denied. The connected user may lack API or object permissions.",
        "NOT_FOUND": "No Salesforce record was found for that ID.",
        "PROVIDER_API_UNAVAILABLE": "The Salesforce API endpoint was not found.",
        "PROVIDER_VALIDATION_FAILED": "Salesforce rejected the request as invalid.",
        "RATE_LIMITED": "Salesforce is limiting API requests right now. Please try again later.",
        "PROVIDER_UNAVAILABLE": "Salesforce is temporarily unavailable.",
        "PROVIDER_TIMEOUT": "Salesforce did not respond in time.",
        "PROVIDER_UNREACHABLE": "Salesforce could not be reached.",
        "PROVIDER_INVALID_RESPONSE": "Salesforce returned an unexpected response.",
        "PROVIDER_ERROR": "Salesforce returned an error.",
        "NOT_SUPPORTED": "This capability is not available for a Salesforce CRM tenant (read-only CRM, no store orders).",
    }

    def __init__(self, access_token: Optional[str], instance_url: Optional[str], api_version: Optional[str] = None,
                 timeout: float = 10.0, retry_backoff: float = 0.5):
        self.access_token = (access_token or "").strip()
        self.raw_instance_url = (instance_url or "").strip()
        self.instance_url = normalize_instance_url(self.raw_instance_url)
        self.raw_api_version = (api_version or "").strip()
        match = API_VERSION_PATTERN.match(self.raw_api_version)
        self.api_version = f"v{match.group(1)}.0" if match else None
        self.timeout = timeout
        self.retry_backoff = retry_backoff

    # ---- configuration & secrets -----------------------------------------------------

    def credentials_configured(self) -> bool:
        return bool(self.access_token)

    def domain_configured(self) -> bool:
        return bool(self.instance_url)

    def configuration_error(self) -> Optional[Dict[str, str]]:
        if not self.access_token:
            return {"error": "Salesforce access token is not configured for this tenant.", "code": "PROVIDER_NOT_CONFIGURED"}
        if not self.raw_instance_url:
            return {"error": "Salesforce instance URL is not configured for this tenant.", "code": "PROVIDER_NOT_CONFIGURED"}
        if not self.instance_url:
            return {"error": "Salesforce instance URL must be an https://<domain>.my.salesforce.com URL.",
                    "code": "PROVIDER_NOT_CONFIGURED"}
        if self.raw_api_version and not self.api_version:
            return {"error": "Salesforce API version must look like v62.0.", "code": "PROVIDER_NOT_CONFIGURED"}
        return None

    def _sanitize_error(self, message: Any) -> str:
        text = str(message or "")
        return text.replace(self.access_token, "[REDACTED_TOKEN]") if self.access_token else text

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
    def _error_codes(response: httpx.Response) -> Optional[List[str]]:
        """Salesforce errors are a JSON list of {"errorCode", "message"}; None when not JSON."""
        try:
            body = response.json()
        except ValueError:
            return None
        items = body if isinstance(body, list) else [body]
        return [str(i.get("errorCode") or "") for i in items if isinstance(i, dict)]

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 operation: str = "", max_retries: int = 3) -> Any:
        """GET only (safe to retry on 429 / 5xx / timeouts: nothing is modified)."""
        method = method.upper()
        if method != "GET":
            return {"error": "Only read operations are allowed for Salesforce.", "code": "NOT_SUPPORTED"}
        config_error = self.configuration_error()
        if config_error:
            return config_error
        operation = f"{method} {operation or path}"  # never the SOQL text (it holds customer data)
        last_error: Dict[str, Any] = self._error("PROVIDER_ERROR")
        for attempt in range(1, max_retries + 1):
            response = None
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
                    response = client.request(
                        method=method, url=f"{self.instance_url}/{path}", params=params,
                        headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json",
                                 "User-Agent": "Luintix-Salesforce-Connector/1.0"},
                    )
                status = response.status_code
                log_provider_call(self.provider_name, operation, status)
                if status == 200:
                    try:
                        return response.json()
                    except ValueError:
                        return self._error("PROVIDER_INVALID_RESPONSE", status)
                codes = self._error_codes(response)
                if status == 401:
                    return self._error("PROVIDER_AUTH_FAILED", status)
                if status == 403:
                    # The org's rolling 24h API request limit is reported as 403; retrying won't help.
                    if codes and "REQUEST_LIMIT_EXCEEDED" in codes:
                        return self._error("RATE_LIMITED", status)
                    return self._error("PROVIDER_FORBIDDEN", status)
                if status == 404:
                    # JSON error list (NOT_FOUND) = the resource; non-JSON = the route / instance.
                    return self._error("NOT_FOUND" if codes else "PROVIDER_API_UNAVAILABLE", status)
                if status == 400:
                    return self._error("PROVIDER_VALIDATION_FAILED", status)
                if status == 429:
                    last_error = self._error("RATE_LIMITED", status)
                elif status >= 500:
                    last_error = self._error("PROVIDER_UNAVAILABLE", status)
                elif 300 <= status < 400:
                    # A redirect usually means a wrong instance URL; never follow it with the token.
                    return self._error("PROVIDER_API_UNAVAILABLE", status)
                else:
                    return self._error("PROVIDER_ERROR", status)
            except httpx.TimeoutException:
                last_error = self._error("PROVIDER_TIMEOUT")
            except httpx.TransportError:
                last_error = self._error("PROVIDER_UNREACHABLE")
            except Exception as e:
                logger.warning(f"[SALESFORCE] request failed: {self._sanitize_error(type(e).__name__)}")
                return self._error("PROVIDER_ERROR")
            if attempt < max_retries:
                time.sleep(self._backoff(attempt, response))
        return last_error

    def _resolve_api_version(self) -> Any:
        """Configured version, else the newest version the org reports (cached per instance)."""
        if self.api_version:
            return self.api_version
        config_error = self.configuration_error()
        if config_error:
            return config_error
        cached = _DISCOVERED_VERSIONS.get(self.instance_url)
        if cached:
            return cached
        body = self._request("GET", VERSIONS_PATH, operation="services/data (API versions)")
        if self._is_error(body):
            return body
        versions = []
        for item in body if isinstance(body, list) else []:
            match = API_VERSION_PATTERN.match(str((item or {}).get("version") or "")) if isinstance(item, dict) else None
            if match:
                versions.append(int(match.group(1)))
        if not versions:
            return self._error("PROVIDER_INVALID_RESPONSE")
        _DISCOVERED_VERSIONS[self.instance_url] = f"v{max(versions)}.0"
        return _DISCOVERED_VERSIONS[self.instance_url]

    def _query(self, soql: str, sobject: str) -> Any:
        """Runs one SOQL query; returns {"records": [...], "done": bool} or an error dict."""
        version = self._resolve_api_version()
        if self._is_error(version):
            return version
        body = self._request("GET", f"services/data/{version}/query", {"q": soql},
                             operation=f"services/data/{version}/query ({sobject})")
        if self._is_error(body):
            return body
        if not isinstance(body, dict) or not isinstance(body.get("records"), list):
            return self._error("PROVIDER_INVALID_RESPONSE")
        return body

    # ---- normalization -----------------------------------------------------------------

    @staticmethod
    def _text(value: Any) -> Optional[str]:
        return str(value) if value not in (None, "") and not isinstance(value, (dict, list)) else None

    @staticmethod
    def _valid(record: Any) -> bool:
        return isinstance(record, dict) and bool(record.get("Id")) and isinstance(record.get("Id"), str)

    def _contact(self, r: Dict[str, Any]) -> Dict[str, Any]:
        account = r.get("Account") if isinstance(r.get("Account"), dict) else {}
        return CRMContact(
            id=r["Id"], first_name=self._text(r.get("FirstName")), last_name=self._text(r.get("LastName")),
            email=self._text(r.get("Email")), phone=self._text(r.get("Phone") or r.get("MobilePhone")),
            company=self._text(account.get("Name")), account_id=self._text(r.get("AccountId")),
            created_at=self._text(r.get("CreatedDate")), provider=self.provider_name,
        ).model_dump()

    def _account(self, r: Dict[str, Any]) -> Dict[str, Any]:
        return CRMAccount(id=r["Id"], name=self._text(r.get("Name")), website=self._text(r.get("Website")),
                          phone=self._text(r.get("Phone")), provider=self.provider_name).model_dump()

    def _opportunity(self, r: Dict[str, Any]) -> Dict[str, Any]:
        try:
            amount = float(r["Amount"]) if r.get("Amount") not in (None, "") else None
        except (TypeError, ValueError):
            amount = None
        return CRMDeal(id=r["Id"], name=self._text(r.get("Name")), amount=amount, stage=self._text(r.get("StageName")),
                       closing_date=self._text(r.get("CloseDate")), provider=self.provider_name).model_dump()

    def _case(self, r: Dict[str, Any]) -> Dict[str, Any]:
        return CRMTicket(
            id=r["Id"], number=self._text(r.get("CaseNumber")), subject=self._text(r.get("Subject")),
            stage=self._text(r.get("Status")), priority=self._text(r.get("Priority")),
            description=(self._text(r.get("Description")) or "")[:DESCRIPTION_LIMIT] or None,
            created_at=self._text(r.get("CreatedDate")), updated_at=self._text(r.get("LastModifiedDate")),
            provider=self.provider_name,
        ).model_dump()

    @staticmethod
    def _valid_record_id(sobject: str, record_id: Any) -> Optional[str]:
        rid = str(record_id or "").strip()
        if RECORD_ID_PATTERN.match(rid) and rid.startswith(KEY_PREFIXES[sobject]):
            return rid
        return None

    def _single(self, sobject: str, fields: str, where: str, build, expected_id: Optional[str] = None) -> Dict[str, Any]:
        body = self._query(f"SELECT {fields} FROM {sobject} WHERE {where} LIMIT 1", sobject)
        if self._is_error(body):
            return body
        records = body["records"]
        if not records:
            return self._error("NOT_FOUND")
        record = records[0]
        if not self._valid(record):
            return self._error("PROVIDER_INVALID_RESPONSE")
        # 15-char IDs are the case-sensitive prefix of the 18-char form.
        if expected_id and record["Id"][:15] != expected_id[:15]:
            return self._error("PROVIDER_INVALID_RESPONSE")
        return build(record)

    def _by_id(self, sobject: str, fields: str, record_id: Any, build) -> Dict[str, Any]:
        rid = self._valid_record_id(sobject, record_id)
        if not rid:
            return {"error": f"That is not a valid Salesforce {sobject} ID.", "code": "INVALID_RECORD_ID"}
        return self._single(sobject, fields, f"Id = {soql_literal(rid)}", build, expected_id=rid)

    # ---- READ capabilities ---------------------------------------------------------------

    def get_contact(self, contact_id: str) -> Dict[str, Any]:
        return self._by_id("Contact", CONTACT_FIELDS, contact_id, self._contact)

    def search_contacts(self, email: Optional[str] = None, phone: Optional[str] = None,
                        name: Optional[str] = None) -> Dict[str, Any]:
        """
        email -> exact match; phone -> exact match on Phone or MobilePhone; name -> prefix match on
        first/last name (both when two names are given). Returns {"contacts": [...], "count": n,
        "searched_by": ...}; no match -> empty list.
        """
        email, phone, name = (email or "").strip(), (phone or "").strip(), " ".join((name or "").split())
        try:
            if email:
                if len(email) > 254 or not EMAIL_PATTERN.match(email):
                    return {"error": "That is not a valid email address.", "code": "INVALID_ARGUMENT"}
                where, searched_by = f"Email = {soql_literal(email)}", "email"
            elif phone:
                if not PHONE_PATTERN.match(phone) or len(re.sub(r"\D", "", phone)) < 7:
                    return {"error": "That is not a valid phone number.", "code": "INVALID_ARGUMENT"}
                value = soql_literal(phone)
                where, searched_by = f"(Phone = {value} OR MobilePhone = {value})", "phone"
            elif name:
                if not NAME_PATTERN.match(name):
                    return {"error": "Please give a customer name (letters only, at least 2 characters).",
                            "code": "INVALID_ARGUMENT"}
                parts = name.split()
                if len(parts) >= 2:
                    where = f"(FirstName LIKE {soql_prefix(parts[0])} AND LastName LIKE {soql_prefix(parts[-1])})"
                else:
                    prefix = soql_prefix(parts[0])
                    where = f"(FirstName LIKE {prefix} OR LastName LIKE {prefix})"
                searched_by = "name"
            else:
                return {"error": "Provide an email address, phone number or name to search.", "code": "INVALID_ARGUMENT"}
        except ValueError:
            return {"error": "The search value contains characters that are not allowed.", "code": "INVALID_ARGUMENT"}

        body = self._query(f"SELECT {CONTACT_FIELDS} FROM Contact WHERE {where} "
                           f"ORDER BY LastModifiedDate DESC LIMIT {SEARCH_LIMIT}", "Contact")
        if self._is_error(body):
            return body
        contacts = [self._contact(r) for r in body["records"][:SEARCH_LIMIT] if self._valid(r)]
        return {"contacts": contacts, "count": len(contacts), "searched_by": searched_by,
                "more_records": body.get("done") is False}

    def get_account(self, account_id: str) -> Dict[str, Any]:
        return self._by_id("Account", ACCOUNT_FIELDS, account_id, self._account)

    def get_opportunity(self, opportunity_id: str) -> Dict[str, Any]:
        return self._by_id("Opportunity", OPPORTUNITY_FIELDS, opportunity_id, self._opportunity)

    def get_case(self, case_id_or_number: str) -> Dict[str, Any]:
        """A Case record ID (500...) or the human-facing CaseNumber (e.g. 00001026 or 1026)."""
        value = str(case_id_or_number or "").strip().lstrip("#")
        if CASE_NUMBER_PATTERN.match(value):
            candidates = {value, value.zfill(CASE_NUMBER_WIDTH)}
            where = "CaseNumber IN (" + ", ".join(soql_literal(v) for v in sorted(candidates)) + ")"
            return self._single("Case", CASE_FIELDS, where, self._case)
        return self._by_id("Case", CASE_FIELDS, value, self._case)

    # ---- BaseConnector commerce capabilities: not applicable to a CRM-only tenant --------

    def _unsupported(self) -> Dict[str, Any]:
        return self._error("NOT_SUPPORTED")

    def get_shop(self, company_id: str) -> Dict[str, Any]:
        return {"name": "Salesforce CRM", "domain": self.instance_url, "provider": self.provider_name}

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
        return {**self._unsupported(), "status": "BLOCKED"}

    def check_proactive_notifications(self, customer_id: str, company_id: str) -> Optional[str]:
        return None

    def update_order(self, order_id: str, company_id: str, **kwargs) -> Dict[str, Any]:
        return self._unsupported()

    def escalate_to_human(self, order_id: str, reason: str, company_id: str) -> Dict[str, Any]:
        return {**self._unsupported(), "status": "ERROR"}  # never invents a case reference

    def get_ticket_details(self, ticket_id: str, company_id: str) -> Dict[str, Any]:
        return self._unsupported()
