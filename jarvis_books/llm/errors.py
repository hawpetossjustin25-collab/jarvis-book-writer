from jarvis_books.errors import JarvisError


class LLMError(JarvisError):
    """A classified provider error. kind drives retry/fallback decisions."""

    RETRYABLE = {"overload", "network", "rate"}

    def __init__(self, message: str, kind: str = "unknown", status: int | None = None,
                 retry_after_s: float | None = None, quota_id: str | None = None, quota_value: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after_s = retry_after_s
        self.quota_id = quota_id
        self.quota_value = quota_value

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE

    @property
    def daily_quota(self) -> bool:
        return bool(self.quota_id and "PerDay" in self.quota_id)


class NoModelAvailable(LLMError):
    def __init__(self, role: str, reasons: list[str]):
        super().__init__(f"no model available for {role}: " + "; ".join(reasons), kind="unavailable")
        self.reasons = reasons


class InvalidStructuredOutput(LLMError):
    def __init__(self, message: str):
        super().__init__(message, kind="invalid_output")
