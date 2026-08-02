"""Typed errors.

Raising a bare Exception loses information: the caller cannot tell a bad
request from a genuine bug. Each class carries an HTTP status and a stable
machine-readable code, so the API layer translates a domain failure into the
right response without inspecting message strings.
"""


class AppError(Exception):
    status: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, details: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ValidationError(AppError):
    status = 400
    code = "VALIDATION_ERROR"


class NotFoundError(AppError):
    status = 404
    code = "NOT_FOUND"


class ConflictError(AppError):
    status = 409
    code = "CONFLICT"


class RuleDefinitionError(AppError):
    """A rule references an unknown feature, operator, or malformed shape.

    Raised when a rule is WRITTEN, not when traffic is evaluated. A rule that
    silently never matches is worse than one that refuses to be saved: it
    looks like a quiet rule and nobody investigates.
    """

    status = 422
    code = "INVALID_RULE"


class NoActiveRulesetError(AppError):
    """A merchant has no ACTIVE ruleset.

    Deliberately an error rather than a default-approve. Silently approving
    everything because configuration is missing is the single most expensive
    failure mode a fraud engine has.
    """

    status = 409
    code = "NO_ACTIVE_RULESET"
