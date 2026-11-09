from __future__ import annotations

from jarvis_books.errors import JarvisError


class PublishError(JarvisError):
    """A platform refused or failed a request. `retryable` errors may succeed if tried again later."""

    def __init__(self, message: str, retryable: bool = False, status: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def api_message(resp) -> str:
    """The platform's own error explanation, without echoing request headers or tokens."""
    try:
        err = resp.json().get("error")
    except Exception:
        return f"HTTP {resp.status_code}"
    if isinstance(err, dict):
        reasons = [e.get("reason", "") for e in err.get("errors", []) if isinstance(e, dict)]
        return (err.get("message") or "") + (f" ({', '.join(r for r in reasons if r)})" if any(reasons) else "")
    return str(err or f"HTTP {resp.status_code}")
