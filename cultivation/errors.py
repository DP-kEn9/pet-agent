class GatewayError(RuntimeError):
    """Static error message plus numeric/enum diagnostics; no provider bodies."""
    def __init__(self, code, message, *, retryable=False, status_code=503, details=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.retryable, self.status_code = retryable, status_code
        self.details = details or {}

    def public(self):
        return {"code": self.code, "message": self.message,
                "retryable": self.retryable, "details": self.details}


class DomainError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def public(self):
        return {"code": self.code, "message": self.message, "details": self.details}

