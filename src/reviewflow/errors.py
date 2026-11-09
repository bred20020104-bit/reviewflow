"""User-visible errors without target code execution or credential disclosure."""


class InvalidInputError(Exception):
    """The requested input or mode is not valid (CLI exit 2)."""


class CollectionError(Exception):
    """Input collection failed; no complete report is available (CLI exit 1)."""


class GitUnavailableError(CollectionError):
    """Git is required for this input but is not installed."""


class GitCommandError(CollectionError):
    """A bounded, read-only Git command failed."""

    def __init__(self, message: str, *, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode
