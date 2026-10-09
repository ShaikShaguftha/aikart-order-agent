import datetime
import uuid
from typing import Dict, Optional
from backend.integrations.models import PendingAction, PolicyDecision


class ConfirmationManager:
    """
    Manages pending write actions requiring explicit 2-step user confirmation.
    Tracks pending actions by session_id, company_id, and order_id.
    """

    def __init__(self):
        self._pending: Dict[str, PendingAction] = {}

    def _make_key(self, session_id: str, company_id: str, order_id: str) -> str:
        return f"{session_id or 'default'}::{company_id or 'default'}::{order_id.strip().upper()}"

    def register_pending_action(
        self,
        session_id: str,
        company_id: str,
        order_id: str,
        reason: str,
        policy_decision: PolicyDecision,
    ) -> PendingAction:
        """Stages a pending cancellation request for user confirmation."""
        action_id = f"ACT-{uuid.uuid4().hex[:8].upper()}"
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

        pending_action = PendingAction(
            action_id=action_id,
            action_type="CANCEL_ORDER",
            session_id=session_id or "default",
            company_id=company_id or "default",
            order_id=order_id.strip(),
            reason=reason or "CUSTOMER",
            created_at=created_at,
            policy_decision=policy_decision,
        )

        key = self._make_key(session_id, company_id, order_id)
        self._pending[key] = pending_action
        return pending_action

    def get_pending_action(
        self, session_id: str, company_id: str, order_id: str
    ) -> Optional[PendingAction]:
        """Retrieves a staged pending action if present."""
        key = self._make_key(session_id, company_id, order_id)
        return self._pending.get(key)

    def clear_pending_action(
        self, session_id: str, company_id: str, order_id: str
    ) -> None:
        """Clears a staged pending action after completion or rejection."""
        key = self._make_key(session_id, company_id, order_id)
        self._pending.pop(key, None)


# Global instance
confirmation_manager = ConfirmationManager()
