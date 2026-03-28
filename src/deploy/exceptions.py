"""Custom exceptions for Pipedream deployment."""


class PipedreamSyncError(Exception):
    """Base exception for all sync-related errors."""

    pass


class AuthenticationError(PipedreamSyncError):
    """Raised when authentication fails or cookies are invalid/expired."""

    pass


class NavigationError(PipedreamSyncError):
    """Raised when navigation to a Pipedream page fails."""

    pass


class StepNotFoundError(PipedreamSyncError):
    """Raised when a workflow step cannot be found by name."""

    def __init__(self, step_name: str, workflow_id: str):
        self.step_name = step_name
        self.workflow_id = workflow_id
        super().__init__(
            f"Step '{step_name}' not found in workflow '{workflow_id}'"
        )


class CodeUpdateError(PipedreamSyncError):
    """Raised when updating code in the editor fails."""

    pass


class SaveError(PipedreamSyncError):
    """Raised when saving changes to Pipedream fails."""

    pass


class ValidationError(PipedreamSyncError):
    """Raised when validation of configuration or scripts fails."""

    pass


class ConfigurationError(PipedreamSyncError):
    """Raised when configuration is invalid or missing."""

    pass


class CredentialSecurityError(PipedreamSyncError):
    """Raised when a credential file has unsafe ownership or permissions."""

    pass
