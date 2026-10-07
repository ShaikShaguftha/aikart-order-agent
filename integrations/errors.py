"""
Normalized Exceptions for Luintix E-commerce Integration Gateway.
"""

class IntegrationError(Exception):
    """Base class for all integration gateway exceptions."""
    def __init__(self, message: str, code: str = "INTEGRATION_ERROR"):
        super().__init__(message)
        self.message = message
        self.code = code


class PolicyViolationError(IntegrationError):
    """Raised when an operation violates business policy rules (e.g. trying to cancel a shipped order)."""
    def __init__(self, message: str):
        super().__init__(message, code="POLICY_VIOLATION")


class ConfirmationRequiredError(IntegrationError):
    """Raised when a write action requires explicit user confirmation before proceeding."""
    def __init__(self, message: str, pending_action_id: str):
        super().__init__(message, code="CONFIRMATION_REQUIRED")
        self.pending_action_id = pending_action_id


class VerificationFailedError(IntegrationError):
    """Raised when post-action state verification fails after connector execution."""
    def __init__(self, message: str):
        super().__init__(message, code="VERIFICATION_FAILED")


class ProviderAPIError(IntegrationError):
    """Raised when provider connector API calls fail or return unrecoverable errors."""
    def __init__(self, message: str, provider: str = "UNKNOWN"):
        super().__init__(message, code="PROVIDER_API_ERROR")
        self.provider = provider


class TenantConfigurationError(IntegrationError):
    """Raised when a tenant has no usable integration configuration (never falls back to LocalDB)."""
    def __init__(self, message: str):
        super().__init__(message, code="TENANT_CONFIGURATION_ERROR")
