class ServiceError(Exception):
    """API error. Rendered as {"error": message, "code": code} (v4 wants `error`; `code` is for machines)."""

    status = 400

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.message = code, message
        if status:
            self.status = status
