import json
import logging
from typing import Any, Dict, Optional
from backend.database import execute_db

logger = logging.getLogger("uvicorn.error")


class AuditLogger:
    """
    Audit logging service for tracking state-changing operations across e-commerce providers.
    Logs execution attempts, policy decisions, provider outcomes, and refund statuses.
    """

    @classmethod
    def log_action(
        cls,
        session_id: Optional[str],
        company_id: str,
        order_id: str,
        action_type: str,
        provider: str,
        status: str,
        reason: Optional[str] = None,
        refund_issued: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Records an action execution audit entry in the SQLite action_audit_logs table."""
        details_str = json.dumps(details or {})
        try:
            row_id = execute_db(
                """
                INSERT INTO action_audit_logs (
                    session_id, company_id, order_id, action_type, provider, status, reason, refund_issued, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id or "default",
                    company_id or "COMP-ALPHA",
                    order_id,
                    action_type,
                    provider,
                    status,
                    reason or "",
                    1 if refund_issued else 0,
                    details_str,
                ),
            )
            logger.info(
                f"[AUDIT LOG] {action_type} for Order {order_id} ({provider}): {status} | Refund Issued: {refund_issued}"
            )
            return row_id
        except Exception as e:
            logger.error(f"[AUDIT LOG ERROR] Failed to record audit log: {e}")
            return -1
