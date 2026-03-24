from __future__ import annotations

"""Custom API exception hierarchy with HTTP status mapping."""

from typing import Optional


class APIError(Exception):
    """Base exception for predictable API failures.

    Attributes:
        status_code: HTTP status to return.
        error_code: Stable machine-readable error identifier.
        message: Human-readable error text.
        call_id: Optional call correlation identifier.
    """

    def __init__(self, status_code: int, error_code: str, message: str, call_id: Optional[str] = None) -> None:
        """Initialize a structured API error."""
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.call_id = call_id


class InvalidInputError(APIError):
    """Error for malformed request input."""

    def __init__(self, error_code: str, message: str, call_id: Optional[str] = None) -> None:
        """Create a 400 Bad Request input error."""
        super().__init__(status_code=400, error_code=error_code, message=message, call_id=call_id)


class UnauthorizedError(APIError):
    """Error for failed authentication."""

    def __init__(self, message: str = "Unauthorized", call_id: Optional[str] = None) -> None:
        """Create a 401 Unauthorized error."""
        super().__init__(status_code=401, error_code="unauthorized", message=message, call_id=call_id)


class FileTooLargeError(APIError):
    """Error for uploads that exceed configured size limits."""

    def __init__(self, max_upload_mb: int, call_id: Optional[str] = None) -> None:
        """Create a 413 Payload Too Large error."""
        super().__init__(
            status_code=413,
            error_code="file_too_large",
            message=f"File size exceeds {max_upload_mb} MB limit",
            call_id=call_id,
        )


class RateLimitError(APIError):
    """Error for throttling conditions from this API or upstreams."""

    def __init__(self, message: str = "Rate limit exceeded", call_id: Optional[str] = None) -> None:
        """Create a 429 Too Many Requests error."""
        super().__init__(status_code=429, error_code="rate_limited", message=message, call_id=call_id)


class UpstreamUnavailableError(APIError):
    """Error when an upstream dependency is temporarily unavailable."""

    def __init__(self, message: str = "Upstream service unavailable", call_id: Optional[str] = None) -> None:
        """Create a 503 Service Unavailable error."""
        super().__init__(status_code=503, error_code="upstream_unavailable", message=message, call_id=call_id)


class UpstreamTimeoutError(APIError):
    """Error when an upstream dependency times out."""

    def __init__(self, message: str = "Upstream service timeout", call_id: Optional[str] = None) -> None:
        """Create a 504 Gateway Timeout error."""
        super().__init__(status_code=504, error_code="upstream_timeout", message=message, call_id=call_id)


class ProcessingError(APIError):
    """Error for unexpected internal processing failures."""

    def __init__(self, error_code: str, message: str, call_id: Optional[str] = None) -> None:
        """Create a 500 Internal Server Error wrapper."""
        super().__init__(status_code=500, error_code=error_code, message=message, call_id=call_id)
