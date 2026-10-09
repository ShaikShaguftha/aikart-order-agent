"""
Provider-neutral stores for support tickets and return requests.

Used for providers that have no native helpdesk/returns API (e.g. WooCommerce core).
Every read is scoped to the tenant (company_id), so a ticket or return ID from one
tenant can never be read through another.
"""
import uuid
from typing import Any, Dict, Optional

from backend.database import execute_db, query_db


class SupportTicketStore:

    @staticmethod
    def create(
        company_id: str,
        order_id: Optional[str],
        reason: str,
        session_id: Optional[str] = None,
        source: str = "AGENT",
    ) -> Dict[str, Any]:
        ticket_id = f"TICK-{uuid.uuid4().hex[:10].upper()}"
        execute_db(
            "INSERT INTO support_tickets (ticket_id, company_id, order_id, reason, status, source, session_id) "
            "VALUES (?, ?, ?, ?, 'OPEN', ?, ?)",
            (ticket_id, company_id, order_id, reason, source, session_id),
        )
        return SupportTicketStore.get(company_id, ticket_id)

    @staticmethod
    def get(company_id: str, ticket_id: str) -> Optional[Dict[str, Any]]:
        return query_db(
            "SELECT ticket_id, order_id, reason, status, source, created_at FROM support_tickets "
            "WHERE ticket_id = ? AND company_id = ?",
            (ticket_id.strip().upper(), company_id),
            one=True,
        )


class ReturnRequestStore:

    @staticmethod
    def create(
        company_id: str,
        order_id: str,
        requested_amount: float,
        reason: Optional[str],
        status: str,
        ticket_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return_id = f"RET-{uuid.uuid4().hex[:10].upper()}"
        execute_db(
            "INSERT INTO return_requests (return_id, company_id, order_id, requested_amount, reason, status, ticket_id, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (return_id, company_id, order_id, requested_amount, reason, status, ticket_id, session_id),
        )
        return ReturnRequestStore.get(company_id, return_id)

    @staticmethod
    def get(company_id: str, return_id: str) -> Optional[Dict[str, Any]]:
        return query_db(
            "SELECT return_id, order_id, requested_amount, reason, status, ticket_id, created_at "
            "FROM return_requests WHERE return_id = ? AND company_id = ?",
            (return_id.strip().upper(), company_id),
            one=True,
        )

    @staticmethod
    def open_for_order(company_id: str, order_id: str) -> Optional[Dict[str, Any]]:
        return query_db(
            "SELECT return_id, status FROM return_requests WHERE company_id = ? AND order_id = ? "
            "AND status IN ('SUBMITTED', 'REQUIRES_HUMAN_REVIEW') ORDER BY created_at DESC LIMIT 1",
            (company_id, order_id),
            one=True,
        )
