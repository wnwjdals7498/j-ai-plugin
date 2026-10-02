"""Errors shared by the PMT runtime."""

class PmtError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2,
                 retryable: bool = False, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.retryable = retryable
        self.details = details

    def as_dict(self):
        result = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.details is not None:
            result["details"] = self.details
        return result
