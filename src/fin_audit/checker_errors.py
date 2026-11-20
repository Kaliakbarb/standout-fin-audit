"""Shared validation error for versioned, public-only accounting checkers."""


class DataValidationError(ValueError):
    """Public records do not describe a supported independently checkable book."""
